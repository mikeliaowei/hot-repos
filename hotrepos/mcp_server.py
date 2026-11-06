"""MCP server: lets any agent ask what's trending on GitHub."""

from datetime import timedelta

from mcp.server.mcpserver import MCPServer

from . import cli, db, score

server = MCPServer(
    name='hot-repos',
    instructions=(
        'Finds GitHub repositories gaining stars fastest. `source` says how a number was obtained: '
        "'measured' is exact from GitHub, 'snapshot' comes from two star counts, 'estimated' is scaled "
        "from GH Archive's sample. Mention when numbers are estimated."
    ),
)


@server.tool()
def trending(window: str = '7d', language: str | None = None, topic: str | None = None, limit: int = 20,
             include_lists: bool = False) -> list[dict]:
    """Repos gaining stars fastest. window is '1d' or '7d'. include_lists keeps awesome-lists and tutorials."""
    if window not in score.WINDOWS:
        raise ValueError(f"window must be one of {', '.join(score.WINDOWS)}")
    return score.trending(db.connect(), window, language, topic, limit, include_lists)


@server.tool()
def breakouts(limit: int = 10) -> list[dict]:
    """Repos under 60 days old whose stars are speeding up the most this week."""
    return score.breakouts(db.connect(), limit)


@server.tool()
def explain(repo: str) -> dict:
    """Everything stored about one repo ('owner/name'): metadata, scores and stars per day for 14 days."""
    conn = db.connect()
    row = conn.execute('SELECT * FROM repos WHERE lower(name) = lower(?)', (repo,)).fetchone()
    name = row['name'] if row else repo
    scores = {w: next((r for r in score.compute(conn, w) if r['name'] == name), None) for w in score.WINDOWS}
    since = db.hour_key(db.now_utc() - timedelta(days=14))
    per_day = {}
    for table in ('measured', 'sampled'):
        rows = conn.execute(f'SELECT substr(hour, 1, 10) day, SUM(stars) n FROM {table} '
                            'WHERE name = ? AND hour >= ? GROUP BY day ORDER BY day', (name, since)).fetchall()
        if rows:
            per_day = {'source': table, 'days': {r['day']: r['n'] for r in rows}}
            break
    if not row and not any(scores.values()):
        return {'repo': repo, 'error': 'Not in the database. It may not be trending, or `hotrepos update` has not run.'}
    return {'repo': name, 'metadata': dict(row) if row else None, 'scores': scores, 'stars_per_day': per_day}


@server.tool()
def data_status() -> dict:
    """How fresh the data is: GH Archive hours loaded, repos measured, whether a GitHub token is set."""
    return cli.status(db.connect())


def main():
    server.run('stdio')


if __name__ == '__main__':
    main()
