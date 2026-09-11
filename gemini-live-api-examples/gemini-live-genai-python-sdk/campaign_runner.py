"""
Campaign runner — paced outbound dialing for EO calling campaigns.

A single in-process asyncio task (one uvicorn worker), a sibling of the RSVP
callback `scheduler`. It:

  1. Promotes `scheduled` campaigns to `live` when their start time arrives.
  2. For each live campaign, PACES outbound dials: at most EO_CAMPAIGN_MAX_PER_TICK
     new dials per tick and at most EO_CAMPAIGN_MAX_CONCURRENT calls "in flight" —
     it never blasts the whole pool at once.
  3. Reaps in-flight ('calling') contacts by matching a call record (campaign_id +
     phone). Answered → `done` (with rsvp outcome); no record within the ring window
     → treated as no-answer and retried per the campaign's callback config
     (delay hours, max attempts/day, number of days) until exhausted → `failed`.
  4. Marks a campaign `completed` once no contact is pending or calling.

Isolation: it only reads/writes the campaign tables + the call index. It never
touches the RSVP `callback` blocks, so the existing callback scheduler is
unaffected. Gated by the same EO "Scheduler" toggle (scheduler.is_enabled()) plus
a master env switch, and it will not dial when Plivo credentials are absent.
"""

import asyncio
import logging
import os
from datetime import datetime, time as dtime, timedelta, timezone

import callbacks
import dialer
import eo_db
import live
import scheduler
import store

logger = logging.getLogger(__name__)

# Outcomes meaning "no live person answered" — a machine picked up. These are retried like a
# ring-out rather than closed out. Agents name this differently ("voicemail" historically,
# "not_reachable" in the 7x agent vocabulary), so both are recognised.
_UNANSWERED_OUTCOMES = frozenset({"voicemail", "not_reachable"})


def _max_active_campaigns():
    """Kept in step with eo_api._max_active_campaigns — the same cap, both entry points."""
    try:
        return max(1, int(os.getenv("EO_MAX_ACTIVE_CAMPAIGNS", "6")))
    except (TypeError, ValueError):
        return 6


