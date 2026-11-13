import pytest


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv('ADMIN_HOSTS', 'admin.internal.test')


def get(app, path, host):
    return app.test_client().get(path, headers={'Host': host})


def test_admin_ui_only_on_admin_host(app, gated):
    assert get(app, '/login', 'admin.internal.test').status_code == 200
    assert get(app, '/login', 'public.test').status_code == 404
    assert get(app, '/', 'public.test').status_code == 404
    assert get(app, '/campaigns/1', 'public.test').status_code == 404


def test_provider_endpoints_still_work_on_public_host(app, gated):
    assert get(app, '/healthz', 'public.test').status_code == 200
    assert get(app, '/audio/nope.mp3', 'public.test').status_code == 404  # reaches the view (unknown token)
    # webhook reaches its own auth (403 = token check), not the host gate's 404
    assert get(app, '/webhooks/plivo/answer/1?t=x', 'public.test').status_code == 403
    assert app.test_client().post('/webhooks/plivo/status/1?t=x', headers={'Host': 'public.test'}).status_code == 403


def test_unset_means_no_restriction(app, monkeypatch):
    monkeypatch.delenv('ADMIN_HOSTS', raising=False)
    assert get(app, '/login', 'anything.test').status_code == 200
