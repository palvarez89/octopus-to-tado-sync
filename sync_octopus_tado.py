import argparse
import asyncio
import os
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

import requests
from playwright.async_api import async_playwright
from PyTado.interface import Tado
from requests.auth import HTTPBasicAuth

DEFAULT_M3_TO_KWH_FACTOR = 11.1868


def get_consumption_unit_multiplier(source_unit, target_unit, m3_to_kwh_factor):
    """Return the multiplier needed to convert Octopus usage to Tado's unit."""
    source_unit = source_unit.lower()
    target_unit = target_unit.lower()
    if source_unit == target_unit:
        return 1.0
    if source_unit == "m3" and target_unit == "kwh":
        return m3_to_kwh_factor
    if source_unit == "kwh" and target_unit == "m3":
        return 1.0 / m3_to_kwh_factor
    raise ValueError(
        f"Unsupported consumption unit conversion: {source_unit} to {target_unit}"
    )


def append_github_step_summary(title, rows):
    """Append a Markdown value table to the GitHub Actions job summary."""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as summary:
        summary.write(f"## {title}\n\n")
        summary.write("| Value | Result |\n|---|---|\n")
        for label, value in rows:
            safe_value = str(value).replace("|", "\\|").replace("\n", " ")
            summary.write(f"| {label} | {safe_value} |\n")
        summary.write("\n")


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

        product_code = agreement.get(
            "product_code"
        ) or derive_product_code_from_tariff_code(tariff_code)
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


def get_tado_meter_readings(tado):
    """Return valid Tado meter readings, newest first."""
    try:
        eiq_data = call_tado_method(
            tado, "get_eiq_meter_readings", "getEIQMeterReadings"
        )
        if not isinstance(eiq_data, dict):
            return []

        readings = []
        for reading in eiq_data.get("readings", []):
            if reading.get("reading") is None or reading.get("date") is None:
                continue
            readings.append(reading)

        return sorted(
            readings, key=lambda reading: parse_api_date(reading["date"]), reverse=True
        )
    except Exception as e:
        print(f"Could not retrieve Tado meter readings: {e}")
        return []


def get_tado_last_meter_reading(tado):
    """
    Retrieves the last meter reading that was sent to Tado.

    Returns: A tuple of (reading_value, datetime_of_reading) or (None, None) if no reading exists.
    """
    readings = get_tado_meter_readings(tado)
    if readings:
        latest_reading = readings[0]
        reading_value = latest_reading["reading"]
        reading_date = latest_reading["date"]
        print(f"Last Tado meter reading: {reading_value} (date: {reading_date})")
        return reading_value, reading_date

    return None, None


def get_consumption_since_date(
    api_key,
    mprn,
    gas_serial_number,
    since_datetime,
    until_datetime=None,
    include_interval_count=False,
):
    """
    Retrieves gas consumption from Octopus Energy API since a specific date.

    Args:
        api_key: Octopus API key
        mprn: Meter Point Reference Number
        gas_serial_number: Gas meter serial number
        since_datetime: datetime object or ISO string - only get consumption after this date
        until_datetime: optional exclusive end date
        include_interval_count: also return the number of source intervals

    Returns:
        Total consumption, optionally with the source interval count
    """
    query = {"period_from": format_api_date(since_datetime), "order_by": "period"}
    if until_datetime is not None:
        query["period_to"] = format_api_date(until_datetime)
    url = (
        f"https://api.octopus.energy/v1/gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/?{urlencode(query)}"
    )
    consumption_delta = 0.0
    interval_count = 0

    while url:
        response = requests.get(url, auth=HTTPBasicAuth(api_key, ""))

        if response.status_code == 200:
            meter_readings = response.json()
            intervals = meter_readings.get("results", [])
            consumption_delta += sum(interval["consumption"] for interval in intervals)
            interval_count += len(intervals)
            url = meter_readings.get("next", "")
        else:
            raise RuntimeError(
                "Failed to retrieve Octopus consumption delta. "
                f"MPRN: {mprn}, Gas serial number: {gas_serial_number}, "
                f"Status code: {response.status_code}, Message: {response.text}"
            )

    if include_interval_count:
        return consumption_delta, interval_count
    return consumption_delta


def get_tado_meter_checkpoint(readings):
    """Choose a checkpoint, rewinding a flat latest streak when possible."""
    if not readings:
        return None

    latest_value = float(readings[0]["reading"])
    for reading in readings[1:]:
        if float(reading["reading"]) != latest_value:
            return reading

    return readings[0]


