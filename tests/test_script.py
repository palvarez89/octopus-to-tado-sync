from unittest.mock import MagicMock, patch

from sync_octopus_tado import (
    get_consumption_since_date,
    get_meter_reading_total_consumption,
    get_tado_last_meter_reading,
    send_reading_to_tado,
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
