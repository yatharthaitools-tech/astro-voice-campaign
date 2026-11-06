import os

os.environ.setdefault('DATABASE_URL', 'postgresql://voice:voice@localhost:5432/voice_test')
os.environ['VOICE_PROVIDER'] = 'mock'
os.environ['ADMIN_EMAIL'] = 'admin@example.com'
os.environ['ADMIN_PASSWORD'] = 'pw-for-tests'
os.environ['PUBLIC_BASE_URL'] = 'https://voice.test'

import re

import pytest

from voicecampaign import db, dispatcher
from voicecampaign.app import create_app


@pytest.fixture(autouse=True)
def _no_background_dialer(monkeypatch):
    # Tests drive the dialer synchronously via drain(); no racing thread.
    monkeypatch.setattr(dispatcher, 'ensure_worker', lambda: True)


@pytest.fixture
def app():
    a = create_app()
    a.testing = True
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE campaigns, campaign_calls, languages RESTART IDENTITY CASCADE")
    db.init_db()
    return a


@pytest.fixture
def client(app):
    c = app.test_client()
    page = c.get('/login').get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    c.post('/login', data={'email': 'admin@example.com', 'password': 'pw-for-tests', '_csrf': token})
    c.get('/')  # a page load issues the post-login CSRF token, as in a browser
    return c


@pytest.fixture
def post(client):
    def _post(url, **kw):
        with client.session_transaction() as s:
            kw.setdefault('data', {})['_csrf'] = s['_csrf']
        return client.post(url, **kw)
    return _post


@pytest.fixture
def drain():
    return lambda: dispatcher.drain(pace=False)