def get_meter_reading_total_consumption(
    api_key,
    mprn,
    gas_serial_number,
    tado=None,
    today=None,
    data_delay_days=2,
    include_reading_date=False,
    consumption_multiplier=1.0,
    source_unit="source units",
    target_unit="target units",
):
    """
    Retrieves total gas consumption and calculates the delta since last Tado reading.

    Strategy:
    1. Only query up to a delayed cutoff, so late Octopus data has time to arrive
    2. If Tado has a previous reading, add consumption since its checkpoint
    3. Rewind a flat latest streak to recover data skipped by older versions
    4. If no previous reading exists, use the last 3 years from Octopus

    This approach works around the API history limit by only syncing the delta,
    allowing cumulative values to grow indefinitely in Tado without needing local cache.
    """
    today = today or date.today()
    cutoff_date = today - timedelta(days=data_delay_days)

    if tado is not None:
        readings = get_tado_meter_readings(tado)
        if readings:
            latest_tado_date = parse_api_date(readings[0]["date"])
            if cutoff_date <= latest_tado_date:
                print(
                    "No complete new Octopus period is available: "
                    f"cutoff {cutoff_date} is not after latest Tado reading "
                    f"{latest_tado_date}. Skipping meter update."
                )
                append_github_step_summary(
                    "Meter sync",
                    [
                        ("Status", "Skipped - waiting for a complete new period"),
                        (
                            "Latest Tado reading",
                            f'{readings[0]["reading"]} {target_unit} on {latest_tado_date}',
                        ),
                        ("Octopus cutoff", cutoff_date),
                        ("Octopus API queried", "No"),
                    ],
                )
                return None

            checkpoint = get_tado_meter_checkpoint(readings)
            checkpoint_value = float(checkpoint["reading"])
            checkpoint_date = parse_api_date(checkpoint["date"])
            if checkpoint is not readings[0]:
                print(
                    "Detected a flat Tado reading streak; rewinding checkpoint to "
                    f"{checkpoint_value} on {checkpoint_date} to recover delayed data."
                )
            print(f"Using delta sync: checkpoint Tado reading was {checkpoint_value}")

            consumption_delta, interval_count = get_consumption_since_date(
                api_key,
                mprn,
                gas_serial_number,
                checkpoint_date,
                cutoff_date,
                include_interval_count=True,
            )
            if interval_count == 0:
                print("Octopus returned no complete intervals. Skipping meter update.")
                append_github_step_summary(
                    "Meter sync",
                    [
                        ("Status", "Skipped - Octopus returned no intervals"),
                        (
                            "Tado checkpoint",
                            f"{checkpoint_value} {target_unit} on {checkpoint_date}",
                        ),
                        ("Octopus period", f"{checkpoint_date} to {cutoff_date}"),
                        ("Octopus intervals", 0),
                    ],
                )
                return None

            raw_consumption_delta = consumption_delta
            consumption_delta *= consumption_multiplier
            if consumption_multiplier != 1.0:
                print(
                    f"Converted consumption delta: {raw_consumption_delta} -> "
                    f"{consumption_delta}"
                )
            total_consumption = checkpoint_value + consumption_delta
            print(f"Consumption delta since last reading: {consumption_delta}")
            print(f"New total consumption: {total_consumption}")

            append_github_step_summary(
                "Meter sync calculation",
                [
                    ("Status", "Ready to submit"),
                    (
                        "Tado checkpoint",
                        f"{checkpoint_value:.3f} {target_unit} on {checkpoint_date}",
                    ),
                    ("Octopus period", f"{checkpoint_date} to {cutoff_date}"),
                    ("Octopus intervals", interval_count),
                    (
                        "Raw Octopus usage",
                        f"{raw_consumption_delta:.3f} {source_unit}",
                    ),
                    ("Conversion multiplier", f"{consumption_multiplier:.6f}"),
                    ("Converted usage", f"{consumption_delta:.3f} {target_unit}"),
                    (
                        "Proposed Tado reading",
                        f"{total_consumption:.3f} {target_unit} on {cutoff_date}",
                    ),
                ],
            )
            if include_reading_date:
                return total_consumption, cutoff_date
            return total_consumption

    print(
        "No previous Tado reading found, falling back to last 3 years of Octopus data"
    )
    period_from = cutoff_date - timedelta(days=1095)
    query = {
        "period_from": period_from.isoformat(),
        "period_to": cutoff_date.isoformat(),
        "order_by": "period",
    }
    url = (
        f"https://api.octopus.energy/v1/gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/?{urlencode(query)}"
    )

    total_consumption = 0.0
    interval_count = 0

    while url:
        response = requests.get(url, auth=HTTPBasicAuth(api_key, ""))

        if response.status_code == 200:
            meter_readings = response.json()
            intervals = meter_readings.get("results", [])
            total_consumption += sum(interval["consumption"] for interval in intervals)
            interval_count += len(intervals)
            url = meter_readings.get("next", "")
        else:
            raise RuntimeError(
                "Failed to retrieve Octopus consumption data. "
                f"MPRN: {mprn}, Gas serial number: {gas_serial_number}, "
                f"Status code: {response.status_code}, Message: {response.text}"
            )

    if interval_count == 0:
        print("Octopus returned no complete intervals. Skipping meter update.")
        append_github_step_summary(
            "Meter sync",
            [
                ("Status", "Skipped - Octopus returned no intervals"),
                ("Octopus period", f"{period_from} to {cutoff_date}"),
                ("Octopus intervals", 0),
            ],
        )
        return None

    total_consumption *= consumption_multiplier
    print(
        "Total consumption (fallback - all available Octopus data): "
        f"{total_consumption}"
    )
    append_github_step_summary(
        "Meter sync calculation",
        [
            ("Status", "Ready to submit using fallback baseline"),
            ("Octopus period", f"{period_from} to {cutoff_date}"),
            ("Octopus intervals", interval_count),
            ("Conversion multiplier", f"{consumption_multiplier:.6f}"),
            (
                "Proposed Tado reading",
                f"{total_consumption:.3f} {target_unit} on {cutoff_date}",
            ),
        ],
    )
    if include_reading_date:
        return total_consumption, cutoff_date
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


