import io

from voicecampaign import db, voice

MP3 = b'ID3' + b'\x00' * 64
CSV = (b'expert_name,phone_number,language,consent_status\n'
       b'Asha,9876543210,Hindi,Yes\n'      # ends 0 -> answered
       b'Bala,9876543215,Tamil,Yes\n'      # ends 5 -> answered
       b'Chitra,9876543212,Telugu,Yes\n'   # ends 2 -> no answer
       b'Dev,9876543216,Gujarati,Yes\n'    # language with no recording
       b'Esha,9876543217,,Yes\n'           # missing language
       b'Farid,9876543218,Hindi,No\n'      # no consent
       b'Asha again,9876543210,Hindi,Yes\n'  # duplicate
       b'Gita,9876543214,Hindi,Yes\n')     # ends 4 -> provider failure


def add_audio(post, key):
    return post(f'/languages/{key}/audio', data={'file': (io.BytesIO(MP3), f'{key}.mp3')},
                content_type='multipart/form-data')


def make_campaign(post):
    for k in ('hindi', 'tamil', 'telugu'):
        add_audio(post, k)
    r = post('/campaigns', data={'name': 'Diwali', 'csv': (io.BytesIO(CSV), 'x.csv'), 'rate': '2'},
             content_type='multipart/form-data')
    assert r.status_code == 302
    return int(r.headers['Location'].rsplit('/', 1)[1])


def by_name(cid):
    rows, _ = db.list_calls(cid, limit=100)
    return {r['expert_name']: r for r in rows}


def test_defaults_and_custom_language(post, client):
    assert {l['key'] for l in db.list_languages()} >= {
        'hindi', 'english', 'tamil', 'telugu', 'malayalam', 'kannada', 'bengali', 'marathi'}
    post('/languages/add', data={'name': 'Gujarati'})
    assert 'gujarati' in {l['key'] for l in db.list_languages()}


def test_audio_upload_validation_and_public_serving(post, client):
    bad = post('/languages/hindi/audio', data={'file': (io.BytesIO(b'not audio'), 'a.mp3')},
               content_type='multipart/form-data', follow_redirects=True)
    assert b'Upload an .mp3 or .wav' in bad.data
    add_audio(post, 'hindi')
    token = next(l for l in db.list_languages() if l['key'] == 'hindi')['audio_token']
    anon = client.application.test_client()  # provider fetches without a login
    r = anon.get(f'/audio/{token}.mp3')
    assert r.status_code == 200 and r.data == MP3
    assert anon.get('/audio/nope.mp3').status_code == 404
    bad_url = post('/languages/tamil/audio', data={'url': 'javascript:alert(1)'}, follow_redirects=True)
    assert b'full public URL' in bad_url.data


def test_preview_flags_and_mapping(post, client):
    cid = make_campaign(post)
    rows = by_name(cid)
    assert rows['Asha']['flag'] == 'ready'
    assert rows['Dev']['flag'] == 'no_recording'
    assert rows['Esha']['flag'] == 'missing_language'
    assert rows['Farid']['flag'] == 'no_consent'
    assert rows['Asha again']['flag'] == 'duplicate'
    s = {r['key']: r for r in db.preview_summary(cid)}
    assert (s['hindi']['experts'], s['hindi']['ready'], s['hindi']['flagged']) == (4, 2, 2)
    assert s['gujarati']['has_audio'] is False and s['gujarati']['ready'] == 0
    page = client.get(f'/campaigns/{cid}').get_data(as_text=True)
    assert 'Recording missing' in page and 'Language missing' in page


def test_launch_calls_only_valid_and_never_twice(post, drain):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'})
    drain()
    rows = by_name(cid)
    for name in ('Dev', 'Esha', 'Farid', 'Asha again'):
        assert rows[name]['status'] == 'pending' and rows[name]['provider_sid'] is None
    assert rows['Asha']['status'] == 'completed' and rows['Asha']['duration_seconds'] == 20
    assert rows['Asha']['recording_used'].endswith('.mp3') and rows['Asha']['recording_used'].startswith('https://voice.test/audio/')
    assert rows['Chitra']['status'] == 'no_answer' and rows['Gita']['status'] == 'failed'
    # second launch (double click / re-submit) queues nothing and dials nothing
    assert db.queue_calls(cid) == 0
    sids = {r['provider_sid'] for r in by_name(cid).values() if r['provider_sid']}
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    assert {r['provider_sid'] for r in by_name(cid).values() if r['provider_sid']} == sids


