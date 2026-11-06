"""Rank repos by how fast they are gaining stars right now."""

import math
import re
from datetime import datetime, timedelta

from . import db

WINDOWS = {'1d': 24, '7d': 168}
# The smallest previous-window count used for acceleration, so 0 -> 3 stars isn't a 300x breakout.
ACCEL_FLOOR = {'1d': 3, '7d': 10}
WEIGHTS = {'size_adj': 1.0, 'accel': 0.5, 'novelty': 0.3}
LIST_RE = re.compile(r'(^|[-_/])(awesome|interview|roadmap|cheatsheet|free-programming-books)|\blearn\b|tutorial',
                     re.I)


def novelty(created_at, now):
    if not created_at:
        return 0.0
    age = (now - datetime.fromisoformat(created_at.replace('Z', '+00:00'))).days
    return 1.0 if age < 30 else max(0.0, (90 - age) / 60)


def flags_for(repo):
    flags = []
    text = f"{repo['name']} {repo['topics'] or ''}"
    if LIST_RE.search(text):
        flags.append('list')
    if repo['archived']:
        flags.append('archived')
    if repo['is_fork']:
        flags.append('fork')
    return flags


def _sum(conn, table, start, end):
    rows = conn.execute(f'SELECT name, SUM(stars) n FROM {table} WHERE hour >= ? AND hour < ? GROUP BY name',
                        (db.hour_key(start), db.hour_key(end)))
    return {r['name']: r['n'] for r in rows}


def _snapshot_velocity(conn, name, hours, now):
    """Stars gained over roughly `hours`, from the two snapshots that best span it."""
    snaps = conn.execute('SELECT taken_at, stars FROM snapshots WHERE name = ? ORDER BY taken_at', (name,)).fetchall()
    if len(snaps) < 2:
        return None
    last = snaps[-1]
    t_last = datetime.fromisoformat(last['taken_at'])
    target = t_last - timedelta(hours=hours)
    first = min(snaps[:-1], key=lambda s: abs(datetime.fromisoformat(s['taken_at']) - target))
    span = (t_last - datetime.fromisoformat(first['taken_at'])).total_seconds() / 3600
    if span < hours * 0.5:
        return None
    return (last['stars'] - first['stars']) * hours / span


def _ratio(pairs, min_sampled=1):
    """sum(real) / sum(sampled), or None when there's too little sample to trust."""
    sampled = sum(s for _, s in pairs)
    return sum(r for r, _ in pairs) / sampled if sampled >= min_sampled else None


def _young_repo_scale(conn, repos, min_repos=3, min_sampled=50):
    """The sample rate, learned without a token: a repo created after the first loaded hour got all of
    its stars inside the loaded hours, so its total stars over its sampled stars is the sample rate."""
    first = conn.execute("SELECT MIN(hour) FROM archive_hours WHERE status = 'ok'").fetchone()[0]
    if not first:
        return None
    totals = conn.execute('SELECT name, SUM(stars) n FROM sampled GROUP BY name').fetchall()
    pairs = []
    for r in totals:
        repo = repos.get(r['name'])
        if repo and repo['created_at'] and repo['stars'] and r['n'] and \
                db.hour_key(datetime.fromisoformat(repo['created_at'].replace('Z', '+00:00'))) >= first:
            pairs.append((repo['stars'], r['n']))
    return _ratio(pairs, min_sampled) if len(pairs) >= min_repos else None


