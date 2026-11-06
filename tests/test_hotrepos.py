import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from hotrepos import cli, db, gharchive, github, score

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def event(kind, repo):
    return json.dumps({'type': kind, 'repo': {'name': repo}})


def repo(name, stars, created_days_ago=400, topics='', **kw):
    return {'name': name, 'description': f'{name} desc', 'language': 'Rust', 'topics': topics,
            'created_at': (NOW - timedelta(days=created_days_ago)).isoformat().replace('+00:00', 'Z'),
            'stars': stars, 'forks': 0, 'homepage': None, 'archived': 0, 'is_fork': 0, 'updated_at': None, **kw}


def add_measured(conn, name, per_day, days=14):
    """per_day: stars per day for the last `days` days, oldest first; spread over noon of each day."""
    for i, n in enumerate(per_day):
        hour = NOW - timedelta(days=days - i) + timedelta(hours=1)
        conn.execute('INSERT INTO measured VALUES (?, ?, ?)', (name, db.hour_key(hour), n))
    conn.execute('INSERT INTO measure_runs VALUES (?, ?, ?)',
                 (name, NOW.isoformat(), db.hour_key(NOW - timedelta(days=days))))


class GHArchiveTest(unittest.TestCase):
    def test_counts_stars_and_forks_per_repo(self):
        lines = [event('WatchEvent', 'a/x'), event('WatchEvent', 'a/x'), event('ForkEvent', 'a/x'),
                 event('WatchEvent', 'b/y'), event('PushEvent', 'b/y'), '',
                 json.dumps({'type': 'WatchEvent', 'repo': {'id': 1}})]
        stars, forks, total = gharchive.count_events(lines)
        self.assertEqual(stars, Counter({'a/x': 2, 'b/y': 1}))
        self.assertEqual(forks, Counter({'a/x': 1}))
        self.assertEqual(total, 6)

    def test_collect_stores_hours_marks_missing_and_skips_loaded(self):
        conn = db.connect(':memory:')
        calls = []

        def fetch(dt):
            calls.append(dt)
            if dt.hour == 10:
                return None
            if dt.hour == 9:
                raise OSError('network down')
            return Counter({'a/x': 3}), Counter(), 50

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gharchive.collect(conn, hours=3, now=NOW, fetch=fetch, workers=1), 1)
        self.assertIn('1 missing upstream, 1 failed', out.getvalue())
        status = {r['hour']: r['status'] for r in conn.execute('SELECT * FROM archive_hours')}
        self.assertEqual(status, {'2026-11-05T11': 'ok', '2026-11-05T10': 'missing'})
        self.assertEqual(conn.execute('SELECT stars FROM sampled').fetchone()[0], 3)

        calls.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            gharchive.collect(conn, hours=3, now=NOW, fetch=fetch, workers=1)
        self.assertEqual(sorted(c.hour for c in calls), [9, 10])  # the loaded hour isn't fetched again

    def test_fetch_hour_parses_gzip_and_maps_404_to_none(self):
        body = gzip.compress('\n'.join([event('WatchEvent', 'a/x'), event('ForkEvent', 'a/x')]).encode())

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        seen = []

        def opener(req, timeout):
            seen.append(req)
            return Resp(body)

        stars, forks, total = gharchive.fetch_hour(NOW, opener=opener)
        self.assertEqual((stars['a/x'], forks['a/x'], total), (1, 1, 2))
        self.assertEqual(seen[0].full_url, 'https://data.gharchive.org/2026-11-05-12.json.gz')
        self.assertEqual(seen[0].get_header('User-agent'), 'hot-repos')

        def missing(url, timeout):
            raise gharchive.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        self.assertIsNone(gharchive.fetch_hour(NOW, opener=missing))


class FakeClient:
    """Serves stargazer pages for a repo with one star per hour, oldest first."""

    def __init__(self, total, first_star):
        self.times = [first_star + timedelta(hours=i) for i in range(total)]
        self.pages = []

    def get(self, path, accept=None, per_page=100, page=1, **_):
        self.pages.append(page)
        chunk = self.times[(page - 1) * per_page: page * per_page]
        return [{'starred_at': t.isoformat().replace('+00:00', 'Z')} for t in chunk]


