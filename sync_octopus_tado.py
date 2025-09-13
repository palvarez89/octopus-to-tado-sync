
import os
import requests
import sys
from datetime import datetime, timedelta
import argparse

OCTOPUS_API_KEY = os.environ.get("OCTOPUS_API_KEY")
OCTOPUS_MPRN = os.environ.get("OCTOPUS_MPRN")
OCTOPUS_SERIAL = os.environ.get("OCTOPUS_SERIAL")
TADO_USERNAME = os.environ.get("TADO_USERNAME")
TADO_PASSWORD = os.environ.get("TADO_PASSWORD")

def parse_args():
    parser = argparse.ArgumentParser(description="Sync Octopus data to Tado")
    parser.add_argument(
        "--historical",
        action="store_true",
        help="Enable historical sync mode (requires start and end dates)",
    )
    parser.add_argument(
        "--start-date",
        help="Start date for historical sync (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        help="End date for historical sync (YYYY-MM-DD)",
    )
    return parser.parse_args()

def get_octopus_consumption():
    args = parse_args()

    url = f"https://api.octopus.energy/v1/gas-meter-points/{OCTOPUS_MPRN}/meters/{OCTOPUS_SERIAL}/consumption/"
    params = {"order_by": "period_start"}

    if args.historical:
        if not args.start_date or not args.end_date:
            print("Error: --historical requires --start-date and --end-date")
            sys.exit(1)
        try:
            start = datetime.strptime(args.start_date, "%Y-%m-%d")
            end = datetime.strptime(args.end_date, "%Y-%m-%d")
        except ValueError:
            print("Error: Dates must be in YYYY-MM-DD format")
            sys.exit(1)
        params["period_from"] = start.isoformat()
        params["period_to"] = end.isoformat()
        print(f"Fetching historical data from {start.date()} to {end.date()}")
    else:
        end = datetime.utcnow()
        start = end - timedelta(days=7)
        params["period_from"] = start.isoformat()
        params["period_to"] = end.isoformat()
        print(f"Fetching recent data from {start.date()} to {end.date()}")

    r = requests.get(url, params=params, auth=(OCTOPUS_API_KEY, ""))
    r.raise_for_status()
    return r.json()["results"]

def push_to_tado(reading):
    # existing logic that posts data to Tado
    pass

def main():
    readings = get_octopus_consumption()
    for r in readings:
        push_to_tado(r)

if __name__ == "__main__":
    main()
