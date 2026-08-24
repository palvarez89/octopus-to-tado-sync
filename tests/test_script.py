import sys
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from sync_octopus_tado import (
    DEFAULT_M3_TO_KWH_FACTOR,
    append_github_step_summary,
    call_tado_method,
    get_calibrated_cumulative_delta,
    get_consumption_since_date,
    get_consumption_unit_multiplier,
    get_meter_reading_total_consumption,
    get_tado_last_meter_reading,
    get_tado_last_tariff_checkpoint,
    parse_args,
    send_reading_to_tado,
    send_reading_to_tado_client,
    sync_octopus_tariffs_to_tado,
    tado_login,
)

# Mock data for Octopus API response
MOCK_CONSUMPTION_RESPONSE = {
    "results": [{"consumption": 1.2}, {"consumption": 2.3}],
    "next": None,
}


@patch("sync_octopus_tado.requests.get")
def test_get_meter_reading_total_consumption_fallback(mock_get):
    """Test fallback to 2-year window when no Tado reading exists"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = MOCK_CONSUMPTION_RESPONSE
    mock_get.return_value = mock_response

    total = get_meter_reading_total_consumption("fake-api-key", "123456789", "GAS123")
    assert total == 3.5


@patch("sync_octopus_tado.requests.get")
def test_get_meter_reading_with_delta_sync(mock_get):
    """Test delta sync when Tado reading exists"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = MOCK_CONSUMPTION_RESPONSE
    mock_get.return_value = mock_response

    # Mock Tado object with existing reading
    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [
            {"id": "test-id-1", "homeId": 123, "reading": 100.0, "date": "2025-01-01"}
        ]
    }

    total = get_meter_reading_total_consumption(
        "fake-api-key", "123456789", "GAS123", tado=mock_tado
    )
    # Should be 100 (previous) + 3.5 (delta) = 103.5
    assert total == 103.5


@patch("sync_octopus_tado.get_calibrated_cumulative_delta")
@patch("sync_octopus_tado.requests.get")
def test_meter_sync_uses_calibrated_cumulative_delta(mock_get, mock_cumulative):
    mock_cumulative.return_value = (
        1.010,
        date(2026, 8, 22),
        Decimal("2"),
        5,
    )
    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [{"reading": 3859, "date": "2026-08-20"}]
    }

    update = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 24),
        include_reading_date=True,
        consumption_multiplier=DEFAULT_M3_TO_KWH_FACTOR,
        meter_source="calibrated-cumulative",
    )

    assert update[0] == pytest.approx(3859 + (1.010 * DEFAULT_M3_TO_KWH_FACTOR))
    assert update[1] == date(2026, 8, 22)
    mock_get.assert_not_called()


@patch("sync_octopus_tado.get_calibrated_cumulative_delta")
def test_meter_sync_rejects_cumulative_reading_not_newer_than_tado(mock_cumulative):
    mock_cumulative.return_value = (
        1.010,
        date(2026, 8, 20),
        Decimal("2"),
        5,
    )
    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [{"reading": 3859, "date": "2026-08-20"}]
    }

    update = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 24),
        include_reading_date=True,
        meter_source="calibrated-cumulative",
    )

    assert update is None


@patch("sync_octopus_tado.get_calibrated_cumulative_delta")
def test_meter_sync_allows_stale_cumulative_preview_only_when_requested(
    mock_cumulative,
):
    mock_cumulative.return_value = (
        3.5,
        date(2026, 8, 22),
        Decimal("2"),
        5,
    )
    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [
            {"reading": 3859, "date": "2026-08-23"},
            {"reading": 3858, "date": "2026-08-20"},
        ]
    }

    update = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 24),
        include_reading_date=True,
        meter_source="calibrated-cumulative",
        allow_stale_preview=True,
    )

    assert update == (3861.5, date(2026, 8, 22))
    mock_cumulative.assert_called_once_with(
        "fake-api-key",
        "123456789",
        date(2026, 8, 20),
        date(2026, 8, 22),
    )


