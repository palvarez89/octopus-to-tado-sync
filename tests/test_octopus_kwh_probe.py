from datetime import datetime, timezone
from decimal import Decimal

import pytest

import check_octopus_kwh as probe


def test_readings_query_uses_octopus_gbr_gas_market():
    assert 'marketName: "GBR_GAS"' in probe.READINGS_QUERY
    assert 'marketName: "GBR"' not in probe.READINGS_QUERY


def reading(value, units, start="2026-08-20T00:00:00+01:00"):
    return {
        "value": value,
        "units": units,
        "intervalStart": start,
        "intervalEnd": "2026-08-21T00:00:00+01:00",
    }


def test_decimal_total_preserves_fractional_usage():
    readings = [reading("1.010", "METERS_CUBED"), reading("0.25", "METERS_CUBED")]

    assert probe.decimal_total(readings) == Decimal("1.260")


def test_extract_readings_uses_requested_alias():
    supply_point = {
        "kwh": {
            "importReadings": {
                "totalCount": 1,
                "edges": [{"node": reading("11.3", "KILOWATT_HOURS")}],
            }
        }
    }

    assert probe.extract_readings(supply_point, "kwh")[0]["value"] == "11.3"


@pytest.mark.parametrize(
    ("kwh_count", "m3_count", "expected"),
    [
        (1, 1, "Direct kWh available"),
        (0, 1, "Conversion still required"),
        (0, 0, "Inconclusive"),
    ],
)
def test_result_conclusion(kwh_count, m3_count, expected):
    assert probe.result_conclusion(kwh_count, m3_count)[0] == expected


def test_report_compares_totals_and_ratio_without_identifiers():
    start = datetime(2026, 8, 20, tzinfo=timezone.utc)
    end = datetime(2026, 8, 22, tzinfo=timezone.utc)

    report = probe.build_report(
        start,
        end,
        [reading("11.1868", "KILOWATT_HOURS")],
        [reading("1.0", "METERS_CUBED")],
        [reading("11000", "METERS_CUBED")],
    )

    assert "Direct cumulative meter reading available" in report
    assert "Latest: 11000" in report
    assert "Observed kWh per m³: 11.1868" in report
    assert "11.1868" in report
    assert "This diagnostic only reads Octopus data" in report


def test_run_probe_rejects_unbounded_day_range():
    with pytest.raises(ValueError, match="between 1 and 90"):
        probe.run_probe("secret", "mprn", 91)


def test_accumulation_query_requests_meter_register_readings():
    assert "readingType: ACCUMULATION" in probe.ACCUMULATION_QUERY
    assert "units: [METERS_CUBED]" in probe.ACCUMULATION_QUERY
    assert "timeGranularity" not in probe.ACCUMULATION_QUERY


def test_latest_reading_selects_newest_accumulation_value():
    older = reading("10998", "METERS_CUBED", "2026-08-20T00:00:00+01:00")
    newer = reading("11000", "METERS_CUBED", "2026-08-21T00:00:00+01:00")
    newer["intervalEnd"] = "2026-08-22T00:00:00+01:00"

    assert probe.latest_reading([newer, older])["value"] == "11000"


def test_report_preserves_accumulation_api_error():
    start = datetime(2026, 8, 20, tzinfo=timezone.utc)
    end = datetime(2026, 8, 22, tzinfo=timezone.utc)

    report = probe.build_report(
        start,
        end,
        [],
        [reading("1.0", "METERS_CUBED")],
        [],
        "Unsupported reading type",
    )

    assert "Cumulative reading unavailable" in report
    assert "API error: Unsupported reading type" in report


def test_device_query_splits_register_discovery_from_readings():
    assert "deviceAccumulation: readings(" in probe.DEVICE_ACCUMULATION_QUERY
    assert "registers(" not in probe.DEVICE_ACCUMULATION_QUERY
    assert "registers(first: 20)" in probe.REGISTER_LIST_QUERY
    assert "readings(" not in probe.REGISTER_LIST_QUERY
    assert (
        "registers(registerIdentifiers: [$registerIdentifier]"
        in probe.REGISTER_READING_QUERY
    )
    assert "readingType: $readingType" in probe.REGISTER_READING_QUERY
    assert "timeGranularity: $timeGranularity" in probe.REGISTER_READING_QUERY


def test_device_report_matches_serial_without_exposing_identifiers():
    device = {
        "deviceIdentifier": "SECRET-SERIAL",
        "deviceAccumulation": {
            "importReadings": {
                "edges": [{"node": reading("11995.610", "METERS_CUBED")}]
            }
        },
    }
    device_supply_point = {"devices": {"edges": [{"node": device}]}}
    register_results = [
        {
            "accumulation_readings": [reading("11995.610", "METERS_CUBED")],
            "interval_readings": [reading("1.010", "METERS_CUBED")],
        }
    ]

    report = probe.build_device_report(
        device_supply_point, register_results, "SECRET-SERIAL"
    )

    assert "| Device 1 accumulation | Yes |" in report
    assert "| Register 1 accumulation | Yes |" in report
    assert "| Register 1 daily interval | Yes |" in report
    assert "period total: 1.010" in report
    assert "11995.610" in report
    assert "SECRET-SERIAL" not in report


def test_device_report_preserves_independent_query_failures():
    report = probe.build_device_report(
        None,
        [
            {
                "accumulation_error": "Accumulation failed",
                "interval_error": "Interval failed",
            }
        ],
        "SECRET-SERIAL",
        device_error="Device SECRET-SERIAL lookup failed",
        register_list_error=None,
    )

    assert "API error: Device [redacted] lookup failed" in report
    assert "API error: Accumulation failed" in report
    assert "API error: Interval failed" in report
    assert "SECRET-SERIAL" not in report
