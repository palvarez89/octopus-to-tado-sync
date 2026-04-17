import argparse
import asyncio
import os
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests
from playwright.async_api import async_playwright
from PyTado.interface import Tado
from requests.auth import HTTPBasicAuth


def call_tado_method(tado, *method_names, **kwargs):
    """Call the first available Tado client method from a list of candidates."""
    for method_name in method_names:
        method = getattr(tado, method_name, None)
        if callable(method):
            return method(**kwargs)

    raise AttributeError(
        f"None of the Tado methods exist on the client: {', '.join(method_names)}"
    )


def parse_api_date(value):
    """Parse a date or datetime value from Octopus/Tado API responses."""
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        normalized_value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized_value).date()

    raise TypeError(f"Unsupported date value: {value!r}")


def format_api_date(value):
    """Format a date-like value as YYYY-MM-DD."""
    return parse_api_date(value).isoformat()


def fetch_paginated_results(url, api_key):
    """Fetch all results from a paginated Octopus endpoint."""
    results = []

    while url:
        response = requests.get(url, auth=HTTPBasicAuth(api_key, ""), timeout=30)

        if response.status_code != 200:
            raise RuntimeError(
                "Failed to retrieve data from Octopus. "
                f"Status code: {response.status_code}, Message: {response.text}"
            )

        payload = response.json()
        results.extend(payload.get("results", []))
        url = payload.get("next")

    return results


def get_octopus_account_details(api_key, account_number):
    """Retrieve Octopus account details, including active meter agreements."""
    url = f"https://api.octopus.energy/v1/accounts/{account_number}/"
    response = requests.get(url, auth=HTTPBasicAuth(api_key, ""), timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            "Failed to retrieve Octopus account details. "
            f"Status code: {response.status_code}, Message: {response.text}"
        )

    return response.json()


def derive_product_code_from_tariff_code(tariff_code):
    """Infer the Octopus product code from a tariff code."""
    parts = tariff_code.split("-")
    if len(parts) <= 2:
        return tariff_code

    product_parts = parts[2:]
    if product_parts and len(product_parts[-1]) == 1 and product_parts[-1].isalpha():
        product_parts = product_parts[:-1]

    return "-".join(product_parts)


def get_octopus_gas_agreements(account_details, mprn, gas_serial_number):
    """Extract matching gas agreements from Octopus account details."""
    matching_agreements = []

    for property_info in account_details.get("properties", []):
        for gas_meter_point in property_info.get("gas_meter_points", []):
            meter_point_mprn = gas_meter_point.get("mprn")
            if mprn and meter_point_mprn != mprn:
                continue

            meters = gas_meter_point.get("meters", [])
            if gas_serial_number:
                serial_numbers = {
                    meter.get("serial_number") or meter.get("serialNumber")
                    for meter in meters
                }
                serial_numbers.discard(None)
                if serial_numbers and gas_serial_number not in serial_numbers:
                    continue

            matching_agreements.extend(gas_meter_point.get("agreements", []))

    return matching_agreements


def get_octopus_standard_unit_rates(api_key, product_code, tariff_code):
    """Retrieve all unit-rate periods for a gas tariff."""
    encoded_tariff_code = quote(tariff_code, safe="")
    url = (
        f"https://api.octopus.energy/v1/products/{product_code}/gas-tariffs/"
        f"{encoded_tariff_code}/standard-unit-rates/"
    )
    return fetch_paginated_results(url, api_key)


def build_octopus_tariff_periods(agreement, unit_rates):
    """Convert Octopus unit-rate records into Tado-friendly tariff periods."""
    agreement_start = parse_api_date(agreement.get("valid_from")) or date.min
    agreement_end = parse_api_date(agreement.get("valid_to"))

    raw_periods = []
    for rate in unit_rates:
        tariff_pence = rate.get("value_inc_vat")
        rate_start = parse_api_date(rate.get("valid_from"))

        if tariff_pence is None or rate_start is None:
            continue

        start_date = max(agreement_start, rate_start)
        if agreement_end is not None and start_date > agreement_end:
            continue

        raw_periods.append(
            {
                "start_date": start_date,
                "tariff_pence_per_kwh": tariff_pence,
                "unit": "kWh",
            }
        )

    raw_periods.sort(key=lambda period: period["start_date"])

    merged_periods = []
    for period in raw_periods:
        if merged_periods and merged_periods[-1]["start_date"] == period["start_date"]:
            merged_periods[-1] = period
            continue

        if (
            merged_periods
            and merged_periods[-1]["tariff_pence_per_kwh"]
            == period["tariff_pence_per_kwh"]
        ):
            continue

        merged_periods.append(period)

    for index, period in enumerate(merged_periods):
        end_date = None
        if index + 1 < len(merged_periods):
            end_date = merged_periods[index + 1]["start_date"] - timedelta(days=1)
        elif agreement_end is not None:
            end_date = agreement_end

        period["end_date"] = end_date

    return merged_periods


