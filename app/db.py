"""SQLite storage for sites, HAR-derived requests and runs."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "scraper.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    allowed_domains TEXT NOT NULL,          -- JSON list; replay is restricted to these hosts
    contract_ref TEXT NOT NULL,             -- contract number / reference (required)
    contract_expires TEXT,                  -- ISO date; scraping stops after this date
    delay_seconds REAL NOT NULL DEFAULT 1.0,
    schedule_minutes INTEGER,               -- NULL = manual only
    enabled INTEGER NOT NULL DEFAULT 1,
    extra_headers TEXT NOT NULL DEFAULT '{}', -- JSON dict; overrides HAR headers at replay (e.g. fresh Cookie)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    label TEXT,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    headers TEXT NOT NULL,                  -- JSON dict
    body TEXT,
    selected INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                   -- running | ok | partial | failed
    trigger TEXT NOT NULL,                  -- manual | schedule
    ok_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    output_file TEXT,
    message TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # Migrations for databases created by older versions
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sites)")}
        if "extra_headers" not in cols:
            c.execute("ALTER TABLE sites ADD COLUMN extra_headers TEXT NOT NULL DEFAULT '{}'")
        # Runs interrupted by a restart would otherwise show "running" forever
        c.execute("UPDATE runs SET status='failed', finished_at=?, message=? WHERE status='running'",
                  (now(), "اجرا به‌دلیل ری‌استارت سرویس نیمه‌کاره ماند"))


def row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("allowed_domains", "headers", "extra_headers"):
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d
