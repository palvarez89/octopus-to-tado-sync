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


def test_dedicated_meter_queries_request_actual_register_values():
    assert "account(accountNumber: $accountNumber)" in probe.ACCOUNT_GAS_METER_QUERY
    assert "meters { id serialNumber }" in probe.ACCOUNT_GAS_METER_QUERY
    assert "gasMeterReadings(" in probe.ACTUAL_GAS_METER_READINGS_QUERY
    assert "registers { identifier name value digits isQuarantined }" in (
        probe.ACTUAL_GAS_METER_READINGS_QUERY
    )


def test_find_matching_gas_meter_ids_uses_mprn_and_serial():
    account = {
        "properties": [
            {
                "gasMeterPoints": [
                    {
                        "mprn": "123456789",
                        "meters": [
                            {"id": "SECRET-ID", "serialNumber": "SECRET-SERIAL"}
                        ],
                    }
                ]
            }
        ]
    }
    assert probe.find_matching_gas_meter_ids(account, "123456789", "SECRET-SERIAL") == [
        "SECRET-ID"
    ]
    assert (
        probe.find_matching_gas_meter_ids(account, "different", "SECRET-SERIAL") == []
    )


def test_actual_meter_report_shows_values_without_identifiers():
    report = probe.build_actual_meter_readings_report(
        [
            [
                {
                    "readAt": "2026-08-23T01:00:00+01:00",
                    "readingType": "SMART",
                    "registers": [
                        {
                            "identifier": "SECRET-REGISTER",
                            "name": "SECRET-NAME",
                            "value": "11995.610",
                            "digits": 6,
                            "isQuarantined": False,
                        }
                    ],
                }
            ]
        ]
    )
    assert "11995.610" in report
    assert "2026-08-23T01:00:00+01:00" in report
    assert "SECRET-REGISTER" not in report
    assert "SECRET-NAME" not in report


def test_latest_actual_meter_anchor_uses_newest_non_quarantined_value():
    results = [
        [
            {
                "readAt": "2022-12-24T00:00:00+00:00",
                "registers": [{"value": "5039.00000", "isQuarantined": False}],
            },
            {
                "readAt": "2023-01-24T00:00:00+00:00",
                "registers": [{"value": "9999", "isQuarantined": True}],
            },
        ]
    ]
    assert probe.latest_actual_meter_anchor(results) == (
        Decimal("5039.00000"),
        "2022-12-24T00:00:00+00:00",
    )


def test_fetch_octopus_consumption_total_sums_intervals(monkeypatch):
    response = type(
        "Response",
        (),
        {
            "status_code": 200,
            "json": lambda self: {
                "results": [
                    {
                        "consumption": "1.010",
                        "interval_start": "2024-08-24T00:00:00+01:00",
                        "interval_end": "2024-08-25T00:00:00+01:00",
                    },
                    {
                        "consumption": "0.25",
                        "interval_start": "2024-08-25T00:00:00+01:00",
                        "interval_end": "2024-08-26T00:00:00+01:00",
                    },
                ],
                "next": None,
            },
        },
    )()
    requested_urls = []

    def fake_get(url, *args, **kwargs):
        requested_urls.append(url)
        return response

    monkeypatch.setattr(probe.requests, "get", fake_get)
    total, count, coverage_start, coverage_end = probe.fetch_octopus_consumption_total(
        "api-key", "mprn", "serial", "2022-12-24", "2026-08-22"
    )
    assert total == Decimal("1.260")
    assert count == 2
    assert coverage_start == "2024-08-24T00:00:00+01:00"
    assert coverage_end == "2024-08-26T00:00:00+01:00"
    assert "group_by=day" in requested_urls[0]
    assert "page_size=25000" in requested_urls[0]


def test_reconstructed_register_report_adds_usage_to_actual_anchor():
    report = probe.build_reconstructed_register_report(
        (Decimal("5039"), "2022-12-24T00:00:00+00:00"),
        Decimal("3814.481"),
        64000,
        "2026-08-22",
        "2024-08-24T00:00:00+01:00",
        "2026-08-22T00:00:00+01:00",
    )
    assert "8853.481 m3" in report
    assert "Incomplete - do not use as a meter reading" in report
    assert "not submitted to Tado" in report


def test_cumulative_calibration_derives_corrected_register():
    historical = reading("10078", "METERS_CUBED")
    current = reading("23991.220", "METERS_CUBED")
    report = probe.build_cumulative_calibration_report(
        (Decimal("5039"), "2022-12-24T00:00:00+00:00"),
        historical,
        current,
    )
    assert "Observed aggregate factor | 2" in report
    assert "11995.610 m3" in report


def test_closest_accumulation_selects_reading_nearest_anchor():
    earlier = reading("10070", "METERS_CUBED", "2022-12-23T00:00:00+00:00")
    earlier["intervalEnd"] = "2022-12-24T00:00:00+00:00"
    later = reading("10080", "METERS_CUBED", "2022-12-25T00:00:00+00:00")
    later["intervalEnd"] = "2022-12-26T00:00:00+00:00"
    assert (
        probe.closest_accumulation_to_time(
            [later, earlier], "2022-12-24T00:00:00+00:00"
        )
        is earlier
    )
