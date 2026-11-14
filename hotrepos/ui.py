"""Streamlit UI for hot-repos. Run with `hotrepos ui`, which also applies the theme (cli.THEME)."""

import pandas as pd
import streamlit as st

from hotrepos import cli, db, mcp_server, score

SOURCE_HELP = {
    'measured': 'exact, from GitHub stargazer timestamps',
    'snapshot': 'difference between two star counts',
    'estimated': 'GH Archive sample, scaled by the learned sample rate',
    'sampled': 'raw GH Archive sample, not scaled',
}
SOURCE_COLOR = {'measured': 'green', 'snapshot': 'blue', 'estimated': 'orange', 'sampled': 'gray'}
WINDOW_LABELS = {'1d': 'Last 24 hours', '7d': 'Last 7 days'}

# Streamlit draws the page; these only refine its text. Headings wrap evenly and captions avoid one-word last
# lines. Browsers without text-wrap support ignore it and wrap as usual.
STYLE = """
<style>
  h1, h2, h3 { text-wrap: balance; }
  [data-testid="stCaptionContainer"] p { text-wrap: pretty; }
  .hero h1 { font-size: clamp(2rem, 1.4rem + 2.4vw, 3rem); letter-spacing: -0.02em; margin: 0; padding: 0; }
  .hero p { margin: 0.25rem 0 0; opacity: 0.7; font-size: 1.05rem; text-wrap: pretty; }
</style>
"""

st.set_page_config(page_title='Hot Repos', page_icon='🔥', layout='wide')
st.html(STYLE)


@st.cache_data(ttl=60)
def load(kind, window='7d', language=None, topic=None, limit=50, include_lists=False):
    conn = db.connect()
    if kind == 'breakouts':
        return score.breakouts(conn, limit)
    return score.trending(conn, window, language, topic, limit, include_lists)


@st.cache_data(ttl=60)
def languages(window):
    return sorted({r['language'] for r in score.compute(db.connect(), window) if r['language']})


def table(rows, key):
    if not rows:
        st.info('Nothing here yet. Run `hotrepos update`, or loosen the filters.', icon=':material/inbox:')
        return None
    head, warning = score.window_note(rows[0]['window'], rows[0]['window_end'],
                                     hours_loaded=rows[0]['window_hours_loaded'])
    st.caption(f':material/schedule: {head} · select a row for details')
    if warning:
        st.warning(warning, icon=':material/warning:')
    df = pd.DataFrame([{
        'repo': r['name'], 'url': r['url'], 'score': r['hot_score'], 'stars gained': r['stars_gained'],
        'source': r['source'], 'acceleration': r['acceleration'] if r['stars_prev_window'] is not None else None,
        'total stars': r['total_stars'], 'language': r['language'] or '', 'created': (r['created_at'] or '')[:10],
        'description': r['description'] or '', 'flags': ', '.join(r['flags']),
    } for r in rows])
    event = st.dataframe(
        df, key=key, hide_index=True, width='stretch', height=min(38 + 35 * len(df), 640),
        on_select='rerun', selection_mode='single-row',
        column_config={
            'repo': st.column_config.TextColumn(pinned=True),
            'url': st.column_config.LinkColumn('link', display_text='open ↗', width='small'),
            # The bar spans this table's scores, so the gaps between rows show; the number is the score itself.
            'score': st.column_config.ProgressColumn(format='%.1f', min_value=float(df['score'].min()) * 0.9,
                                                     max_value=float(df['score'].max()) or 1, width='small'),
            'stars gained': st.column_config.NumberColumn('★ gained', format='+%d'),
            'source': st.column_config.TextColumn(help='; '.join(f'{k}: {v}' for k, v in SOURCE_HELP.items())),
            'acceleration': st.column_config.NumberColumn('× prior', format='%.1f×',
                                                          help='Stars gained compared with the window before'),
            'total stars': st.column_config.NumberColumn('★ total', format='compact'),
            'description': st.column_config.TextColumn(width='large'),
        },
    )
    picked = event.selection.rows
    return rows[picked[0]]['name'] if picked else None