def get_tado_last_tariff_checkpoint(tado):
    """Return the most recent tariff start date stored in Tado, if any."""
    try:
        tariff_data = call_tado_method(tado, "get_eiq_tariffs", "getEIQTariffs")

        if isinstance(tariff_data, dict):
            tariffs = tariff_data.get("tariffs", [])
        elif isinstance(tariff_data, list):
            tariffs = tariff_data
        else:
            tariffs = []

        latest_start_date = None
        for tariff in tariffs:
            start_value = (
                tariff.get("startDate")
                or tariff.get("start_date")
                or tariff.get("date")
                or tariff.get("fromDate")
                or tariff.get("from_date")
            )

            if not start_value:
                continue

            start_date = parse_api_date(start_value)
            if latest_start_date is None or start_date > latest_start_date:
                latest_start_date = start_date

        if latest_start_date is not None:
            print(f"Last Tado tariff starts on: {latest_start_date.isoformat()}")

        return latest_start_date
    except Exception as e:
        print(f"Could not retrieve Tado tariff history: {e}")
        return None


def discover_octopus_tariff_periods(
    api_key, account_number, mprn, gas_serial_number, since_date=None
):
    """Discover Octopus gas tariff periods that should be sent to Tado."""
    account_details = get_octopus_account_details(api_key, account_number)
    agreements = get_octopus_gas_agreements(account_details, mprn, gas_serial_number)

    if not agreements:
        raise RuntimeError(
            "No matching gas agreements found in Octopus account details for the "
            "provided MPRN / gas serial number."
        )

    periods_to_sync = []
    sorted_agreements = sorted(
        agreements,
        key=lambda agreement: parse_api_date(agreement.get("valid_from")) or date.min,
    )

    for agreement in sorted_agreements:
        tariff_code = agreement.get("tariff_code") or agreement.get("tariffCode")
        if not tariff_code:
            continue

        product_code = agreement.get("product_code") or derive_product_code_from_tariff_code(
            tariff_code
        )
        unit_rates = get_octopus_standard_unit_rates(api_key, product_code, tariff_code)
        agreement_periods = build_octopus_tariff_periods(agreement, unit_rates)

        for period in agreement_periods:
            if since_date is not None and period["start_date"] <= since_date:
                continue
            periods_to_sync.append(period)

    periods_to_sync.sort(key=lambda period: period["start_date"])
    return periods_to_sync


def sync_octopus_tariffs_to_tado(
    tado, api_key, account_number, mprn, gas_serial_number
):
    """Sync missing Octopus gas tariff periods into Tado Energy IQ."""
    last_tado_tariff_start = get_tado_last_tariff_checkpoint(tado)
    tariff_periods = discover_octopus_tariff_periods(
        api_key,
        account_number,
        mprn,
        gas_serial_number,
        since_date=last_tado_tariff_start,
    )

    if not tariff_periods:
        print("No Octopus tariff changes need to be synced to Tado")
        return []

    synced_periods = []
    for period in tariff_periods:
        payload = {
            "from_date": format_api_date(period["start_date"]),
            "tariff": period["tariff_pence_per_kwh"] / 100,
            "unit": period["unit"],
        }

        if period["end_date"] is not None:
            payload["to_date"] = format_api_date(period["end_date"])
            payload["is_period"] = True
        else:
            payload["is_period"] = False

        result = call_tado_method(tado, "set_eiq_tariff", "setEIQTariff", **payload)
        print(f"Synced tariff period to Tado: {payload} -> {result}")
        synced_periods.append(payload)

    return synced_periods


def get_tado_last_meter_reading(tado):
    """
    Retrieves the last meter reading that was sent to Tado.

    Returns: A tuple of (reading_value, datetime_of_reading) or (None, None) if no reading exists.
    """
    try:
        # Get energy IQ status which includes meter reading info
        eiq_data = call_tado_method(
            tado, "get_eiq_meter_readings", "getEIQMeterReadings"
        )

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

    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        reading=int(reading),
    )
    print(result)


def send_reading_to_tado_client(tado, reading):
    """Send the total consumption reading to an authenticated Tado client."""
    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        reading=int(reading),
    )
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
    parser.add_argument(
        "--octopus-account-number",
        default=os.getenv("OCTOPUS_ACCOUNT_NUMBER"),
        help=(
            "Octopus account number. Required when --update-tariff is enabled; "
            "can also be supplied via OCTOPUS_ACCOUNT_NUMBER."
        ),
    )
    parser.add_argument(
        "--update-tariff",
        action="store_true",
        help="Also sync Octopus gas tariff periods to Tado Energy IQ.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # First, authenticate with Tado to retrieve the last reading
    tado = tado_login(args.tado_email, args.tado_password)

    # Get total consumption from Octopus Energy API
    # This will use delta sync if possible, falling back to 2-year window
    consumption = get_meter_reading_total_consumption(
        args.octopus_api_key, args.mprn, args.gas_serial_number, tado=tado
    )

    # Send the total consumption to Tado
    send_reading_to_tado_client(tado, consumption)

    if args.update_tariff:
        if not args.octopus_account_number:
            print(
                "--update-tariff was enabled but no Octopus account number was "
                "provided. Set OCTOPUS_ACCOUNT_NUMBER or use "
                "--octopus-account-number."
            )
        else:
            try:
                sync_octopus_tariffs_to_tado(
                    tado,
                    args.octopus_api_key,
                    args.octopus_account_number,
                    args.mprn,
                    args.gas_serial_number,
                )
            except Exception as e:
                print(f"Tariff sync failed: {e}")


if __name__ == "__main__":
    main()
