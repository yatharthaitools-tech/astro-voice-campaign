# AstroLokal Voice Campaigns

One outbound voice campaign for ~500 experts across many languages. Upload a
single CSV; each expert automatically hears the pre-recorded message for **their
own language**. No per-language campaigns.

## How it works

1. **Recordings** (`/languages`) — per language, upload an `.mp3`/`.wav` or paste a
   public URL. Hindi, English, Tamil, Telugu, Malayalam, Kannada, Bengali and Marathi
   are pre-created; add more any time. A language that appears in a CSV but isn't
   known yet is added automatically (without audio) so you can upload it afterwards.
2. **New campaign** (`/`) — upload a CSV with
   `expert_name, phone_number, language, consent_status`. Nothing is called yet.
3. **Preview** — per-language summary (experts, ready, flagged, recording, status)
   plus a row-by-row mapping. Flagged rows are never called:
   language missing · invalid phone · no consent · duplicate number · recording not uploaded.
   Uploading a missing recording later un-flags those experts without re-uploading the CSV.
4. **Test, then launch** — tick up to 20 experts and "Send test call", or launch
   **all languages** or **one language**. Each expert is called at most once per
   campaign (enforced atomically in the DB, plus a unique index); a tested expert
   counts as called.
5. **Results** — per language: calls, answered, no answer/busy, failed, average
   duration, estimated cost; overall cost; CSV report (expert, phone, language,
   recording used, call status, duration, cost).

Consent: only `Yes`/`Y`/`True`/`1` (case-insensitive) is treated as consent; blank or anything else is skipped.
Phone numbers are normalised to E.164 (10-digit Indian numbers get `+91`; set `DEFAULT_COUNTRY_CODE` to change).

## Telephony

`VOICE_PROVIDER=mock` (default) places **no** real calls and simulates outcomes.
`VOICE_PROVIDER=twilio` places real calls that `<Play>` the language's recording, and
receives status/duration at `POST /webhooks/call-status/<id>` (signature-verified).
Needs `TWILIO_*`, and `PUBLIC_BASE_URL` reachable from the internet over HTTPS (Twilio also
fetches uploaded recordings from `/audio/<token>.mp3`). Other providers: implement
`place_call` in `voicecampaign/voice.py`.

Calls are never retried automatically — a failed or ambiguous attempt is final, so nobody is called twice.
Cost is an **estimate**: answered calls rounded up to whole minutes × the per-minute rate set on the campaign.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in, then export the variables
gunicorn -w 1 --threads 8 wsgi:app
```

Tests (need a Postgres reachable via `DATABASE_URL`, default `voice_test` DB):

```bash
pip install -r requirements-dev.txt && pytest
```

## Notes

- Single admin login from `ADMIN_EMAIL` / `ADMIN_PASSWORD`; CSRF-protected forms. Put it behind HTTPS.
- The dialer runs in-process at `CALLS_PER_SECOND`; if the server restarts mid-campaign, press "Resume dialing".
- Check local telemarketing/DND and consent rules for your numbers before a live launch.
