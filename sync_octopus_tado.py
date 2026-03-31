import argparse
import asyncio
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright
from PyTado.interface import Tado
from requests.auth import HTTPBasicAuth


def get_tado_last_meter_reading(tado):
    """
    Retrieves the last meter reading that was sent to Tado.

    Returns: A tuple of (reading_value, datetime_of_reading) or (None, None) if no reading exists.
    """
    try:
        # Get energy IQ status which includes meter reading info
        eiq_data = tado.get_eiq_meter_readings()

        if eiq_data and isinstance(eiq_data, dict) and "readings" in eiq_data:
            readings = eiq_data["readings"]
            if readings and len(readings) > 0:
                # The first reading in the list is the most recent
                latest_reading = readings[0]
                reading_value = latest_reading.get("reading")
                reading_date = latest_reading.get("date")

                if reading_value is not None and reading_date is not None:
                    print(
                        f"Last Tado meter reading: {reading_value} (date: {reading_date})"
                    )
                    return reading_value, reading_date
    except Exception as e:
        print(f"Could not retrieve last Tado meter reading: {e}")

    return None, None


def get_consumption_since_date(api_key, mprn, gas_serial_number, since_datetime):
    """
    Retrieves gas consumption from Octopus Energy API since a specific date.

    Args:
        api_key: Octopus API key
        mprn: Meter Point Reference Number
        gas_serial_number: Gas meter serial number
        since_datetime: datetime object or ISO string - only get consumption after this date

    Returns:
        Total consumption since the given date
    """
    if isinstance(since_datetime, str):
        # Parse ISO format datetime string
        since_datetime = datetime.fromisoformat(since_datetime.replace("Z", "+00:00"))

    url = (
        f"https://api.octopus.energy/v1/gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/?group_by=quarter&period_from="
        f"{since_datetime.isoformat()}Z"
    )
    consumption_delta = 0.0

    while url:
        response = requests.get(url, auth=HTTPBasicAuth(api_key, ""))

        if response.status_code == 200:
            meter_readings = response.json()
            consumption_delta += sum(
                interval["consumption"] for interval in meter_readings["results"]
            )
            url = meter_readings.get("next", "")
        else:
            print(
                f"Failed to retrieve data. Status code: {response.status_code}, Message: {response.text}"
            )
            break

    return consumption_delta


def get_meter_reading_total_consumption(api_key, mprn, gas_serial_number, tado=None):
    """
    Retrieves total gas consumption and calculates the delta since last Tado reading.

    Strategy:
    1. If Tado has a previous reading, query Octopus for consumption SINCE that date
    2. Add the delta to the previous reading to get the new total
    3. If no previous reading exists, fall back to getting the last 2 years from Octopus

    This approach works around the 2-year API limit by only syncing the delta,
    allowing cumulative values to grow indefinitely in Tado without needing local cache.
    """
    if tado is not None:
        # Try to get the last reading from Tado
        last_tado_reading, last_tado_update = get_tado_last_meter_reading(tado)

        if last_tado_reading is not None and last_tado_update is not None:
            print(f"Using delta sync: last Tado reading was {last_tado_reading}")

            # Get consumption since that date
            consumption_delta = get_consumption_since_date(
                api_key, mprn, gas_serial_number, last_tado_update
            )

            # New total = old total + new delta
            total_consumption = last_tado_reading + consumption_delta
            print(f"Consumption delta since last reading: {consumption_delta}")
            print(f"New total consumption: {total_consumption}")

            return total_consumption

    # Fallback: Get the last 2 years of consumption if we can't retrieve Tado's last reading
    print(
        "No previous Tado reading found, falling back to last 2 years of Octopus data"
    )
    period_from = datetime.now() - timedelta(days=1095)  # 3 years back
    url = (
        f"https://api.octopus.energy/v1/gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/?group_by=quarter&period_from="
        f"{period_from.isoformat()}Z"
    )

    total_consumption = 0.0

    while url:
        response = requests.get(url, auth=HTTPBasicAuth(api_key, ""))

        if response.status_code == 200:
            meter_readings = response.json()
            total_consumption += sum(
                interval["consumption"] for interval in meter_readings["results"]
            )
            url = meter_readings.get("next", "")
        else:
            print(
                f"Failed to retrieve data. Status code: {response.status_code}, Message: {response.text}"
            )
            break

    print(
        "Total consumption (fallback - all available Octopus data): "
        f"{total_consumption}"
    )
    return total_consumption


async def browser_login(url, username, password):

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True
        )  # Set to True if you don't want a browser window
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(url)

        # Click the "Submit" button before login
        await page.wait_for_selector('text="Submit"', timeout=5000)
        await page.click('text="Submit"')

        # Wait for the login form to appear
        await page.wait_for_selector('input[name="loginId"]')

        # Replace with actual selectors for your site
        await page.fill('input[id="loginId"]', username)
        await page.fill('input[name="password"]', password)

        await page.click('button.c-btn--primary:has-text("Sign in")')

        # Optionally take a screenshot
        await page.screenshot(path="screenshot.png")

        await page.wait_for_selector(
            ".text-center.message-screen.b-bubble-screen__spaced", timeout=10000
        )

        # Take a screenshot (optional)
        await page.screenshot(path="after-message.png")
        await browser.close()


def tado_login(username, password):
    tado = Tado(token_file_path="/tmp/tado_refresh_token")

    status = tado.device_activation_status()

    if status == "PENDING":
        url = tado.device_verification_url()

        asyncio.run(browser_login(url, username, password))

        tado.device_activation()

        status = tado.device_activation_status()

    if status == "COMPLETED":
        print("Login successful")
    else:
        print(f"Login status is {status}")

    return tado


def send_reading_to_tado(username, password, reading):
    """
    Sends the total consumption reading to Tado using its Energy IQ feature.
    """

    tado = tado_login(username=username, password=password)

    result = tado.set_eiq_meter_readings(reading=int(reading))
    print(result)


def parse_args():
    """
    Parses command-line arguments for Tado and Octopus API credentials and meter details.
    """
    parser = argparse.ArgumentParser(
        description="Tado and Octopus API Interaction Script"
    )

    # Tado API arguments
    parser.add_argument("--tado-email", required=True, help="Tado account email")
    parser.add_argument("--tado-password", required=True, help="Tado account password")

    # Octopus API arguments
    parser.add_argument(
        "--mprn",
        required=True,
        help="MPRN (Meter Point Reference Number) for the gas meter",
    )
    parser.add_argument(
        "--gas-serial-number", required=True, help="Gas meter serial number"
    )
    parser.add_argument("--octopus-api-key", required=True, help="Octopus API key")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # First, authenticate with Tado to retrieve the last reading
    tado = tado_login(args.tado_email, args.tado_password)

    # Get total consumption from Octopus Energy API
    # This will use delta sync if possible, falling back to 2-year window
    consumption = get_meter_reading_total_consumption(
        args.octopus_api_key, args.mprn, args.gas_serial_number, tado=tado
    )

    # Send the total consumption to Tado
    send_reading_to_tado(args.tado_email, args.tado_password, consumption)