def detail(name):
    x = mcp_server.explain(name)
    if 'error' in x:
        st.warning(x['error'])
        return
    m = x['metadata'] or {}
    s7, s1 = x['scores'].get('7d'), x['scores'].get('1d')
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment='center')
        top.subheader(name, anchor=False)
        top.link_button('Open on GitHub', f'https://github.com/{name}', icon=':material/open_in_new:')
        if m.get('description'):
            st.write(m['description'])
        tags = st.container(horizontal=True, gap='small')
        if m.get('language'):
            tags.badge(m['language'], icon=':material/code:', color='violet')
        if s7:
            tags.badge(f"7d count: {s7['source']}", color=SOURCE_COLOR.get(s7['source'], 'gray'),
                       help=SOURCE_HELP[s7['source']])
            for flag in s7.get('flags') or []:
                tags.badge(flag, color='gray')
        c = st.columns(4)
        c[0].metric('Stars, last 7 days', f"+{s7['stars_gained']:,}" if s7 else '–', border=True,
                    help=s7 and SOURCE_HELP[s7['source']])
        c[1].metric('Stars, last 24 hours', f"+{s1['stars_gained']:,}" if s1 else '–', border=True,
                    help=s1 and SOURCE_HELP[s1['source']])
        c[2].metric('Total stars', f"{m['stars']:,}" if m.get('stars') else '–', border=True)
        c[3].metric('Created', (m.get('created_at') or '–')[:10], border=True)
        days = (x['stars_per_day'] or {}).get('days')
        if days:
            source = x['stars_per_day']['source']
            st.caption(f'Stars per day, last 14 days ({"exact" if source == "measured" else "GH Archive sample, unscaled"})')
            st.bar_chart(pd.Series(days, name='stars'), height=220, color=cli.THEME['primaryColor'])
        if s7:
            st.caption('Score parts (7d): ' + ' + '.join(f'{k} {v}' for k, v in s7['score_parts'].items())
                       + f" = {s7['hot_score']}")


st.html('<div class="hero"><h1>🔥 Hot Repos</h1>'
        '<p>GitHub repositories gaining stars fastest right now, from GH Archive and the GitHub API.</p></div>')

status = cli.status(db.connect())
ok = status['archive_hours'].get('ok') or {}
c = st.columns(4)
c[0].metric('GH Archive hours', ok.get('count', 0), border=True, help=f"{ok.get('from')} → {ok.get('to')}" if ok else None)
c[1].metric('Repos known', f"{status['repos_known']:,}", border=True)
c[2].metric('Measured exactly', status['repos_measured'], border=True)
c[3].metric('GitHub token', 'set' if status['github_token'] else 'not set', border=True)
if not status['github_token']:
    st.caption('Without a token the numbers are estimates. Run `gh auth login` (or set GITHUB_TOKEN), then '
               '`hotrepos update` for exact counts.')

with st.container(border=True):
    f = st.columns([1.3, 1, 1.2, 0.5], vertical_alignment='bottom')
    window = f[0].segmented_control('Window', list(score.WINDOWS), default='7d', required=True,
                                    format_func=WINDOW_LABELS.get)
    language = f[1].selectbox('Language', ['Any'] + languages(window), key='language')
    topic = f[2].text_input('Topic', placeholder='e.g. agents', icon=':material/search:', key='topic')
    with f[3].popover('More', icon=':material/tune:', width='stretch'):
        include_lists = st.toggle('Include lists, forks, archived')
        limit = st.slider('Rows', 10, 200, 50, step=10)
        if st.button('Reload data', icon=':material/refresh:'):
            st.cache_data.clear()

trending_tab, breakouts_tab = st.tabs([':material/trending_up: Trending', ':material/rocket_launch: Breakouts'])
with trending_tab:
    picked = table(load('trending', window, None if language == 'Any' else language, topic.strip() or None,
                        limit, include_lists), 'trending')
    if picked:
        detail(picked)
with breakouts_tab:
    st.caption('Repos under 60 days old, fastest-rising first. Only the row count applies here.')
    picked = table(load('breakouts', limit=limit), 'breakouts')
    if picked:
        detail(picked)
