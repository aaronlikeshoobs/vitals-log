#!/usr/bin/env python3
"""
Pulls the latest Health Auto Export JSON files from a Google Drive folder,
extracts fitness + body-composition metrics, and merges them into data.json
in this repo. Designed to run on a schedule via GitHub Actions.

Auth: service account JSON provided via GDRIVE_SA_KEY env var (the folder
must be shared with the service account's client_email as Viewer).
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from statistics import mean

from google.oauth2 import service_account
from googleapiclient.discovery import build

DRIVE_FOLDER_ID = "1OfrLtiSMFNm7z2PFO4bzbPNLyFMkSPAt"  # HealthAutoExport folder
DATA_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")
LOOKBACK_DAYS = 10  # how many days of exported files to pull each run

# Map Health Auto Export metric name -> (our internal id, aggregation)
# aggregation: "daily_avg" = one avg-per-day entry, "point" = each raw sample is its own entry
METRIC_MAP = {
    "step_count": ("steps", "daily_sum"),
    "walking_running_distance": ("distance", "daily_sum"),
    "active_energy": ("active_energy", "daily_sum"),
    "apple_exercise_time": ("exercise_time", "daily_sum"),
    "flights_climbed": ("flights", "daily_sum"),
    "walking_speed": ("walking_speed", "daily_avg"),
    "resting_heart_rate": ("resting_hr", "daily_avg"),
    "heart_rate_variability": ("hrv", "daily_avg"),
    "vo2_max": ("vo2max", "point"),
    "cardio_recovery": ("cardio_recovery", "point"),
    "six_minute_walking_test_distance": ("six_min_walk", "point"),
    "weight_body_mass": ("weight", "point"),
    "body_mass_index": ("bmi", "point"),
    "body_fat_percentage": ("fat_mass", "point"),
    "lean_body_mass": ("lean_mass", "point"),
    "cycling_distance": ("cyc_distance", "point"),
    "cycling_speed": ("cyc_speed", "point"),
    "cycling_power": ("cyc_power", "point"),
    "cycling_cadence": ("cyc_cadence", "point"),
}


def get_drive_service():
    key_json = os.environ.get("GDRIVE_SA_KEY")
    if not key_json:
        print("ERROR: GDRIVE_SA_KEY env var not set", file=sys.stderr)
        sys.exit(1)
    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def list_recent_files(service, since_iso):
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents and "
        f"mimeType='text/json' and "
        f"modifiedTime > '{since_iso}'"
    )
    files = []
    page_token = None
    while True:
        resp = service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, modifiedTime)",
            pageToken=page_token,
            pageSize=100,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_json(service, file_id):
    raw = service.files().get_media(fileId=file_id).execute()
    return json.loads(raw)


def parse_date(date_str):
    # "2026-07-27 08:00:00 -0700" -> "2026-07-27"
    return date_str.split(" ")[0]


def merge_point(store, metric_id, date, value):
    entries = store.setdefault(metric_id, [])
    for e in entries:
        if e["date"] == date:
            e["value"] = value  # overwrite with latest same-day reading
            return
    entries.append({"date": date, "value": value})


def load_existing_data():
    if os.path.exists(DATA_JSON_PATH):
        with open(DATA_JSON_PATH) as f:
            return json.load(f)
    return {}


def save_data(store):
    for metric_id in store:
        store[metric_id].sort(key=lambda e: e["date"])
    with open(DATA_JSON_PATH, "w") as f:
        json.dump(store, f, indent=2)


def main():
    service = get_drive_service()
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    files = list_recent_files(service, since)

    if not files:
        print("No recent export files found.")
        return

    print(f"Found {len(files)} recent export file(s).")

    # daily_raw[metric_name][date] = list of qty values seen that day
    daily_raw = {}
    point_raw = {}  # metric_name -> {date: value}  (last-write-wins across files)

    for f in files:
        print(f"  Processing {f['name']}...")
        try:
            content = download_json(service, f["id"])
        except Exception as e:
            print(f"    Failed to download/parse {f['name']}: {e}", file=sys.stderr)
            continue

        metrics = content.get("data", {}).get("metrics", [])
        for m in metrics:
            name = m.get("name")
            if name not in METRIC_MAP:
                continue
            _, agg = METRIC_MAP[name]
            for pt in m.get("data", []):
                qty = pt.get("qty")
                date_field = pt.get("date") or pt.get("start")
                if qty is None or not date_field:
                    continue
                day = parse_date(date_field)
                if agg == "point":
                    prev = point_raw.setdefault(name, {})
                    prev[day] = qty  # last one wins (files are processed in listing order)
                else:
                    daily_raw.setdefault(name, {}).setdefault(day, []).append(qty)

    store = load_existing_data()

    # Point-in-time metrics: one entry per day seen
    for name, by_day in point_raw.items():
        metric_id, _ = METRIC_MAP[name]
        for day, value in by_day.items():
            merge_point(store, metric_id, day, round(value, 2))

    # Daily aggregate metrics
    for name, by_day in daily_raw.items():
        metric_id, agg = METRIC_MAP[name]
        for day, values in by_day.items():
            if agg == "daily_sum":
                value = sum(values)
            else:  # daily_avg
                value = mean(values)
            merge_point(store, metric_id, day, round(value, 2))

    save_data(store)
    print(f"data.json updated with metrics: {sorted(store.keys())}")


if __name__ == "__main__":
    main()
