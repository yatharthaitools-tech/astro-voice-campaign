"""Postgres storage. One campaign holds every expert; the language -> recording
mapping lives in `languages` and is resolved per call, so there is never a
per-language campaign."""
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from voicecampaign import rules


def database_url():
    url = os.environ.get('DATABASE_URL', 'postgresql://voice:voice@localhost:5432/voice_campaign')
    return url.replace('postgres://', 'postgresql://', 1) if url.startswith('postgres://') else url


SCHEMA = """
CREATE TABLE IF NOT EXISTS languages (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    audio_url TEXT,
    audio_data BYTEA,
    audio_token TEXT UNIQUE,          -- set only when audio_data is stored here
    audio_filename TEXT,
    content_type TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    source_filename TEXT,
    cost_per_minute NUMERIC NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_launched_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS campaign_calls (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    line_no INTEGER,
    expert_name TEXT NOT NULL DEFAULT '',
    phone_raw TEXT NOT NULL DEFAULT '',
    phone_e164 TEXT,
    language_raw TEXT NOT NULL DEFAULT '',
    language_key TEXT,
    consent_raw TEXT NOT NULL DEFAULT '',
    eligibility TEXT NOT NULL,        -- ok | missing_language | invalid_phone | no_consent | duplicate
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | queued | dialing | ringing | in_progress | completed
        -- | busy | no_answer | failed | canceled
    is_test BOOLEAN NOT NULL DEFAULT FALSE,
    recording_used TEXT,
    provider_sid TEXT,
    duration_seconds INTEGER,
    error TEXT,
    queued_at TIMESTAMPTZ,
    dialed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The hard guarantee behind "no duplicate calls in a campaign".
CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_phone
    ON campaign_calls(campaign_id, phone_e164) WHERE eligibility = 'ok';
CREATE INDEX IF NOT EXISTS idx_calls_campaign ON campaign_calls(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_calls_sid ON campaign_calls(provider_sid);
"""

# SQL fragments reused by several queries. has_audio never reads the bytea.
HAS_AUDIO = "(l.audio_url IS NOT NULL OR l.audio_token IS NOT NULL)"
FLAG_SQL = (f"CASE WHEN c.eligibility <> 'ok' THEN c.eligibility "
            f"WHEN NOT COALESCE({HAS_AUDIO}, FALSE) THEN 'no_recording' ELSE 'ready' END")
ANSWERED = ('in_progress', 'completed')
UNANSWERED = ('busy', 'no_answer')
FAILED = ('failed', 'canceled')
IN_FLIGHT = ('queued', 'dialing', 'ringing')
TERMINAL = ('completed', 'busy', 'no_answer', 'failed', 'canceled')