@patch("sync_octopus_tado.octopus_probe.graphql_request")
@patch("sync_octopus_tado.octopus_probe.obtain_token", return_value="token")
def test_calibrated_cumulative_delta_requires_stable_recent_factor(
    mock_token, mock_graphql
):
    def item(value, end):
        return {
            "value": value,
            "units": "METERS_CUBED",
            "intervalStart": "2026-08-18T00:00:00+01:00",
            "intervalEnd": end,
        }

    intervals = [
        item("1.000", "2026-08-20T00:00:00+01:00"),
        item("1.200", "2026-08-21T00:00:00+01:00"),
        item("1.100", "2026-08-22T00:00:00+01:00"),
        item("1.200", "2026-08-23T00:00:00+01:00"),
    ]
    accumulations = [
        item("100.0", "2026-08-19T01:00:00+01:00"),
        item("102.0", "2026-08-20T01:00:00+01:00"),
        item("104.4", "2026-08-21T01:00:00+01:00"),
        item("106.6", "2026-08-22T01:00:00+01:00"),
        item("109.0", "2026-08-23T01:00:00+01:00"),
    ]
    mock_graphql.side_effect = [
        {
            "supplyPoint": {
                "m3": {
                    "importReadings": {
                        "edges": [{"node": reading} for reading in intervals]
                    }
                }
            }
        },
        {
            "supplyPoint": {
                "accumulation": {
                    "importReadings": {
                        "edges": [{"node": reading} for reading in accumulations]
                    }
                }
            }
        },
    ]

    delta, reading_date, factor, matches = get_calibrated_cumulative_delta(
        "api-key",
        "mprn",
        date(2026, 8, 19),
        date(2026, 8, 22),
    )

    assert delta == pytest.approx(3.5)
    assert reading_date == date(2026, 8, 22)
    assert factor == Decimal("2")
    assert matches == 4


def test_append_github_step_summary_writes_values(tmp_path, monkeypatch):
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    append_github_step_summary(
        "Meter sync",
        [("Raw Octopus usage", "1.010 m3"), ("Converted usage", "11.299 kwh")],
    )

    summary = summary_path.read_text(encoding="utf-8")
    assert "## Meter sync" in summary
    assert "| Raw Octopus usage | 1.010 m3 |" in summary
    assert "| Converted usage | 11.299 kwh |" in summary


def test_consumption_unit_multiplier_converts_m3_to_kwh():
    multiplier = get_consumption_unit_multiplier("m3", "kwh", DEFAULT_M3_TO_KWH_FACTOR)

    assert multiplier == DEFAULT_M3_TO_KWH_FACTOR
    assert get_consumption_unit_multiplier("kwh", "kwh", multiplier) == 1.0


@patch("sync_octopus_tado.requests.get")
def test_meter_sync_converts_octopus_m3_to_tado_kwh(mock_get):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "results": [{"consumption": 1.010}],
        "next": None,
    }
    mock_get.return_value = response

    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [{"reading": 3859, "date": "2026-08-20"}]
    }

    total = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 24),
        consumption_multiplier=DEFAULT_M3_TO_KWH_FACTOR,
    )

    assert total == pytest.approx(3859 + (1.010 * DEFAULT_M3_TO_KWH_FACTOR))


@patch("sync_octopus_tado.requests.get")
def test_meter_sync_waits_for_complete_period(mock_get):
    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [
            {"reading": 3859, "date": "2026-08-23"},
            {"reading": 3859, "date": "2026-08-22"},
        ]
    }

    update = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 24),
        include_reading_date=True,
    )

    assert update is None
    mock_get.assert_not_called()


@patch("sync_octopus_tado.requests.get")
def test_meter_sync_does_not_advance_checkpoint_when_octopus_has_no_data(mock_get):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"results": [], "next": None}
    mock_get.return_value = response

    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [{"reading": 3859, "date": "2026-08-20"}]
    }

    update = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 24),
        include_reading_date=True,
    )

    assert update is None


@patch("sync_octopus_tado.requests.get")
def test_meter_sync_recovers_flat_streak_from_last_changed_reading(mock_get):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "results": [{"consumption": 1.5}, {"consumption": 2.0}],
        "next": None,
    }
    mock_get.return_value = response

    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [
            {"reading": 3859, "date": "2026-08-23"},
            {"reading": 3859, "date": "2026-08-22"},
            {"reading": 3859, "date": "2026-08-21"},
            {"reading": 3859, "date": "2026-08-20"},
            {"reading": 3859, "date": "2026-08-19"},
            {"reading": 3858, "date": "2026-08-18"},
        ]
    }

    update = get_meter_reading_total_consumption(
        "fake-api-key",
        "123456789",
        "GAS123",
        tado=mock_tado,
        today=date(2026, 8, 26),
        include_reading_date=True,
    )

    assert update == (3861.5, date(2026, 8, 24))
    requested_url = mock_get.call_args.args[0]
    assert "period_from=2026-08-18" in requested_url
    assert "period_to=2026-08-24" in requested_url
    assert "group_by=quarter" not in requested_url


