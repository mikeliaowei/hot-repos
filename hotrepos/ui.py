"""Streamlit UI for hot-repos. Run with `hotrepos ui`."""

import pandas as pd
import streamlit as st

from hotrepos import cli, db, mcp_server, score

SOURCE_HELP = {
    'measured': 'exact, from GitHub stargazer timestamps',
    'snapshot': 'difference between two star counts',
    'estimated': 'GH Archive sample, scaled by the learned sample rate',
    'sampled': 'raw GH Archive sample, not scaled',
}

st.set_page_config(page_title='Hot Repos', page_icon='🔥', layout='wide')


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
        st.info('Nothing here yet. Run `hotrepos update`, or loosen the filters.')
        return None
    df = pd.DataFrame([{
        'repo': r['name'], 'url': r['url'], 'stars gained': r['stars_gained'], 'source': r['source'],
        'acceleration': r['acceleration'] if r['stars_prev_window'] is not None else None,
        'total stars': r['total_stars'], 'language': r['language'], 'created': (r['created_at'] or '')[:10],
        'description': r['description'], 'score': r['hot_score'], 'flags': ', '.join(r['flags']),
    } for r in rows])
    event = st.dataframe(
        df, key=key, hide_index=True, width='stretch', on_select='rerun', selection_mode='single-row',
        column_config={
            'url': st.column_config.LinkColumn('link', display_text='open'),
            'stars gained': st.column_config.NumberColumn(format='+%d'),
            'acceleration': st.column_config.NumberColumn('× prior', format='%.2f'),
            'total stars': st.column_config.NumberColumn(format='%d'),
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
    st.subheader(name)
    st.caption(m.get('description') or '')
    s7, s1 = x['scores'].get('7d'), x['scores'].get('1d')
    c = st.columns(4)
    c[0].metric('Stars, last 7 days', f"+{s7['stars_gained']:,}" if s7 else '–', help=s7 and SOURCE_HELP[s7['source']])
    c[1].metric('Stars, last 24 hours', f"+{s1['stars_gained']:,}" if s1 else '–', help=s1 and SOURCE_HELP[s1['source']])
    c[2].metric('Total stars', f"{m['stars']:,}" if m.get('stars') else '–')
    c[3].metric('Created', (m.get('created_at') or '–')[:10])
    days = (x['stars_per_day'] or {}).get('days')
    if days:
        source = x['stars_per_day']['source']
        st.caption(f'Stars per day, last 14 days ({"exact" if source == "measured" else "GH Archive sample, unscaled"})')
        st.bar_chart(pd.Series(days, name='stars'), height=220)
    if s7:
        st.caption('Score parts (7d): ' + ' + '.join(f'{k} {v}' for k, v in s7['score_parts'].items())
                   + f" = {s7['hot_score']}")
    st.link_button('Open on GitHub', f'https://github.com/{name}')


st.title('🔥 Hot Repos')
status = cli.status(db.connect())
ok = status['archive_hours'].get('ok') or {}
c = st.columns(4)
c[0].metric('GH Archive hours', ok.get('count', 0), help=f"{ok.get('from')} → {ok.get('to')}" if ok else None)
c[1].metric('Repos known', f"{status['repos_known']:,}")
c[2].metric('Measured exactly', status['repos_measured'])
c[3].metric('GitHub token', 'set' if status['github_token'] else 'not set')
if not status['github_token']:
    st.caption('Without a token the numbers are estimates. Set GITHUB_TOKEN and run `hotrepos update` for exact counts.')

with st.sidebar:
    st.header('Filters')
    window = st.radio('Window', list(score.WINDOWS), index=1, format_func={'1d': 'Last 24 hours', '7d': 'Last 7 days'}.get)
    language = st.selectbox('Language', ['Any'] + languages(window))
    topic = st.text_input('Topic', placeholder='e.g. agents')
    include_lists = st.toggle('Include lists, forks, archived')
    limit = st.slider('Rows', 10, 200, 50, step=10)
    if st.button('Reload data'):
        st.cache_data.clear()

trending_tab, breakouts_tab = st.tabs(['Trending', 'Breakouts'])
with trending_tab:
    picked = table(load('trending', window, None if language == 'Any' else language, topic.strip() or None,
                        limit, include_lists), 'trending')
    st.caption('Select a row to see its details.')
    if picked:
        detail(picked)
with breakouts_tab:
    st.caption('Repos under 60 days old, fastest-rising first. Filters do not apply here.')
    picked = table(load('breakouts', limit=limit), 'breakouts')
    if picked:
        detail(picked)
