import threading

import pytest

from voicecampaign import db, voice
from tests.test_flow import by_name, make_campaign
from tests.test_plivo import FakeResp, plivo_env  # noqa: F401  (fixture)


@pytest.fixture
def plivo_calls(plivo_env, monkeypatch):
    sent = []

    def fake_post(url, **kw):
        sent.append(kw['json']['to'])
        return FakeResp(201, {'request_uuid': f'u{len(sent)}'})
    monkeypatch.setattr(voice.requests, 'post', fake_post)
    return sent


def finish(call_id):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE campaign_calls SET status='completed', duration_seconds=30 WHERE id=%s", (call_id,))


def test_one_call_at_a_time(post, drain, plivo_calls):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'})  # 4 callable experts queued
    drain()
    assert len(plivo_calls) == 1, 'second call must wait while the first is still live'
    assert db.count_active() == 1
    drain(); drain()
    assert len(plivo_calls) == 1  # still waiting, nothing dialed on repeat

    live = next(r for r in by_name(cid).values() if r['status'] == 'ringing')
    finish(live['id']); drain()
    assert len(plivo_calls) == 2 and db.count_active() == 1  # next one starts only after the first ended

    for _ in range(3):
        live = next(r for r in by_name(cid).values() if r['status'] == 'ringing')
        finish(live['id']); drain()
    assert len(plivo_calls) == 4 and len(set(plivo_calls)) == 4  # each expert exactly once


def test_limit_is_configurable_to_plivo_channels(post, drain, plivo_calls, monkeypatch):
    monkeypatch.setenv('MAX_CONCURRENT_CALLS', '2')
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    assert len(plivo_calls) == 2 and db.count_active() == 2


def test_bad_limit_value_falls_back_to_one(monkeypatch):
    for v in ('0', '-3', 'abc', ''):
        monkeypatch.setenv('MAX_CONCURRENT_CALLS', v)
        assert db.max_concurrent() == 1


def test_parallel_workers_cannot_exceed_the_limit(post, plivo_calls):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'})
    claimed, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        c = db.claim_next_call(limit=1)
        if c:
            claimed.append(c['id'])
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(claimed) == 1 and db.count_active() == 1


def test_lost_callback_does_not_freeze_the_queue(post, drain, plivo_calls, monkeypatch):
    monkeypatch.setenv('MAX_CALL_SECONDS', '60')
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    first = next(r for r in by_name(cid).values() if r['status'] == 'ringing')
    drain(); assert len(plivo_calls) == 1  # not stale yet
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE campaign_calls SET updated_at = now() - interval '2 minutes' WHERE id=%s", (first['id'],))
    drain()
    done = db.get_call(first['id'])
    assert done['status'] == 'failed' and 'released' in done['error']
    assert len(plivo_calls) == 2  # queue moved on to the next expert