def test_send_reading_to_tado_client_uses_source_cutoff_date():
    mock_tado = MagicMock()
    mock_tado.set_eiq_meter_readings.return_value = {"status": "success"}

    send_reading_to_tado_client(mock_tado, 3861.5, date(2026, 8, 24))

    mock_tado.set_eiq_meter_readings.assert_called_once_with(
        reading=3862, date="2026-08-24"
    )


@patch("sync_octopus_tado.browser_login")
@patch("sync_octopus_tado.Tado")
def test_tado_login_success(mock_tado_class, mock_browser_login):
    mock_tado = MagicMock()
    mock_tado.device_activation_status.side_effect = ["PENDING", "COMPLETED"]
    mock_tado.device_verification_url.return_value = "https://fake.url"
    mock_tado_class.return_value = mock_tado

    result = tado_login("test@example.com", "pass")
    assert result == mock_tado
    mock_browser_login.assert_called_once()


@patch("sync_octopus_tado.tado_login")
def test_send_reading_to_tado(mock_tado_login):
    mock_tado = MagicMock()
    mock_tado.set_eiq_meter_readings.return_value = {"status": "success"}
    mock_tado_login.return_value = mock_tado

    send_reading_to_tado("email", "pass", 42)
    mock_tado.set_eiq_meter_readings.assert_called_once_with(reading=42)


def test_get_tado_last_meter_reading():
    """Test retrieving last meter reading from Tado"""
    mock_tado = MagicMock()
    mock_tado.get_eiq_meter_readings.return_value = {
        "readings": [
            {"id": "test-id-1", "homeId": 123, "reading": 150.5, "date": "2025-03-01"},
            {"id": "test-id-2", "homeId": 123, "reading": 140.0, "date": "2025-02-22"},
        ]
    }

    reading, timestamp = get_tado_last_meter_reading(mock_tado)
    assert reading == 150.5
    assert timestamp == "2025-03-01"