def send_reading_to_tado(username, password, reading, reading_date=None):
    """
    Sends the total consumption reading to Tado using its Energy IQ feature.
    """

    tado = tado_login(username=username, password=password)

    payload = {"reading": int(reading + 0.5)}
    if reading_date is not None:
        payload["date"] = format_api_date(reading_date)

    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        **payload,
    )
    print(result)


def send_reading_to_tado_client(tado, reading, reading_date=None):
    """Send the total consumption reading to an authenticated Tado client."""
    payload = {"reading": int(reading + 0.5)}
    if reading_date is not None:
        payload["date"] = format_api_date(reading_date)

    result = call_tado_method(
        tado,
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
        **payload,
    )
    print(result)
    append_github_step_summary(
        "Tado submission",
        [
            ("Status", "Submitted"),
            ("Reading", payload["reading"]),
            ("Reading date", payload.get("date", "today")),
        ],
    )


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
    parser.add_argument(
        "--octopus-consumption-unit",
        choices=("m3", "kwh"),
        default=os.getenv("OCTOPUS_CONSUMPTION_UNIT", "m3").lower(),
        help="Unit returned by the Octopus gas consumption API (default: m3).",
    )
    parser.add_argument(
        "--tado-reading-unit",
        choices=("m3", "kwh"),
        default=os.getenv("TADO_READING_UNIT", "kwh").lower(),
        help="Meter-reading unit configured in Tado Energy IQ (default: kwh).",
    )
    parser.add_argument(
        "--m3-to-kwh-factor",
        type=float,
        default=float(os.getenv("M3_TO_KWH_FACTOR", str(DEFAULT_M3_TO_KWH_FACTOR))),
        help=(
            "Conversion factor when Octopus returns m3 and Tado expects kWh "
            f"(default: {DEFAULT_M3_TO_KWH_FACTOR})."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # First, authenticate with Tado to retrieve the last reading
    tado = tado_login(args.tado_email, args.tado_password)

    consumption_multiplier = get_consumption_unit_multiplier(
        args.octopus_consumption_unit,
        args.tado_reading_unit,
        args.m3_to_kwh_factor,
    )
    meter_update = get_meter_reading_total_consumption(
        args.octopus_api_key,
        args.mprn,
        args.gas_serial_number,
        tado=tado,
        include_reading_date=True,
        consumption_multiplier=consumption_multiplier,
        source_unit=args.octopus_consumption_unit,
        target_unit=args.tado_reading_unit,
    )

    if meter_update is not None:
        consumption, reading_date = meter_update
        send_reading_to_tado_client(tado, consumption, reading_date)

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
