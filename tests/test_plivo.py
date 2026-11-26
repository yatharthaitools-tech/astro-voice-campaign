import io

import pytest

from voicecampaign import db, voice
from tests.test_flow import by_name, make_campaign


@pytest.fixture
def plivo_env(monkeypatch):
    for k, v in {'VOICE_PROVIDER': 'plivo', 'PLIVO_AUTH_ID': 'MAID', 'PLIVO_AUTH_TOKEN': 'tok',
                 'PLIVO_FROM_NUMBER': '+911234567890', 'SECRET_KEY': 'stable-secret'}.items():
        monkeypatch.setenv(k, v)


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


def test_plivo_requires_config(monkeypatch):
    monkeypatch.setenv('VOICE_PROVIDER', 'plivo')
    for k in ('PLIVO_AUTH_ID', 'PLIVO_AUTH_TOKEN', 'PLIVO_FROM_NUMBER', 'SECRET_KEY'):
        monkeypatch.delenv(k, raising=False)
    err = voice.check_ready()
    assert 'PLIVO_AUTH_ID' in err and 'SECRET_KEY' in err


def test_plivo_dispatch_request_shape(plivo_env, post, drain, monkeypatch):
    sent = []

    def fake_post(url, **kw):
        sent.append((url, kw))
        return FakeResp(201, {'message': 'call fired', 'request_uuid': 'uuid-%d' % len(sent)})
    monkeypatch.setattr(voice.requests, 'post', fake_post)
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'tamil'}); drain()
    url, kw = sent[0]
    assert url == 'https://api.plivo.com/v1/Account/MAID/Call/' and kw['auth'] == ('MAID', 'tok')
    body = kw['json']
    assert body['to'] == '919876543215' and body['from'] == '911234567890'
    bala = by_name(cid)['Bala']
    assert body['answer_url'].startswith(f"https://voice.test/webhooks/plivo/answer/{bala['id']}?t=")
    # Final status + duration arrive on hangup_url; callback_url is a recording hook, not a status one.
    assert body['hangup_url'].startswith(f"https://voice.test/webhooks/plivo/status/{bala['id']}?t=")
    assert body['hangup_method'] == 'POST' and 'callback_url' not in body
    assert bala['status'] == 'ringing' and bala['provider_sid'] == 'uuid-1'


def test_plivo_api_error_marks_failed_without_retry(plivo_env, post, drain, monkeypatch):
    calls = []
    monkeypatch.setattr(voice.requests, 'post',
                        lambda *a, **k: calls.append(1) or FakeResp(400, {'error': 'invalid number'}))
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'tamil'}); drain(); drain()
    bala = by_name(cid)['Bala']
    assert bala['status'] == 'failed' and 'invalid number' in bala['error'] and len(calls) == 1


def test_plivo_answer_plays_the_experts_language(plivo_env, post, app):
    cid = make_campaign(post)
    rows = by_name(cid)
    anon = app.test_client()
    tok = lambda r: voice.callback_token(r['id'])
    xml = anon.get(f"/webhooks/plivo/answer/{rows['Bala']['id']}?t={tok(rows['Bala'])}")
    tamil = next(l for l in db.list_languages() if l['key'] == 'tamil')['audio_token']
    assert xml.status_code == 200 and xml.mimetype == 'text/xml'
    assert f'<Play>https://voice.test/audio/{tamil}.mp3</Play>' in xml.get_data(as_text=True)
    assert tamil in db.get_call(rows['Bala']['id'])['recording_used']
    # the answer fetch carries CallStatus=in-progress: the call is now answered
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE campaign_calls SET status='ringing' WHERE id=%s", (rows['Bala']['id'],))
    anon.get(f"/webhooks/plivo/answer/{rows['Bala']['id']}?t={tok(rows['Bala'])}&CallStatus=in-progress&Event=StartApp")
    assert db.get_call(rows['Bala']['id'])['status'] == 'in_progress'
    # a language with no recording hangs up instead of playing nothing
    assert '<Hangup/>' in anon.get(f"/webhooks/plivo/answer/{rows['Dev']['id']}?t={tok(rows['Dev'])}").get_data(as_text=True)
    # wrong / missing token is refused
    assert anon.get(f"/webhooks/plivo/answer/{rows['Bala']['id']}?t=bad").status_code == 403
    assert anon.get(f"/webhooks/plivo/answer/{rows['Bala']['id']}").status_code == 403


def test_plivo_status_callbacks(plivo_env, post, app):
    cid = make_campaign(post)
    call = by_name(cid)['Asha']
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE campaign_calls SET status='ringing', provider_sid='u1' WHERE id=%s", (call['id'],))
    anon, t = app.test_client(), voice.callback_token(call['id'])
    path = f"/webhooks/plivo/status/{call['id']}?t={t}"
    assert anon.post(path.replace(t, 'x'), data={'CallStatus': 'completed'}).status_code == 403
    anon.post(path, data={'CallStatus': 'in-progress'})
    assert db.get_call(call['id'])['status'] == 'in_progress'
    anon.post(path, data={'CallStatus': 'completed', 'Duration': '75'})
    done = db.get_call(call['id'])
    assert done['status'] == 'completed' and done['duration_seconds'] == 75
    anon.post(path, data={'CallStatus': 'ringing'})  # late, out-of-order
    assert db.get_call(call['id'])['status'] == 'completed'


@pytest.mark.parametrize('plivo,ours', [('no-answer', 'no_answer'), ('timeout', 'no_answer'), ('busy', 'busy'),
                                        ('failed', 'failed'), ('cancel', 'canceled'), ('weird', None)])
def test_plivo_status_mapping(plivo, ours):
    assert voice.map_plivo_status(plivo) == ours


@pytest.mark.parametrize('setting,limit', [('1', 1), ('2', 2), ('0', 1)])
def test_dialer_keeps_at_most_max_concurrent_calls_live(plivo_env, post, drain, app, monkeypatch, setting, limit):
    """The dialer never has more than MAX_CONCURRENT_CALLS calls live, and the
    next expert is dialed only after Plivo's hangup webhook frees a line.
    0 is not "unlimited": it falls back to one call at a time."""
    from voicecampaign import dispatcher
    monkeypatch.setenv('MAX_CONCURRENT_CALLS', setting)
    monkeypatch.setattr(dispatcher.time, 'sleep', lambda _: pytest.fail('drain(pace=False) must not sleep'))
    others_live_at_dial, hangup_urls = [], []

    def fake_post(url, **kw):
        # The row being dialed is already 'dialing' (claimed), so subtract it.
        others_live_at_dial.append(db.count_active() - 1)
        hangup_urls.append(kw['json']['hangup_url'].replace('https://voice.test', ''))
        return FakeResp(201, {'message': 'call fired', 'request_uuid': 'uuid-%d' % len(hangup_urls)})
    monkeypatch.setattr(voice.requests, 'post', fake_post)

    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    assert len(hangup_urls) == limit, 'dialer should fill exactly the free lines, then stop'
    drain()
    assert len(hangup_urls) == limit, 'no new call while every line is busy'

    plivo, hung_up = app.test_client(), 0
    while db.has_queued() or db.count_active():
        assert plivo.post(hangup_urls[hung_up], data={'CallStatus': 'completed', 'Duration': '30'}).status_code == 204
        hung_up += 1
        drain()
    rows = list(by_name(cid).values())
    placed = [r for r in rows if r['provider_sid']]
    assert len(placed) == len(hangup_urls) == hung_up >= 3
    assert max(others_live_at_dial) == limit - 1      # lines were used up to the limit, never beyond
    assert all(r['status'] == 'completed' for r in placed)