def compute(conn, window='7d', now=None):
    """Score every known repo for `window`. Returns dicts sorted by hot_score, best first."""
    hours = WINDOWS[window]
    now = (now or db.now_utc()).replace(minute=0, second=0, microsecond=0)
    start, prev_start = now - timedelta(hours=hours), now - timedelta(hours=2 * hours)

    sampled, sampled_prev = _sum(conn, 'sampled', start, now), _sum(conn, 'sampled', prev_start, start)
    # Without most of the previous window's GH Archive hours, its count is a gap, not a slow week.
    prev_loaded = conn.execute("SELECT COUNT(*) FROM archive_hours WHERE status = 'ok' AND hour >= ? AND hour < ?",
                               (db.hour_key(prev_start), db.hour_key(start))).fetchone()[0]
    prev_sampled_known = prev_loaded >= 0.8 * hours
    measured, measured_prev = _sum(conn, 'measured', start, now), _sum(conn, 'measured', prev_start, start)
    covered = {r['name']: r['covered_since'] for r in conn.execute('SELECT * FROM measure_runs')}

    def is_covered(name, since):
        c = covered.get(name)
        return c == 'all' or (c is not None and c <= db.hour_key(since))

    repos = {r['name']: r for r in conn.execute('SELECT * FROM repos')}
    # One factor turns sampled counts into estimated real counts, learned from repos that have both.
    pairs = [(measured.get(n, 0), sampled[n]) for n in sampled if is_covered(n, start)]
    scale = _ratio(pairs) or _young_repo_scale(conn, repos)
    results = []
    for name in set(sampled) | set(measured) | set(covered) | set(repos):
        repo = repos.get(name)
        if is_covered(name, start):
            v, source = measured.get(name, 0), 'measured'
            prev = measured_prev.get(name, 0) if is_covered(name, prev_start) else None
        elif (snap := _snapshot_velocity(conn, name, hours, now)) is not None:
            v, prev, source = snap, None, 'snapshot'
        elif name in sampled:
            factor, source = (scale, 'estimated') if scale else (1, 'sampled')
            v = sampled[name] * factor
            prev = sampled_prev.get(name, 0) * factor if prev_sampled_known else None
        else:
            continue
        if v <= 0:
            continue
        total = (repo['stars'] if repo else None) or v
        size_adj = v / math.log10(total + 10)
        accel = v / max(prev, ACCEL_FLOOR[window]) if prev is not None else 1.0
        nov = novelty(repo['created_at'] if repo else None, now)
        parts = {
            'size_adj': WEIGHTS['size_adj'] * math.log1p(size_adj),
            'accel': WEIGHTS['accel'] * math.log(max(accel, 0.1)),
            'novelty': WEIGHTS['novelty'] * nov,
        }
        results.append({
            'name': name, 'url': f'https://github.com/{name}', 'window': window,
            'stars_gained': round(v), 'stars_prev_window': None if prev is None else round(prev),
            'acceleration': round(accel, 2), 'source': source,
            'total_stars': repo['stars'] if repo else None,
            'description': repo['description'] if repo else None,
            'language': repo['language'] if repo else None,
            'topics': (repo['topics'] or '').split(',') if repo and repo['topics'] else [],
            'created_at': repo['created_at'] if repo else None,
            'flags': flags_for(repo) if repo else [],
            'hot_score': round(sum(parts.values()), 3),
            'score_parts': {k: round(p, 3) for k, p in parts.items()},
        })
    results.sort(key=lambda r: r['hot_score'], reverse=True)
    return results


def trending(conn, window='7d', language=None, topic=None, limit=20, include_lists=False,
             measured_only=False, now=None):
    out = []
    for r in compute(conn, window, now):
        if not include_lists and set(r['flags']) & {'list', 'archived', 'fork'}:
            continue
        if language and (r['language'] or '').lower() != language.lower():
            continue
        if topic and topic.lower() not in [t.lower() for t in r['topics']]:
            continue
        if measured_only and r['source'] != 'measured':
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def breakouts(conn, limit=10, max_age_days=60, now=None):
    """Young repos whose stars are speeding up the most this week."""
    now = now or db.now_utc()
    young = [r for r in trending(conn, '7d', limit=10_000, now=now)
             if r['created_at'] and novelty(r['created_at'], now) > 0
             and (now - datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))).days <= max_age_days]
    young.sort(key=lambda r: (r['acceleration'], r['stars_gained']), reverse=True)
    return young[:limit]


def candidates(conn, limit=150, now=None):
    """Repos worth spending API calls on: the busiest in the sample, plus new repos found by search."""
    now = now or db.now_utc()
    start = db.hour_key(now - timedelta(days=7))
    rows = conn.execute('SELECT name, SUM(stars) n FROM sampled WHERE hour >= ? GROUP BY name ORDER BY n DESC LIMIT ?',
                        (start, limit)).fetchall()
    names = [r['name'] for r in rows]
    names += [r['name'] for r in conn.execute(
        'SELECT name FROM repos WHERE name NOT IN (SELECT name FROM measure_runs) ORDER BY stars DESC LIMIT ?',
        (limit,)) if r['name'] not in names]
    return names
