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
CREATE TABLE IF NOT EXISTS categories (     -- a section of a site, e.g. doctors / clinics / pharmacies
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    max_pages INTEGER NOT NULL DEFAULT 200, -- follow ?page=N up to this many pages
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (          -- records extracted from responses, deduplicated per category
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    data TEXT NOT NULL,                     -- JSON dict (flattened)
    source_url TEXT,
    source TEXT,                            -- endpoint name, used to group records
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(category_id, item_key)
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
        for table in ("requests", "runs"):
            if "category_id" not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE")
        for table, col, typ in (("items", "source", "TEXT"),
                                ("items", "details_done", "TEXT NOT NULL DEFAULT '[]'"),  # detail requests already merged
                                ("requests", "kind", "TEXT NOT NULL DEFAULT 'list'"),     # list | detail
                                ("requests", "url_template", "TEXT"),                     # detail: /dr/{{id}}/...
                                ("requests", "body_template", "TEXT"),
                                ("requests", "family", "INTEGER"),      # original request id (copies share it)
                                ("requests", "detail_of", "INTEGER"),   # detail: family of the list it belongs to
                                ("items", "list_id", "INTEGER")):        # list request that produced the record
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # Sections created when the default was a single page: collect all pages now
        c.execute("UPDATE categories SET max_pages=200 WHERE max_pages=1")
        # Requests imported before sections existed go to a default section
        for (site_id,) in c.execute("SELECT DISTINCT site_id FROM requests WHERE category_id IS NULL").fetchall():
            cid = c.execute("INSERT INTO categories(site_id, name, max_pages, created_at) VALUES (?,?,200,?)",
                            (site_id, "عمومی", now())).lastrowid
            c.execute("UPDATE requests SET category_id=? WHERE site_id=? AND category_id IS NULL", (cid, site_id))
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
