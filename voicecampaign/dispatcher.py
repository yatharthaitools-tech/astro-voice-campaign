"""Background dialer. Queued rows are claimed one at a time (FOR UPDATE SKIP
LOCKED), so any number of threads/pods can run this safely, and a restart just
resumes whatever is still 'queued'. Calls are paced at CALLS_PER_SECOND and at
most MAX_CONCURRENT_CALLS experts are on the line at any moment."""
"""Background dialer. Calls go out one at a time by default: the next call is
claimed only when fewer than MAX_CONCURRENT_CALLS are live (Plivo limits
channels). The slot check is serialised in Postgres, so any number of
threads/pods stay within the limit, and a restart just resumes whatever is
still 'queued'."""
import logging
import os
import threading
import time

from voicecampaign import db, voice

log = logging.getLogger(__name__)
_lock = threading.Lock()
_thread = None


def _pace():
    try:
        cps = float(os.environ.get('CALLS_PER_SECOND', '1'))
    except ValueError:
        cps = 1.0
    return 1.0 / cps if cps > 0 else 1.0


SLOT_POLL_SECONDS = 2        # how often to re-check for a free line
LIVE_CALL_MAX_MINUTES = 5    # a call silent this long no longer occupies a line


def _max_concurrent():
    """0 means unlimited."""
    try:
        n = int(os.environ.get('MAX_CONCURRENT_CALLS', '2'))
    except ValueError:
        n = -1
    return n if n >= 0 else 2


def wait_for_free_line():
    """Blocks until fewer than MAX_CONCURRENT_CALLS calls are live. A line is
    freed by the provider's hangup webhook, or by the age cut-off in
    db.count_live_calls when that webhook never comes."""
    limit = _max_concurrent()
    while limit and db.count_live_calls(LIVE_CALL_MAX_MINUTES) >= limit:
        time.sleep(SLOT_POLL_SECONDS)
POLL_SECONDS = 2


def dial_one():
    """Dials the next queued call if a channel is free. Returns True if one was
    dialed. Never retries: a failed or ambiguous attempt is final."""
    call = db.claim_next_call()
    if not call:
        return False
    audio_url = None
    try:
        audio_url = db.recording_url_for(call['language_key'], voice.public_base_url())
        if not audio_url:
            db.mark_failed(call['id'], 'No recording for this language at dial time.')
            return True
        result = voice.place_call(call, audio_url)
        db.mark_dialed(call['id'], result.sid, result.status, audio_url, result.duration)
    except voice.ProviderError as e:
        db.mark_failed(call['id'], str(e), audio_url)
    except Exception as e:  # noqa: BLE001 — one bad row must not stop the campaign
        log.exception('dial failed for call %s', call['id'])
        db.mark_failed(call['id'], f'Unexpected error: {e}', audio_url)
    return True


def drain(pace=True):
    """Works through the queue. With pace=True (the real dialer) it waits for a
    channel to free up between calls; with pace=False (tests) it returns as soon
    as nothing more can be dialed right now."""
    db.fail_stale_dialing()
    while db.has_queued():
        wait_for_free_line()
        if not dial_one():
            break
        if pace:
            time.sleep(_pace())
    while True:
        db.fail_stuck_active()
        if dial_one():
            if pace:
                time.sleep(_pace())
            continue
        if pace and db.has_queued():
            time.sleep(POLL_SECONDS)  # all channels busy: wait for a call to finish
            continue
        return


def _run():
    global _thread
    try:
        drain()
    finally:
        with _lock:
            _thread = None
        # A launch that landed right as we finished must not be stranded.
        try:
            if db.has_queued():
                ensure_worker()
        except Exception:  # noqa: BLE001
            log.exception('re-check after drain failed')


def ensure_worker():
    """Starts the dialer thread in this process if it isn't already running."""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return False
        _thread = threading.Thread(target=_run, name='dialer', daemon=True)
        _thread.start()
        return True
