# Padel Availability Tracker

A small, dependency-free Python tool that polls [Playtomic](https://playtomic.com)
every 15 minutes for new padel slot availability at the clubs you care about,
within the time window you actually play, and pings you on Telegram **only when
something new opens up.**

No LLMs. No frameworks. No databases. Stdlib only. Runs from cron.

---

## The problem

Booking padel courts in London is a contact sport. Slots evaporate within
minutes of release, and prime evening times are particularly competitive.
Refreshing a club's booking page on your phone is the wrong tool for the job —
it's slow, easy to forget, and you miss late cancellations.

What you actually want:

- A passive watcher across **multiple clubs simultaneously**
- That filters to **your specific availability window** (e.g. weekday evenings)
- That tells you **only about new slots** — not the same availability every 15 minutes
- That **survives transient API errors** without spamming you a "everything's gone! everything's back!" notification storm
- And does all this **without burning your Anthropic / OpenAI quota** by stuffing an LLM in the loop

This tool does that. ~250 lines of Python, two files of glue.

---

## How it works

```
                 every 15 min, Mon–Fri 06:00–23:00
                                │
                                ▼
                     ┌──────────────────────┐
                     │   padel_notifier.sh  │  cron entry point
                     │   (sources .env)     │
                     └──────────┬───────────┘
                                │
                                ▼
                     ┌──────────────────────┐
                     │   padel_notify.py    │  acquires file lock,
                     │                      │  loads previous state
                     └──────────┬───────────┘
                                │ subprocess
                                ▼
                     ┌──────────────────────┐
                     │   padel_poll.py      │  fetches Playtomic
                     │                      │  availability per club
                     └──────────┬───────────┘
                                │ JSON
                                ▼
                  ┌──────────────────────────┐
                  │  diff vs padel_seen.json │  per-club state diff
                  └──────────┬───────────────┘
                             │
                  ┌──────────┴──────────┐
                  │                     │
                  ▼                     ▼
            new slots?              nothing new
                  │                     │
                  ▼                     ▼
          api.telegram.org        exit silently
       (one HTTP POST per run)
```

Per-run costs: **two HTTP requests per club per matching weekday** (so for three
clubs over 14 days with ~10 weekdays in window, ~30 requests every 15 min) plus
**one POST to Telegram if there's something new**. No daemons, no long-running
processes, no resource creep.

---

## What it covers (defaults)

These are the defaults the script ships with — edit `padel_poll.py` to adjust.

| Setting | Default | Where to change |
|---|---|---|
| **Clubs** | Padel Box Bermondsey · Powerleague Shoreditch · Padel Social Club Earls Court | `CLUBS` list in `padel_poll.py` |
| **Days out** | 14 | `DAYS_OUT` in `padel_poll.py` |
| **Weekdays** | Mon–Fri | `WEEKDAYS` set in `padel_poll.py` |
| **Earliest start** | 18:30 | `EARLIEST_START` in `padel_poll.py` |
| **Latest end** | 22:00 | `LATEST_END` in `padel_poll.py` |
| **Cron frequency** | every 15 min, 06:00–23:00, Mon–Fri | your crontab |
| **Delivery** | Telegram bot DM | `padel_notify.py::send_telegram` |

The default clubs are London-based — they're there to show the pattern. To use
this for your own clubs, see [Configure for your own clubs](#configure-for-your-own-clubs).

---

## Quick start

You'll need: Python 3 (stdlib only — nothing to `pip install`), a Telegram
account, and 5 minutes.

### 1. Clone and enter the folder

```bash
git clone https://github.com/OscarC178/padel-availability-tracking.git
cd padel-availability-tracking
chmod +x padel_notifier.sh
```

### 2. Create a Telegram bot

1. In Telegram, open a chat with [`@BotFather`](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts (name + username ending in `_bot`)
3. BotFather gives you a token of the form `1234567890:AA...`. Copy it.
4. **Important:** open a chat with your new bot and tap **Start**. The bot
   can't DM you until you've initiated the conversation.

### 3. Find your chat ID

The bot needs to know where to send messages. Easiest way:

```bash
# replace <TOKEN> with the token from step 2
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

Look for `"chat":{"id": <NUMBER>` — that number is your chat ID. If you get
`{"ok":true,"result":[]}`, send any message to your bot in Telegram and try
again.

### 4. Configure

```bash
cp .env.example .env
# edit .env and paste in:
#   TELEGRAM_BOT_TOKEN=<token from step 2>
#   TELEGRAM_CHAT_ID=<chat id from step 3>
```

### 5. Smoke-test

```bash
./padel_notifier.sh
```

Expect: `[YYYY-MM-DD HH:MM:SS] First run — saving state silently, no notification sent.`

This is by design — first run populates the seen-set so you don't get spammed
with every currently-available slot.

### 6. Force a delivery test

```bash
echo '{"version":1,"first_run_done":true,"clubs":{}}' > padel_seen.json
./padel_notifier.sh
```

A Telegram message should arrive within a couple of seconds listing every slot
currently in your window. The next run will be quiet again because state now
matches reality.

### 7. Schedule it

Add to your crontab (`crontab -e`):

```cron
*/15 6-23 * * 1-5 cd /absolute/path/to/padel-availability-tracking && ./padel_notifier.sh >> padel.log 2>&1
```

Then watch:

```bash
tail -f padel.log
```

You'll see one line per run — `No new slots.` most of the time, `Sent: N new slot(s).` when something opens.

### macOS Full Disk Access caveat

On macOS Catalina and later, `cron` itself runs sandboxed and may need **Full
Disk Access** to execute scripts in protected folders (Desktop, Documents,
Downloads). If your scheduled runs silently don't fire:

1. System Settings → Privacy & Security → Full Disk Access
2. Click `+`, press `Cmd+Shift+G`, type `/usr/sbin/cron`, add it
3. Toggle it on, restart cron with `sudo killall cron` (it'll respawn)

Alternatively, move this folder out of `~/Desktop`, `~/Documents`, or
`~/Downloads` to bypass the issue entirely (e.g. `~/code/padel-availability-tracking`).

---

## How dedupe works

This is the part that decides whether you trust the bot. The state file
`padel_seen.json` is **not** a growing log — it's the set of slots currently
visible right now, partitioned by club:

```json
{
  "version": 1,
  "first_run_done": true,
  "last_updated": "2026-05-04T11:05:54Z",
  "clubs": {
    "Padel Box Bermondsey": [
      "Padel Box Bermondsey|2026-05-07 21:00|60|60 GBP"
    ],
    "Powerleague Shoreditch": [],
    "Padel Social Club Earls Court": [
      "Padel Social Club Earls Court|2026-05-06 19:30|90|120 GBP"
    ]
  }
}
```

Each run does, **per club**:

```
new = current_keys - previous_keys   # send only these
state[club] = current_keys           # replace
```

This produces three useful behaviours:

1. **A slot you've been told about will not be told to you again** — no matter
   how long it sits unbooked.
2. **A slot that gets booked and later cancelled re-notifies** — those late
   cancellations are the most grabbable slots, so this is what you want.
3. **API errors don't cause notification storms.** If a single club's API call
   fails, that club's state is preserved untouched — so you don't get a
   spurious "everything's gone, then everything's back!" sequence on transient
   network blips.

The slot identity key is `club|YYYY-MM-DD HH:MM|duration|price`. Same time, same
club, but a price drop counts as a new slot — that's deliberate. Adjust in
`padel_poll.py::_parse_slot` if you'd rather ignore price changes.

---

## Configure for your own clubs

Open `padel_poll.py` and edit the `CLUBS` list:

```python
CLUBS = [
    {"name": "Your Club Name",
     "tenant_id": "club-slug-or-uuid",
     "api": "old"},   # "old" for slug-based clubs, "new" for UUID-based
]
```

To find a club's `tenant_id`:

1. Open the club's Playtomic page in a browser, e.g.
   `https://playtomic.com/clubs/your-club-name`
2. Open the network tab in DevTools and look for an `availability` request.
3. The query string contains `tenant_id=...` — that's what you need. If it's
   a UUID like `1c97a3d1-...`, use `"api": "new"`. If it's a kebab-case slug,
   use `"api": "old"`.

Alternatively, view-source on the club page and grep for `tenant_id` — the
first match in the embedded JSON is the right one.

The window/days/weekday filters are also at the top of `padel_poll.py` — they
should be self-explanatory.

---

## Operations & troubleshooting

### Files

| File | Tracked | Role |
|---|---|---|
| `padel_poll.py` | yes | One-shot poller. Prints JSON to stdout. |
| `padel_notify.py` | yes | Reads poller output, diffs against state, sends Telegram. |
| `padel_notifier.sh` | yes | Cron entry point. Sources `.env`, runs the notifier. |
| `.env.example` | yes | Template — copy to `.env` and fill in. |
| `.env` | **NO** | Real credentials. Gitignored. |
| `padel_seen.json` | **NO** | State file. Auto-created. Holds current set of seen slot keys per club. |
| `.padel.lock` | **NO** | flock target so two cron runs can't overlap. |
| `padel.log` | **NO** | All output from cron runs. Tail this when debugging. |

### Common issues

**"No new slots" forever, even though the club has availability**
The state file thinks it's already told you about everything. Sanity-check by
deleting `padel_seen.json` and watching the next run — it should re-populate
and (after the silent first-run) you'll start getting fresh diffs again.

**`Telegram returned not ok: {...}` in the log**
Bot token is wrong, revoked, or the bot has never been started. Check
BotFather, or open the bot in Telegram and tap Start.

**`poller exit 0` but `0 new slots` forever and the club page shows availability**
Open one of the test API URLs in a browser to see what Playtomic returns:
`https://playtomic.com/api/clubs/availability?tenant_id=<UUID>&date=YYYY-MM-DD&sport_id=PADEL`
If the response is empty, the club hasn't released slots that far out yet.
Some clubs only release ~7-8 days ahead.

**Two parallel runs warning**
Shouldn't happen due to `flock`, but if you see "Another run holds the lock"
repeatedly, something is hanging. Inspect with `lsof .padel.lock`.

---

## What it doesn't do

This is intentionally minimal. If you need any of the following, fork it:

- **Auto-booking.** This is a notifier only. You still tap "Book" yourself.
- **Cross-platform booking aggregation.** Playtomic only. Other platforms
  (Matchi, ClubSpark, etc.) would need their own poller.
- **Web UI.** None. Logs are tailed in a terminal.
- **Multiple users.** One `.env`, one Telegram chat ID. Trivial to fork for
  group use but I haven't.
- **Daily digest mode.** It's strict deltas, not summaries. Easy to add — gate
  the Telegram send behind a time-of-day check in `padel_notify.py`.
- **Price-aware filtering.** All slots in your window are reported regardless
  of price. Add a `MAX_PRICE_GBP` filter in `_in_window` if you want one.

---

## Why no LLM?

Earlier versions of this lived inside a more complex multi-agent setup that
routed every job through a fallback chain of LLM providers. The result: every
15 minutes a deterministic JSON-diff job was being sent through Gemini → MiniMax
fallbacks, timing out, and producing nothing. The right call was to remove the
model from a job that has no judgement step. There's nothing here for a model
to *think* about — fetch JSON, diff against last run, format a string, POST.

If you're tempted to add an LLM somewhere here, ask yourself what *judgement* it
would exercise. If you can't articulate that in one sentence, leave it out.

---

## Licence

MIT. See [LICENSE](LICENSE).
