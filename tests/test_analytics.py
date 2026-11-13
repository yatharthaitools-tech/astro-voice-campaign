import re
from tests.test_flow import make_campaign, add_audio, CSV
import io


def test_analytics_is_default_landing_and_empty_state_renders(client):
    assert client.get('/').status_code == 200
    page = client.get('/').get_data(as_text=True)
    assert 'Platform analytics' in page and 'No data yet' in page


def test_login_lands_on_analytics(app):
    c = app.test_client()
    tok = re.search(r'name="_csrf" value="([^"]+)"', c.get('/login').get_data(as_text=True)).group(1)
    r = c.post('/login', data={'email': 'admin@example.com', 'password': 'pw-for-tests', '_csrf': tok})
    assert r.headers['Location'].endswith('/')
    assert 'Platform analytics' in c.get('/').get_data(as_text=True)


def test_campaigns_list_moved_and_flows_still_work(client, post):
    cid = make_campaign(post)
    page = client.get('/campaigns').get_data(as_text=True)
    assert 'New campaign' in page and 'Diwali' in page


def test_platform_numbers_across_two_campaigns(post, drain, client):
    from voicecampaign import db
    cid1 = make_campaign(post)
    post(f'/campaigns/{cid1}/launch', data={'language': 'all'}); drain()
    r = post('/campaigns', data={'name': 'Second', 'rate': '1', 'csv': (io.BytesIO(CSV), 'y.csv')},
             content_type='multipart/form-data')
    cid2 = int(r.headers['Location'].rsplit('/', 1)[1])
    post(f'/campaigns/{cid2}/launch', data={'language': 'tamil'}); drain()
    a = db.platform_analytics()
    t = a['totals']
    assert t['campaigns'] == 2 and t['experts'] == 16
    # campaign 1: 4 placed (Asha 20s ans, Bala 55s ans, Chitra no-answer, Gita failed); campaign 2: Bala only
    assert (t['placed'], t['answered'], t['unanswered'], t['failed']) == (5, 3, 1, 1)
    # billed minutes: 1 + 1 + 1 ; rates 2 (campaign 1) and 1 (campaign 2) -> 2 + 2 + 1
    assert float(t['cost']) == 5.0
    tamil = next(r for r in a['by_language'] if r['key'] == 'tamil')
    assert tamil['placed'] == 2 and tamil['answered'] == 2
    reasons = {r['reason']: r['n'] for r in a['skipped']}
    assert reasons == {'missing_language': 2, 'no_consent': 2, 'duplicate': 2}
    assert sum(r['placed'] for r in a['trend']) == 5
    page = client.get('/').get_data(as_text=True)
    assert 'Diwali' in page and 'Second' in page and '5.00' in page


def test_date_filter_excludes_old_window(post, drain, client):
    cid = make_campaign(post)
    post(f'/campaigns/{cid}/launch', data={'language': 'all'}); drain()
    page = client.get('/?from=2000-01-01&to=2000-01-02').get_data(as_text=True)
    assert 'No data yet' in page
    assert 'Diwali' not in page


def test_analytics_requires_login(app):
    assert app.test_client().get('/').status_code == 302
