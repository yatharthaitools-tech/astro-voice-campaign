"""Telephony providers. A provider turns (phone, audio URL) into a placed call.

`mock` places nothing and returns a simulated outcome — the default, so the app
can be exercised end to end without spending money. `twilio` places real calls
that play the language's recording; progress/duration come back through the
status webhook (see app.py)."""
import base64
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


def status_callback_url(call_id):
    return f"{public_base_url()}/webhooks/call-status/{call_id}"


def twiml_play(audio_url):
    return f"<Response><Play>{escape(audio_url)}</Play></Response>"


def check_ready():
    """Returns an error string if real calling can't work, else None."""
    if provider_name() == 'mock':
        return None
    if provider_name() != 'twilio':
        return f"Unknown VOICE_PROVIDER '{provider_name()}'."
    missing = [k for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER', 'PUBLIC_BASE_URL')
               if not os.environ.get(k, '').strip()]
    return ('Set ' + ', '.join(missing) + ' before launching.') if missing else None


def place_call(call, audio_url):
    if provider_name() == 'mock':
        return _mock(call)
    return _twilio(call, audio_url)


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


_TWILIO_STATUS = {
    'queued': 'ringing', 'initiated': 'ringing', 'ringing': 'ringing',
    'in-progress': 'in_progress', 'completed': 'completed', 'busy': 'busy',
    'failed': 'failed', 'no-answer': 'no_answer', 'canceled': 'canceled',
}


def map_twilio_status(s):
    return _TWILIO_STATUS.get((s or '').lower())


def _twilio(call, audio_url):
    sid, token = os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN']
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
            auth=(sid, token), timeout=15,
            data={
                'To': call['phone_e164'],
                'From': os.environ['TWILIO_FROM_NUMBER'],
                'Twiml': twiml_play(audio_url),
                'StatusCallback': status_callback_url(call['id']),
                'StatusCallbackMethod': 'POST',
                'StatusCallbackEvent': ['initiated', 'ringing', 'answered', 'completed'],
            })
    except requests.RequestException as e:
        raise ProviderError(f"Network error talking to Twilio (call may or may not have been placed): {e}")
    if resp.status_code >= 400:
        try:
            msg = resp.json().get('message', resp.text)
        except ValueError:
            msg = resp.text
        raise ProviderError(f"Twilio {resp.status_code}: {msg}"[:400])
    body = resp.json()
    return CallResult(body['sid'], map_twilio_status(body.get('status')) or 'ringing')


def valid_twilio_signature(url, params, signature):
    """Twilio signs url + sorted(key+value...) with the account auth token."""
    token = os.environ.get('TWILIO_AUTH_TOKEN', '')
    if not token or not signature:
        return False
    payload = url + ''.join(k + v for k, v in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
