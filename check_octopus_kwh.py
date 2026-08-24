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
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR_GAS") {
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
ACCUMULATION_QUERY = """
query GasAccumulationReadings($mprn: String!, $start: DateTime!, $end: DateTime!) {
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR_GAS") {
    accumulation: readings(
      startAt: $start
      endAt: $end
      readingType: ACCUMULATION
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
DEVICE_ACCUMULATION_QUERY = """
query GasDeviceAccumulationReadings(
  $mprn: String!
  $gasSerial: String!
  $start: DateTime!
  $end: DateTime!
) {
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR_GAS") {
    devices(deviceIdentifiers: [$gasSerial], first: 5) {
      totalCount
      edges {
        node {
          deviceIdentifier
          deviceAccumulation: readings(
            startAt: $start
            endAt: $end
            readingType: ACCUMULATION
            timezone: "Europe/London"
            units: [METERS_CUBED]
          ) {
            importReadings(first: 20) {
              edges {
                node { value units intervalStart intervalEnd }
              }
            }
          }
        }
      }
    }
  }
}
"""
REGISTER_ACCUMULATION_QUERY = """
query GasRegisterAccumulationReadings(
  $mprn: String!
  $gasSerial: String!
  $start: DateTime!
  $end: DateTime!
) {
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR_GAS") {
    devices(deviceIdentifiers: [$gasSerial], first: 5) {
      totalCount
      edges {
        node {
          deviceIdentifier
          registers(first: 20) {
            totalCount
            edges {
              node {
                registerIdentifier
                registerAccumulation: readings(
                  startAt: $start
                  endAt: $end
                  readingType: ACCUMULATION
                  timezone: "Europe/London"
                  units: [METERS_CUBED]
                ) {
                  importReadings(first: 20) {
                    edges {
                      node { value units intervalStart intervalEnd }
                    }
                  }
                }
              }
            }
          }
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


def connection_nodes(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
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


def latest_reading(
    readings: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not readings:
        return None
    return max(
        readings,
        key=lambda reading: str(
            reading.get("intervalEnd") or reading.get("intervalStart") or ""
        ),
    )


def accumulation_conclusion(
    accumulation_count: int,
    interval_m3_count: int,
    accumulation_error: Optional[str] = None,
) -> Tuple[str, str]:
    if accumulation_count:
        return (
            "Direct cumulative meter reading available",
            "Octopus returned an accumulation value that can be tested as the direct Tado meter reading.",
        )
    if accumulation_error:
        return (
            "Cumulative reading unavailable",
            "Octopus rejected the accumulation query; interval consumption must not be presented as an actual meter register.",
        )
    if interval_m3_count:
        return (
            "Only interval usage available",
            "Octopus returned interval consumption but no cumulative meter-register reading.",
        )
    return ("Inconclusive", "No gas readings were returned for the selected period.")


def latest_value_and_time(
    readings: Sequence[Dict[str, Any]],
) -> Tuple[str, str]:
    latest = latest_reading(readings)
    if not latest:
        return "none", "none"
    value = str(latest.get("value", "unknown"))
    timestamp = str(
        latest.get("intervalEnd") or latest.get("intervalStart") or "unknown"
    )
    return value, timestamp


def build_device_report(
    device_supply_point: Optional[Dict[str, Any]],
    register_supply_point: Optional[Dict[str, Any]],
    gas_serial_number: Optional[str],
    device_error: Optional[str] = None,
    register_error: Optional[str] = None,
) -> str:
    devices = connection_nodes((device_supply_point or {}).get("devices", {}))
    register_devices = connection_nodes(
        (register_supply_point or {}).get("devices", {})
    )
    if gas_serial_number:
        if device_error:
            device_error = device_error.replace(gas_serial_number, "[redacted]")
        if register_error:
            register_error = register_error.replace(gas_serial_number, "[redacted]")
    lines = [
        "",
        "## Per-device and per-register accumulation",
        "",
        "Identifiers are deliberately hidden; only a match against the configured gas serial is shown.",
        "",
        "| Scope | Matches configured gas serial | Rows | Latest m3 | Timestamp |",
        "|---|---|---:|---:|---|",
    ]
    matched_configured_meter = False
    for device_index, device in enumerate(devices, start=1):
        device_identifier = str(device.get("deviceIdentifier") or "")
        is_match = bool(gas_serial_number and device_identifier == gas_serial_number)
        matched_configured_meter = matched_configured_meter or is_match
        device_readings = extract_readings(device, "deviceAccumulation")
        device_value, device_timestamp = latest_value_and_time(device_readings)
        lines.append(
            f"| Device {device_index} | {'Yes' if is_match else 'No'} | "
            f"{len(device_readings)} | {device_value} | {device_timestamp} |"
        )

    for device_index, device in enumerate(register_devices, start=1):
        device_identifier = str(device.get("deviceIdentifier") or "")
        is_match = bool(gas_serial_number and device_identifier == gas_serial_number)
        matched_configured_meter = matched_configured_meter or is_match
        registers = connection_nodes(device.get("registers", {}))
        for register_index, register in enumerate(registers, start=1):
            register_readings = extract_readings(register, "registerAccumulation")
            register_value, register_timestamp = latest_value_and_time(
                register_readings
            )
            lines.append(
                f"| Device {device_index} / Register {register_index} | "
                f"{'Yes' if is_match else 'No'} | {len(register_readings)} | "
                f"{register_value} | {register_timestamp} |"
            )

    if device_error:
        lines.append(
            f"| Device query | Unknown | 0 | API error: {device_error} | none |"
        )
    elif not devices:
        lines.append("| No matching device returned | No | 0 | none | none |")
    if register_error:
        lines.append(
            f"| Register query | Unknown | 0 | API error: {register_error} | none |"
        )
    elif not register_devices:
        lines.append(
            "| No matching device returned for register query | No | 0 | none | none |"
        )
    elif not any(
        connection_nodes(device.get("registers", {})) for device in register_devices
    ):
        lines.append("| No registers returned | No | 0 | none | none |")

    if (
        gas_serial_number
        and (not device_error or not register_error)
        and not matched_configured_meter
    ):
        lines.extend(
            [
                "",
                "The configured gas serial did not match any returned device identifier.",
            ]
        )
    lines.extend(
        [
            "",
            "This section does not print device identifiers or the configured gas serial.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_report(
    start: datetime,
    end: datetime,
    kwh_readings: Sequence[Dict[str, Any]],
    m3_readings: Sequence[Dict[str, Any]],
    accumulation_readings: Sequence[Dict[str, Any]],
    accumulation_error: Optional[str] = None,
) -> str:
    kwh_total = decimal_total(kwh_readings)
    m3_total = decimal_total(m3_readings)
    latest_accumulation = latest_reading(accumulation_readings)
    status, explanation = accumulation_conclusion(
        len(accumulation_readings), len(m3_readings), accumulation_error
    )

    lines = [
        "# Octopus gas readings API check",
        "",
        f"**Result:** {status}",
        "",
        explanation,
        "",
        f"Period tested: {start.isoformat()} to {end.isoformat()}",
        "",
        "| Dataset | Rows | Result | Returned units |",
        "|---|---:|---:|---|",
        f"| kWh | {len(kwh_readings)} | {kwh_total} | {returned_units(kwh_readings)} |",
        f"| m³ | {len(m3_readings)} | {m3_total} | {returned_units(m3_readings)} |",
    ]

    if latest_accumulation:
        accumulation_value = latest_accumulation.get("value", "unknown")
        accumulation_timestamp = (
            latest_accumulation.get("intervalEnd")
            or latest_accumulation.get("intervalStart")
            or "unknown"
        )
        lines.append(
            f"| Cumulative meter register (m3) | {len(accumulation_readings)} | Latest: {accumulation_value} | {returned_units(accumulation_readings)} |"
        )
        lines.extend(
            ["", f"Latest cumulative reading timestamp: {accumulation_timestamp}"]
        )
    elif accumulation_error:
        lines.append(
            f"| Cumulative meter register (m3) | 0 | API error: {accumulation_error} | none |"
        )
    else:
        lines.append("| Cumulative meter register (m3) | 0 | No readings | none |")

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
    for reading in accumulation_readings:
        lines.append(
            "| Accumulation m3 | {start} | {end} | {value} | {unit} |".format(
                start=reading.get("intervalStart", "unknown"),
                end=reading.get("intervalEnd", "unknown"),
                value=reading.get("value", "unknown"),
                unit=reading.get("units", "unknown"),
            )
        )

    if not kwh_readings and not m3_readings and not accumulation_readings:
        lines.append("| — | — | — | — | — |")

    lines.extend(
        [
            "",
            "This diagnostic only reads Octopus data. It does not contact Tado or submit a meter reading.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_probe(
    api_key: str, mprn: str, days: int, gas_serial_number: Optional[str] = None
) -> str:
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

    accumulation_readings: List[Dict[str, Any]] = []
    accumulation_error = None
    try:
        accumulation_data = graphql_request(
            ACCUMULATION_QUERY,
            {"mprn": mprn, "start": start.isoformat(), "end": end.isoformat()},
            token=token,
        )
        accumulation_supply_point = accumulation_data.get("supplyPoint")
        if isinstance(accumulation_supply_point, dict):
            accumulation_readings = extract_readings(
                accumulation_supply_point, "accumulation"
            )
        else:
            accumulation_error = "Octopus returned no matching gas supply point"
    except OctopusProbeError as exc:
        accumulation_error = str(exc)

    report = build_report(
        start,
        end,
        extract_readings(supply_point, "kwh"),
        extract_readings(supply_point, "m3"),
        accumulation_readings,
        accumulation_error,
    )
    device_supply_point = None
    register_supply_point = None
    device_error = None
    register_error = None
    device_variables = {
        "mprn": mprn,
        "gasSerial": gas_serial_number,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if gas_serial_number:
        try:
            device_data = graphql_request(
                DEVICE_ACCUMULATION_QUERY, device_variables, token=token
            )
            candidate = device_data.get("supplyPoint")
            if not isinstance(candidate, dict):
                raise OctopusProbeError(
                    "Octopus returned no matching gas supply point for the device"
                )
            device_supply_point = candidate
        except OctopusProbeError as exc:
            device_error = str(exc)

        try:
            register_data = graphql_request(
                REGISTER_ACCUMULATION_QUERY, device_variables, token=token
            )
            candidate = register_data.get("supplyPoint")
            if not isinstance(candidate, dict):
                raise OctopusProbeError(
                    "Octopus returned no matching gas supply point for registers"
                )
            register_supply_point = candidate
        except OctopusProbeError as exc:
            register_error = str(exc)
    else:
        device_error = "No configured gas serial was provided"
        register_error = "No configured gas serial was provided"

    report += build_device_report(
        device_supply_point,
        register_supply_point,
        gas_serial_number,
        device_error,
        register_error,
    )
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether Octopus can return gas consumption in kWh"
    )
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--mprn", required=True)
    parser.add_argument("--gas-serial-number", required=True)
    parser.add_argument("--days", type=int, default=7)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_probe(args.api_key, args.mprn, args.days, args.gas_serial_number)
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
