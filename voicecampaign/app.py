import csv
import hmac
import io
import os
import re
import secrets
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)

from voicecampaign import db, dispatcher, rules, voice

PAGE_SIZE = 100
IST = timezone(timedelta(hours=5, minutes=30))
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# Called by the telephony provider, not a browser: authenticated by signature/token instead of CSRF.
WEBHOOK_ENDPOINTS = {'plivo_status', 'plivo_answer'}
STATUS_LABELS = {
    'pending': 'Not started', 'queued': 'Queued', 'dialing': 'Dialing', 'ringing': 'Ringing',
    'in_progress': 'In call', 'completed': 'Answered', 'busy': 'Busy', 'no_answer': 'No answer',
    'failed': 'Failed', 'canceled': 'Canceled',
}


def create_app(init=True):
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('INSECURE_COOKIES') != '1' and not app.testing
    if init:
        db.init_db()

    # --- helpers ----------------------------------------------------------
    def csrf_token():
        if '_csrf' not in session:
            session['_csrf'] = secrets.token_urlsafe(24)
        return session['_csrf']

    # Optional: when set, the admin UI answers ONLY on these hostnames. Any other
    # hostname (e.g. a public one given to Plivo) serves just the endpoints the
    # provider needs, so the login page is never exposed to the internet.
    PUBLIC_ENDPOINTS = WEBHOOK_ENDPOINTS | {'serve_audio', 'healthz', 'static'}

    @app.before_request
    def _host_gate():
        hosts = {h.strip().lower() for h in os.environ.get('ADMIN_HOSTS', '').split(',') if h.strip()}
        if hosts and request.host.split(':')[0].lower() not in hosts \
                and request.endpoint not in PUBLIC_ENDPOINTS:
            abort(404)

    @app.before_request
    def _csrf_check():
        if request.method == 'POST' and request.endpoint not in WEBHOOK_ENDPOINTS:
            sent = request.form.get('_csrf', '')
            if not sent or not hmac.compare_digest(sent, session.get('_csrf', '')):
                abort(400, 'Invalid or missing CSRF token. Reload the page and retry.')

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'DENY')
        return resp

    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not session.get('admin'):
                return redirect(url_for('login'))
            return view(*a, **kw)
        return wrapped

    @app.template_filter('ist')
    def _ist(value, fmt='%d %b %Y, %H:%M'):
        return value.astimezone(IST).strftime(fmt) if value else ''

    @app.context_processor
    def _ctx():
        return {'csrf_token': csrf_token, 'status_labels': STATUS_LABELS,
                'flag_labels': rules.FLAG_LABELS, 'provider': voice.provider_name()}

    def money(rate, minutes):
        return (Decimal(rate) * Decimal(minutes)).quantize(Decimal('0.01'))

    def parse_rate(raw, default=None):
        try:
            rate = Decimal((raw or '').strip())
        except InvalidOperation:
            return default
        return rate if 0 <= rate <= 1000 else default

    def default_rate():
        return parse_rate(os.environ.get('COST_PER_MINUTE'), Decimal('1.0'))

    def must_campaign(cid):
        c = db.get_campaign(cid)
        if not c:
            abort(404)
        return c

    # --- auth ---------------------------------------------------------------
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = os.environ.get('ADMIN_EMAIL', '')
            pw = os.environ.get('ADMIN_PASSWORD', '')
            ok = bool(email and pw) and \
                hmac.compare_digest(request.form.get('email', '').strip().lower().encode(), email.strip().lower().encode()) and \
                hmac.compare_digest(request.form.get('password', '').encode(), pw.encode())
            if ok:
                session.clear()
                session['admin'] = True
                return redirect(url_for('home'))
            flash('Wrong email or password.', 'error')
        return render_template('login.html')

    @app.route('/logout', methods=['POST'])
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/healthz')
    def healthz():
        return 'ok'

    # --- campaigns list / create -------------------------------------------
    @app.route('/')
    @login_required
    def home():
        return render_template('home.html', campaigns=db.list_campaigns(), nav='campaigns',
                               rate=default_rate(), not_ready=voice.check_ready())

    @app.route('/campaigns', methods=['POST'])
    @login_required
    def create_campaign():
        f = request.files.get('csv')
        name = (request.form.get('name') or '').strip()
        if not f or not f.filename:
            flash('Choose a CSV file.', 'error')
            return redirect(url_for('home'))
        try:
            rows = rules.parse_csv(f.read())
        except rules.CsvError as e:
            flash(str(e), 'error')
            return redirect(url_for('home'))
        rate = parse_rate(request.form.get('rate'), default_rate())
        cid = db.create_campaign(name or os.path.splitext(f.filename)[0], f.filename, rows, rate)
        flash(f'Imported {len(rows)} rows. Review the preview below, then launch.', 'ok')
        return redirect(url_for('campaign', cid=cid))

    # --- recordings (language -> audio) ------------------------------------
    @app.route('/languages')
    @login_required
    def languages():
        return render_template('languages.html', languages=db.list_languages(), nav='languages')

    @app.route('/languages/add', methods=['POST'])
    @login_required
    def add_language():
        name = request.form.get('name', '')
        if len(name.strip()) > 40 or not db.add_language(name):
            flash('Enter a language name (up to 40 characters).', 'error')
        else:
            flash(f'Added {rules.display_language(name)}. Now give it a recording.', 'ok')
        return redirect(url_for('languages'))

    @app.route('/languages/<key>/audio', methods=['POST'])
    @login_required
    def set_audio(key):
        f = request.files.get('file')
        url = (request.form.get('url') or '').strip()
        if f and f.filename:
            data = f.read()
            ext = os.path.splitext(f.filename)[1].lower()
            is_mp3 = data[:3] == b'ID3' or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
            is_wav = data[:4] == b'RIFF' and data[8:12] == b'WAVE'
            if ext not in ('.mp3', '.wav') or not (is_mp3 or is_wav):
                flash('Upload an .mp3 or .wav file.', 'error')
            elif len(data) > MAX_AUDIO_BYTES:
                flash('Audio file is over 10 MB.', 'error')
            elif db.set_audio_file(key, data, os.path.basename(f.filename)[:120],
                                   'audio/wav' if is_wav else 'audio/mpeg'):
                flash('Recording uploaded.', 'ok')
        elif url:
            p = urlparse(url)
            if p.scheme not in ('http', 'https') or not p.netloc:
                flash('Enter a full public URL starting with https://', 'error')
            elif db.set_audio_url(key, url):
                flash('Recording URL saved.', 'ok')
        else:
            flash('Choose a file or paste a URL.', 'error')
        return redirect(url_for('languages'))

    @app.route('/languages/<key>/clear', methods=['POST'])
    @login_required
    def clear_audio(key):
        db.clear_audio(key)
        flash('Recording removed. Experts in that language will be flagged until you add one.', 'ok')
        return redirect(url_for('languages'))

    @app.route('/audio/<token>.mp3')
    def serve_audio(token):
        # Public on purpose (the provider fetches it); the token is unguessable.
        row = db.get_audio_by_token(token)
        if not row:
            abort(404)
        return Response(bytes(row['audio_data']), mimetype=row['content_type'] or 'audio/mpeg',
                        headers={'Cache-Control': 'public, max-age=300'})

    # --- one campaign: preview, launch, results ------------------------------
    @app.route('/campaigns/<int:cid>')
    @login_required
    def campaign(cid):
        c = must_campaign(cid)
        lang = request.args.get('language')
        flag = request.args.get('flag') or None
        status = request.args.get('status') or None
        page = max(1, request.args.get('page', 1, type=int))
        summary = db.preview_summary(cid)
        results = db.report_by_language(cid)
        calls, total = db.list_calls(cid, lang, flag, status, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        rate = c['cost_per_minute']
        totals = {k: sum(r[k] for r in results) for k in ('placed', 'answered', 'unanswered', 'failed', 'in_flight')}
        totals['minutes'] = sum(r['billed_minutes'] for r in results)
        totals['cost'] = money(rate, totals['minutes'])
        rows = [dict(r, cost=money(rate, r['billed_minutes'])) for r in results]
        pending_ready = sum(s['ready'] for s in summary)
        active = totals['in_flight'] > 0 or db.count_in_flight(cid) > 0
        return render_template(
            'campaign.html', c=c, summary=summary, results=rows, totals=totals, calls=calls,
            total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)), active_refresh=active,
            filters={'language': lang or '', 'flag': flag or '', 'status': status or ''},
            pending_ready=pending_ready, not_ready=voice.check_ready(), nav='campaigns',
            all_flags=['ready', 'no_recording', 'missing_language', 'invalid_phone', 'no_consent', 'duplicate'])

    def _launch(cid, language_key, call_ids, is_test):
        err = voice.check_ready()
        if err:
            flash(err, 'error')
            return
        n = db.queue_calls(cid, language_key, call_ids, is_test)
        if n:
            dispatcher.ensure_worker()
            flash(f'{n} call(s) queued.' + (' (Test)' if is_test else ''), 'ok')
        else:
            flash('Nothing to call: no pending experts with a valid language and recording.', 'error')

    @app.route('/campaigns/<int:cid>/launch', methods=['POST'])
    @login_required
    def launch(cid):
        must_campaign(cid)
        choice = request.form.get('language', 'all')
        _launch(cid, None if choice == 'all' else choice, None, False)
        return redirect(url_for('campaign', cid=cid))

    @app.route('/campaigns/<int:cid>/test', methods=['POST'])
    @login_required
    def test_calls(cid):
        must_campaign(cid)
        ids = [int(i) for i in request.form.getlist('call_ids') if i.isdigit()]
        if not ids:
            flash('Tick at least one expert to test.', 'error')
        elif len(ids) > 20:
            flash('Test up to 20 experts at a time.', 'error')
        else:
            _launch(cid, None, ids, True)
        return redirect(url_for('campaign', cid=cid, **{k: v for k, v in request.args.items()}))

    @app.route('/campaigns/<int:cid>/resume', methods=['POST'])
    @login_required
    def resume(cid):
        must_campaign(cid)
        err = voice.check_ready()
        if err:
            flash(err, 'error')
        elif dispatcher.ensure_worker():
            flash('Dialing resumed.', 'ok')
        else:
            flash('Dialer is already running.', 'ok')
        return redirect(url_for('campaign', cid=cid))

    @app.route('/campaigns/<int:cid>/rate', methods=['POST'])
    @login_required
    def set_rate(cid):
        must_campaign(cid)
        rate = parse_rate(request.form.get('rate'))
        if rate is None:
            flash('Enter a rate between 0 and 1000.', 'error')
        else:
            db.set_cost_per_minute(cid, rate)
            flash('Cost rate updated.', 'ok')
        return redirect(url_for('campaign', cid=cid))

    @app.route('/campaigns/<int:cid>/delete', methods=['POST'])
    @login_required
    def delete_campaign(cid):
        if db.count_in_flight(cid):
            flash('This campaign still has calls in progress.', 'error')
            return redirect(url_for('campaign', cid=cid))
        db.delete_campaign(cid)
        flash('Campaign deleted.', 'ok')
        return redirect(url_for('home'))

    @app.route('/campaigns/<int:cid>/report.csv')
    @login_required
    def report_csv(cid):
        c = must_campaign(cid)
        rate = c['cost_per_minute']

        def safe(v):  # neutralise spreadsheet formula injection from uploaded text
            v = v or ''
            return "'" + v if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['expert_name', 'phone_number', 'language', 'recording_used', 'call_status',
                    'duration_seconds', 'estimated_cost', 'test_call', 'note'])
        for r in db.all_calls_for_report(cid):
            if r['eligibility'] != 'ok':
                status, note = 'Skipped', rules.FLAG_LABELS[r['eligibility']]
            elif r['status'] == 'pending':
                status, note = ('Not called', 'Recording not uploaded') if r['flag'] == 'no_recording' else ('Not called', '')
            else:
                status, note = STATUS_LABELS.get(r['status'], r['status']), r['error'] or ''
            answered = r['status'] in db.ANSWERED and (r['duration_seconds'] or 0) > 0
            cost = money(rate, -(-r['duration_seconds'] // 60)) if answered else ''
            w.writerow([safe(r['expert_name']), r['phone_e164'] or safe(r['phone_raw']),
                        safe(r['language_name'] or r['language_raw']),
                        r['recording_used'] or (r['recording_label'] if r['status'] != 'pending' else ''),
                        status, r['duration_seconds'] if r['duration_seconds'] is not None else '',
                        cost, 'yes' if r['is_test'] else '', safe(note)])
        name = re.sub(r'[^A-Za-z0-9_-]+', '_', c['name'])[:40] or 'campaign'
        return Response('﻿' + buf.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={name}_report.csv'})

    # --- provider webhooks (Plivo) ---------------------------------------------
    @app.route('/webhooks/plivo/answer/<int:call_id>', methods=['GET', 'POST'])
    def plivo_answer(call_id):
        """Plivo asks what to do once the expert picks up: play the recording
        for THIS expert's language (resolved now, so a re-uploaded file is used)."""
        if voice.provider_name() != 'plivo' or not voice.valid_callback_token(call_id, request.args.get('t')):
            abort(403)
        call = db.get_call(call_id)
        url = db.recording_url_for(call['language_key'], voice.public_base_url()) if call else None
        xml = voice.plivo_xml_play(url) if url else voice.PLIVO_HANGUP_XML
        if url:
            db.set_recording_used(call_id, url)
        return Response(xml, mimetype='text/xml')

    @app.route('/webhooks/plivo/status/<int:call_id>', methods=['POST'])
    def plivo_status(call_id):
        if voice.provider_name() != 'plivo' or not voice.valid_callback_token(call_id, request.args.get('t')):
            abort(403)
        status = voice.map_plivo_status(request.form.get('CallStatus'))
        if status and db.get_call(call_id):
            dur = request.form.get('Duration') or request.form.get('BillDuration')
            db.apply_status_update(call_id, status, int(dur) if dur and dur.isdigit() else None)
        return ('', 204)

    return app


app = None


def get_app():
    global app
    if app is None:
        app = create_app()
        if db.has_queued():
            dispatcher.ensure_worker()  # pick up where a restart left off
    return app
