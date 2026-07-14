# ✈️ Flight Deal Watcher

Watches Google Flights for **business-class round-trip deals** for 2 travelers and
alerts you when a fare drops to **$3,500/person or less** (taxes included):

- Shanghai (PVG) / Hangzhou (HGH) → Los Angeles, up to 1 stop
- **Nonstop PVG → any US gateway**: SFO, SEA, JFK, EWR, ORD, DTW, DFW, and Honolulu
- Nonstop "positioning" routes Tokyo (NRT) / Seoul (ICN) → LAX (parents fly to
  Tokyo/Seoul on a separate cheap ticket)

Skyscanner won't do flexible dates for business class, so this scans a grid of
departure dates (every 7–10 days, up to 6 months out) × trip lengths (2–4 weeks)
twice a day and remembers everything it sees.

## How it runs

Three `launchd` jobs are installed (they run while the Mac is on and you're logged
in; jobs missed during sleep run once on wake):

- **Full scan** daily at 08:00 + 20:00 — the entire date grid (~420 combinations, ~30 min)
- **Pulse scan** every 2 hours + at login — a rotating ~45-query slice of the grid,
  so a flash sale that lasts less than a day still gets spotted within hours
- **Watchdog** daily at 12:00 — banners/emails you if scans have silently stopped
  (broken venv, unloaded schedule) or email alerts are unconfigured

Reliability design: the first qualifying deal in a run is alerted **immediately**
(not when the run ends); deals are only marked "already alerted" after an email
actually sends, so a failed/unconfigured email re-fires every run until one lands;
every query is retried once and isolated so one bad response can't kill a run;
8 consecutive failures aborts with a "degraded" warning email/banner; a lock file
prevents overlapping scans; `caffeinate` keeps the Mac awake mid-scan; and if the
watcher process itself crashes, launchd pops a notification with the fix.

```bash
# manual commands (from this folder)
.venv/bin/python watcher.py scan            # full scan now (add --pulse for a quick slice)
.venv/bin/python watcher.py report          # cheapest fare seen per route, last 7 days
.venv/bin/python watcher.py status          # recent runs + alert history
.venv/bin/python watcher.py healthcheck     # is everything running & configured?
.venv/bin/python watcher.py test-email      # verify email setup
.venv/bin/python watcher.py test-notify     # verify macOS banner
./uninstall_schedule.sh                     # stop scheduled scans
./install_schedule.sh                       # (re)install scheduled scans
```

## Alert channels

1. **Email** — full deal details + Google Flights links (address + Gmail app
   password live in the local `config.toml`, which is never committed; in the
   cloud they come from repo secrets).
2. **iMessage** — the local Mac texts the phone number in `config.toml` via
   Messages.app (free, no accounts; cloud runs use email only).
3. **macOS banner** on the local Mac.
4. (Optional, off by default: SMS via AWS SNS — `[sms]` in config.)

A deal is only marked "handled" after email OR a text actually delivers — if all
fail, it re-fires every run until one gets through. Test with
`watcher.py test-email` / `test-sms` / `test-notify`.

## Cloud copy (GitHub Actions)

The same code runs on GitHub Actions so scans continue when the Mac is closed:
`full-scan.yml` (8:05 AM/PM Pacific) + `pulse-scan.yml` (every 2 h), using
`config.cloud.toml` + repo secrets `EMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`.
The alert-dedup DB rides the Actions cache between runs (worst case on cache
eviction: a duplicate alert, never a missed one). Each scanner (cloud, Mac,
Raspberry Pi — see `PI_SETUP.md`) dedups independently — a deal seen by all
produces one email from each, which is intentional redundancy. If you change
routes or thresholds, edit BOTH `config.toml` (local) and `config.cloud.toml`
(cloud/Pi), then `git push`.

## Tuning (edit `config.toml`, no restart needed)

- **Travel window**: `window_start_days` / `window_end_days` (default: 3 weeks – 6 months out).
- **Trip lengths**: per route `trip_length_days` (default 14/21/28 days). If your parents
  stay longer, add e.g. `42, 56`.
- **Budget**: `max_price_per_person` (set to $3,800: budget is $3,500, but past winning
  deals were often ~10% cheaper on Trip.com/with UnionPay promos than the fare Google
  shows — treat $3,500–3,800 alerts as "check the OTAs").
- **Layovers**: `min_layover_minutes = 90`, `max_layover_minutes = 360`.
- **Routes**: add/remove `[[routes]]` blocks freely (e.g. HND, SEA, SFO, SNA).

## What "deal" means here

Price per person ≤ budget, ≤ 1 stop with a 1.5–6 h layover, whole one-way journey ≤ 26 h.
Each alert shows Google's *typical* price range for that route/date so you can tell a
real deal from a normal price. The same flight won't re-alert unless it drops another $100.

## Quirks worth knowing

- Some city pairs (e.g. PVG→SAN) can't be priced as one round-trip ticket by Google
  (no single carrier publishes a through fare); for those the watcher prices **two
  one-way tickets** and alerts on the sum (nonstop-watch routes skip this fallback —
  `one_way_fallback = false`).
- Round-trip alerts link to the *outbound* date on Google Flights; you pick the return
  there (the shown price is the "from" price for that outbound).
- The NRT/ICN routes are nonstop-only fares like the Tokyo→Seattle deal that inspired
  this: your parents would book a separate cheap flight to Tokyo/Seoul first. Alerts
  mark those with a note.
- Data comes from scraping Google Flights (via `fast-flights`), with a custom parser
  in `flightsearch.py` because the library's own parser crashes on business-class
  results that have unpriced itineraries. If Google changes their page format,
  scans will log `no flight data in page` — that means this needs updating.
- Always **verify the price on Google Flights before booking** — deals can vanish in hours.

## Where the app lives

The real folder is `~/FlightWatcher`; the Desktop folder ("Fun Projects/Airplain
Ticket Watcher") is a symlink to it. Don't move it back onto the Desktop — macOS
blocks scheduled background jobs from running inside Desktop/Documents/Downloads
(that's why it was moved), and the schedule would silently die.

## Files

- `watcher.py` — CLI + scan loop, deal rules, alert dedup
- `flightsearch.py` — Google Flights query + tolerant payload parser
- `alerts.py` — email formatting/sending + macOS notifications
- `store.py` — SQLite history (`data/watcher.db`)
- `logs/watcher.log` — every scan; `logs/deals.log` — every deal ever alerted
