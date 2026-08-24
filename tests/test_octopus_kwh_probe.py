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
    )

    assert "Direct kWh available" in report
    assert "Observed kWh per m³: 11.1868" in report
    assert "11.1868" in report
    assert "This diagnostic only reads Octopus data" in report


def test_run_probe_rejects_unbounded_day_range():
    with pytest.raises(ValueError, match="between 1 and 90"):
        probe.run_probe("secret", "mprn", 91)
