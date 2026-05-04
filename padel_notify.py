#!/usr/bin/env python3
"""
Padel Availability Tracking — notifier.

Runs `padel_poll.py`, diffs the result against the last seen state per
club, and sends a Telegram message if any new slots have appeared.

Design choices and guardrails:
  - Per-club state. If a club's API fetch fails, its previous state is
    preserved (no spurious "everything booked → everything reappeared"
    storms when the API has a blip).
  - First run is silent. We populate state and set first_run_done=True
    without sending anything, so you don't get spammed with the entire
    current availability the moment the cron starts running.
  - Atomic state writes (.tmp + os.replace). A crash mid-write can't
    corrupt padel_seen.json.
  - File lock (fcntl.flock) prevents two cron fires from overlapping.
  - All notifications are plain text. No parse_mode = no HTML escaping
    bugs from prices like "112.5 GBP".

Required env (loaded from ../.env by padel_notifier.sh):
  TELEGRAM_BOT_TOKEN — the bot token
  TELEGRAM_CHAT_ID   — destination chat id (numeric, can be negative for groups)
"""
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT       = Path(__file__).resolve().parent
POLL       = ROOT / "padel_poll.py"
STATE      = ROOT / "padel_seen.json"
LOCK       = ROOT / ".padel.lock"
TIMEOUT    = 15

# Months / weekday formatting for the Telegram body
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES   = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_state():
    if not STATE.exists():
        return {"version": 1, "first_run_done": False, "clubs": {}}
    try:
        with STATE.open() as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARN: state file unreadable, starting fresh: {e}")
        return {"version": 1, "first_run_done": False, "clubs": {}}
    data.setdefault("version", 1)
    data.setdefault("first_run_done", False)
    data.setdefault("clubs", {})
    return data


def save_state(state):
    state["last_updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = STATE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE)


def run_poller():
    """Run the poller as a subprocess. Returns the parsed JSON result, or None on failure."""
    try:
        proc = subprocess.run(
            [sys.executable, str(POLL)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"ERROR: poller invocation failed: {e}")
        return None
    if proc.stderr:
        for line in proc.stderr.strip().splitlines():
            log(f"poll: {line}")
    if proc.returncode != 0:
        log(f"ERROR: poller exit {proc.returncode}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        log(f"ERROR: poller output not JSON: {e}")
        log(f"first 200 chars of stdout: {proc.stdout[:200]!r}")
        return None


def format_slot_line(s):
    # date is "YYYY-MM-DD"
    y, m, d = s["date"].split("-")
    dt = datetime(int(y), int(m), int(d))
    wd = WEEKDAY_NAMES[dt.weekday()]
    mo = MONTH_NAMES[int(m)]
    return f"  • {wd} {int(d)} {mo} {s['start']} ({s['duration']}min) — {s['price']}"


def format_message(new_by_club, errors_by_club):
    total = sum(len(v) for v in new_by_club.values())
    if total == 0:
        return None
    if total == 1:
        header = "🎾 New padel slot"
    else:
        header = f"🎾 {total} new padel slots"
    lines = [header, ""]
    for club_name in sorted(new_by_club):
        slots = new_by_club[club_name]
        if not slots:
            continue
        lines.append(club_name)
        # sort within club by date+start
        slots.sort(key=lambda s: (s["date"], s["start"]))
        for s in slots:
            lines.append(format_slot_line(s))
        lines.append("")
    if errors_by_club:
        lines.append("⚠ Skipped (API errors, kept last known state):")
        for c in sorted(errors_by_club):
            lines.append(f"  • {c}")
    return "\n".join(lines).rstrip()


def send_telegram(message):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlencode({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode())
    except (URLError, HTTPError, TimeoutError, ValueError) as e:
        log(f"ERROR: Telegram send failed: {e}")
        return False
    if not payload.get("ok"):
        log(f"ERROR: Telegram returned not ok: {payload}")
        return False
    return True


def acquire_lock():
    """Open and flock LOCK. Returns the file handle (kept open for run lifetime)."""
    f = open(LOCK, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        log("Another run holds the lock, exiting cleanly.")
        sys.exit(0)
    f.write(f"{os.getpid()}\n")
    f.flush()
    return f


# ---------------------------------------------------------------------------
def main():
    lock_handle = acquire_lock()
    try:
        result = run_poller()
        if result is None:
            log("Poller returned no usable data; preserving state.")
            return

        state = load_state()
        new_by_club = {}
        errors_by_club = {}

        for club_name, club_result in result.get("clubs", {}).items():
            if not club_result.get("ok"):
                errors_by_club[club_name] = club_result.get("error", "unknown")
                # state[club] left untouched
                continue
            # Dedupe within the club: multiple courts can advertise the same
            # (date, start, duration, price) — we want one entry per key.
            unique_by_key = {}
            for s in club_result.get("slots", []):
                unique_by_key.setdefault(s["key"], s)
            current_keys = sorted(unique_by_key)
            previous_keys = set(state["clubs"].get(club_name, []))
            new_slots = [unique_by_key[k] for k in current_keys if k not in previous_keys]
            new_by_club[club_name] = new_slots
            state["clubs"][club_name] = current_keys

        if not state["first_run_done"]:
            log("First run — saving state silently, no notification sent.")
            state["first_run_done"] = True
            save_state(state)
            return

        message = format_message(new_by_club, errors_by_club)
        if message is None:
            # Still update state so 'gone' slots are reflected in the seen set
            save_state(state)
            log("No new slots.")
            return

        ok = send_telegram(message)
        if ok:
            save_state(state)
            total = sum(len(v) for v in new_by_club.values())
            log(f"Sent: {total} new slot(s).")
        else:
            log("Telegram send failed — state NOT updated, will retry next run.")
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