def _cfg_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _enabled():
    return os.getenv("EO_CAMPAIGN_RUNNER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _plivo_ready():
    return bool(os.getenv("PLIVO_AUTH_ID") and os.getenv("PLIVO_AUTH_TOKEN") and os.getenv("PLIVO_FROM_NUMBER"))


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _apply_failure(cc, campaign, now, error=None):
    """A dial attempt did not connect (dial error or no-answer). Schedule a retry
    per the campaign callback config, or mark failed when exhausted. `attempts`
    and the per-day counter are already bumped at dial time."""
    delay_h = int(campaign.get("callback_delay_hours") or 4)
    max_day = max(1, int(campaign.get("callback_max_per_day") or 3))
    days = max(1, int(campaign.get("callback_days") or 1))
    attempts = int(cc.get("attempts") or 0)
    max_total = max_day * days

    first = _parse(cc.get("created_at")) or now
    days_elapsed = (now.date() - first.date()).days

    fields = {"last_error": (error or "no answer")}
    if attempts >= max_total or days_elapsed >= days:
        fields["call_status"] = "failed"
        fields["next_attempt_at"] = None
    else:
        fields["call_status"] = "pending"
        if int(cc.get("day_attempts") or 0) >= max_day:
            # Daily quota spent → resume next calendar day at window start; date AND time must be computed in the calling tz (UTC math mis-dates near local midnight).
            tz = callbacks._tz()
            start_min = callbacks.campaign_window(campaign)[0]
            now_local = now.astimezone(tz)
            nxt = datetime.combine(now_local.date() + timedelta(days=1),
                                   dtime(start_min // 60, start_min % 60), tzinfo=tz)
        else:
            nxt = now + timedelta(hours=delay_h)
        fields["next_attempt_at"] = _iso(nxt)
    eo_db.cc_update(cc["id"], **fields)


async def _reap_calling(campaign, now):
    ring_window = _cfg_int("EO_CAMPAIGN_NOANSWER_SECONDS", 90)
    for cc in eo_db.cc_by_status(campaign["id"], "calling"):
        last = _parse(cc.get("last_attempt_at")) or now
        rec = await store.find_campaign_call(campaign["id"], cc["phone"], since_iso=cc.get("last_attempt_at"))
        if rec:
            if rec.get("ended_at"):
                outcome = rec.get("rsvp_outcome_status") or ("yes" if rec.get("booking_created") else None)
                if outcome in _UNANSWERED_OUTCOMES:
                    # A machine answered: that is a no-answer, not a final outcome. Keep the marker
                    # + call link, then retry like a ring-out.
                    eo_db.cc_update(cc["id"], rsvp_outcome=outcome, last_call_id=rec.get("id"))
                    _apply_failure(cc, campaign, now, error=outcome)
                else:
                    fields = dict(call_status="done", rsvp_outcome=outcome, last_call_id=rec.get("id"))
                    # the agent's note rides onto the contact's Remark — never over a human edit
                    note = (rec.get("remark") or rec.get("rsvp_note") or "").strip()
                    if note and not (cc.get("remark") or "").strip():
                        fields["remark"] = note
                    eo_db.cc_update(cc["id"], **fields)
            # else: still on the call — leave it as 'calling'
        elif (now - last).total_seconds() > ring_window:
            _apply_failure(cc, campaign, now, error="no answer")


async def _dial_one(cc, campaign, now):
    today = now.date().isoformat()
    day_attempts = (int(cc.get("day_attempts") or 0) + 1) if cc.get("day_key") == today else 1
    attempts = int(cc.get("attempts") or 0) + 1
    # claim BEFORE dialing so a crash can't double-dial silently
    eo_db.cc_update(cc["id"], call_status="calling", attempts=attempts, day_attempts=day_attempts,
                    day_key=today, last_attempt_at=_iso(now), next_attempt_at=None)
    cc = {**cc, "attempts": attempts, "day_attempts": day_attempts, "day_key": today}
    try:
        res = await asyncio.wait_for(
            dialer.place_call(cc["phone"], base_url=os.getenv("PUBLIC_URL"),
                              name=cc.get("name") or "", campaign_id=campaign["id"],
                              # Which script this call speaks, and for whom.
                              agent_id=campaign.get("agent_id"),
                              event_id=campaign.get("event_id"),
                              wedding_id=campaign.get("wedding_id"),
                              guest_id=cc.get("contact_id"),
                              provider=eo_db.user_provider(campaign.get("created_by")) or None),
            timeout=_cfg_int("EO_CAMPAIGN_DIAL_TIMEOUT", 60))
    except asyncio.TimeoutError:
        res = {"error": "dial timeout"}
    if res.get("success"):
        eo_db.cc_update(cc["id"], last_call_id=res.get("call_uuid"))
    else:
        _apply_failure(cc, campaign, now, error=res.get("error"))


async def dial_contact_now(campaign_id, cc_id):
    """Admin 'Call now': dial ONE campaign contact immediately, even if the campaign is
    scheduled for later or already finished. Re-activates a non-live campaign to live so the
    runner then tracks the call (reap / retries), guarded by the active-campaign cap.
    Returns {'ok': True} or {'error': '...'}."""
    campaign = eo_db.get_campaign(campaign_id)
    cc = eo_db.get_campaign_contact(cc_id)
    if not campaign or not cc or int(cc.get("campaign_id") or 0) != int(campaign_id):
        return {"error": "not found"}
    if cc.get("call_status") == "calling":
        return {"error": "already calling"}
    if not _plivo_ready():
        return {"error": "Plivo is not configured on the server (PLIVO_* / PUBLIC_URL)"}
    prev = campaign.get("status")
    if prev != "live":
        others = [c for c in eo_db.active_campaigns() if int(c["id"]) != int(campaign_id)]
        cap = _max_active_campaigns()
        if len(others) >= cap:
            return {"error": f"{len(others)} campaigns are already active (limit {cap}) — "
                             f"finish or cancel one first."}
        eo_db.set_campaign_status(campaign_id, "live")
        campaign = {**campaign, "status": "live"}
        logger.info(f"Campaign {campaign_id} re-activated to live via Call-now (was {prev})")
    await _dial_one(cc, campaign, _now())
    logger.info(f"Call-now dialed contact {cc_id} ({cc.get('phone')}) in campaign {campaign_id}")
    return {"ok": True}


async def _process_campaign(campaign, now):
    await _reap_calling(campaign, now)
    if eo_db.cc_open_count(campaign["id"]) == 0:
        eo_db.set_campaign_status(campaign["id"], "completed")
        logger.info(f"Campaign {campaign['id']} '{campaign['name']}' completed")
        return
    if not _plivo_ready():
        return                       # nothing to dial with (dev/local) — leave pending
    # Calling-hours hard stop: no new dials outside the campaign window (reap/completion above still run).
    if not callbacks.in_call_window(campaign.get("call_start_min"), campaign.get("call_end_min")):
        return
    calling = len(eo_db.cc_by_status(campaign["id"], "calling"))
    budget = min(_cfg_int("EO_CAMPAIGN_MAX_PER_TICK", 3),
                 max(0, _cfg_int("EO_CAMPAIGN_MAX_CONCURRENT", 10) - calling))
    # Campaign + callbacks share MAX_LIVE_CALLS; reserve a slot per due callback so callbacks get first claim.
    due_callbacks = len(await store.list_pending_callbacks(_iso(now)))
    budget = min(budget, max(0, live.room() - due_callbacks))
    if budget <= 0:
        return
    for cc in eo_db.cc_pending_due(campaign["id"], _iso(now), budget):
        await _dial_one(cc, campaign, _now())


# Rotates the order campaigns are served in, so no single campaign can monopolise the
# shared MAX_LIVE_CALLS budget tick after tick (see _tick).
_tick_count = 0


def _fair_order(campaigns):
    """Rotate the campaign list by tick number.

    The global dial budget is `live.room()` shared across every live campaign, and it is
    consumed in iteration order. With one campaign that never mattered; with six reminder
    batches on the wedding afternoon, a fixed order means campaign #1 takes the whole
    budget every tick and #2-#6 never dial at all. Rotating gives each one the first
    claim in turn."""
    if len(campaigns) <= 1:
        return campaigns
    offset = _tick_count % len(campaigns)
    return campaigns[offset:] + campaigns[:offset]


async def _tick():
    global _tick_count
    if not scheduler.is_enabled():        # shares the "Scheduler" ON/OFF toggle
        return
    now = _now()
    try:
        eo_db.promote_due_campaigns(_iso(now))
    except Exception as e:
        logger.warning(f"promote_due_campaigns failed: {e}")
    campaigns = _fair_order(eo_db.live_campaigns())
    _tick_count += 1
    for campaign in campaigns:
        try:
            await _process_campaign(campaign, _now())
        except Exception as e:
            logger.warning(f"Campaign {campaign.get('id')} tick error: {e}")


async def run_loop():
    if not _enabled():
        logger.info("Campaign runner disabled (EO_CAMPAIGN_RUNNER_ENABLED)")
        return
    interval = _cfg_int("EO_CAMPAIGN_POLL_INTERVAL", 30)
    logger.info(f"Campaign runner started (interval={interval}s, plivo_ready={_plivo_ready()}, "
                f"max_concurrent={_cfg_int('EO_CAMPAIGN_MAX_CONCURRENT', 10)}, "
                f"per_tick={_cfg_int('EO_CAMPAIGN_MAX_PER_TICK', 3)}, "
                f"max_active_campaigns={_max_active_campaigns()})")
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            logger.info("Campaign runner loop cancelled")
            raise
        except Exception as e:
            logger.warning(f"Campaign runner tick error: {e}")
        await asyncio.sleep(interval)