def test_each_language_gets_its_own_recording(post, drain):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    rows = by_name(cid)
    tokens = {l['key']: l['audio_token'] for l in db.list_languages()}
    assert tokens['hindi'] in rows['Asha']['recording_used']
    assert tokens['tamil'] in rows['Bala']['recording_used']
    assert tokens['telugu'] in rows['Chitra']['recording_used']


def test_launch_single_language_then_rest(post, drain):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'tamil'}); drain()
    rows = by_name(cid)
    assert rows['Bala']['status'] == 'completed'
    assert rows['Asha']['status'] == 'pending' and rows['Chitra']['status'] == 'pending'
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    assert by_name(cid)['Asha']['status'] == 'completed'
    assert len([r for r in by_name(cid).values() if r['provider_sid']]) == 4  # Dev has no recording


def test_test_calls_then_full_launch_skips_them(post, drain):
    cid = make_campaign(post)
    ids = [by_name(cid)['Bala']['id']]
    post(f'/campaigns/{cid}/test', data={'call_ids': ids}); drain()
    bala = by_name(cid)['Bala']
    assert bala['is_test'] and bala['status'] == 'completed'
    sid = bala['provider_sid']
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    assert by_name(cid)['Bala']['provider_sid'] == sid
    # an expert who can't be called can't be force-tested either
    assert db.queue_calls(cid, call_ids=[by_name(cid)['Dev']['id']], is_test=True) == 0


def test_recording_uploaded_after_import_unblocks_language(post, drain):
    cid = make_campaign(post)
    post('/languages/gujarati/audio', data={'url': 'https://cdn.example.com/guj.mp3'})
    assert by_name(cid)['Dev']['flag'] == 'ready'
    post(f'/campaigns/{cid}/launch', data={'language': 'gujarati'}); drain()
    dev = by_name(cid)['Dev']
    assert dev['status'] == 'completed' and dev['recording_used'] == 'https://cdn.example.com/guj.mp3'


def test_report_numbers_and_csv(post, client, drain):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    rep = {r['key']: r for r in db.report_by_language(cid)}
    # Hindi: Asha answered (20s), Gita failed
    assert (rep['hindi']['placed'], rep['hindi']['answered'], rep['hindi']['failed']) == (2, 1, 1)
    assert float(rep['hindi']['avg_duration']) == 20 and rep['hindi']['billed_minutes'] == 1
    assert rep['telugu']['unanswered'] == 1 and rep['telugu']['billed_minutes'] == 0
    assert rep['tamil']['billed_minutes'] == 1  # 55s rounds up to 1 minute
    csv_text = client.get(f'/campaigns/{cid}/report.csv').get_data(as_text=True)
    lines = csv_text.lstrip('﻿').splitlines()
    assert lines[0].startswith('expert_name,phone_number,language,recording_used,call_status')
    assert len(lines) == 1 + 8
    assert 'Esha' in csv_text and 'Skipped' in csv_text and 'Language missing' in csv_text
    page = client.get(f'/campaigns/{cid}').get_data(as_text=True)
    assert '4.00' in page  # 2 billed minutes (Asha 20s, Bala 55s) x rate 2


def test_csv_formula_injection_is_neutralised(post, client):
    evil = (b'expert_name,phone_number,language,consent_status\n=HYPERLINK("http://x"),9876543210,Hindi,Yes\n')
    add_audio(post, 'hindi')
    r = post('/campaigns', data={'csv': (io.BytesIO(evil), 'e.csv')}, content_type='multipart/form-data')
    cid = int(r.headers['Location'].rsplit('/', 1)[1])
    assert '"\'=HYPERLINK' in client.get(f'/campaigns/{cid}/report.csv').get_data(as_text=True)


def test_auth_and_csrf(app, client):
    anon = app.test_client()
    assert anon.get('/').status_code == 302
    assert anon.get('/campaigns/1').status_code == 302
    assert client.post('/languages/add', data={'name': 'X'}).status_code == 400  # no CSRF token


def test_bad_csv_is_rejected_with_message(post):
    r = post('/campaigns', data={'csv': (io.BytesIO(b'a,b\n1,2\n'), 'x.csv')},
             content_type='multipart/form-data', follow_redirects=True)
    assert b'Missing column' in r.data and db.list_campaigns() == []


def test_launch_blocked_when_provider_not_configured(monkeypatch, post):
    monkeypatch.setenv('VOICE_PROVIDER', 'plivo')
    for k in ('PLIVO_AUTH_ID', 'PLIVO_AUTH_TOKEN', 'PLIVO_FROM_NUMBER'):
        monkeypatch.delenv(k, raising=False)
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'})
    assert all(r['status'] == 'pending' for r in by_name(cid).values())
