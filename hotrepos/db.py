"""SQLite store. Hours are UTC strings like '2026-11-05T14' so they sort and compare as text."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  name TEXT PRIMARY KEY, description TEXT, language TEXT, topics TEXT,
  created_at TEXT, stars INTEGER, forks INTEGER, homepage TEXT,
  archived INTEGER DEFAULT 0, is_fork INTEGER DEFAULT 0, updated_at TEXT
);
-- Star and fork events seen in GH Archive. GH Archive is now a small sample of GitHub,
-- so these counts only find candidates; they are scaled, never shown as real numbers.
CREATE TABLE IF NOT EXISTS sampled (
  name TEXT, hour TEXT, stars INTEGER, forks INTEGER, PRIMARY KEY (name, hour)
);
CREATE TABLE IF NOT EXISTS archive_hours (hour TEXT PRIMARY KEY, events INTEGER, status TEXT);
-- Exact new stars per hour, from the stargazer timestamps GitHub returns.
CREATE TABLE IF NOT EXISTS measured (
  name TEXT, hour TEXT, stars INTEGER, PRIMARY KEY (name, hour)
);
-- covered_since: every star after this hour is in `measured`. 'all' means the full history.
CREATE TABLE IF NOT EXISTS measure_runs (name TEXT PRIMARY KEY, measured_at TEXT, covered_since TEXT);
-- Total star counts over time; the only velocity source for repos too big to list (>40k stars).
CREATE TABLE IF NOT EXISTS snapshots (name TEXT, taken_at TEXT, stars INTEGER, PRIMARY KEY (name, taken_at));
"""


def default_path():
    return os.environ.get('HOTREPOS_DB') or os.path.expanduser('~/.local/share/hotrepos/hotrepos.db')


def connect(path=None):
    path = path or default_path()
    if path != ':memory:':
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def hour_key(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H')


def parse_hour(key):
    return datetime.strptime(key, '%Y-%m-%dT%H').replace(tzinfo=timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def hours_back(now, n):
    """The n full hours before `now`, newest first."""
    top = now.replace(minute=0, second=0, microsecond=0)
    return [top - timedelta(hours=i) for i in range(1, n + 1)]


def upsert_repo(conn, r):
    conn.execute(
        """INSERT INTO repos (name, description, language, topics, created_at, stars, forks,
                              homepage, archived, is_fork, updated_at)
           VALUES (:name, :description, :language, :topics, :created_at, :stars, :forks,
                   :homepage, :archived, :is_fork, :updated_at)
           ON CONFLICT(name) DO UPDATE SET description=excluded.description, language=excluded.language,
             topics=excluded.topics, created_at=excluded.created_at, stars=excluded.stars,
             forks=excluded.forks, homepage=excluded.homepage, archived=excluded.archived,
             is_fork=excluded.is_fork, updated_at=excluded.updated_at""",
        r,
    )
