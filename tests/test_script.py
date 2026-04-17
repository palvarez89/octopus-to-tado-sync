import sys
from datetime import date
from unittest.mock import MagicMock, patch

from sync_octopus_tado import (
    call_tado_method,
    get_tado_last_tariff_checkpoint,
    get_consumption_since_date,
    get_meter_reading_total_consumption,
    get_tado_last_meter_reading,
    parse_args,
    send_reading_to_tado,
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