class RecentStarsTest(unittest.TestCase):
    def test_reads_newest_pages_until_older_than_since(self):
        client = FakeClient(1000, NOW - timedelta(hours=1000))
        per_hour, covered = github.recent_stars(client, 'a/x', 1000, NOW - timedelta(hours=150))
        self.assertEqual(sum(per_hour.values()), 150)
        self.assertEqual(client.pages, [10, 9])
        self.assertEqual(covered, db.hour_key(NOW - timedelta(hours=150)))

    def test_small_repo_reads_whole_history(self):
        client = FakeClient(40, NOW - timedelta(hours=40))
        per_hour, covered = github.recent_stars(client, 'a/x', 40, NOW - timedelta(days=14))
        self.assertEqual((sum(per_hour.values()), covered, client.pages), (40, 'all', [1]))

    def test_page_cap_reports_partial_coverage(self):
        client = FakeClient(1000, NOW - timedelta(hours=1000))
        per_hour, covered = github.recent_stars(client, 'a/x', 1000, NOW - timedelta(hours=500), max_pages=2)
        self.assertEqual(sum(per_hour.values()), 200)
        self.assertGreater(covered, db.hour_key(NOW - timedelta(hours=500)))

    def test_too_big_to_list(self):
        self.assertIsNone(github.recent_stars(FakeClient(0, NOW), 'a/x', 50_000, NOW))


class FakeAPI(FakeClient):
    def __init__(self, token):
        super().__init__(5, db.now_utc() - timedelta(hours=5))
        self.token = token
        self.paths = []

    def get(self, path, accept=None, **params):
        self.paths.append(path)
        if path == '/repos/a/x':
            return {'full_name': 'a/x', 'stargazers_count': 5, 'forks_count': 1, 'created_at': '2026-11-01T00:00:00Z'}
        return super().get(path, accept, **params)


