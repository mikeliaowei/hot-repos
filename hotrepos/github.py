"""Measurement: exact star counts and stargazer timestamps from the GitHub REST API."""

import json
import math
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

from . import db

API = 'https://api.github.com'
# GitHub refuses stargazer pages past the first 40,000 stars.
STARGAZER_LIST_LIMIT = 40_000


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    pass


class TokenRejected(GitHubError):
    pass


def gh_cli_token():
    """The token of the GitHub CLI's login (`gh auth token`), or None without gh or a gh login. It is read from gh's
    own store (the OS keychain), so it works however hot-repos was started: a terminal opened before ~/.zshrc
    changed, an editor, a scheduled job."""
    try:
        r = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def default_token():
    """(token, where it came from): GITHUB_TOKEN, then GH_TOKEN, then the gh CLI's login; (None, '') for none."""
    for name in ('GITHUB_TOKEN', 'GH_TOKEN'):
        if os.environ.get(name):
            return os.environ[name], name
    token = gh_cli_token()
    return (token, 'gh') if token else (None, '')


class Client:
    def __init__(self, token=None, opener=urllib.request.urlopen):
        if token is not None:
            self.token, self.token_source = token, 'GITHUB_TOKEN'
        else:
            self.token, self.token_source = default_token()
        self.opener = opener

    def get(self, path, accept='application/vnd.github+json', **params):
        url = API + path + ('?' + urllib.parse.urlencode(params) if params else '')
        req = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'hot-repos'})
        if self.token:
            req.add_header('Authorization', f'Bearer {self.token}')
        try:
            with self.opener(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read() or b'{}').get('message', '')
            except ValueError:
                message = ''
            if e.code in (403, 429) and e.headers.get('X-RateLimit-Remaining') == '0':
                raise RateLimited(f'GitHub rate limit reached (resets at {e.headers.get("X-RateLimit-Reset")})')
            if e.code in (403, 429) and (e.headers.get('Retry-After') or 'secondary rate limit' in message.lower()):
                raise RateLimited(f'GitHub secondary rate limit: wait {e.headers.get("Retry-After") or "a few"} '
                                  'seconds and run again')
            if e.code == 401 and self.token and self.token_source == 'gh':
                raise TokenRejected('GitHub rejected the token of your gh CLI login (401 Bad credentials). Run '
                                    '`gh auth login` again, or set GITHUB_TOKEN.')
            if e.code == 401 and self.token:
                raise TokenRejected('GitHub rejected GITHUB_TOKEN (401 Bad credentials). Check that it was copied '
                                    'in full and has not expired, or unset it to run without a token.')
            if e.code == 404:
                return None
            raise GitHubError(f'GitHub API {e.code} on {path}: {message or e.reason}') from None


def repo_row(item):
    return {
        'name': item['full_name'], 'description': item.get('description'), 'language': item.get('language'),
        'topics': ','.join(item.get('topics') or []), 'created_at': item.get('created_at'),
        'stars': item.get('stargazers_count'), 'forks': item.get('forks_count'), 'homepage': item.get('homepage'),
        'archived': int(bool(item.get('archived'))), 'is_fork': int(bool(item.get('fork'))),
        'updated_at': item.get('pushed_at'),
    }


def search_new_repos(client, days=30, min_stars=50, pages=2):
    """The most-starred repos created in the last `days` days."""
    since = (db.now_utc() - timedelta(days=days)).strftime('%Y-%m-%d')
    rows = []
    for page in range(1, pages + 1):
        res = client.get('/search/repositories', q=f'created:>{since} stars:>={min_stars}',
                         sort='stars', order='desc', per_page=100, page=page)
        items = (res or {}).get('items') or []
        rows += [repo_row(i) for i in items]
        if len(items) < 100:
            break
    return rows


def recent_stars(client, name, total, since, max_pages=30):
    """Stars per hour after `since`, read newest page first.

    Returns (Counter of hour -> stars, covered_since), where covered_since is 'all' when the whole
    history was read, otherwise the oldest hour fully covered. None when the repo is too big to list.
    """
    if total > STARGAZER_LIST_LIMIT:
        return None
    per_hour = Counter()
    page = max(1, math.ceil(total / 100))
    covered = None
    for _ in range(max_pages):
        batch = client.get(f'/repos/{name}/stargazers', accept='application/vnd.github.star+json',
                           per_page=100, page=page) or []
        times = [datetime.fromisoformat(s['starred_at'].replace('Z', '+00:00')) for s in batch]
        for t in times:
            if t >= since:
                per_hour[db.hour_key(t)] += 1
        if times and min(times) < since:
            return per_hour, db.hour_key(since)
        if page == 1:
            return per_hour, 'all'
        if times:
            # The oldest star on this page bounds coverage; the hour it falls in may be partial.
            covered = db.hour_key(min(times) + timedelta(hours=1))
        page -= 1
    return per_hour, covered


def measure(conn, client, names, lookback_days=14, max_pages=30, log=print):
    """Refresh metadata, a star snapshot and recent stargazer times for each repo. Stops at the rate limit."""
    now = db.now_utc()
    since = now - timedelta(days=lookback_days)
    done = 0
    list_stargazers = bool(client.token)
    if not list_stargazers:
        log('No GitHub token (set GITHUB_TOKEN or run `gh auth login`): saving star snapshots only '
            '(exact recent stars need a token).')
    try:
        for name in names:
            item = client.get(f'/repos/{name}')
            if item is None:
                continue
            row = repo_row(item)
            name = row['name']  # follow renames
            db.upsert_repo(conn, row)
            conn.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)', (name, now.isoformat(), row['stars']))
            # Listing stargazers needs a token; without one, snapshots alone build up velocity over time.
            got = None
            if list_stargazers:
                try:
                    got = recent_stars(client, name, row['stars'] or 0, since, max_pages)
                except (RateLimited, TokenRejected):
                    raise
                except GitHubError as e:
                    # The same refusal would repeat for every repo, so stop asking and keep the snapshots.
                    log(f'{e}\nGitHub refused the stargazer list; continuing with star snapshots only.')
                    list_stargazers = False
            if got is not None:
                per_hour, covered = got
                conn.execute('DELETE FROM measured WHERE name = ?', (name,))
                conn.executemany('INSERT INTO measured VALUES (?, ?, ?)', [(name, h, n) for h, n in per_hour.items()])
                conn.execute('INSERT OR REPLACE INTO measure_runs VALUES (?, ?, ?)', (name, now.isoformat(), covered))
            conn.commit()
            done += 1
    except RateLimited as e:
        log(f'{e}; measured {done} of {len(names)} repos. Set GITHUB_TOKEN or run `gh auth login` for 5,000 requests an hour.')
    conn.commit()
    return done
