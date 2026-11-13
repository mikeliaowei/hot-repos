"""hotrepos: find the GitHub repos gaining stars fastest right now."""

import argparse
import json
import sys
from datetime import datetime, timedelta

from . import db, gharchive, github, score


def recently_measured(conn, hours):
    cutoff = (db.now_utc() - timedelta(hours=hours)).isoformat()
    return {r['name'] for r in conn.execute('SELECT name FROM measure_runs WHERE measured_at >= ?', (cutoff,))}


def cmd_collect(conn, a):
    gharchive.collect(conn, hours=a.hours)


def cmd_discover(conn, a):
    rows = github.search_new_repos(github.Client(), days=a.days, min_stars=a.min_stars)
    for r in rows:
        db.upsert_repo(conn, r)
    conn.commit()
    print(f'GitHub search: {len(rows)} repos created in the last {a.days} days')


def cmd_measure(conn, a):
    skip = recently_measured(conn, a.skip_hours)
    names = [n for n in score.candidates(conn, a.limit) if n not in skip][:a.limit]
    n = github.measure(conn, github.Client(), names, max_pages=a.max_pages)
    print(f'GitHub API: measured {n} repos')


def cmd_update(conn, a):
    cmd_collect(conn, a)
    try:
        cmd_discover(conn, a)
        cmd_measure(conn, a)
    except github.RateLimited as e:
        print(e)


def print_rows(rows, as_json):
    if as_json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return
    if not rows:
        print('No data yet. Run: hotrepos update')
        return
    head, warning = score.window_note(rows[0]['window'], rows[0]['window_end'],
                                     hours_loaded=rows[0]['window_hours_loaded'])
    print(head + (f'\n{warning}' if warning else '') + '\n')
    for i, r in enumerate(rows, 1):
        est = '' if r['source'] == 'measured' else f" ({r['source']})"
        prev = '' if r['stars_prev_window'] is None else f", x{r['acceleration']} vs previous {r['window']}"
        print(f"{i:>2}. {r['name']}  +{r['stars_gained']} stars{est}{prev}  [{r['language'] or '-'}]")
        if r['description']:
            print(f"    {r['description'][:110]}")


def cmd_top(conn, a):
    print_rows(score.trending(conn, a.window, a.language, a.topic, a.limit, a.include_lists, a.measured_only), a.json)


def cmd_breakouts(conn, a):
    print_rows(score.breakouts(conn, a.limit), a.json)


def cmd_status(conn, a):
    print(json.dumps(status(conn), indent=2))


def status(conn):
    hours = conn.execute("SELECT status, COUNT(*) n, MIN(hour) lo, MAX(hour) hi FROM archive_hours GROUP BY status")
    return {
        'archive_hours': {r['status']: {'count': r['n'], 'from': r['lo'], 'to': r['hi']} for r in hours},
        'repos_known': conn.execute('SELECT COUNT(*) FROM repos').fetchone()[0],
        'repos_measured': conn.execute('SELECT COUNT(*) FROM measure_runs').fetchone()[0],
        'last_measured': conn.execute('SELECT MAX(measured_at) FROM measure_runs').fetchone()[0],
        'github_token': bool(github.Client().token),
        'db': db.default_path(),
    }


def cmd_serve(conn, a):
    from . import mcp_server
    mcp_server.main()


def cmd_ui(conn, a):
    import os
    import socket
    import subprocess
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1', a.port)) == 0:
            sys.exit(f'Port {a.port} is in use: the hot-repos UI may already be running at http://127.0.0.1:{a.port}. '
                     f'Stop it first, or pick another port with --port {a.port + 1}.')
    app = os.path.join(os.path.dirname(__file__), 'ui.py')
    subprocess.run([sys.executable, '-m', 'streamlit', 'run', app, '--server.address', '127.0.0.1',
                    '--server.port', str(a.port), '--browser.gatherUsageStats', 'false',
                    # headless skips Streamlit's first-run email prompt, which would block without a terminal
                    '--server.headless', 'true'], check=False)


def parser():
    p = argparse.ArgumentParser(prog='hotrepos', description=__doc__)
    p.add_argument('--db', help='SQLite path (default: $HOTREPOS_DB or ~/.local/share/hotrepos/hotrepos.db)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    def fetch_args(s):
        s.add_argument('--hours', type=int, default=168, help='GH Archive hours to load (default 168)')
        s.add_argument('--days', type=int, default=30, help='search repos created in the last N days')
        s.add_argument('--min-stars', type=int, default=50)
        s.add_argument('--limit', type=int, default=150, help='repos to measure via the GitHub API')
        s.add_argument('--max-pages', type=int, default=30, help='stargazer pages per repo (100 stars each)')
        s.add_argument('--skip-hours', type=int, default=6, help='skip repos measured this recently')

    def list_args(s):
        s.add_argument('--limit', type=int, default=20)
        s.add_argument('--json', action='store_true')

    fetch_args(add('update', cmd_update, 'collect + discover + measure'))
    add('collect', cmd_collect, 'load GH Archive hours').add_argument('--hours', type=int, default=168)
    s = add('discover', cmd_discover, 'find new repos with GitHub search')
    s.add_argument('--days', type=int, default=30)
    s.add_argument('--min-stars', type=int, default=50)
    s = add('measure', cmd_measure, 'measure exact star velocity for candidates')
    s.add_argument('--limit', type=int, default=150)
    s.add_argument('--max-pages', type=int, default=30)
    s.add_argument('--skip-hours', type=int, default=6)
    s = add('top', cmd_top, 'show trending repos')
    s.add_argument('--window', choices=list(score.WINDOWS), default='7d')
    s.add_argument('--language')
    s.add_argument('--topic')
    s.add_argument('--include-lists', action='store_true', help='keep awesome-lists, tutorials, forks, archived')
    s.add_argument('--measured-only', action='store_true', help='only repos with exact GitHub counts')
    list_args(s)
    list_args(add('breakouts', cmd_breakouts, 'young repos that are speeding up'))
    add('status', cmd_status, 'what data is stored')
    add('serve', cmd_serve, 'run the MCP server on stdio')
    add('ui', cmd_ui, 'open the Streamlit UI (pip install -e ".[ui]")').add_argument('--port', type=int, default=8501)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if a.db:
        import os
        os.environ['HOTREPOS_DB'] = a.db
    conn = db.connect()
    try:
        a.fn(conn, a)
    except github.GitHubError as e:
        sys.exit(str(e))


if __name__ == '__main__':
    main()
