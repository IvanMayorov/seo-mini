"""SQLite: каждый запуск аудита хранится отдельно, поэтому копится история."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY,
  site TEXT NOT NULL,
  start_url TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  settings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts(
  run_id INTEGER NOT NULL REFERENCES runs(id),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY(run_id, key)
);
CREATE TABLE IF NOT EXISTS pages(
  run_id INTEGER NOT NULL REFERENCES runs(id),
  url TEXT NOT NULL,
  depth INTEGER,
  status INTEGER,
  location TEXT,
  content_type TEXT,
  elapsed_ms INTEGER,
  error TEXT,
  blocked INTEGER NOT NULL DEFAULT 0,
  x_robots TEXT,
  is_html INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  title_count INTEGER,
  description TEXT,
  description_count INTEGER,
  h1 TEXT,
  canonical TEXT,
  robots_meta TEXT,
  word_count INTEGER,
  text_hash TEXT,
  scripts INTEGER,
  images INTEGER,
  images_no_alt INTEGER,
  has_viewport INTEGER,
  PRIMARY KEY(run_id, url)
);
CREATE TABLE IF NOT EXISTS links(
  run_id INTEGER NOT NULL REFERENCES runs(id),
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  kind TEXT NOT NULL,
  anchor TEXT,
  nofollow INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id, source, target, kind)
);
CREATE INDEX IF NOT EXISTS links_target ON links(run_id, target);
CREATE TABLE IF NOT EXISTS sitemap_urls(
  run_id INTEGER NOT NULL REFERENCES runs(id),
  url TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY(run_id, url)
);
CREATE TABLE IF NOT EXISTS external_links(
  run_id INTEGER NOT NULL REFERENCES runs(id),
  url TEXT NOT NULL,
  status INTEGER,
  error TEXT,
  PRIMARY KEY(run_id, url)
);
CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  url TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS issues_run ON issues(run_id, code);
CREATE TABLE IF NOT EXISTS plan_keywords(
  site TEXT NOT NULL,
  phrase TEXT NOT NULL,
  source TEXT NOT NULL,
  position INTEGER,
  broad INTEGER,
  exact INTEGER,
  base TEXT,
  collected_on TEXT NOT NULL,
  PRIMARY KEY(site, phrase, source)
);
CREATE TABLE IF NOT EXISTS queries(
  id INTEGER PRIMARY KEY,
  site TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(site, text)
);
CREATE TABLE IF NOT EXISTS competitors(
  site TEXT NOT NULL,
  domain TEXT NOT NULL,
  PRIMARY KEY(site, domain)
);
CREATE TABLE IF NOT EXISTS serp_requests(
  id INTEGER PRIMARY KEY,
  query_id INTEGER NOT NULL REFERENCES queries(id),
  region TEXT NOT NULL,
  collected_on TEXT NOT NULL,
  operation_id TEXT,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  submitted_at TEXT NOT NULL,
  done_at TEXT,
  error TEXT,
  UNIQUE(query_id, region, collected_on)
);
CREATE TABLE IF NOT EXISTS serp_results(
  id INTEGER PRIMARY KEY,
  request_id INTEGER NOT NULL REFERENCES serp_requests(id),
  position INTEGER NOT NULL,
  url TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT,
  snippet TEXT,
  UNIQUE(request_id, position)
);
CREATE TABLE IF NOT EXISTS wordstat_requests(
  id INTEGER PRIMARY KEY,
  query_id INTEGER NOT NULL REFERENCES queries(id),
  region TEXT NOT NULL,
  collected_on TEXT NOT NULL,
  total_count INTEGER NOT NULL,
  UNIQUE(query_id, region, collected_on)
);
CREATE TABLE IF NOT EXISTS wordstat_phrases(
  request_id INTEGER NOT NULL REFERENCES wordstat_requests(id),
  kind TEXT NOT NULL,
  position INTEGER NOT NULL,
  phrase TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(request_id, kind, position)
);
CREATE TABLE IF NOT EXISTS metrika_rows(
  site TEXT NOT NULL,
  report TEXT NOT NULL,
  key TEXT NOT NULL,
  subkey TEXT NOT NULL DEFAULT '',
  visits REAL,
  bounce REAL,
  duration REAL,
  goals TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(site, report, key, subkey)
);
CREATE TABLE IF NOT EXISTS search_metrics(
  source TEXT NOT NULL,
  site TEXT NOT NULL,
  region TEXT NOT NULL,
  dimension TEXT NOT NULL,
  key TEXT NOT NULL,
  date TEXT NOT NULL,
  impressions REAL,
  clicks REAL,
  position REAL,
  demand REAL,
  PRIMARY KEY(source, site, region, dimension, key, date)
);
CREATE TABLE IF NOT EXISTS search_snapshots(
  source TEXT NOT NULL,
  site TEXT NOT NULL,
  kind TEXT NOT NULL,
  collected_on TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY(source, site, kind, collected_on)
);
"""

PAGE_COLUMNS = (
    "url depth status location content_type elapsed_ms error blocked x_robots is_html "
    "title title_count description description_count h1 canonical robots_meta word_count "
    "text_hash scripts images images_no_alt has_viewport"
).split()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(conn: sqlite3.Connection, site: str, start_url: str, settings: dict) -> int:
    cursor = conn.execute(
        "INSERT INTO runs(site, start_url, started_at, settings) VALUES(?,?,?,?)",
        (site, start_url, now(), json.dumps(settings, ensure_ascii=False)),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (now(), run_id))
    conn.commit()


def set_fact(conn: sqlite3.Connection, run_id: int, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO facts(run_id, key, value) VALUES(?,?,?)",
        (run_id, key, json.dumps(value, ensure_ascii=False)),
    )


def facts(conn: sqlite3.Connection, run_id: int) -> dict:
    rows = conn.execute("SELECT key, value FROM facts WHERE run_id=?", (run_id,))
    return {row["key"]: json.loads(row["value"]) for row in rows}


def insert_page(conn: sqlite3.Connection, run_id: int, page: dict) -> None:
    columns = ",".join(PAGE_COLUMNS)
    marks = ",".join("?" for _ in PAGE_COLUMNS)
    values = [page.get(c, 0 if c in ("blocked", "is_html") else None) for c in PAGE_COLUMNS]
    conn.execute(
        f"INSERT OR REPLACE INTO pages(run_id,{columns}) VALUES(?,{marks})", [run_id, *values]
    )
