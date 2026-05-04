#!/bin/bash
# Padel Availability Tracking — cron entry point.
# Loads .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) and runs padel_notify.py.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env not found at $SCRIPT_DIR/.env" >&2
  echo "Copy .env.example to .env and fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID." >&2
  exit 1
fi

# Load env without leaking the values into ps/proc args
set -o allexport
# shellcheck disable=SC1091
. ./.env
set +o allexport

exec /usr/bin/env python3 padel_notify.py
