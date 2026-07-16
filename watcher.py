#!/usr/bin/env python3
"""Flight Deal Watcher — scans Google Flights for business-class round-trip deals.

Usage:
  python3 watcher.py scan [--pulse] [--limit N] [--route PVG-LAX]
      Full scan of the whole date grid, or --pulse: a fast rotating slice of it.
      Alerts the moment the first qualifying deal is found.
  python3 watcher.py report        cheapest fare seen per route (last 7 days)
  python3 watcher.py status        recent runs and alert history
  python3 watcher.py healthcheck   warn (banner+email) if scans have stopped running
  python3 watcher.py test-email    send a test email
  python3 watcher.py test-sms      send a test text message
  python3 watcher.py test-notify   pop a test macOS notification
"""

import argparse
import fcntl
import logging
import random
import sys
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import alerts
from flightsearch import SearchError, search_round_trip
from store import Store

log = logging.getLogger("watcher")

MAX_CONSECUTIVE_FAILURES = 8   # abort the run if Google keeps failing (likely blocked)
PULSE_TARGET_QUERIES = 45      # approximate size of a --pulse slice


def setup_logging():
    logdir = BASE / "logs"
    logdir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(logdir / "watcher.log", maxBytes=2_000_000, backupCount=3)
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    for noisy in ("fast_flights", "primp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config() -> dict:
    with open(BASE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    # cloud runners inject secrets via env instead of writing them into the file
    import os
    if os.getenv("FDW_EMAIL"):
        cfg.setdefault("email", {})
        cfg["email"]["to"] = cfg["email"]["username"] = os.environ["FDW_EMAIL"]
    if os.getenv("FDW_APP_PASSWORD"):
        cfg["email"]["app_password"] = os.environ["FDW_APP_PASSWORD"]
    return cfg


def email_ready(cfg) -> bool:
    e = cfg.get("email", {})
    return bool(e.get("enabled")) and bool((e.get("app_password") or "").strip())


def _dep_excluded(dep: date, ranges) -> bool:
    """ranges: [["06-01", "09-10"], ...] month-day windows (year-agnostic).
    A range with start > end wraps around New Year (e.g. ["10-16", "04-14"])."""
    md = dep.strftime("%m-%d")
    for a, b in ranges:
        if a <= b:
            if a <= md <= b:
                return True
        elif md >= a or md <= b:
            return True
    return False


def build_queries(cfg: dict) -> list[dict]:
    """Expand routes x departure dates x trip lengths into a query list,
    round-robin interleaved across routes so an aborted run still covers every route."""
    s = cfg["search"]
    today = date.today()
    start = today + timedelta(days=s["window_start_days"])
    end = today + timedelta(days=s["window_end_days"])

    per_route = []
    for route in cfg["routes"]:
        step = route.get("departure_step_days", 7)
        lengths = route.get("trip_length_days", [14, 21])
        excl = route.get("exclude_dep_ranges", [])
        # routes with narrow seasonal windows can look further out than the default
        end = today + timedelta(days=route.get("window_end_days", s["window_end_days"]))
        qs = []
        dep = start
        while dep <= end:
            if not _dep_excluded(dep, excl):
                for length in lengths:
                    qs.append({
                        "origin": route["origin"],
                        "destination": route["destination"],
                        "dep_date": dep.isoformat(),
                        "ret_date": (dep + timedelta(days=length)).isoformat(),
                        "max_stops": route.get("max_stops", cfg["deal"]["max_stops"]),
                        "ow_fallback": route.get("one_way_fallback", True),
                        "alert_below": route.get("alert_below", cfg["deal"]["max_price_per_person"]),
                        "seat": route.get("seat", s.get("seat", "business")),
                        "note": route.get("note", ""),
                    })
            dep += timedelta(days=step)
        per_route.append(qs)

    # open-jaw watches (e.g. into LAX, home from HNL) - priced as two one-way tickets
    for route in cfg.get("openjaw_routes", []):
        (o1, d1), (o2, d2) = route["legs"]
        step = route.get("departure_step_days", 14)
        gaps = route.get("gap_days", [14, 28])
        excl = route.get("exclude_dep_ranges", [])
        end = today + timedelta(days=route.get("window_end_days", s["window_end_days"]))
        qs = []
        dep = start
        while dep <= end:
            if _dep_excluded(dep, excl):
                dep += timedelta(days=step)
                continue
            for gap in gaps:
                qs.append({
                    "type": "openjaw",
                    "origin": o1,
                    # out to d1; "~X" = return departs X (or lands at X when the
                    # return leaves from the same city we flew into)
                    "destination": f"{d1}~{d2 if o2 == d1 else o2}",
                    "leg2": [o2, d2],
                    "dep_date": dep.isoformat(),
                    "ret_date": (dep + timedelta(days=gap)).isoformat(),
                    "max_stops": route.get("max_stops", cfg["deal"]["max_stops"]),
                    "ow_fallback": False,
                    "alert_below": route.get("alert_below", cfg["deal"]["max_price_per_person"]),
                    # mixed-cabin open-jaws: seats = [outbound_cabin, return_cabin]
                    "seat": (route.get("seats") or [route.get("seat", s.get("seat", "business"))] * 2)[0],
                    "seat2": (route.get("seats") or [route.get("seat", s.get("seat", "business"))] * 2)[1],
                    "note": route.get("note", "open-jaw"),
                })
            dep += timedelta(days=step)
        per_route.append(qs)

    interleaved = []
    while any(per_route):
        for qs in per_route:
            if qs:
                interleaved.append(qs.pop(0))

    cap = s.get("max_queries_per_run", 500)
    if len(interleaved) > cap:
        log.warning("query grid has %d entries, capping at %d (raise max_queries_per_run or "
                    "increase departure_step_days to cover everything)", len(interleaved), cap)
        interleaved = interleaved[:cap]
    return interleaved


def itinerary_ok(it, deal_cfg) -> bool:
    """Constraint check (stops / layover time / duration) for one direction of travel."""
    if it.stops > deal_cfg["max_stops"]:
        return False
    for lay in it.layovers_min:
        if not (deal_cfg["min_layover_minutes"] <= lay <= deal_cfg["max_layover_minutes"]):
            return False
    return it.total_duration_min <= deal_cfg["max_total_hours"] * 60


def qualify(it, deal_cfg, adults, alert_below=None) -> dict | None:
    """Check a round-trip-priced itinerary against the deal rules; return a deal dict or None."""
    price_pp = round(it.price_total / adults)
    if price_pp > (alert_below or deal_cfg["max_price_per_person"]) or not itinerary_ok(it, deal_cfg):
        return None
    return {
        "price_pp": price_pp,
        "airlines": it.airlines,
        "stops": it.stops,
        "stop_airport": it.legs[0].to_code if it.stops else "",
        "layover_min": it.layovers_min[0] if it.layovers_min else 0,
        "total_duration_min": it.total_duration_min,
    }


def combine_one_ways(q, res_out, res_ret, deal_cfg, adults) -> dict | None:
    """For routes Google won't price as a round trip: cheapest valid outbound +
    cheapest valid return, bought as two one-way tickets."""
    out_ok = [it for it in res_out.itineraries if itinerary_ok(it, deal_cfg)]
    ret_ok = [it for it in res_ret.itineraries if itinerary_ok(it, deal_cfg)]
    if not out_ok or not ret_ok:
        return None
    o = min(out_ok, key=lambda x: x.price_total)
    r = min(ret_ok, key=lambda x: x.price_total)
    price_pp = round((o.price_total + r.price_total) / adults)
    ret_stop = f" via {r.legs[0].to_code}" if r.stops else " nonstop"
    return {
        "price_pp": price_pp,
        "airlines": sorted(set(o.airlines) | set(r.airlines)),
        "stops": o.stops,
        "stop_airport": o.legs[0].to_code if o.stops else "",
        "layover_min": o.layovers_min[0] if o.layovers_min else 0,
        "total_duration_min": o.total_duration_min,
        "origin": q["origin"], "destination": q["destination"],
        "dep_date": q["dep_date"], "ret_date": q["ret_date"],
        "typical_low_pp": None, "typical_high_pp": None,
        "url": res_out.url, "url_ret": res_ret.url,
        "note": (q["note"] + "; " if q["note"] else "")
                + f"two one-way tickets; return {'/'.join(r.airlines)}{ret_stop}",
        "cabin": (q.get("seat", "business") if q.get("seat2", q.get("seat")) == q.get("seat")
                  else f"{q.get('seat')} out + {q.get('seat2')} home"),
        # dates deliberately NOT in the key: the same flight combo on other dates
        # is the same deal - alert once, re-alert only on a real price drop
        "key": f"{q['origin']}-{q['destination']}|{q.get('seat', 'business')}+{q.get('seat2', q.get('seat', 'business'))}|"
               f"OW:{'/'.join(sorted(o.airlines))}+{'/'.join(sorted(r.airlines))}",
    }


def dispatch_alerts(cfg, store, deals: list[dict], adults: int) -> int:
    """Email + banner for a batch of deals. Deals are marked as alerted ONLY when
    the email was actually sent (or email is deliberately disabled) — an
    undelivered deal stays fresh and re-fires next run, so nothing is ever
    silently swallowed. Returns number of deals durably delivered."""
    deals = sorted(deals, key=lambda x: x["price_pp"])
    for d in deals:
        log.info("DEAL: %s", alerts.deal_line(d))
    with open(BASE / "logs" / "deals.log", "a") as f:
        for d in deals:
            f.write(f"{date.today()} {alerts.deal_line(d)}\n    {d['url']}\n")

    emailed = False
    try:
        subject, text, html = alerts.build_email(deals, adults)
        emailed = alerts.send_email(cfg, subject, text, html)
    except Exception as e:
        log.error("email failed: %s", e)
        alerts.macos_notify("⚠️ Flight Deal Watcher",
                            "Deal found but EMAIL FAILED - check config/logs! Deal is in logs/deals.log")

    best = deals[0]
    phoned = alerts.phone_ping(
        cfg,
        f"✈️ Biz-class deal: {best['origin']}>{best['destination']} ${best['price_pp']:,}/pp RT "
        f"{best['dep_date']} to {best['ret_date']}, {'/'.join(best['airlines'])}, "
        f"{'nonstop' if best['stops'] == 0 else '1 stop'}"
        + (f" (+{len(deals) - 1} more)" if len(deals) > 1 else "")
        + ". Links in email.")

    if cfg.get("notify", {}).get("macos", True):
        alerts.macos_notify(
            "✈️ Business-class deal found!",
            f"{best['origin']}→{best['destination']} ${best['price_pp']:,}/person RT "
            f"{best['dep_date']} ({len(deals)} deal{'s' if len(deals) > 1 else ''})")
        if cfg.get("email", {}).get("enabled") and not email_ready(cfg):
            alerts.macos_notify(
                "⚠️ Email alerts are OFF",
                "Add your Gmail app password to config.toml or you'll only see deals at this Mac!")

    wants_email = cfg.get("email", {}).get("enabled", False)
    delivered = emailed or phoned or not (wants_email or alerts.phone_wanted(cfg))
    if delivered:
        for d in deals:
            store.mark_alerted(d["key"], d["price_pp"])
        return len(deals)
    log.warning("deals NOT marked as alerted (no email/SMS delivered) - they will re-fire next run")
    return 0


def acquire_lock():
    """One scan at a time; a pulse that collides with a running full scan just skips."""
    lock = open(BASE / "data" / "scan.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        return None


def cmd_scan(args):
    cfg = load_config()
    s, deal_cfg = cfg["search"], cfg["deal"]
    adults = s.get("adults", 2)
    (BASE / "data").mkdir(exist_ok=True)
    lock = acquire_lock()
    if lock is None:
        log.info("another scan is already running - skipping this one")
        return
    store = Store(BASE / "data" / "watcher.db")

    queries = build_queries(cfg)
    if args.route:
        o, _, d = args.route.partition("-")
        queries = [q for q in queries if q["origin"] == o.upper() and q["destination"] == d.upper()]
    if args.pulse:
        stride = max(1, round(len(queries) / PULSE_TARGET_QUERIES))
        offset = int(time.time() // 7200) % stride  # rotates which slice each pulse covers
        queries = queries[offset::stride]
    if args.limit:
        queries = queries[: args.limit]

    run_id = store.start_run()
    log.info("run %d (%s): %d date combinations, %d adults, %s class",
             run_id, "pulse" if args.pulse else "full", len(queries), adults, s.get("seat", "business"))

    pending, dispatched = [], 0
    dispatched_keys: set[str] = set()
    attempted = failures = consecutive_failures = 0
    immediate_sent = False
    dmin, dmax = s.get("delay_seconds", [1.5, 4.0])

    for i, q in enumerate(queries):
        if i:
            time.sleep(random.uniform(dmin, dmax))
        label = f"{q['origin']}->{q['destination']} {q['dep_date']}/{q['ret_date']}"
        attempted += 1

        res, query_deals = None, []
        if q.get("type") == "openjaw":
            # Google won't price open-jaws in the page we scrape; watch as 2 one-ways
            try:
                first_dest = q["destination"].split("~")[0]
                o2, d2 = q["leg2"]
                res_out = search_round_trip(
                    q["origin"], first_dest, q["dep_date"], None,
                    adults=adults, seat=q["seat"],
                    currency=s.get("currency", "USD"), max_stops=q["max_stops"])
                time.sleep(random.uniform(dmin, dmax))
                res_ret = search_round_trip(
                    o2, d2, q["ret_date"], None,
                    adults=adults, seat=q.get("seat2", q["seat"]),
                    currency=s.get("currency", "USD"), max_stops=q["max_stops"])
                consecutive_failures = 0
                if res_out.itineraries and res_ret.itineraries:
                    cheapest_sum = round((min(i.price_total for i in res_out.itineraries)
                                          + min(i.price_total for i in res_ret.itineraries)) / adults)
                    store.record_observation(
                        run_id, q["origin"], q["destination"], q["dep_date"], q["ret_date"],
                        "open-jaw combo", None, cheapest_sum, None, None, res_out.url)
                    log.info("%s: open-jaw as 2 one-ways, cheapest sum $%s/pp", label, f"{cheapest_sum:,}")
                    d = combine_one_ways(q, res_out, res_ret, deal_cfg, adults)
                    if d and d["price_pp"] <= q["alert_below"]:
                        query_deals = [d]
                else:
                    log.info("%s: open-jaw legs unavailable", label)
            except SearchError as e:
                failures += 1
                consecutive_failures += 1
                log.warning("%s (open-jaw): %s", label, e)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("%d consecutive failures - aborting run early", consecutive_failures)
                    break
            except Exception:
                failures += 1
                log.exception("%s: unexpected error (open-jaw)", label)
            fresh_now = [d for d in query_deals
                         if store.should_alert(d["key"], d["price_pp"], deal_cfg["re_alert_drop"],
                                       repeat_hours=24 if d.get("golden") else None)]
            if fresh_now:
                pending.extend(fresh_now)
                if not immediate_sent:
                    dispatched += dispatch_alerts(cfg, store, fresh_now, adults)
                    dispatched_keys.update(d["key"] for d in fresh_now)
                    immediate_sent = True
            continue
        if q.get("type") != "openjaw":
          for attempt in (1, 2):  # one retry per query so a transient blip doesn't cost a date pair
            try:
                res = search_round_trip(
                    q["origin"], q["destination"], q["dep_date"], q["ret_date"],
                    adults=adults, seat=q["seat"],
                    currency=s.get("currency", "USD"), max_stops=q["max_stops"],
                )
                consecutive_failures = 0
                break
            except SearchError as e:
                if attempt == 1:
                    time.sleep(random.uniform(4, 8))
                    continue
                failures += 1
                consecutive_failures += 1
                log.warning("%s: %s", label, e)
            except Exception:
                failures += 1
                consecutive_failures += 1
                log.exception("%s: unexpected error", label)
                break
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            log.error("%d consecutive failures - Google may be rate-limiting; aborting run early",
                      consecutive_failures)
            break
        if res is None:
            continue

        try:
            if res.itineraries:
                cheapest = min(res.itineraries, key=lambda x: x.price_total)
                t_low = round(res.typical_low / adults) if res.typical_low else None
                t_high = round(res.typical_high / adults) if res.typical_high else None
                store.record_observation(
                    run_id, q["origin"], q["destination"], q["dep_date"], q["ret_date"],
                    ",".join(cheapest.airlines), cheapest.stops,
                    round(cheapest.price_total / adults), t_low, t_high, res.url)
                log.info("%s: %d options, cheapest $%s/pp (%s)", label, len(res.itineraries),
                         f"{round(cheapest.price_total / adults):,}", ",".join(cheapest.airlines))

                best_per_airline = {}
                for it in res.itineraries:
                    d = qualify(it, deal_cfg, adults, q["alert_below"])
                    if d is None:
                        continue
                    akey = "/".join(sorted(it.airlines))
                    if akey in best_per_airline and best_per_airline[akey]["price_pp"] <= d["price_pp"]:
                        continue
                    d.update({
                        "origin": q["origin"], "destination": q["destination"],
                        "dep_date": q["dep_date"], "ret_date": q["ret_date"],
                        "typical_low_pp": t_low, "typical_high_pp": t_high,
                        "url": res.url, "note": q["note"],
                        "cabin": q["seat"],
                        # same flight on other dates = same deal (no dates in key)
                        "key": f"{q['origin']}-{q['destination']}|{q['seat']}|{akey}",
                        # the golden deal - Shanghai-LA nonstop - may repeat daily
                        "golden": q["origin"] == "PVG" and q["destination"] == "LAX" and d["stops"] == 0,
                    })
                    best_per_airline[akey] = d
                query_deals = list(best_per_airline.values())
            elif s.get("one_way_fallback", True) and q["ow_fallback"]:
                # Google prices some pairs only asynchronously, never in the round-trip
                # page we scrape - fall back to two one-way searches.
                time.sleep(random.uniform(dmin, dmax))
                res_out = search_round_trip(
                    q["origin"], q["destination"], q["dep_date"], None,
                    adults=adults, seat=q["seat"],
                    currency=s.get("currency", "USD"), max_stops=q["max_stops"])
                time.sleep(random.uniform(dmin, dmax))
                res_ret = search_round_trip(
                    q["destination"], q["origin"], q["ret_date"], None,
                    adults=adults, seat=q["seat"],
                    currency=s.get("currency", "USD"), max_stops=q["max_stops"])
                if res_out.itineraries and res_ret.itineraries:
                    cheapest_sum = round((min(i.price_total for i in res_out.itineraries)
                                          + min(i.price_total for i in res_ret.itineraries)) / adults)
                    store.record_observation(
                        run_id, q["origin"], q["destination"], q["dep_date"], q["ret_date"],
                        "one-way combo", None, cheapest_sum, None, None, res_out.url)
                    log.info("%s: priced as 2 one-ways, cheapest sum $%s/pp", label, f"{cheapest_sum:,}")
                    d = combine_one_ways(q, res_out, res_ret, deal_cfg, adults)
                    if d and d["price_pp"] <= q["alert_below"]:
                        query_deals = [d]
                else:
                    log.info("%s: no flights (even as one-ways)", label)
            else:
                log.info("%s: no flights", label)
        except SearchError as e:
            failures += 1
            log.warning("%s (fallback): %s", label, e)
        except Exception:
            failures += 1
            log.exception("%s: unexpected error while processing results", label)

        fresh_now = [d for d in query_deals
                     if store.should_alert(d["key"], d["price_pp"], deal_cfg["re_alert_drop"],
                                       repeat_hours=24 if d.get("golden") else None)]
        if fresh_now:
            pending.extend(fresh_now)
            if not immediate_sent:
                # fire the very first deal right away - never wait for a long run to finish
                dispatched += dispatch_alerts(cfg, store, fresh_now, adults)
                dispatched_keys.update(d["key"] for d in fresh_now)
                immediate_sent = True

    # end-of-run digest for everything still fresh; deals whose delivery failed
    # are excluded here and simply re-fire on the next run
    remaining = [d for d in pending
                 if d["key"] not in dispatched_keys
                 and store.should_alert(d["key"], d["price_pp"], deal_cfg["re_alert_drop"],
                                       repeat_hours=24 if d.get("golden") else None)]
    if remaining:
        dispatched += dispatch_alerts(cfg, store, remaining, adults)
    elif not pending:
        log.info("no new deals this run")

    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES or (attempted >= 20 and failures / attempted > 0.3):
        msg = (f"Scan degraded: {failures}/{attempted} queries failed. Google may be "
               f"rate-limiting or the page format changed. Check logs/watcher.log.")
        log.error(msg)
        alerts.macos_notify("⚠️ Flight Deal Watcher degraded", msg)
        alerts.phone_ping(cfg, "⚠️ Flight Deal Watcher degraded: scans failing, check the Mac. " + msg[:100])
        try:
            alerts.send_plain(cfg, "⚠️ Flight Deal Watcher is degraded", msg)
        except Exception as e:
            log.warning("could not email degraded warning: %s", e)

    store.finish_run(run_id, attempted, failures, len(pending), dispatched)
    log.info("run %d done: %d queries, %d failures, %d fresh deals, %d delivered",
             run_id, attempted, failures, len(pending), dispatched)


def cmd_healthcheck(_args):
    """Meant for a daily watchdog job: complain loudly if scanning has stopped."""
    cfg = load_config()
    stale_hours = cfg.get("notify", {}).get("stale_hours", 30)
    problems = []

    runs = Store(BASE / "data" / "watcher.db").recent_runs(1)
    if not runs:
        problems.append("No scans have ever completed.")
    else:
        last = runs[0][2] or runs[0][1]  # finished_at, else started_at
        age_h = (datetime.now(timezone.utc)
                 - datetime.strptime(last, "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc)
                 ).total_seconds() / 3600
        if age_h > stale_hours:
            problems.append(f"Last scan was {age_h:.0f}h ago (threshold {stale_hours}h). "
                            "The schedule may be broken - try ./install_schedule.sh again.")

    if cfg.get("email", {}).get("enabled") and not email_ready(cfg):
        problems.append("Email alerts are not configured (no Gmail app password in config.toml) - "
                        "deals only show as Mac banners right now.")

    if problems:
        msg = " | ".join(problems)
        log.warning("healthcheck: %s", msg)
        alerts.macos_notify("⚠️ Flight Deal Watcher needs attention", msg[:230])
        alerts.phone_ping(cfg, "⚠️ Flight Deal Watcher needs attention: " + msg[:140])
        try:
            alerts.send_plain(cfg, "⚠️ Flight Deal Watcher needs attention", msg)
        except Exception as e:
            log.warning("could not email healthcheck warning: %s", e)
        print("PROBLEMS:", msg)
    else:
        log.info("healthcheck: OK")
        print("OK - last scan recent, alerting configured.")


def cmd_report(_args):
    store = Store(BASE / "data" / "watcher.db")
    rows = store.cheapest_by_route()
    if not rows:
        print("No observations yet - run: python3 watcher.py scan")
        return
    print(f"{'route':<12}{'dates':<26}{'$/person':>9}  {'typical':>13}  airline (cheapest date sampled)")
    for o, d, dep, ret, airlines, stops, price, tl, th, _url in rows:
        typical = f"${tl:,}-${th:,}" if tl else "-"
        if stops is None:
            stops_txt = "2 one-ways"
        else:
            stops_txt = "nonstop" if stops == 0 else f"{stops} stop"
        print(f"{o+'-'+d:<12}{dep + ' / ' + ret:<26}{'$' + format(price, ','):>9}  {typical:>13}  "
              f"{airlines} ({stops_txt})")


def cmd_status(_args):
    store = Store(BASE / "data" / "watcher.db")
    print("Recent runs:")
    for rid, start, fin, q, f, deals, sent in store.recent_runs():
        print(f"  #{rid}  {start} -> {fin or 'running/aborted'}  queries={q} failures={f} "
              f"fresh_deals={deals} delivered={sent}")
    print("\nRecent alerts:")
    rows = store.recent_alerts()
    if not rows:
        print("  (none yet)")
    for key, price, at, times in rows:
        print(f"  {at}  ${price:,}/pp  {key}  (alerted {times}x)")


def cmd_test_email(_args):
    cfg = load_config()
    fake = [{
        "origin": "PVG", "destination": "LAX", "dep_date": "2026-10-12", "ret_date": "2026-11-02",
        "price_pp": 2999, "airlines": ["Test Airline"], "stops": 1, "stop_airport": "ICN",
        "layover_min": 150, "total_duration_min": 1020, "typical_low_pp": 4200, "typical_high_pp": 5600,
        "url": "https://www.google.com/travel/flights", "note": "this is a test alert",
    }]
    subject, text, html = alerts.build_email(fake, cfg["search"].get("adults", 2))
    if alerts.send_email(cfg, "[TEST] " + subject, text, html):
        print("Test email sent - check your inbox (and spam folder the first time).")
    else:
        print("Email is not configured yet. Put your Gmail app password in config.toml under [email].")


def cmd_heartbeat(_args):
    """Weekly proof-of-life email (scheduled on the Pi): silence must never be
    mistaken for health."""
    cfg = load_config()
    store = Store(BASE / "data" / "watcher.db")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%SZ")
    week = [r for r in store.recent_runs(400) if r[1] and r[1] >= cutoff]
    completed = sum(1 for r in week if r[2])
    failures = sum(r[4] or 0 for r in week)
    delivered = sum(r[6] or 0 for r in week)
    text = (f"Watcher alive on the Pi: {completed} scans completed in the last 7 days, "
            f"{failures} query failures, {delivered} deal alerts delivered. "
            f"If this weekly email ever stops arriving, the Pi needs attention.")
    try:
        alerts.send_plain(cfg, "✅ Flight watcher weekly heartbeat", text)
    except Exception as e:
        log.error("heartbeat email failed: %s", e)
    print(text)


def cmd_promos(_args):
    """Check deal-blog feeds for new premium-cabin promo announcements."""
    import os
    cfg = load_config()
    if not (cfg.get("promos", {}).get("enabled") or os.getenv("FDW_PROMOS") == "1"):
        log.info("promo watch disabled on this machine")
        return
    from promos import check_promos
    fits, others = check_promos(cfg, BASE)
    # everything matched gets remembered (logged), only criteria-fits get emailed
    if fits or others:
        with open(BASE / "logs" / "promos.log", "a") as f:
            from datetime import date
            for h in fits + others:
                price = f"${h['best_price_usd']:,}" if h.get("best_price_usd") else "no price parsed"
                f.write(f"{date.today()} [{'FIT' if h in fits else 'seen'}] {price}  {h['title']}\n"
                        f"    {h['link']}\n")
    if not fits:
        log.info("promo check: %d matched posts logged, none fit the criteria - no email", len(others))
        return
    log.info("PROMO FIT: %d post(s) meet criteria", len(fits))
    text = "Promo announcement(s) matching your route + cabin + budget:\n\n" + "\n\n".join(
        f"• {h['title']}\n  best price seen: ${h['best_price_usd']:,}/person\n  {h['link']}" for h in fits)
    try:
        alerts.send_plain(cfg, f"🎯 Promo fits your criteria: {fits[0]['title'][:80]}", text)
    except Exception as e:
        log.error("promo email failed: %s", e)
    alerts.macos_notify("🎯 Promo fits your criteria!", fits[0]["title"][:200])


def cmd_test_notify(_args):
    alerts.macos_notify("✈️ Flight Deal Watcher", "Test notification - alerts are working!")
    print("Notification sent (check the banner / Notification Center).")


def cmd_test_sms(_args):
    if alerts.phone_ping(load_config(), "✈️ Flight Deal Watcher: text test - phone alerts are working!"):
        print("Test text sent - check your phone.")
    else:
        print("No text channel worked (SMS and iMessage both disabled or failing) - see log above.")


def main():
    setup_logging()
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--pulse", action="store_true")
    scan.add_argument("--limit", type=int)
    scan.add_argument("--route")
    for name in ("report", "status", "healthcheck", "promos", "heartbeat",
                 "test-email", "test-notify", "test-sms"):
        sub.add_parser(name)
    args = p.parse_args()
    {"scan": cmd_scan, "report": cmd_report, "status": cmd_status, "healthcheck": cmd_healthcheck,
     "promos": cmd_promos, "heartbeat": cmd_heartbeat, "test-email": cmd_test_email,
     "test-notify": cmd_test_notify, "test-sms": cmd_test_sms}[args.cmd](args)


if __name__ == "__main__":
    main()
