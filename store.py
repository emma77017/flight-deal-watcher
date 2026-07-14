"""SQLite persistence: scan history, price observations, and alert dedup."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    queries INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    deals_found INTEGER DEFAULT 0, alerts_sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, origin TEXT, destination TEXT,
    dep_date TEXT, ret_date TEXT, airlines TEXT, stops INTEGER,
    price_pp INTEGER, typical_low_pp INTEGER, typical_high_pp INTEGER,
    url TEXT, seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_route ON observations(origin, destination, seen_at);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    last_price_pp INTEGER, last_alerted_at TEXT, times INTEGER DEFAULT 1
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs(started_at) VALUES (?)", (_now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id, queries, failures, deals_found, alerts_sent):
        self.db.execute(
            "UPDATE runs SET finished_at=?, queries=?, failures=?, deals_found=?, alerts_sent=? WHERE id=?",
            (_now(), queries, failures, deals_found, alerts_sent, run_id),
        )
        self.db.commit()

    def record_observation(self, run_id, origin, destination, dep_date, ret_date,
                           airlines, stops, price_pp, typical_low_pp, typical_high_pp, url):
        self.db.execute(
            "INSERT INTO observations(run_id, origin, destination, dep_date, ret_date, airlines,"
            " stops, price_pp, typical_low_pp, typical_high_pp, url, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, origin, destination, dep_date, ret_date, airlines, stops,
             price_pp, typical_low_pp, typical_high_pp, url, _now()),
        )
        self.db.commit()

    def should_alert(self, key: str, price_pp: int, re_alert_drop: int) -> bool:
        row = self.db.execute("SELECT last_price_pp FROM alerts WHERE key=?", (key,)).fetchone()
        if row is None:
            return True
        return price_pp <= row[0] - re_alert_drop

    def mark_alerted(self, key: str, price_pp: int):
        self.db.execute(
            "INSERT INTO alerts(key, last_price_pp, last_alerted_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET last_price_pp=excluded.last_price_pp,"
            " last_alerted_at=excluded.last_alerted_at, times=times+1",
            (key, price_pp, _now()),
        )
        self.db.commit()

    def recent_runs(self, n=10):
        return self.db.execute(
            "SELECT id, started_at, finished_at, queries, failures, deals_found, alerts_sent"
            " FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def cheapest_by_route(self, days=7):
        """Cheapest observation per route seen in the last `days` days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%SZ")
        return self.db.execute(
            "SELECT origin, destination, dep_date, ret_date, airlines, stops, MIN(price_pp),"
            " typical_low_pp, typical_high_pp, url"
            " FROM observations WHERE seen_at >= ? AND price_pp IS NOT NULL"
            " GROUP BY origin, destination ORDER BY MIN(price_pp)", (cutoff,)).fetchall()

    def recent_alerts(self, n=20):
        return self.db.execute(
            "SELECT key, last_price_pp, last_alerted_at, times FROM alerts"
            " ORDER BY last_alerted_at DESC LIMIT ?", (n,)).fetchall()