class MeasureTest(unittest.TestCase):
    def run_measure(self, token):
        conn, api = db.connect(':memory:'), FakeAPI(token)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(github.measure(conn, api, ['a/x']), 1)
        return conn, api, out.getvalue()

    def test_with_token_stores_exact_recent_stars(self):
        conn, api, _ = self.run_measure('t')
        self.assertEqual(api.paths, ['/repos/a/x', '/repos/a/x/stargazers'])
        self.assertEqual(conn.execute('SELECT SUM(stars) FROM measured').fetchone()[0], 5)
        self.assertEqual(conn.execute('SELECT covered_since FROM measure_runs').fetchone()[0], 'all')

    def test_without_token_saves_only_a_snapshot(self):
        conn, api, out = self.run_measure(None)
        self.assertEqual(api.paths, ['/repos/a/x'])
        self.assertIn('No GITHUB_TOKEN', out)
        self.assertEqual(conn.execute('SELECT stars FROM snapshots').fetchone()[0], 5)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM measure_runs').fetchone()[0], 0)


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(':memory:')

    def test_breakout_beats_bigger_steady_repo(self):
        db.upsert_repo(self.conn, repo('new/rocket', 900, created_days_ago=10))
        db.upsert_repo(self.conn, repo('old/steady', 80_000))
        add_measured(self.conn, 'new/rocket', [5] * 7 + [120] * 7)
        add_measured(self.conn, 'old/steady', [100] * 14)
        rows = score.trending(self.conn, '7d', now=NOW)
        self.assertEqual([r['name'] for r in rows], ['new/rocket', 'old/steady'])
        rocket = rows[0]
        self.assertEqual((rocket['stars_gained'], rocket['stars_prev_window'], rocket['source']), (840, 35, 'measured'))
        self.assertEqual(rocket['acceleration'], 24.0)
        self.assertEqual(rows[1]['acceleration'], 1.0)

    def test_lists_are_hidden_unless_asked(self):
        db.upsert_repo(self.conn, repo('x/awesome-rust', 500))
        add_measured(self.conn, 'x/awesome-rust', [50] * 14)
        self.assertEqual(score.trending(self.conn, now=NOW), [])
        self.assertEqual(score.trending(self.conn, include_lists=True, now=NOW)[0]['flags'], ['list'])

    def test_sampled_counts_are_scaled_by_measured_repos(self):
        db.upsert_repo(self.conn, repo('a/measured', 5000))
        add_measured(self.conn, 'a/measured', [100] * 14)
        hour = db.hour_key(NOW - timedelta(days=1))
        self.conn.executemany('INSERT INTO sampled VALUES (?, ?, ?, 0)', [('a/measured', hour, 7), ('b/only-sampled', hour, 2)])
        rows = {r['name']: r for r in score.compute(self.conn, '7d', NOW)}
        self.assertEqual(rows['b/only-sampled']['source'], 'estimated')
        self.assertEqual(rows['b/only-sampled']['stars_gained'], 200)  # 2 sampled x (700 measured / 7 sampled)
        self.assertIsNone(rows['b/only-sampled']['stars_prev_window'])  # previous week never loaded

    def test_without_measurements_young_repos_calibrate_the_sample_rate(self):
        for h in db.hours_back(NOW, 168):
            self.conn.execute("INSERT INTO archive_hours VALUES (?, 1, 'ok')", (db.hour_key(h),))
        hour = db.hour_key(NOW - timedelta(days=1))
        # Three repos created inside the loaded week: 6,000 real stars over 100 sampled, a rate of 60.
        for name, total, seen in (('y/a', 3000, 50), ('y/b', 2000, 30), ('y/c', 1000, 20)):
            db.upsert_repo(self.conn, repo(name, total, created_days_ago=3))
            self.conn.execute('INSERT INTO sampled VALUES (?, ?, ?, 0)', (name, hour, seen))
        db.upsert_repo(self.conn, repo('old/z', 90_000))  # older than the loaded hours: not used to calibrate
        self.conn.execute('INSERT INTO sampled VALUES (?, ?, ?, 0)', ('old/z', hour, 5))
        rows = {r['name']: r for r in score.compute(self.conn, '7d', NOW)}
        self.assertEqual((rows['old/z']['source'], rows['old/z']['stars_gained']), ('estimated', 300))
        self.assertEqual(rows['y/a']['stars_gained'], 3000)

    def test_too_few_young_repos_leaves_counts_sampled(self):
        self.conn.execute("INSERT INTO archive_hours VALUES (?, 1, 'ok')", (db.hour_key(NOW - timedelta(days=8)),))
        db.upsert_repo(self.conn, repo('y/a', 3000, created_days_ago=3))
        self.conn.execute('INSERT INTO sampled VALUES (?, ?, ?, 0)', ('y/a', db.hour_key(NOW - timedelta(days=1)), 50))
        self.assertEqual(score.compute(self.conn, '7d', NOW)[0]['source'], 'sampled')

    def test_sampled_acceleration_needs_the_previous_window_loaded(self):
        for h in db.hours_back(NOW, 336):
            self.conn.execute("INSERT INTO archive_hours VALUES (?, 1, 'ok')", (db.hour_key(h),))
        self.conn.executemany('INSERT INTO sampled VALUES (?, ?, ?, 0)', [
            ('a/x', db.hour_key(NOW - timedelta(days=1)), 30), ('a/x', db.hour_key(NOW - timedelta(days=10)), 15)])
        r = score.compute(self.conn, '7d', NOW)[0]
        self.assertEqual((r['source'], r['stars_gained'], r['stars_prev_window'], r['acceleration']),
                         ('sampled', 30, 15, 2.0))

    def test_big_repo_uses_snapshots(self):
        db.upsert_repo(self.conn, repo('big/one', 100_000))
        self.conn.executemany('INSERT INTO snapshots VALUES (?, ?, ?)', [
            ('big/one', (NOW - timedelta(days=7)).isoformat(), 99_000), ('big/one', NOW.isoformat(), 100_000)])
        r = score.compute(self.conn, '7d', NOW)[0]
        self.assertEqual((r['source'], r['stars_gained'], r['stars_prev_window']), ('snapshot', 1000, None))

    def test_language_and_topic_filters(self):
        db.upsert_repo(self.conn, repo('a/rs', 100, topics='cli,agents'))
        db.upsert_repo(self.conn, repo('b/py', 100, language='Python'))
        for n in ('a/rs', 'b/py'):
            add_measured(self.conn, n, [10] * 14)
        self.assertEqual([r['name'] for r in score.trending(self.conn, language='python', now=NOW)], ['b/py'])
        self.assertEqual([r['name'] for r in score.trending(self.conn, topic='Agents', now=NOW)], ['a/rs'])

    def test_breakouts_only_young_repos(self):
        db.upsert_repo(self.conn, repo('new/a', 300, created_days_ago=5))
        db.upsert_repo(self.conn, repo('old/b', 300, created_days_ago=300))
        for n in ('new/a', 'old/b'):
            add_measured(self.conn, n, [1] * 7 + [40] * 7)
        self.assertEqual([r['name'] for r in score.breakouts(self.conn, now=NOW)], ['new/a'])