@contextmanager
def connect():
    conn = psycopg2.connect(database_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        for name in rules.DEFAULT_LANGUAGES:
            cur.execute("INSERT INTO languages (key, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (rules.norm_language(name), name))


def _q(sql, params=(), one=False):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()


# --- languages / recordings ----------------------------------------------

def list_languages():
    return _q(f"""SELECT l.key, l.name, l.audio_url, l.audio_token, l.audio_filename,
                         {HAS_AUDIO} AS has_audio, l.updated_at
                  FROM languages l ORDER BY l.name""")


def add_language(name):
    key = rules.norm_language(name)
    if not key:
        return None
    _q("INSERT INTO languages (key, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
       (key, rules.display_language(name)))
    return key


def set_audio_url(key, url):
    return _q("""UPDATE languages SET audio_url=%s, audio_data=NULL, audio_token=NULL,
                 audio_filename=NULL, content_type=NULL, updated_at=now()
                 WHERE key=%s RETURNING key""", (url, key), one=True) is not None


def set_audio_file(key, data, filename, content_type):
    return _q("""UPDATE languages SET audio_url=NULL, audio_data=%s, audio_token=%s,
                 audio_filename=%s, content_type=%s, updated_at=now()
                 WHERE key=%s RETURNING key""",
              (psycopg2.Binary(data), secrets.token_urlsafe(24), filename, content_type, key),
              one=True) is not None


def clear_audio(key):
    _q("""UPDATE languages SET audio_url=NULL, audio_data=NULL, audio_token=NULL,
          audio_filename=NULL, content_type=NULL, updated_at=now() WHERE key=%s""", (key,))


def get_audio_by_token(token):
    return _q("SELECT audio_data, content_type FROM languages WHERE audio_token=%s", (token,), one=True)


def recording_url_for(language_key, base_url):
    """The URL the provider should play for this language, or None."""
    row = _q("SELECT audio_url, audio_token FROM languages WHERE key=%s", (language_key,), one=True)
    if not row:
        return None
    if row['audio_token']:
        return f"{base_url.rstrip('/')}/audio/{row['audio_token']}.mp3"
    return row['audio_url']


# --- campaigns -------------------------------------------------------------

def create_campaign(name, filename, rows, cost_per_minute):
    """Creates the campaign and all its call rows atomically. Any language in
    the file we don't know yet is added (without audio) so it can be uploaded
    afterwards instead of the rows being dropped."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO campaigns (name, source_filename, cost_per_minute) VALUES (%s,%s,%s) RETURNING id",
                    (name, filename, cost_per_minute))
        cid = cur.fetchone()['id']
        seen_langs = {}
        for r in rows:
            if r['language_key'] and r['language_key'] not in seen_langs:
                seen_langs[r['language_key']] = rules.display_language(r['language_raw'])
        for key, nm in seen_langs.items():
            cur.execute("INSERT INTO languages (key, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (key, nm))
        psycopg2.extras.execute_values(cur, """INSERT INTO campaign_calls
            (campaign_id, line_no, expert_name, phone_raw, phone_e164, language_raw,
             language_key, consent_raw, eligibility) VALUES %s""",
            [(cid, r['line_no'], r['expert_name'], r['phone_raw'], r['phone_e164'],
              r['language_raw'], r['language_key'], r['consent_raw'], r['eligibility']) for r in rows])
    return cid


def list_campaigns():
    return _q("""SELECT c.*, COUNT(k.id) AS experts,
                   COUNT(k.id) FILTER (WHERE k.status NOT IN ('pending','queued')) AS placed
                 FROM campaigns c LEFT JOIN campaign_calls k ON k.campaign_id=c.id
                 GROUP BY c.id ORDER BY c.id DESC""")


def get_campaign(cid):
    return _q("SELECT * FROM campaigns WHERE id=%s", (cid,), one=True)


def set_cost_per_minute(cid, rate):
    _q("UPDATE campaigns SET cost_per_minute=%s WHERE id=%s", (rate, cid))


def delete_campaign(cid):
    _q("DELETE FROM campaigns WHERE id=%s", (cid,))


def preview_summary(cid):
    """One row per language: how many experts, how many can actually be called."""
    return _q(f"""
        SELECT COALESCE(c.language_key, '') AS key,
               COALESCE(MAX(l.name), MAX(NULLIF(c.language_raw,'')), 'Missing language') AS name,
               COUNT(*) AS experts,
               COUNT(*) FILTER (WHERE {FLAG_SQL} = 'ready') AS ready,
               COUNT(*) FILTER (WHERE {FLAG_SQL} <> 'ready') AS flagged,
               COALESCE(BOOL_OR({HAS_AUDIO}), FALSE) AS has_audio,
               MAX(COALESCE(l.audio_filename, l.audio_url)) AS recording
        FROM campaign_calls c LEFT JOIN languages l ON l.key = c.language_key
        WHERE c.campaign_id=%s GROUP BY COALESCE(c.language_key,'') ORDER BY experts DESC, name""", (cid,))


def list_calls(cid, language=None, flag=None, status=None, limit=200, offset=0):
    where, params = ["c.campaign_id=%s"], [cid]
    if language is not None and language != '':
        where.append("COALESCE(c.language_key,'')=%s"); params.append(language)
    if flag:
        where.append(f"{FLAG_SQL}=%s"); params.append(flag)
    if status:
        where.append("c.status=%s"); params.append(status)
    w = ' AND '.join(where)
    rows = _q(f"""SELECT c.*, l.name AS language_name, {FLAG_SQL} AS flag,
                         COALESCE(l.audio_filename, l.audio_url) AS recording_label
                  FROM campaign_calls c LEFT JOIN languages l ON l.key=c.language_key
                  WHERE {w} ORDER BY c.id LIMIT %s OFFSET %s""", params + [limit, offset])
    total = _q(f"""SELECT COUNT(*) AS n FROM campaign_calls c LEFT JOIN languages l ON l.key=c.language_key
                   WHERE {w}""", params, one=True)['n']
    return rows, total


def all_calls_for_report(cid):
    return _q(f"""SELECT c.*, l.name AS language_name, {FLAG_SQL} AS flag,
                         COALESCE(c.recording_used, l.audio_filename, l.audio_url) AS recording_label
                  FROM campaign_calls c LEFT JOIN languages l ON l.key=c.language_key
                  WHERE c.campaign_id=%s ORDER BY c.id""", (cid,))


def report_by_language(cid):
    """Per-language results. Cost = billed minutes (each answered call rounded
    up to a whole minute) x the campaign's per-minute rate; unanswered = free."""
    return _q(f"""
        SELECT COALESCE(c.language_key,'') AS key,
               COALESCE(MAX(l.name), MAX(NULLIF(c.language_raw,'')), 'Missing language') AS name,
               COUNT(*) FILTER (WHERE c.status NOT IN ('pending','queued')) AS placed,
               COUNT(*) FILTER (WHERE c.status IN %(ans)s) AS answered,
               COUNT(*) FILTER (WHERE c.status IN %(un)s) AS unanswered,
               COUNT(*) FILTER (WHERE c.status IN %(fail)s) AS failed,
               COUNT(*) FILTER (WHERE c.status IN %(fly)s) AS in_flight,
               COALESCE(AVG(c.duration_seconds) FILTER (WHERE c.status IN %(ans)s AND c.duration_seconds > 0), 0) AS avg_duration,
               COALESCE(SUM(CEIL(c.duration_seconds / 60.0)) FILTER (WHERE c.status IN %(ans)s AND c.duration_seconds > 0), 0) AS billed_minutes
        FROM campaign_calls c LEFT JOIN languages l ON l.key=c.language_key
        WHERE c.campaign_id=%(cid)s AND c.eligibility='ok'
        GROUP BY COALESCE(c.language_key,'') ORDER BY placed DESC, name""",
              {'cid': cid, 'ans': ANSWERED, 'un': UNANSWERED, 'fail': FAILED, 'fly': IN_FLIGHT})


def count_in_flight(cid):
    return _q("SELECT COUNT(*) AS n FROM campaign_calls WHERE campaign_id=%s AND status IN %s",
              (cid, IN_FLIGHT + ('in_progress',)), one=True)['n']


# --- launching / dialing ---------------------------------------------------

def queue_calls(cid, language_key=None, call_ids=None, is_test=False):
    """Atomically moves callable rows pending -> queued and returns how many.
    Callable = eligible AND its language has a recording. Because the status
    flip is a single UPDATE ... WHERE status='pending', a double click or two
    admins can never queue (and so never dial) the same expert twice."""
    where, params = ["c.campaign_id=%s", "c.status='pending'", "c.eligibility='ok'", f"{HAS_AUDIO}"], [cid]
    if language_key:
        where.append("c.language_key=%s"); params.append(language_key)
    if call_ids is not None:
        where.append("c.id = ANY(%s)"); params.append(list(call_ids))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"""UPDATE campaign_calls c SET status='queued', is_test=%s, queued_at=now(), updated_at=now()
                        FROM languages l
                        WHERE l.key=c.language_key AND {' AND '.join(where)}""", [is_test] + params)
        n = cur.rowcount
        if n and not is_test:
            cur.execute("UPDATE campaigns SET first_launched_at=COALESCE(first_launched_at, now()) WHERE id=%s", (cid,))
    return n


def claim_next_call():
    """Takes one queued call for dialing; safe with several workers/pods."""
    return _q("""UPDATE campaign_calls SET status='dialing', dialed_at=now(), updated_at=now()
                 WHERE id = (SELECT id FROM campaign_calls WHERE status='queued'
                             ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
                 RETURNING *""", one=True)


def has_queued():
    return _q("SELECT EXISTS(SELECT 1 FROM campaign_calls WHERE status='queued') AS e", one=True)['e']


def fail_stale_dialing(minutes=10):
    """A call stuck in 'dialing' with no provider id (process died mid-request)
    may or may not have been placed. We mark it failed rather than retry, so an
    expert is never called twice."""
    return _q("""UPDATE campaign_calls SET status='failed', updated_at=now(),
                 error='Interrupted while dialing; outcome unknown. Not retried to avoid a duplicate call.'
                 WHERE status='dialing' AND provider_sid IS NULL
                   AND dialed_at < now() - make_interval(mins => %s) RETURNING id""", (minutes,))


def mark_dialed(call_id, sid, status, recording, duration=None):
    _q("""UPDATE campaign_calls SET provider_sid=%s, status=%s, recording_used=%s,
          duration_seconds=COALESCE(%s, duration_seconds), updated_at=now() WHERE id=%s""",
       (sid, status, recording, duration, call_id))


def set_recording_used(call_id, url):
    _q("UPDATE campaign_calls SET recording_used=%s, updated_at=now() WHERE id=%s", (url, call_id))


def mark_failed(call_id, error, recording=None):
    _q("""UPDATE campaign_calls SET status='failed', error=%s, recording_used=COALESCE(%s, recording_used),
          updated_at=now() WHERE id=%s""", (error[:500], recording, call_id))


_RANK = {'pending': 0, 'queued': 1, 'dialing': 2, 'ringing': 3, 'in_progress': 4,
         'completed': 5, 'busy': 5, 'no_answer': 5, 'failed': 5, 'canceled': 5}


def apply_status_update(call_id, status, duration=None):
    """Webhook callbacks can arrive out of order; never move a call backwards
    or out of a terminal state."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM campaign_calls WHERE id=%s FOR UPDATE", (call_id,))
        row = cur.fetchone()
        if not row:
            return False
        if row['status'] in TERMINAL or _RANK.get(status, 0) < _RANK.get(row['status'], 0):
            return False
        cur.execute("""UPDATE campaign_calls SET status=%s, duration_seconds=COALESCE(%s, duration_seconds),
                       updated_at=now() WHERE id=%s""", (status, duration, call_id))
        return True


def get_call(call_id):
    return _q("SELECT * FROM campaign_calls WHERE id=%s", (call_id,), one=True)


def now():
    return datetime.now(timezone.utc)


# --- platform analytics (all campaigns) ---------------------------------

def _range(date_from, date_to):
    """Optional campaign-created-date window (inclusive), IST calendar days."""
    where, params = [], {}
    if date_from:
        where.append("(c.created_at AT TIME ZONE 'Asia/Kolkata')::date >= %(f)s"); params['f'] = date_from
    if date_to:
        where.append("(c.created_at AT TIME ZONE 'Asia/Kolkata')::date <= %(t)s"); params['t'] = date_to
    return (' AND '.join(where) or 'TRUE'), params


_COUNTS = """
    COUNT(*) FILTER (WHERE k.status NOT IN ('pending','queued')) AS placed,
    COUNT(*) FILTER (WHERE k.status IN %(ans)s) AS answered,
    COUNT(*) FILTER (WHERE k.status IN %(un)s) AS unanswered,
    COUNT(*) FILTER (WHERE k.status IN %(fail)s) AS failed,
    COALESCE(AVG(k.duration_seconds) FILTER (WHERE k.status IN %(ans)s AND k.duration_seconds > 0), 0) AS avg_duration,
    COALESCE(SUM(CEIL(k.duration_seconds / 60.0)) FILTER (WHERE k.status IN %(ans)s AND k.duration_seconds > 0), 0) AS billed_minutes,
    COALESCE(SUM(CEIL(k.duration_seconds / 60.0) * c.cost_per_minute)
             FILTER (WHERE k.status IN %(ans)s AND k.duration_seconds > 0), 0) AS cost"""
_P = {'ans': ANSWERED, 'un': UNANSWERED, 'fail': FAILED}


def platform_analytics(date_from=None, date_to=None):
    w, rp = _range(date_from, date_to)
    params = dict(_P, **rp)
    totals = _q(f"""
        SELECT COUNT(DISTINCT c.id) AS campaigns, COUNT(k.id) AS experts,
               COUNT(*) FILTER (WHERE k.eligibility='ok') AS eligible,
               COUNT(*) FILTER (WHERE k.id IS NOT NULL AND k.eligibility<>'ok') AS flagged,
               {_COUNTS}
        FROM campaigns c LEFT JOIN campaign_calls k ON k.campaign_id=c.id WHERE {w}""", params, one=True)
    by_language = _q(f"""
        SELECT COALESCE(k.language_key,'') AS key,
               COALESCE(MAX(l.name), MAX(NULLIF(k.language_raw,'')), 'Missing language') AS name,
               COUNT(*) AS experts, {_COUNTS}
        FROM campaigns c JOIN campaign_calls k ON k.campaign_id=c.id
             LEFT JOIN languages l ON l.key=k.language_key
        WHERE {w} AND k.eligibility='ok'
        GROUP BY COALESCE(k.language_key,'') ORDER BY placed DESC, experts DESC, name""", params)
    by_campaign = _q(f"""
        SELECT c.id, c.name, c.created_at, c.first_launched_at,
               COUNT(k.id) AS experts, {_COUNTS}
        FROM campaigns c LEFT JOIN campaign_calls k ON k.campaign_id=c.id
        WHERE {w} GROUP BY c.id ORDER BY c.id DESC LIMIT 25""", params)
    skipped = _q(f"""
        SELECT k.eligibility AS reason, COUNT(*) AS n
        FROM campaigns c JOIN campaign_calls k ON k.campaign_id=c.id
        WHERE {w} AND k.eligibility<>'ok' GROUP BY k.eligibility ORDER BY n DESC""", rp)
    no_rec = _q(f"""
        SELECT COUNT(*) AS n FROM campaigns c JOIN campaign_calls k ON k.campaign_id=c.id
             LEFT JOIN languages l ON l.key=k.language_key
        WHERE {w} AND k.eligibility='ok' AND k.status='pending' AND NOT COALESCE({HAS_AUDIO}, FALSE)""",
        rp, one=True)['n']
    trend = _q("""
        SELECT (k.dialed_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
               COUNT(*) AS placed, COUNT(*) FILTER (WHERE k.status IN %(ans)s) AS answered
        FROM campaign_calls k
        WHERE k.dialed_at >= now() - interval '30 days' AND k.status NOT IN ('pending','queued')
        GROUP BY 1 ORDER BY 1""", {'ans': ANSWERED})
    return {'totals': totals, 'by_language': by_language, 'by_campaign': by_campaign,
            'skipped': skipped, 'no_recording': no_rec, 'trend': trend}
