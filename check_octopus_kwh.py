"""Safely check whether Octopus GraphQL returns gas readings in kWh."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"
TOKEN_QUERY = """
mutation ObtainToken($apiKey: String!) {
  obtainKrakenToken(input: {APIKey: $apiKey}) {
    token
  }
}
"""
READINGS_QUERY = """
query GasReadings($mprn: String!, $start: DateTime!, $end: DateTime!) {
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR") {
    kwh: readings(
      startAt: $start
      endAt: $end
      readingType: INTERVAL
      timeGranularity: DAY
      timezone: "Europe/London"
      units: [KILOWATT_HOURS]
    ) {
      importReadings {
        totalCount
        edges {
          node { value units intervalStart intervalEnd }
        }
      }
    }
    m3: readings(
      startAt: $start
      endAt: $end
      readingType: INTERVAL
      timeGranularity: DAY
      timezone: "Europe/London"
      units: [METERS_CUBED]
    ) {
      importReadings {
        totalCount
        edges {
          node { value units intervalStart intervalEnd }
        }
      }
    }
  }
}
"""


class OctopusProbeError(RuntimeError):
    """Raised when the diagnostic API request cannot be completed."""


def graphql_request(
    query: str,
    variables: Dict[str, Any],
    token: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token

    response = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        messages = "; ".join(
            str(error.get("message", "Unknown GraphQL error"))
            for error in payload["errors"]
        )
        raise OctopusProbeError(messages)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OctopusProbeError("Octopus returned no GraphQL data")
    return data


def obtain_token(api_key: str) -> str:
    data = graphql_request(TOKEN_QUERY, {"apiKey": api_key})
    token = data.get("obtainKrakenToken", {}).get("token")
    if not token:
        raise OctopusProbeError("Octopus authentication returned no token")
    return str(token)


def extract_readings(supply_point: Dict[str, Any], alias: str) -> List[Dict[str, Any]]:
    connection = supply_point.get(alias, {}).get("importReadings", {})
    edges = connection.get("edges") or []
    return [edge["node"] for edge in edges if isinstance(edge.get("node"), dict)]


def decimal_total(readings: Sequence[Dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for reading in readings:
        try:
            total += Decimal(str(reading["value"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise OctopusProbeError(
                "Octopus returned an invalid reading value"
            ) from exc
    return total


def returned_units(readings: Sequence[Dict[str, Any]]) -> str:
    units = sorted({str(reading.get("units", "unknown")) for reading in readings})
    return ", ".join(units) if units else "none"


def result_conclusion(kwh_count: int, m3_count: int) -> Tuple[str, str]:
    if kwh_count:
        return (
            "Direct kWh available",
            "The Octopus GraphQL API returned gas readings in kWh for this supply point.",
        )
    if m3_count:
        return (
            "Conversion still required",
            "No kWh readings were returned, but m³ readings were available.",
        )
    return (
        "Inconclusive",
        "Neither kWh nor m³ readings were returned for the selected period.",
    )


def build_report(
    start: datetime,
    end: datetime,
    kwh_readings: Sequence[Dict[str, Any]],
    m3_readings: Sequence[Dict[str, Any]],
) -> str:
    kwh_total = decimal_total(kwh_readings)
    m3_total = decimal_total(m3_readings)
    status, explanation = result_conclusion(len(kwh_readings), len(m3_readings))

    lines = [
        "# Octopus direct kWh API check",
        "",
        f"**Result:** {status}",
        "",
        explanation,
        "",
        f"Period tested: {start.isoformat()} to {end.isoformat()}",
        "",
        "| Requested unit | Daily readings | Total | Returned units |",
        "|---|---:|---:|---|",
        f"| kWh | {len(kwh_readings)} | {kwh_total} | {returned_units(kwh_readings)} |",
        f"| m³ | {len(m3_readings)} | {m3_total} | {returned_units(m3_readings)} |",
    ]

    if kwh_total and m3_total:
        lines.extend(
            [
                "",
                f"Observed kWh per m³: {kwh_total / m3_total:.4f}",
            ]
        )

    lines.extend(["", "## Returned intervals", ""])
    lines.append("| Query | Start | End | Value | Unit |")
    lines.append("|---|---|---|---:|---|")
    for label, readings in (("kWh", kwh_readings), ("m³", m3_readings)):
        for reading in readings:
            lines.append(
                "| {label} | {start} | {end} | {value} | {unit} |".format(
                    label=label,
                    start=reading.get("intervalStart", "unknown"),
                    end=reading.get("intervalEnd", "unknown"),
                    value=reading.get("value", "unknown"),
                    unit=reading.get("units", "unknown"),
                )
            )
    if not kwh_readings and not m3_readings:
        lines.append("| — | — | — | — | — |")

    lines.extend(
        [
            "",
            "This diagnostic only reads Octopus data. It does not contact Tado or submit a meter reading.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_probe(api_key: str, mprn: str, days: int) -> str:
    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    token = obtain_token(api_key)
    data = graphql_request(
        READINGS_QUERY,
        {"mprn": mprn, "start": start.isoformat(), "end": end.isoformat()},
        token=token,
    )
    supply_point = data.get("supplyPoint")
    if not isinstance(supply_point, dict):
        raise OctopusProbeError("Octopus returned no matching gas supply point")
    return build_report(
        start,
        end,
        extract_readings(supply_point, "kwh"),
        extract_readings(supply_point, "m3"),
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether Octopus can return gas consumption in kWh"
    )
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--mprn", required=True)
    parser.add_argument("--days", type=int, default=7)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_probe(args.api_key, args.mprn, args.days)
    except (OctopusProbeError, requests.RequestException, ValueError) as exc:
        print(f"Octopus kWh check failed: {exc}", file=sys.stderr)
        return 1

    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
