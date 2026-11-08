# hot-repos

Find the GitHub repos gaining stars fastest right now, from the command line or from any agent over MCP.

GitHub has no trending API, so hot-repos measures star velocity itself:

1. **Discover** candidates from [GH Archive](https://www.gharchive.org/) (hourly public events) and GitHub search (new repos).
2. **Measure** them with the GitHub API: exact recent stars from stargazer timestamps, plus star-count snapshots.
3. **Score** by stars gained, adjusted for repo size, acceleration against the previous window, and newness.

GH Archive now records only a small sample of GitHub (an hour was ~137 MB in late 2024, ~5 MB in late 2026,
and some hours are missing), so its counts only pick candidates. Every number says where it came from:

| `source` | Meaning |
|---|---|
| `measured` | Exact, from stargazer timestamps (needs `GITHUB_TOKEN`) |
| `snapshot` | The difference between two star counts taken over time |
| `estimated` | GH Archive count scaled by the sample rate, learned from measured repos or, without a token, from repos created inside the loaded hours (all their stars fall in the sample window). In November 2026 the rate was about 1 in 69. |
| `sampled` | Raw GH Archive count; nothing measured yet to scale it |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
export GITHUB_TOKEN=github_pat_...   # optional but recommended; see "GitHub token" below
.venv/bin/hotrepos update            # GH Archive (7 days) + search + measure 150 candidates
```

## GitHub token

hot-repos works without a token, but every number is then an estimate. GitHub only lists who starred a repo,
with the time of each star, to signed-in requests (anonymous requests get `401`), and anonymous requests are
limited to 60 an hour. With a token, one `update` gives exact 1-day and 7-day numbers (`source: measured`) and
the limit is 5,000 requests an hour.

**Create one (free, no permissions needed).** hot-repos only reads public data.

1. Open https://github.com/settings/personal-access-tokens/new.
2. Name it `hot-repos` and pick an expiry.
3. Under **Repository access** choose **Public repositories**, and leave every permission at "No access".
4. Generate it and copy the token. A classic token with no scopes ticked works too.

**Set it once, not every time.** `export` only lasts for the terminal you type it in. Add it to your shell
startup file so every new terminal has it:

```bash
echo 'export GITHUB_TOKEN=github_pat_...' >> ~/.zshrc   # your real token
source ~/.zshrc
echo ${#GITHUB_TOKEN}                                    # prints the length (not the token); 0 means not set
```

`~/.zshrc` is plain text: never commit or share it, and revoke the token on GitHub if it leaks.

**When to renew.** Only when the token expires (fine-grained tokens last up to a year; GitHub emails you
before). An expired token shows `GitHub rejected GITHUB_TOKEN`. Create a new one and replace the line in
`~/.zshrc`.

**Programs read the token when they start.** A UI started before you set the token keeps showing
"GitHub token: not set". Stop it (Ctrl+C) and run `hotrepos ui` again from a terminal that has the token.
The MCP server only reads the local database, so it doesn't need the token.

## Scheduled updates

Run `update` hourly or daily from cron. Cron doesn't read `~/.zshrc`, so load it explicitly:

```cron
15 * * * * zsh -c 'source ~/.zshrc; cd ~/Dev/hot-repos && .venv/bin/hotrepos update' >> ~/.local/share/hotrepos/update.log 2>&1
```

## Commands

```bash
hotrepos top [--window 1d|7d] [--language Rust] [--topic agents] [--limit 20] [--include-lists] [--measured-only] [--json]
hotrepos breakouts [--limit 10] [--json]   # repos under 60 days old that are speeding up
hotrepos update | collect | discover | measure
hotrepos status                            # what's stored, token present
hotrepos serve                             # MCP server on stdio
```

`top` hides awesome-lists, tutorials, interview prep, forks and archived repos unless `--include-lists` is set.
Data lives in `~/.local/share/hotrepos/hotrepos.db`, or `$HOTREPOS_DB`, or `--db`.

## UI

```bash
.venv/bin/pip install -e ".[ui]"
.venv/bin/hotrepos ui            # http://127.0.0.1:8501
```

A Streamlit page with Trending and Breakouts tabs, filters for window, language and topic, and a detail view
(stars per day, score parts) when you select a row.

## Agent tools (MCP)

`trending(window, language, topic, limit, include_lists)`, `breakouts(limit)`, `explain(repo)`, `data_status()`.

```bash
claude mcp add hot-repos --scope user -- ~/Dev/hot-repos/.venv/bin/hotrepos serve
```

## Troubleshooting

| Message | Fix |
|---|---|
| `GitHub rejected GITHUB_TOKEN (401 Bad credentials)` | The token was copied incompletely or has expired. Copy it again, or create a new one. |
| `GitHub API 403 on .../stargazers: Resource not accessible by personal access token` | Edit the token: set Repository access to **Public repositories**, or use a classic token with no scopes. |
| `GitHub API 403 ...: ...SAML enforcement...` | That organization requires SSO: on the token's page, choose **Configure SSO** and authorize it. |
| `GitHub secondary rate limit: wait N seconds` | GitHub asked for a pause. Run again after the wait. |
| `GitHub rate limit reached` | Out of requests for this hour. Without a token that's 60; set one for 5,000. |
| `Port 8501 is in use` | The UI is already running at http://127.0.0.1:8501. Use it, stop it, or pass `--port 8502`. |
| UI says "GitHub token: not set" | Restart the UI from a terminal where `echo ${#GITHUB_TOKEN}` prints more than 0. |

When GitHub refuses the stargazer list for any other reason, `measure` prints GitHub's message once and
carries on with star-count snapshots, so a run still produces data.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## License

[MIT](LICENSE)

## Not built yet

Hacker News and Reddit mentions, fake-star detection, LLM summaries and "why it's hot", personal interests and
feedback, and a daily digest. See the design notes in the original proposal.
