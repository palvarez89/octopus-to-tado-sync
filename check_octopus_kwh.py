"""Safely check whether Octopus GraphQL returns gas readings in kWh."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests
from requests.auth import HTTPBasicAuth

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
REGISTER_LIST_QUERY = """
query GasRegisterList($mprn: String!, $gasSerial: String!) {
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR_GAS") {
    devices(deviceIdentifiers: [$gasSerial], first: 5) {
      edges {
        node {
          deviceIdentifier
          registers(first: 20) {
            totalCount
            edges {
              node { registerIdentifier }
            }
          }
        }
      }
    }
  }
}
"""
REGISTER_READING_QUERY = """
query GasRegisterReadings(
  $mprn: String!
  $gasSerial: String!
  $registerIdentifier: String!
  $start: DateTime!
  $end: DateTime!
  $readingType: ReadingTypes!
  $timeGranularity: TimeGranularities
) {
  supplyPoint(externalIdentifier: $mprn, marketName: "GBR_GAS") {
    devices(deviceIdentifiers: [$gasSerial], first: 5) {
      edges {
        node {
          registers(registerIdentifiers: [$registerIdentifier], first: 1) {
            edges {
              node {
                selectedReadings: readings(
                  startAt: $start
                  endAt: $end
                  readingType: $readingType
                  timeGranularity: $timeGranularity
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


ACCOUNT_GAS_METER_QUERY = """
query FindGasMeter($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    properties {
      gasMeterPoints {
        mprn
        meters { id serialNumber }
      }
    }
  }
}
"""
ACTUAL_GAS_METER_READINGS_QUERY = """
query ActualGasMeterReadings($accountNumber: String!, $meterId: String!) {
  gasMeterReadings(
    accountNumber: $accountNumber
    meterId: $meterId
    last: 20
  ) {
    totalCount
    edges {
      node {
        readAt
        readingType
        registers { identifier name value digits isQuarantined }
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
    register_results: Sequence[Dict[str, Any]],
    gas_serial_number: Optional[str],
    device_error: Optional[str] = None,
    register_list_error: Optional[str] = None,
) -> str:
    devices = connection_nodes((device_supply_point or {}).get("devices", {}))
    if gas_serial_number:
        if device_error:
            device_error = device_error.replace(gas_serial_number, "[redacted]")
        if register_list_error:
            register_list_error = register_list_error.replace(
                gas_serial_number, "[redacted]"
            )
    lines = [
        "",
        "## Per-device and per-register accumulation",
        "",
        "Identifiers are deliberately hidden; only a match against the configured gas serial is shown.",
        "",
        "| Scope | Matches configured gas serial | Rows | Value m3 | Timestamp |",
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
            f"| Device {device_index} accumulation | "
            f"{'Yes' if is_match else 'No'} | {len(device_readings)} | "
            f"{device_value} | {device_timestamp} |"
        )

    for register_index, result in enumerate(register_results, start=1):
        accumulation = result.get("accumulation_readings") or []
        interval = result.get("interval_readings") or []
        accumulation_error = str(result.get("accumulation_error") or "")
        interval_error = str(result.get("interval_error") or "")
        accumulation_value, accumulation_timestamp = latest_value_and_time(accumulation)
        interval_timestamp = latest_value_and_time(interval)[1]
        interval_total = decimal_total(interval)
        if accumulation_error:
            accumulation_value = f"API error: {accumulation_error}"
            accumulation_timestamp = "none"
        if interval_error:
            interval_value = f"API error: {interval_error}"
            interval_timestamp = "none"
        else:
            interval_value = f"period total: {interval_total}"
        lines.append(
            f"| Register {register_index} accumulation | Yes | "
            f"{len(accumulation)} | {accumulation_value} | "
            f"{accumulation_timestamp} |"
        )
        lines.append(
            f"| Register {register_index} daily interval | Yes | "
            f"{len(interval)} | {interval_value} | {interval_timestamp} |"
        )

    if device_error:
        lines.append(
            f"| Device query | Unknown | 0 | API error: {device_error} | none |"
        )
    elif not devices:
        lines.append("| No matching device returned | No | 0 | none | none |")
    if register_list_error:
        lines.append(
            "| Register list query | Unknown | 0 | "
            f"API error: {register_list_error} | none |"
        )
    elif not register_results:
        lines.append("| No registers returned | No | 0 | none | none |")

    if gas_serial_number and not device_error and not matched_configured_meter:
        lines.extend(
            [
                "",
                "The configured gas serial did not match any returned device identifier.",
            ]
        )
    lines.extend(
        [
            "",
            "This section does not print device identifiers, register identifiers, or the configured gas serial.",
        ]
    )
    return "\n".join(lines) + "\n"


def find_matching_gas_meter_ids(
    account: Dict[str, Any], mprn: str, gas_serial_number: str
) -> List[str]:
    meter_ids = []
    for property_info in account.get("properties") or []:
        for meter_point in property_info.get("gasMeterPoints") or []:
            if str(meter_point.get("mprn") or "") != mprn:
                continue
            for meter in meter_point.get("meters") or []:
                if str(meter.get("serialNumber") or "") != gas_serial_number:
                    continue
                meter_id = str(meter.get("id") or "")
                if meter_id:
                    meter_ids.append(meter_id)
    return meter_ids


def redact_message(message: str, identifiers: Sequence[Optional[str]]) -> str:
    for identifier in identifiers:
        if identifier:
            message = message.replace(identifier, "[redacted]")
    return message


def latest_actual_meter_anchor(
    meter_results: Sequence[Sequence[Dict[str, Any]]],
) -> Optional[Tuple[Decimal, str]]:
    candidates: List[Tuple[str, Decimal]] = []
    for readings in meter_results:
        for reading in readings:
            read_at = str(reading.get("readAt") or "")
            if not read_at:
                continue
            for register in reading.get("registers") or []:
                if register.get("isQuarantined") is True:
                    continue
                try:
                    value = Decimal(str(register["value"]))
                except (KeyError, InvalidOperation, TypeError, ValueError):
                    continue
                candidates.append((read_at, value))
    if not candidates:
        return None
    read_at, value = max(candidates, key=lambda candidate: candidate[0])
    return value, read_at


def fetch_octopus_consumption_total(
    api_key: str,
    mprn: str,
    gas_serial_number: str,
    period_from: str,
    period_to: str,
) -> Tuple[Decimal, int, Optional[str], Optional[str]]:
    query = urlencode(
        {
            "period_from": period_from,
            "period_to": period_to,
            "order_by": "period",
            "group_by": "day",
            "page_size": 25000,
        }
    )
    url = (
        f"https://api.octopus.energy/v1/gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/?{query}"
    )
    total = Decimal("0")
    interval_count = 0
    coverage_starts: List[str] = []
    coverage_ends: List[str] = []
    while url:
        response = requests.get(url, auth=HTTPBasicAuth(api_key, ""), timeout=30)
        if response.status_code != 200:
            raise OctopusProbeError(
                "Octopus consumption API returned HTTP " f"{response.status_code}"
            )
        payload = response.json()
        for interval in payload.get("results") or []:
            try:
                total += Decimal(str(interval["consumption"]))
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise OctopusProbeError(
                    "Octopus returned an invalid consumption value"
                ) from exc
            if interval.get("interval_start"):
                coverage_starts.append(str(interval["interval_start"]))
            if interval.get("interval_end"):
                coverage_ends.append(str(interval["interval_end"]))
            interval_count += 1
        url = payload.get("next")
    return (
        total,
        interval_count,
        min(coverage_starts) if coverage_starts else None,
        max(coverage_ends) if coverage_ends else None,
    )


def build_reconstructed_register_report(
    anchor: Optional[Tuple[Decimal, str]],
    usage: Optional[Decimal],
    interval_count: int,
    cutoff: str,
    coverage_start: Optional[str] = None,
    coverage_end: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    lines = ["", "## Reconstructed physical meter register", ""]
    if error:
        lines.append(f"API error: {error}")
    elif anchor is None or usage is None:
        lines.append("No usable actual meter-reading anchor was returned.")
    else:
        anchor_value, anchor_date = anchor
        reconstructed = anchor_value + usage
        coverage_complete = False
        if coverage_start and coverage_end:
            anchor_time = datetime.fromisoformat(anchor_date.replace("Z", "+00:00"))
            requested_end = datetime.fromisoformat(f"{cutoff}T00:00:00+00:00")
            actual_start = datetime.fromisoformat(coverage_start.replace("Z", "+00:00"))
            actual_end = datetime.fromisoformat(coverage_end.replace("Z", "+00:00"))
            coverage_complete = (
                actual_start <= anchor_time and actual_end >= requested_end
            )
        lines.extend(
            [
                "| Value | Result |",
                "|---|---:|",
                f"| Actual meter-reading anchor | {anchor_value} m3 on {anchor_date} |",
                f"| REST coverage returned | {coverage_start or 'unknown'} to {coverage_end or 'unknown'} |",
                f"| Octopus usage returned | {usage} m3 ({interval_count} intervals) |",
                f"| Partial reconstruction | {reconstructed} m3 on {cutoff} |",
                f"| Coverage status | {'Complete' if coverage_complete else 'Incomplete - do not use as a meter reading'} |",
                "",
                "This calculation is read-only and was not submitted to Tado.",
            ]
        )
    return "\n".join(lines) + "\n"


def closest_accumulation_to_time(
    readings: Sequence[Dict[str, Any]], target: str
) -> Optional[Dict[str, Any]]:
    target_time = datetime.fromisoformat(target.replace("Z", "+00:00"))
    candidates = []
    for reading in readings:
        timestamp = reading.get("intervalEnd") or reading.get("intervalStart")
        if not timestamp:
            continue
        reading_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        candidates.append((abs(reading_time - target_time), reading))
    return (
        min(candidates, key=lambda candidate: candidate[0])[1] if candidates else None
    )


def build_cumulative_calibration_report(
    anchor: Optional[Tuple[Decimal, str]],
    historical_accumulation: Optional[Dict[str, Any]],
    current_accumulation: Optional[Dict[str, Any]],
    error: Optional[str] = None,
) -> str:
    lines = ["", "## Cumulative-series calibration", ""]
    if error:
        lines.append(f"API error: {error}")
    elif not anchor or not historical_accumulation or not current_accumulation:
        lines.append("Insufficient readings to calibrate the cumulative series.")
    else:
        anchor_value, anchor_date = anchor
        try:
            historical_value = Decimal(str(historical_accumulation["value"]))
            current_value = Decimal(str(current_accumulation["value"]))
            factor = historical_value / anchor_value
            corrected_current = current_value / factor
        except (KeyError, InvalidOperation, TypeError, ValueError, ZeroDivisionError):
            lines.append("Octopus returned an invalid calibration value.")
        else:
            historical_date = (
                historical_accumulation.get("intervalEnd")
                or historical_accumulation.get("intervalStart")
                or "unknown"
            )
            current_date = (
                current_accumulation.get("intervalEnd")
                or current_accumulation.get("intervalStart")
                or "unknown"
            )
            lines.extend(
                [
                    "| Value | Result |",
                    "|---|---:|",
                    f"| Actual anchor | {anchor_value} m3 on {anchor_date} |",
                    f"| Aggregate near anchor | {historical_value} m3 on {historical_date} |",
                    f"| Observed aggregate factor | {factor} |",
                    f"| Latest aggregate | {current_value} m3 on {current_date} |",
                    f"| Factor-corrected current register | {corrected_current} m3 |",
                    "",
                    "This calibration is read-only and was not submitted to Tado.",
                ]
            )
    return "\n".join(lines) + "\n"


def build_actual_meter_readings_report(
    meter_results: Sequence[Sequence[Dict[str, Any]]],
    error: Optional[str] = None,
) -> str:
    lines = [
        "",
        "## Dedicated Octopus gas meter readings",
        "",
        "This uses Octopus's gasMeterReadings query for actual meter-reading records.",
        "",
        "| Scope | Read at | Reading type | Value | Digits | Quarantined |",
        "|---|---|---|---:|---:|---|",
    ]
    row_count = 0
    for meter_index, readings in enumerate(meter_results, start=1):
        ordered = sorted(
            readings, key=lambda reading: str(reading.get("readAt") or ""), reverse=True
        )
        for reading_index, reading in enumerate(ordered, start=1):
            registers = reading.get("registers") or []
            for register_index, register in enumerate(registers, start=1):
                row_count += 1
                lines.append(
                    f"| Meter {meter_index} / Reading {reading_index} / "
                    f"Register {register_index} | "
                    f"{reading.get('readAt') or 'unknown'} | "
                    f"{reading.get('readingType') or 'unknown'} | "
                    f"{register.get('value') or 'unknown'} | "
                    f"{register.get('digits') if register.get('digits') is not None else 'unknown'} | "
                    f"{register.get('isQuarantined') if register.get('isQuarantined') is not None else 'unknown'} |"
                )
    if error:
        lines.append(f"| API query | none | none | API error: {error} | none | none |")
    elif not row_count:
        lines.append("| No readings returned | none | none | none | none | none |")
    lines.extend(["", "Meter and register identifiers are deliberately hidden."])
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
    api_key: str,
    mprn: str,
    days: int,
    gas_serial_number: Optional[str] = None,
    account_number: Optional[str] = None,
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
    device_error = None
    register_list_error = None
    register_results: List[Dict[str, Any]] = []
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

        registers: List[Dict[str, Any]] = []
        try:
            register_list_data = graphql_request(
                REGISTER_LIST_QUERY, device_variables, token=token
            )
            register_supply_point = register_list_data.get("supplyPoint")
            if not isinstance(register_supply_point, dict):
                raise OctopusProbeError(
                    "Octopus returned no matching gas supply point for registers"
                )
            for device in connection_nodes(register_supply_point.get("devices", {})):
                registers.extend(connection_nodes(device.get("registers", {})))
        except OctopusProbeError as exc:
            register_list_error = str(exc)

        for register in registers:
            register_identifier = str(register.get("registerIdentifier") or "")
            result: Dict[str, Any] = {}
            for label, reading_type, granularity in (
                ("accumulation", "ACCUMULATION", None),
                ("interval", "INTERVAL", "DAY"),
            ):
                variables = {
                    **device_variables,
                    "registerIdentifier": register_identifier,
                    "readingType": reading_type,
                    "timeGranularity": granularity,
                }
                try:
                    register_data = graphql_request(
                        REGISTER_READING_QUERY, variables, token=token
                    )
                    register_supply_point = register_data.get("supplyPoint")
                    if not isinstance(register_supply_point, dict):
                        raise OctopusProbeError(
                            "Octopus returned no matching gas supply point"
                        )
                    selected_registers: List[Dict[str, Any]] = []
                    for device in connection_nodes(
                        register_supply_point.get("devices", {})
                    ):
                        selected_registers.extend(
                            connection_nodes(device.get("registers", {}))
                        )
                    readings = (
                        extract_readings(selected_registers[0], "selectedReadings")
                        if selected_registers
                        else []
                    )
                    result[f"{label}_readings"] = readings
                except OctopusProbeError as exc:
                    message = str(exc)
                    for identifier in (gas_serial_number, register_identifier):
                        if identifier:
                            message = message.replace(identifier, "[redacted]")
                    result[f"{label}_error"] = message
            register_results.append(result)
    else:
        device_error = "No configured gas serial was provided"
        register_list_error = "No configured gas serial was provided"

    report += build_device_report(
        device_supply_point,
        register_results,
        gas_serial_number,
        device_error,
        register_list_error,
    )
    actual_meter_results: List[List[Dict[str, Any]]] = []
    actual_meter_errors: List[str] = []
    if account_number and gas_serial_number:
        try:
            account_data = graphql_request(
                ACCOUNT_GAS_METER_QUERY,
                {"accountNumber": account_number},
                token=token,
            )
            account = account_data.get("account")
            if not isinstance(account, dict):
                raise OctopusProbeError("Octopus returned no matching account")
            meter_ids = find_matching_gas_meter_ids(account, mprn, gas_serial_number)
            if not meter_ids:
                raise OctopusProbeError(
                    "No gas meter matched the configured MPRN and serial"
                )
            for meter_id in meter_ids:
                try:
                    reading_data = graphql_request(
                        ACTUAL_GAS_METER_READINGS_QUERY,
                        {"accountNumber": account_number, "meterId": meter_id},
                        token=token,
                    )
                    connection = reading_data.get("gasMeterReadings")
                    if not isinstance(connection, dict):
                        raise OctopusProbeError(
                            "Octopus returned no gas meter reading connection"
                        )
                    actual_meter_results.append(connection_nodes(connection))
                except OctopusProbeError as exc:
                    actual_meter_errors.append(
                        redact_message(
                            str(exc),
                            (account_number, mprn, gas_serial_number, meter_id),
                        )
                    )
        except OctopusProbeError as exc:
            actual_meter_errors.append(
                redact_message(str(exc), (account_number, mprn, gas_serial_number))
            )
    else:
        actual_meter_errors.append(
            "No Octopus account number or configured gas serial was provided"
        )

    report += build_actual_meter_readings_report(
        actual_meter_results,
        "; ".join(actual_meter_errors) if actual_meter_errors else None,
    )
    anchor = latest_actual_meter_anchor(actual_meter_results)
    reconstruction_error = None
    reconstructed_usage = None
    reconstruction_interval_count = 0
    reconstruction_coverage_start = None
    reconstruction_coverage_end = None
    cutoff_date = (end.date() - timedelta(days=2)).isoformat()
    cutoff_datetime = f"{cutoff_date}T00:00:00Z"
    if anchor:
        try:
            (
                reconstructed_usage,
                reconstruction_interval_count,
                reconstruction_coverage_start,
                reconstruction_coverage_end,
            ) = fetch_octopus_consumption_total(
                api_key,
                mprn,
                gas_serial_number or "",
                anchor[1],
                cutoff_datetime,
            )
        except (OctopusProbeError, requests.RequestException) as exc:
            reconstruction_error = redact_message(str(exc), (mprn, gas_serial_number))
    report += build_reconstructed_register_report(
        anchor,
        reconstructed_usage,
        reconstruction_interval_count,
        cutoff_date,
        reconstruction_coverage_start,
        reconstruction_coverage_end,
        reconstruction_error,
    )

    historical_accumulation = None
    calibration_error = None
    if anchor:
        anchor_time = datetime.fromisoformat(anchor[1].replace("Z", "+00:00"))
        try:
            historical_data = graphql_request(
                ACCUMULATION_QUERY,
                {
                    "mprn": mprn,
                    "start": (anchor_time - timedelta(days=1)).isoformat(),
                    "end": (anchor_time + timedelta(days=2)).isoformat(),
                },
                token=token,
            )
            historical_supply_point = historical_data.get("supplyPoint")
            if not isinstance(historical_supply_point, dict):
                raise OctopusProbeError(
                    "Octopus returned no historical gas supply point"
                )
            historical_readings = extract_readings(
                historical_supply_point, "accumulation"
            )
            historical_accumulation = closest_accumulation_to_time(
                historical_readings, anchor[1]
            )
        except OctopusProbeError as exc:
            calibration_error = redact_message(str(exc), (mprn, gas_serial_number))
    report += build_cumulative_calibration_report(
        anchor,
        historical_accumulation,
        latest_reading(accumulation_readings),
        calibration_error,
    )
    return report


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether Octopus can return gas consumption in kWh"
    )
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--mprn", required=True)
    parser.add_argument("--gas-serial-number", required=True)
    parser.add_argument("--account-number", required=True)
    parser.add_argument("--days", type=int, default=7)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = run_probe(
            args.api_key,
            args.mprn,
            args.days,
            args.gas_serial_number,
            args.account_number,
        )
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
