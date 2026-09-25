# 7x — deployment

Voice agents that call wedding guests with the right reminder for the right function.
One FastAPI app serves the API, the React admin SPA, and the telephony webhooks.

## What runs where

| URL | What it is | Auth |
|---|---|---|
| `/` | Public landing page | none |
| `/admin` | The 7x admin SPA | user login |
| `/superadmin` | Legacy cost dashboard | shared secret `ANALYTICS_SECRET` |
| `/plivo/answer`, `/plivo/media-stream` | Telephony webhooks — **Plivo calls these** | Plivo |
| `/ws` | Browser-mic test socket | short-lived token minted by the Agents tab |

---

## Docker (recommended)

```bash
cp .env.example .env       # then edit — see "Required settings" below
docker compose up -d --build
docker compose logs -f
```

A healthy boot logs three lines:

```
INFO:eo_db:...                      (silence here is fine)
INFO:scheduler:Callback scheduler started
INFO:campaign_runner:Campaign runner started (... plivo_ready=True ...)
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**Check `plivo_ready=True`.** If it says `False`, outbound calls will never place — see
Troubleshooting.

The image is multi-stage: Node builds the SPA, then it's copied into the Python runtime.
`.dockerignore` excludes your local `admin/dist`, so the container always builds the SPA
fresh — you cannot accidentally ship a stale UI against a new API.

### HTTPS

The container binds `127.0.0.1:8000` only. Terminate TLS on the host (Caddy):

```
7x.yourdomain.com {
    reverse_proxy localhost:8000
}
```

Point the DNS A record at the server; Caddy provisions the cert. **`PUBLIC_URL` must be
this HTTPS URL** — Plivo fetches `/plivo/answer` from it, and the media stream upgrades to
`wss://`, which browsers and Plivo both require to be secure.

### Updating

```bash
git pull
docker compose up -d --build
```

The `eo-data` volume persists, so weddings, guests, campaigns and call logs survive.

---

## Without Docker

```bash
pip install -r requirements.txt
cd admin && npm install && npm run build && cd ..   # emits admin/dist, served at /admin
python main.py
```

**One worker, always.** The callback scheduler, campaign runner, live-call gauge, prewarm
cache and the scheduler's circuit breaker are all in-process singletons. A second worker
gives you two dialers racing the same contacts and a call budget that counts wrong:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Re-run `npm run build` after any SPA change — FastAPI serves the built `admin/dist`, not
the source. If `admin/dist` is missing, `/admin` returns a 503 telling you to build.

---

## Required settings

| Var | Notes |
|---|---|
| `PUBLIC_URL` | **Required for any phone call.** The public HTTPS base Plivo fetches `/plivo/answer` from. Blank ⇒ `plivo_ready=False`. |
| `GEMINI_API_KEY` | |
| `MODEL` | A Gemini **Live** model id valid for your account. |
| `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` / `PLIVO_FROM_NUMBER` | |
| `EO_ADMIN_USER` / `EO_ADMIN_PASS` | Seeds the first admin **only when the users table is empty**. Change the password from Profile after first login. |
| `EO_SESSION_SECRET` | Long random string; signs login tokens. Falls back to `ANALYTICS_SECRET`. |
| `DATA_DIR` | Leave blank under compose — it sets `/var/eo-data` (the persistent volume). |

### Capacity

| Var | Default | What it means |
|---|---|---|
| `MAX_LIVE_CALLS` | `10` | Simultaneous live calls, shared by campaigns **and** callbacks. Costs concurrent Gemini sessions and telephony spend. |
| `EO_MAX_ACTIVE_CAMPAIGNS` | `6` | Campaigns allowed scheduled/live at once. A wedding day runs several in parallel. |
| `EO_CAMPAIGN_MAX_PER_TICK` | `3` | New dials started per 30 s tick. |
| `EO_FATIGUE_WINDOW_HOURS` | `12` | Warn when a guest was already called this recently. A warning, never a block. |

Before a big day, place a few real calls and listen, then raise `MAX_LIVE_CALLS` if the
quality holds. The runner rotates which campaign dials first each tick, so no campaign is
starved when the budget is tight.

The **Scheduler ON/OFF** toggle in the UI pauses *all* outbound dialing — both the campaign
runner and the callback scheduler. It is the fastest kill switch if something goes wrong
mid-event.

---

## Data and backups

Everything lives under `DATA_DIR`:

- `eo.db` — SQLite: weddings, events, agents, guests, campaigns, users.
- `calls/*.json` — one file per call: transcript, outcome, cost.
- `recordings/*.wav` — call audio, when `EO_RECORD_CALLS` is on.

Back up the whole directory. With compose:

```bash
docker compose exec sevenx tar czf - /var/eo-data > 7x-backup-$(date +%F).tgz
```

**Guest data is personal** — phone numbers, hotels, room numbers, flight details. Treat
backups accordingly.

### Migrating an older database

`init()` migrates on boot. A pre-7x database gets a `contacts` **table rebuild**, because
its unique key has to change and SQLite cannot alter a constraint in place. You'll see:

```
WARNING:eo_db:contacts: migrating a pre-7x table to UNIQUE(created_by, wedding_id, phone)
```

It runs in one transaction and preserves every row, but **copy `eo.db` first** — a rebuild
is the one migration worth having a backup of. Existing guests land with `wedding_id = 0`
("no wedding"); assign them by re-importing the sheet against the right wedding.

---

## First run

1. Sign in at `/admin` with `EO_ADMIN_USER` / `EO_ADMIN_PASS`; change the password on Profile.
2. **Weddings → New Wedding.** The spoken fields matter — the hospitality team name and
   placard text are read aloud on calls.
3. **Add events** — time, venue, and who each function is for (everyone / groom's / bride's side).
4. **Agents → Event Reminder → Show the script.** Read what it will actually say, with your
   event's details filled in. Duplicate it to change the wording.
5. **Create Campaign** — pick the agent and event; guests pre-select by the event's audience.

Test before you dial guests: the Agents tab can ring your own phone or talk to you in the
browser, using the real script.

---

## Troubleshooting

**`plivo_ready=False`** — `PUBLIC_URL`, `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` or
`PLIVO_FROM_NUMBER` is unset. Calls will queue and never place.

**Calls connect but there's silence** — the answer webhook must be reachable *from the
internet*. `curl https://your-public-url/plivo/answer` from off-box; anything but XML back
means Plivo can't reach you either.

**`/admin` 503s** — the SPA isn't built. `cd admin && npm run build`.

**UI works, new pages 404** — a stale `admin/dist` against a new API. Rebuild. Docker
avoids this entirely.

**Agent says a sentence with a gap** — a placeholder had no data. Agents tab → Show the
script shows exactly which, in an amber banner. Fill it in on the event or guest.

**"DOUBLE-REPLY FIX DEGRADED"** — genuinely means `google-genai < 2.10`; `pip install -U
'google-genai>=2.10,<3'`. (If you see it *alongside* a `--- Logging error ---` traceback on
Windows, that's a console encoding problem, not the SDK.)

**Campaigns 2-6 never dial** — should not happen; the runner rotates order each tick.
If it does, check `MAX_LIVE_CALLS` isn't 1.

## Inbound calls (a guest rings one of our numbers)

Point each Plivo number's **Answer URL** at:

```
https://<PUBLIC_URL host>/plivo/answer        method: GET or POST (both work)
```

For the test server that is `https://7x-test.globalvoxinc.com/plivo/answer`. Set it in the
Plivo console under **Phone Numbers → your number → Application** (or an Application whose
Answer URL is that address, with **Hangup URL** left blank). Do this for **both** numbers
(`+91 80 3170 4911` and `+91 80 3170 4910`) — nothing else changes between them.

What the caller hears:

- **Known guest** (their number is on a wedding's guest list): the inbound agent greets them
  by first name and delivers its script, exactly as on an outbound call. On a one-function
  agent (the strict reminder) that is the next function to start — never one already under
  way, so nobody hears about the Hi-Tea at six in the evening.
- **Unknown number**: the same agent, without a name and without asking "am I speaking
  with…?".
- **A guest we rang earlier** (a missed call-back): the campaign's own agent, with an opening
  that says we tried to reach them.

The inbound agent is `EO_INBOUND_AGENT_SLUG` (code default `wedding_schedule`; the Ved & Riya
server sets `event_reminder`, the strict script); if that agent is switched off the call falls
back to the shipped reminder agent. Plivo delivers the caller's
number in several shapes — `917043020542`, `+917043020542`, `7043020542`, `07043020542` — and
all of them match the same guest.

## The strict reminder campaign (one function, three lines)

The Event Reminder Specialist speaks the family's script and nothing else: "Hello, I'm
speaking from <team>. Am I speaking to <name>?" → "I just wanted to inform you that
<function> will start at <time> at <venue>." → "Looking forward to seeing you." It names no
other function, answers no question (one fixed line, then the sign-off) and speaks English,
Hindi or Gujarati only.

After a deploy that changes `agent_seeds.py`:

```
python seed_demo_wedding.py --refresh-agents          # updates the shipped global rows
python seed_demo_wedding.py --preview reminder        # read the script for the next function
python seed_demo_wedding.py --preview reminder --event <id>
```

A per-wedding copy of the OLD reminder is reported as stale ("still says 'have some details
about this evening'"); `--force-all` rewrites it, or delete it in the Agents tab.

Server `.env` for a strict-script campaign: `EO_INBOUND_AGENT_SLUG=event_reminder`,
`EO_TRANSCRIBE_LANGUAGE_HINTS=en-IN,hi-IN,gu-IN`, `EO_UNANSWERED_REPLY_SECONDS=1.5`,
`EO_TURN_EXPIRE_SECONDS=2`, `EO_GREETING_NUDGE_MAX=3`, `EO_CONNECT_TONE_MAX_S=12`. Then
`docker compose up -d`. In the Agents tab switch the other agents off; create the campaign on
the Event Reminder Specialist with the function selected.