@patch("sync_octopus_tado.requests.get")
def test_get_consumption_since_date(mock_get):
    """Test consumption calculation since a specific date"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = MOCK_CONSUMPTION_RESPONSE
    mock_get.return_value = mock_response

    delta = get_consumption_since_date(
        "fake-api-key", "123456789", "GAS123", "2025-01-01T00:00:00Z"
    )
    assert delta == 3.5


@patch("sync_octopus_tado.requests.get")
def test_get_consumption_since_date_raises_on_error(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = '{"detail":"No GasMeterPoint matches the given query."}'
    mock_get.return_value = mock_response

    with pytest.raises(
        RuntimeError,
        match="Failed to retrieve Octopus consumption delta.*MPRN: 123456789, Gas serial number: GAS123",
    ):
        get_consumption_since_date(
            "fake-api-key", "123456789", "GAS123", "2025-01-01T00:00:00Z"
        )


@patch("sync_octopus_tado.requests.get")
def test_get_meter_reading_total_consumption_fallback_raises_on_error(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = '{"detail":"No GasMeterPoint matches the given query."}'
    mock_get.return_value = mock_response

    with pytest.raises(
        RuntimeError,
        match="Failed to retrieve Octopus consumption data.*MPRN: 123456789, Gas serial number: GAS123",
    ):
        get_meter_reading_total_consumption("fake-api-key", "123456789", "GAS123")


def test_call_tado_method_uses_first_available_name():
    class TadoClientStub:
        def getEIQMeterReadings(self):
            return {"readings": []}

    mock_tado = TadoClientStub()

    result = call_tado_method(
        mock_tado, "get_eiq_meter_readings", "getEIQMeterReadings"
    )

    assert result == {"readings": []}


def test_get_tado_last_tariff_checkpoint():
    mock_tado = MagicMock()
    mock_tado.get_eiq_tariffs.return_value = {
        "tariffs": [
            {"startDate": "2025-02-01", "tariffInCents": 700},
            {"startDate": "2025-01-01", "tariffInCents": 623},
        ]
    }

    latest_start = get_tado_last_tariff_checkpoint(mock_tado)

    assert latest_start == date(2025, 2, 1)


@patch("sync_octopus_tado.requests.get")
def test_sync_octopus_tariffs_to_tado(mock_get):
    account_response = MagicMock()
    account_response.status_code = 200
    account_response.json.return_value = {
        "properties": [
            {
                "gas_meter_points": [
                    {
                        "mprn": "123456789",
                        "meters": [{"serial_number": "GAS123"}],
                        "agreements": [
                            {
                                "tariff_code": "G-1R-VAR-24-01-01",
                                "valid_from": "2025-01-01T00:00:00Z",
                                "valid_to": None,
                            }
                        ],
                    }
                ]
            }
        ]
    }

    rate_response = MagicMock()
    rate_response.status_code = 200
    rate_response.json.return_value = {
        "results": [
            {"value_inc_vat": 7.10, "valid_from": "2025-04-01T00:00:00Z"},
            {"value_inc_vat": 6.23, "valid_from": "2025-01-01T00:00:00Z"},
        ],
        "next": None,
    }

    mock_get.side_effect = [account_response, rate_response]

    mock_tado = MagicMock()
    mock_tado.get_eiq_tariffs.return_value = {"tariffs": []}
    mock_tado.set_eiq_tariff.side_effect = [{"status": "ok-1"}, {"status": "ok-2"}]

    synced = sync_octopus_tariffs_to_tado(
        mock_tado, "fake-api-key", "A-12345", "123456789", "GAS123"
    )

    assert synced == [
        {
            "from_date": "2025-01-01",
            "to_date": "2025-03-31",
            "tariff": 0.0623,
            "unit": "kWh",
            "is_period": True,
        },
        {
            "from_date": "2025-04-01",
            "tariff": 0.071,
            "unit": "kWh",
            "is_period": False,
        },
    ]


@patch("sync_octopus_tado.requests.get")
def test_sync_octopus_tariffs_to_tado_uses_checkpoint(mock_get):
    account_response = MagicMock()
    account_response.status_code = 200
    account_response.json.return_value = {
        "properties": [
            {
                "gas_meter_points": [
                    {
                        "mprn": "123456789",
                        "meters": [{"serial_number": "GAS123"}],
                        "agreements": [
                            {
                                "tariff_code": "G-1R-VAR-24-01-01",
                                "valid_from": "2025-01-01T00:00:00Z",
                                "valid_to": None,
                            }
                        ],
                    }
                ]
            }
        ]
    }

    rate_response = MagicMock()
    rate_response.status_code = 200
    rate_response.json.return_value = {
        "results": [
            {"value_inc_vat": 7.10, "valid_from": "2025-04-01T00:00:00Z"},
            {"value_inc_vat": 6.23, "valid_from": "2025-01-01T00:00:00Z"},
        ],
        "next": None,
    }

    mock_get.side_effect = [account_response, rate_response]

    mock_tado = MagicMock()
    mock_tado.get_eiq_tariffs.return_value = {
        "tariffs": [{"startDate": "2025-01-01", "tariffInCents": 623}]
    }

    synced = sync_octopus_tariffs_to_tado(
        mock_tado, "fake-api-key", "A-12345", "123456789", "GAS123"
    )

    assert synced == [
        {
            "from_date": "2025-04-01",
            "tariff": 0.071,
            "unit": "kWh",
            "is_period": False,
        }
    ]
    mock_tado.set_eiq_tariff.assert_called_once_with(
        from_date="2025-04-01",
        tariff=0.071,
        unit="kWh",
        is_period=False,
    )


def test_parse_args_update_tariff_flag():
    test_argv = [
        "sync_octopus_tado.py",
        "--tado-email",
        "user@example.com",
        "--tado-password",
        "secret",
        "--mprn",
        "123456789",
        "--gas-serial-number",
        "GAS123",
        "--octopus-api-key",
        "fake-api-key",
        "--update-tariff",
        "--octopus-account-number",
        "A-12345",
    ]

    with patch.object(sys, "argv", test_argv):
        args = parse_args()

    assert args.update_tariff is True
    assert args.octopus_account_number == "A-12345"
    assert args.octopus_consumption_unit == "m3"
    assert args.tado_reading_unit == "kwh"
    assert args.m3_to_kwh_factor == DEFAULT_M3_TO_KWH_FACTOR
