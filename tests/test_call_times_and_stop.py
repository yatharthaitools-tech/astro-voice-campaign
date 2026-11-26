from datetime import timedelta

from voicecampaign import db, voice
from tests.test_flow import by_name, make_campaign
from tests.test_plivo import FakeResp, plivo_env  # noqa: F401  (fixture)


def _set(call_id, **cols):
    sets = ', '.join(f'{k}=%s' for k in cols)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE campaign_calls SET {sets} WHERE id=%s", list(cols.values()) + [call_id])


def test_answer_and_end_times_are_recorded_and_shown(post, client, drain):
    cid = make_campaign(post)
    assert db.campaign_timing(cid)['state'] == 'draft'
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()   # mock provider: instant outcomes
    rows = by_name(cid)

    asha, chitra, gita = rows['Asha'], rows['Chitra'], rows['Gita']      # answered 20 s / no answer / failed
    assert asha['answered_at'] and asha['ended_at']
    assert abs((asha['ended_at'] - asha['answered_at']) - timedelta(seconds=20)) < timedelta(seconds=2)
    assert chitra['answered_at'] is None and chitra['ended_at']
    assert gita['answered_at'] is None and gita['ended_at']

    t = db.campaign_timing(cid)
    assert t['state'] == 'finished'
    assert t['started_at'] == db.get_campaign(cid)['first_launched_at']
    assert t['ended_at'] == max(r['ended_at'] for r in rows.values() if r['ended_at'])

    page = client.get(f'/campaigns/{cid}').get_data(as_text=True)
    assert 'Answered at' in page and 'Finished' in page and 'Started ' in page
    assert 'Stop campaign' not in page                                    # nothing left to stop

    csv = client.get(f'/campaigns/{cid}/report.csv').get_data(as_text=True).splitlines()
    header = csv[0].lstrip('﻿').split(',')
    col = header.index('answered_at_ist')
    by_expert = {line.split(',')[0]: line.split(',') for line in csv[1:]}
    assert by_expert['Asha'][col].count(':') == 2                          # e.g. 2026-11-26 17:40:12
    assert by_expert['Chitra'][col] == ''


def test_plivo_answer_webhook_stamps_answer_time(plivo_env, post, app):  # noqa: F811
    cid = make_campaign(post)
    call = by_name(cid)['Asha']
    _set(call['id'], status='ringing', provider_sid='u1')
    anon, tok = app.test_client(), voice.callback_token(call['id'])

    anon.get(f"/webhooks/plivo/answer/{call['id']}?t={tok}&CallStatus=in-progress")
    picked_up = db.get_call(call['id'])
    assert picked_up['answered_at'] and picked_up['ended_at'] is None

    anon.post(f"/webhooks/plivo/status/{call['id']}?t={tok}", data={'CallStatus': 'completed', 'Duration': '40'})
    done = db.get_call(call['id'])
    assert done['answered_at'] == picked_up['answered_at']                # not overwritten by the hangup
    assert done['ended_at'] >= done['answered_at']


def test_completed_without_answer_event_estimates_answer_time(plivo_env, post, app):  # noqa: F811
    cid = make_campaign(post)
    call = by_name(cid)['Asha']
    _set(call['id'], status='ringing', provider_sid='u1')
    tok = voice.callback_token(call['id'])
    app.test_client().post(f"/webhooks/plivo/status/{call['id']}?t={tok}",
                           data={'CallStatus': 'completed', 'Duration': '60'})
    done = db.get_call(call['id'])
    assert abs((done['ended_at'] - done['answered_at']) - timedelta(seconds=60)) < timedelta(seconds=2)


def test_unanswered_call_has_no_answer_time(plivo_env, post, app):  # noqa: F811
    cid = make_campaign(post)
    call = by_name(cid)['Asha']
    _set(call['id'], status='ringing', provider_sid='u1')
    tok = voice.callback_token(call['id'])
    app.test_client().post(f"/webhooks/plivo/status/{call['id']}?t={tok}", data={'CallStatus': 'no-answer'})
    done = db.get_call(call['id'])
    assert done['status'] == 'no_answer' and done['answered_at'] is None and done['ended_at']


def test_stop_campaign_cancels_queue_and_lets_live_call_finish(plivo_env, post, client, drain, app, monkeypatch):  # noqa: F811
    dialed = []
    monkeypatch.setattr(voice.requests, 'post',
                        lambda url, **kw: dialed.append(kw['json']['to']) or FakeResp(201, {'request_uuid': f'u{len(dialed)}'}))
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    assert len(dialed) == 1                                               # one on the line, the rest queued
    t = db.campaign_timing(cid)
    assert t['state'] == 'running' and t['queued'] == 3
    assert 'Stop campaign' in client.get(f'/campaigns/{cid}').get_data(as_text=True)

    r = post(f'/campaigns/{cid}/stop')
    assert r.status_code == 302
    rows = by_name(cid).values()
    assert sum(r['status'] == 'queued' for r in rows) == 0
    live = next(r for r in rows if r['status'] == 'ringing')
    t = db.campaign_timing(cid)
    assert t['state'] == 'stopping' and t['live'] == 1 and t['ended_at'] is None

    drain(); drain()
    assert len(dialed) == 1                                               # nothing new is dialed after stop

    tok = voice.callback_token(live['id'])
    app.test_client().post(f"/webhooks/plivo/status/{live['id']}?t={tok}", data={'CallStatus': 'completed', 'Duration': '30'})
    t = db.campaign_timing(cid)
    assert t['state'] == 'stopped' and t['ended_at'] == db.get_campaign(cid)['stopped_at']
    page = client.get(f'/campaigns/{cid}').get_data(as_text=True)
    assert 'Stopped' in page and 'Stop campaign' not in page

    # Launching again resumes with the experts who were never called.
    post(f'/campaigns/{cid}/launch', data={'language': 'all'})
    assert db.get_campaign(cid)['stopped_at'] is None
    assert db.campaign_timing(cid)['state'] == 'running'
    drain()
    assert len(dialed) == 2 and len(set(dialed)) == 2                     # next expert, never the same one twice


def test_existing_database_gets_new_columns(app):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE campaign_calls DROP COLUMN answered_at, DROP COLUMN ended_at")
        cur.execute("ALTER TABLE campaigns DROP COLUMN stopped_at")
    db.init_db()
    cols = {r['column_name'] for r in db._q(
        "SELECT column_name FROM information_schema.columns WHERE table_name IN ('campaigns','campaign_calls')")}
    assert {'answered_at', 'ended_at', 'stopped_at'} <= cols
