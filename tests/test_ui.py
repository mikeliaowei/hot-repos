import os
import tempfile
import unittest
from datetime import timedelta

from hotrepos import db

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # the UI is an optional extra
    AppTest = None

APP = os.path.join(os.path.dirname(__file__), '..', 'hotrepos', 'ui.py')


@unittest.skipIf(AppTest is None, 'streamlit not installed (pip install -e ".[ui]")')
class UITest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ['HOTREPOS_DB'] = os.path.join(self.dir.name, 'h.db')
        import streamlit as st
        st.cache_data.clear()  # cached results would leak between tests

    def tearDown(self):
        os.environ.pop('HOTREPOS_DB', None)
        self.dir.cleanup()

    def seed(self):
        conn, now = db.connect(), db.now_utc()
        db.upsert_repo(conn, {'name': 'new/rocket', 'description': 'Goes up', 'language': 'Rust', 'topics': 'agents',
                              'created_at': (now - timedelta(days=5)).isoformat(), 'stars': 900, 'forks': 1,
                              'homepage': None, 'archived': 0, 'is_fork': 0, 'updated_at': None})
        for i in range(14):
            conn.execute('INSERT INTO measured VALUES (?, ?, ?)',
                         ('new/rocket', db.hour_key(now - timedelta(days=i, hours=1)), 20))
        conn.execute("INSERT INTO measure_runs VALUES ('new/rocket', ?, 'all')", (now.isoformat(),))
        conn.commit()

    def run_app(self):
        at = AppTest.from_file(APP, default_timeout=30)
        at.run()
        self.assertFalse(at.exception, at.exception)
        return at

    def test_empty_database_shows_hint(self):
        at = self.run_app()
        self.assertIn('Nothing here yet', at.info[0].value)

    def test_lists_repo_and_filters_by_language(self):
        self.seed()
        at = self.run_app()
        df = at.dataframe[0].value
        self.assertEqual((df.iloc[0]['repo'], df.iloc[0]['stars gained'], df.iloc[0]['source']),
                         ('new/rocket', 140, 'measured'))
        self.assertEqual(at.metric[2].value, '1')  # measured exactly
        at.sidebar.selectbox[0].select('Rust').run()
        self.assertEqual(len(at.dataframe[0].value), 1)
        at.sidebar.text_input[0].input('nope').run()
        self.assertIn('Nothing here yet', at.info[0].value)

    def test_detail_view(self):
        self.seed()
        script = open(APP).read() + "\ndetail('new/rocket')\n"
        at = AppTest.from_string(script, default_timeout=30)
        at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertIn('new/rocket', [s.value for s in at.subheader])
        self.assertIn('+140', [m.value for m in at.metric])


if __name__ == '__main__':
    unittest.main()
