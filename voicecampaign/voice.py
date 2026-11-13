"""Telephony providers. A provider turns (phone, audio URL) into a placed call.

`mock` places nothing and returns a simulated outcome — the default, so the app
can be exercised end to end without spending money. `plivo` places real calls
that play the language's recording; progress/duration come back through the
status webhook (see app.py)."""
import hashlib
import hmac
import os
from dataclasses import dataclass
from xml.sax.saxutils import escape

import requests


@dataclass
class CallResult:
    sid: str
    status: str              # a campaign_calls.status value
    duration: int = None     # only when the outcome is already final (mock)


class ProviderError(Exception):
    pass


def provider_name():
    return os.environ.get('VOICE_PROVIDER', 'mock').strip().lower()


def public_base_url():
    return os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')


REQUIRED_ENV = {
    'plivo': ('PLIVO_AUTH_ID', 'PLIVO_AUTH_TOKEN', 'PLIVO_FROM_NUMBER', 'PUBLIC_BASE_URL', 'SECRET_KEY'),
}


def check_ready():
    """Returns an error string if real calling can't work, else None."""
    name = provider_name()
    if name == 'mock':
        return None
    if name not in REQUIRED_ENV:
        return f"Unknown VOICE_PROVIDER '{name}'."
    missing = [k for k in REQUIRED_ENV[name] if not os.environ.get(k, '').strip()]
    return ('Set ' + ', '.join(missing) + ' before launching.') if missing else None


def place_call(call, audio_url):
    name = provider_name()
    if name == 'mock':
        return _mock(call)
    return _plivo(call)


# --- Plivo ----------------------------------------------------------------
# Plivo fetches the call's instructions from answer_url when the expert picks
# up, so the recording is resolved by language at that moment (app.py's
# plivo_answer). Webhooks are authenticated with an unguessable per-call token
# in the URL (HMAC of the call id under SECRET_KEY) rather than a signature.

def callback_token(call_id):
    key = os.environ.get('SECRET_KEY', '').encode()
    return hmac.new(key, f"plivo-cb:{call_id}".encode(), hashlib.sha256).hexdigest()[:32]


def valid_callback_token(call_id, token):
    return bool(os.environ.get('SECRET_KEY')) and hmac.compare_digest(callback_token(call_id), token or '')


def plivo_answer_url(call_id):
    return f"{public_base_url()}/webhooks/plivo/answer/{call_id}?t={callback_token(call_id)}"


def plivo_status_url(call_id):
    return f"{public_base_url()}/webhooks/plivo/status/{call_id}?t={callback_token(call_id)}"


def plivo_xml_play(audio_url):
    return f"<Response><Play>{escape(audio_url)}</Play></Response>"


PLIVO_HANGUP_XML = "<Response><Hangup/></Response>"

_PLIVO_STATUS = {
    'ringing': 'ringing', 'in-progress': 'in_progress', 'completed': 'completed',
    'busy': 'busy', 'no-answer': 'no_answer', 'timeout': 'no_answer',
    'failed': 'failed', 'cancel': 'canceled', 'canceled': 'canceled',
}


def map_plivo_status(s):
    return _PLIVO_STATUS.get((s or '').strip().lower())


def _plivo(call):
    auth_id, token = os.environ['PLIVO_AUTH_ID'], os.environ['PLIVO_AUTH_TOKEN']
    try:
        resp = requests.post(
            f"https://api.plivo.com/v1/Account/{auth_id}/Call/",
            auth=(auth_id, token), timeout=15,
            json={
                'from': os.environ['PLIVO_FROM_NUMBER'].lstrip('+'),
                'to': call['phone_e164'].lstrip('+'),
                'answer_url': plivo_answer_url(call['id']),
                'answer_method': 'GET',
                # Plivo reports the final outcome (CallStatus, Duration) to
                # hangup_url. callback_url is NOT a call-status hook (it is for
                # recordings), so a call would otherwise stay 'ringing' forever.
                'hangup_url': plivo_status_url(call['id']),
                'hangup_method': 'POST',
            })
    except requests.RequestException as e:
        raise ProviderError(f"Network error talking to Plivo (call may or may not have been placed): {e}")
    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = body.get('error') or body.get('message') or resp.text
        except ValueError:
            msg = resp.text
        raise ProviderError(f"Plivo {resp.status_code}: {msg}"[:400])
    uuid = resp.json().get('request_uuid')
    if not uuid:
        raise ProviderError("Plivo accepted the request but returned no request_uuid.")
    return CallResult(uuid, 'ringing')


def _mock(call):
    # Deterministic from the last digit so demos/tests are repeatable.
    d = int(call['phone_e164'][-1])
    sid = f"MOCK{call['id']:08d}"
    if d in (1, 2):
        return CallResult(sid, 'no_answer', 0)
    if d == 3:
        return CallResult(sid, 'busy', 0)
    if d == 4:
        return CallResult(sid, 'failed', 0)
    return CallResult(sid, 'completed', 20 + d * 7)
