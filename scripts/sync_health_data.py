#!/usr/bin/env python3
"""
Pulls the latest Health Auto Export JSON files from two Google Drive folders —
one for daily health metrics, one for individual workout sessions — extracts
what's useful, and merges it into data.json in this repo. Runs on a schedule
via GitHub Actions.

Auth: service account JSON provided via GDRIVE_SA_KEY env var. BOTH folders
must be shared with the service account's client_email as Viewer — they are
two separate Drive folders (Health Auto Export created a second one with the
same name for the workouts export job), not one shared folder.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from statistics import mean

from google.oauth2 import service_account
from googleapiclient.discovery import build

METRICS_FOLDER_ID = "1OfrLtiSMFNm7z2PFO4bzbPNLyFMkSPAt"   # HealthAutoExport (daily metrics)
WORKOUTS_FOLDER_ID = "1XGde6wlCPZBFtWcKlA3Ks-lOsM2i3T6H"  # HealthAutoExport (workout sessions)
DATA_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")
LOOKBACK_DAYS = 10  # how many days of exported files to pull each run

KM_TO_MI = 0.621371
M_TO_FT = 3.28084

# Map Health Auto Export metric name -> (our internal id, aggregation)
# aggregation: "daily_avg"/"daily_sum" = one entry per day, "point" = each raw sample is its own entry
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


def list_recent_files(service, folder_id, since_iso):
    query = (
        f"'{folder_id}' in parents and "
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
    return date_str.split(" ")[0] if date_str else None


def merge_point(store, metric_id, date, value):
    entries = store.setdefault(metric_id, [])
    for e in entries:
        if e["date"] == date:
            e["value"] = value  # overwrite with latest same-day reading
            return
    entries.append({"date": date, "value": value})


def merge_workout(store, summary):
    entries = store.setdefault("workouts", [])
    for i, e in enumerate(entries):
        if e["id"] == summary["id"]:
            entries[i] = summary
            return
    entries.append(summary)


def qty(field_dict):
    return field_dict.get("qty") if isinstance(field_dict, dict) else None


def extract_workout_summary(w):
    dist_km = qty(w.get("distance"))
    elev_m = qty(w.get("elevationUp"))
    avg_hr = qty(w.get("avgHeartRate"))
    max_hr = qty(w.get("maxHeartRate"))
    calories = qty(w.get("activeEnergyBurned")) or qty(w.get("totalEnergy"))
    duration_s = w.get("duration")
    return {
        "id": w.get("id"),
        "date": parse_date(w.get("start", "")),
        "start": w.get("start"),
        "end": w.get("end"),
        "name": w.get("name"),
        "duration_min": round(duration_s / 60, 1) if duration_s is not None else None,
        "distance_mi": round(dist_km * KM_TO_MI, 2) if dist_km is not None else None,
        "calories": round(calories) if calories is not None else None,
        "avg_hr": round(avg_hr) if avg_hr is not None else None,
        "max_hr": round(max_hr) if max_hr is not None else None,
        "elevation_ft": round(elev_m * M_TO_FT) if elev_m is not None else None,
    }


def load_existing_data():
    if os.path.exists(DATA_JSON_PATH):
        with open(DATA_JSON_PATH) as f:
            return json.load(f)
    return {}


def save_data(store):
    for key in store:
        store[key].sort(key=lambda e: e.get("date") or "")
    with open(DATA_JSON_PATH, "w") as f:
        json.dump(store, f, indent=2)


def sync_metrics(service, since_iso, store):
    files = list_recent_files(service, METRICS_FOLDER_ID, since_iso)
    if not files:
        print("No recent metrics export files found.")
        return
    print(f"Found {len(files)} recent metrics export file(s).")

    daily_raw = {}
    point_raw = {}

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
                q = pt.get("qty")
                date_field = pt.get("date") or pt.get("start")
                if q is None or not date_field:
                    continue
                day = parse_date(date_field)
                if agg == "point":
                    point_raw.setdefault(name, {})[day] = q
                else:
                    daily_raw.setdefault(name, {}).setdefault(day, []).append(q)

    for name, by_day in point_raw.items():
        metric_id, _ = METRIC_MAP[name]
        for day, value in by_day.items():
            merge_point(store, metric_id, day, round(value, 2))

    for name, by_day in daily_raw.items():
        metric_id, agg = METRIC_MAP[name]
        for day, values in by_day.items():
            value = sum(values) if agg == "daily_sum" else mean(values)
            merge_point(store, metric_id, day, round(value, 2))


def sync_workouts(service, since_iso, store):
    files = list_recent_files(service, WORKOUTS_FOLDER_ID, since_iso)
    if not files:
        print("No recent workout export files found.")
        return
    print(f"Found {len(files)} recent workout export file(s).")

    for f in files:
        print(f"  Processing {f['name']}...")
        try:
            content = download_json(service, f["id"])
        except Exception as e:
            print(f"    Failed to download/parse {f['name']}: {e}", file=sys.stderr)
            continue

        workouts = content.get("data", {}).get("workouts", [])
        for w in workouts:
            summary = extract_workout_summary(w)
            if summary["date"] and summary["id"]:
                merge_workout(store, summary)


# Cron fires every hour in UTC (that trigger never needs to change). This is the DST-aware
# filter that decides whether "now" actually falls in the desired Pacific-time sync window —
# zoneinfo resolves America/Los_Angeles against the real IANA tz database, so PDT/PST
# transitions are handled correctly with no manual updates, ever.
SYNC_HOURS_PACIFIC = {7, 9, 11, 13, 15, 17, 19, 21}


def should_run_now():
    pacific_hour = datetime.now(ZoneInfo("America/Los_Angeles")).hour
    return pacific_hour in SYNC_HOURS_PACIFIC


def main():
    if not should_run_now():
        print("Outside the configured Pacific-time sync window — skipping this run.")
        return

    service = get_drive_service()
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()

    store = load_existing_data()
    sync_metrics(service, since, store)
    sync_workouts(service, since, store)
    save_data(store)
    print(f"data.json updated with keys: {sorted(store.keys())}")


if __name__ == "__main__":
    main()
