# Deploy & go-live checklist

## 1. Deploy (Render, ~10 min)
1. Render → **New → Blueprint** → select this repo (`render.yaml` is picked up).
2. Fill the prompted values: `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `PUBLIC_BASE_URL`
   (the service's own `https://…onrender.com` URL; set it after first deploy, then redeploy).
   Leave `PLIVO_*` blank for now. Keep `VOICE_PROVIDER=mock`.
3. Open the URL, sign in, and do a **mock dry run** with your real CSV (no calls are placed).
   Any host works if it gives you Docker, Postgres and public HTTPS; the service must stay
   always-on (no free-tier sleeping) because the dialer runs inside it.

## 2. Plivo
1. Plivo console → copy **Auth ID** and **Auth Token**.
2. Have an outbound **caller ID** Plivo allows for India (rented/approved number, or the
   registered caller ID your account is approved for). Put it in `PLIVO_FROM_NUMBER`.
3. Set `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_FROM_NUMBER`, then `VOICE_PROVIDER=plivo`
   and redeploy. The header badge should read **Live calling (Plivo)**.
4. Do not change `SECRET_KEY` after calls are placed — Plivo's callbacks for them would be rejected.

## 3. Before the real launch
- [ ] Recordings page: all 8 languages show **Uploaded/URL**; play each once.
- [ ] Campaign rate set to your real per-minute cost.
- [ ] Make a small CSV of 3 numbers you own (different languages, `consent_status=Yes`),
      launch it, answer, and confirm: correct language heard, status → Answered, duration and cost appear.
- [ ] Also confirm an unanswered call shows **No answer** (not stuck on Ringing).
- [ ] Confirm DND/telemarketing rules and that every `Yes` in the CSV is real, recorded consent.
- [ ] Then upload the real 500-row CSV, check the summary table and flagged rows, launch.

## 4. Operating notes
- Calls go out at `CALLS_PER_SECOND` (default 1 → ~8 minutes for 500). Raise only within your Plivo limits.
- If the server restarts mid-campaign, press **Resume dialing**. Interrupted dial attempts are
  marked Failed, never retried, so nobody is called twice.
- Uploaded recordings are stored in Postgres; back up the database as you would anything else.
