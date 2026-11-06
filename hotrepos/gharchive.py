"""Discovery: count star and fork events per repo in GH Archive's hourly dumps."""

import gzip
import io
import json
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import db

URL = 'https://data.gharchive.org/{:%Y-%m-%d}-{}.json.gz'


def count_events(lines):
    """(stars, forks, total events) from GH Archive JSON lines."""
    stars, forks, total = Counter(), Counter(), 0
    for line in lines:
        if not line.strip():
            continue
        total += 1
        e = json.loads(line)
        name = (e.get('repo') or {}).get('name')  # a few events carry no repo name
        if not name:
            continue
        if e['type'] == 'WatchEvent':
            stars[name] += 1
        elif e['type'] == 'ForkEvent':
            forks[name] += 1
    return stars, forks, total


def fetch_hour(dt, opener=urllib.request.urlopen):
    """Counts for one hour, or None when GH Archive has no file for it."""
    try:
        # GH Archive's CDN rejects Python's default User-Agent with a 403.
        req = urllib.request.Request(URL.format(dt, dt.hour), headers={'User-Agent': 'hot-repos'})
        with opener(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    with gzip.open(io.BytesIO(raw), 'rt', encoding='utf-8') as lines:
        return count_events(lines)


def collect(conn, hours=168, now=None, workers=8, fetch=fetch_hour, log=print):
    """Load the last `hours` hours that aren't stored yet. Returns how many were loaded."""
    done = {r['hour'] for r in conn.execute("SELECT hour FROM archive_hours WHERE status = 'ok'")}
    todo = [h for h in db.hours_back(now or db.now_utc(), hours) if db.hour_key(h) not in done]
    def safe_fetch(dt):
        try:
            return fetch(dt)
        except Exception as e:  # one bad hour shouldn't sink the run; it's retried next time
            return e

    loaded = failed = 0
    with ThreadPoolExecutor(workers) as pool:
        for dt, result in zip(todo, pool.map(safe_fetch, todo)):
            key = db.hour_key(dt)
            if isinstance(result, Exception):
                log(f'GH Archive {key}: {result}')
                failed += 1
                continue
            if result is None:
                conn.execute("INSERT OR REPLACE INTO archive_hours VALUES (?, 0, 'missing')", (key,))
                continue
            stars, forks, total = result
            conn.executemany(
                'INSERT OR REPLACE INTO sampled VALUES (?, ?, ?, ?)',
                [(name, key, stars[name], forks[name]) for name in stars.keys() | forks.keys()],
            )
            conn.execute("INSERT OR REPLACE INTO archive_hours VALUES (?, ?, 'ok')", (key, total))
            conn.commit()
            loaded += 1
    conn.commit()
    missing = len(todo) - loaded - failed
    log(f'GH Archive: loaded {loaded} of {len(todo)} new hours ({missing} missing upstream, {failed} failed)')
    return loaded
