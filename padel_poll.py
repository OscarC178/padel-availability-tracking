#!/usr/bin/env python3
"""
Padel Availability Tracking — poller.

Queries Playtomic availability for the configured clubs across the next 14
days, filters to weekday evening slots that fit Oscar's window, and prints
the result as JSON to stdout.

Output shape (always, even on partial failure):
    {
      "fetched_at": "2026-05-04T11:00:00Z",
      "clubs": {
        "<club name>": {"ok": true, "slots": [...]},
        "<club name>": {"ok": false, "error": "<msg>"}
      }
    }

Each slot:
    {
      "club": "...",
      "date": "YYYY-MM-DD",
      "start": "HH:MM",
      "duration": 90,
      "price": "112.5 GBP",
      "key": "<club>|<date> <HH:MM>|<duration>|<price>"
    }

The notifier consumes this and decides what's new. Per-club ok flags let
the notifier preserve previous state for clubs that errored, so a transient
API failure never produces a notification storm of "everything booked".
"""
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DAYS_OUT = 14
WEEKDAYS = {0, 1, 2, 3, 4}          # Mon-Fri
EARLIEST_START = time(18, 30)        # slot start time >= 18:30
LATEST_END     = time(22, 0)         # slot end time   <= 22:00
HTTP_TIMEOUT   = 12
USER_AGENT     = "padel-availability-tracker/1.0 (oscar)"

OLD_API = "https://api.playtomic.io/v1/availability"
NEW_API = "https://playtomic.com/api/clubs/availability"

CLUBS = [
    {"name": "Padel Box Bermondsey",
     "tenant_id": "padel-box-bermondsey",
     "api": "old"},
    {"name": "Powerleague Shoreditch",
     "tenant_id": "2ab75436-9bb0-4e9c-9a6f-b12931a9ca4a",
     "api": "new"},
    {"name": "Padel Social Club Earls Court",
     "tenant_id": "1c97a3d1-ded7-4c4b-808e-8c37bb1b2a1f",
     "api": "new"},
]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _http_json(url):
    req = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode())


def _build_url(club, day):
    iso = day.isoformat()
    if club["api"] == "new":
        return NEW_API + "?" + urlencode({
            "tenant_id": club["tenant_id"],
            "date": iso,
            "sport_id": "PADEL",
        })
    return OLD_API + "?" + urlencode({
        "sport_id": "PADEL",
        "tenant_id": club["tenant_id"],
        "start_min": iso + "T00:00:00",
        "start_max": iso + "T23:59:59",
    })


def _parse_slot(raw, club_name, day):
    # The new API returns "start_time" (HH:MM:SS); old API also returns
    # "start_time". Some legacy responses use "start". Be defensive.
    start_str = raw.get("start_time") or raw.get("start") or ""
    if not start_str:
        return None
    try:
        parts = start_str.split(":")
        start_t = time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None
    try:
        duration = int(raw.get("duration") or 0)
    except (ValueError, TypeError):
        return None
    if duration <= 0:
        return None
    price = str(raw.get("price") or "?")
    start_hhmm = start_t.strftime("%H:%M")
    return {
        "club": club_name,
        "date": day.isoformat(),
        "start": start_hhmm,
        "duration": duration,
        "price": price,
        "key": f"{club_name}|{day.isoformat()} {start_hhmm}|{duration}|{price}",
    }


def _in_window(slot):
    sh, sm = (int(x) for x in slot["start"].split(":"))
    start_min = sh * 60 + sm
    earliest = EARLIEST_START.hour * 60 + EARLIEST_START.minute
    latest = LATEST_END.hour * 60 + LATEST_END.minute
    if start_min < earliest:
        return False
    if start_min + slot["duration"] > latest:
        return False
    return True


def _fetch_club(club, today):
    """Fetch all eligible slots across DAYS_OUT for a single club."""
    out = []
    for delta in range(1, DAYS_OUT + 1):
        day = today + timedelta(days=delta)
        if day.weekday() not in WEEKDAYS:
            continue
        url = _build_url(club, day)
        try:
            data = _http_json(url)
        except (URLError, HTTPError, TimeoutError, ValueError) as e:
            # One day failing shouldn't poison the whole club —
            # but the simplest correct thing is to fail the club, so the
            # notifier preserves previous state. We bubble up.
            raise RuntimeError(f"{day.isoformat()}: {e}") from e
        if not isinstance(data, list):
            continue
        for court in data:
            for raw in court.get("slots", []):
                slot = _parse_slot(raw, club["name"], day)
                if slot and _in_window(slot):
                    out.append(slot)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    today = date.today()
    result = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clubs": {},
    }
    for club in CLUBS:
        try:
            slots = _fetch_club(club, today)
            result["clubs"][club["name"]] = {"ok": True, "slots": slots}
        except Exception as e:
            print(f"WARN: {club['name']} fetch failed: {e}", file=sys.stderr)
            result["clubs"][club["name"]] = {"ok": False, "error": str(e)}
    json.dump(result, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