class CLITest(unittest.TestCase):
    def test_top_prints_ranked_rows_from_the_db(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'h.db')
            conn = db.connect(path)
            now = db.now_utc()
            db.upsert_repo(conn, repo('new/rocket', 900))
            for i in range(14):
                conn.execute('INSERT INTO measured VALUES (?, ?, ?)',
                             ('new/rocket', db.hour_key(now - timedelta(days=i, hours=1)), 20))
            conn.execute('INSERT INTO measure_runs VALUES (?, ?, ?)', ('new/rocket', now.isoformat(), 'all'))
            conn.commit()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(['--db', path, 'top', '--limit', '5'])
            self.assertIn('1. new/rocket  +140 stars, x1.0 vs previous 7d  [Rust]', out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(['--db', path, 'top', '--json'])
            self.assertEqual(json.loads(out.getvalue())[0]['source'], 'measured')
        os.environ.pop('HOTREPOS_DB', None)


if __name__ == '__main__':
    unittest.main()


class ErrorMessageTest(unittest.TestCase):
    def test_rejected_token_raises_a_clear_error(self):
        def opener(req, timeout):
            raise github.urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, None)
        with self.assertRaisesRegex(github.TokenRejected, 'GitHub rejected GITHUB_TOKEN'):
            github.Client(token='bad', opener=opener).get('/repos/a/x')
        self.assertIsNone(github.Client(token='', opener=lambda r, timeout: (_ for _ in ()).throw(
            github.urllib.error.HTTPError(r.full_url, 404, 'Not Found', {}, None))).get('/repos/a/x'))

    def test_cli_exits_with_the_message_instead_of_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'h.db')
            conn = db.connect(path)
            db.upsert_repo(conn, repo('a/x', 10))
            conn.commit()
            from unittest import mock
            with mock.patch.object(github.Client, 'get', side_effect=github.TokenRejected('GitHub rejected it')), \
                    self.assertRaises(SystemExit) as exit_, contextlib.redirect_stdout(io.StringIO()):
                cli.main(['--db', path, 'measure', '--limit', '1'])
            self.assertEqual(str(exit_.exception), 'GitHub rejected it')
        os.environ.pop('HOTREPOS_DB', None)

    def test_ui_refuses_a_busy_port(self):
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            s.listen()
            port = s.getsockname()[1]
            with self.assertRaises(SystemExit) as exit_:
                cli.main(['ui', '--port', str(port)])
        self.assertIn(f'Port {port} is in use', str(exit_.exception))
        os.environ.pop('HOTREPOS_DB', None)


def http_error(req, code, message, headers=None):
    from email.message import Message
    h = Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return github.urllib.error.HTTPError(req.full_url, code, 'Forbidden', h, io.BytesIO(json.dumps({'message': message}).encode()))


class Forbidden403Test(unittest.TestCase):
    def client(self, message, headers=None):
        def opener(req, timeout):
            if req.full_url.endswith('/repos/a/x'):
                return contextlib.nullcontext(io.BytesIO(json.dumps(
                    {'full_name': 'a/x', 'stargazers_count': 5, 'created_at': '2026-11-01T00:00:00Z'}).encode()))
            raise http_error(req, 403, message, headers)
        return github.Client(token='t', opener=opener)

    def test_stargazer_refusal_shows_githubs_reason_and_keeps_snapshots(self):
        conn = db.connect(':memory:')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            done = github.measure(conn, self.client('Resource not accessible by personal access token'), ['a/x', 'a/x'])
        self.assertEqual(done, 2)
        log = out.getvalue()
        self.assertIn('GitHub API 403 on /repos/a/x/stargazers: Resource not accessible by personal access token', log)
        self.assertEqual(log.count('GitHub refused the stargazer list'), 1)  # asked once, not per repo
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM measure_runs').fetchone()[0], 0)

    def test_secondary_rate_limit_stops_politely(self):
        conn = db.connect(':memory:')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            github.measure(conn, self.client('You have exceeded a secondary rate limit', {'Retry-After': '60'}), ['a/x'])
        self.assertIn('secondary rate limit: wait 60 seconds', out.getvalue())
