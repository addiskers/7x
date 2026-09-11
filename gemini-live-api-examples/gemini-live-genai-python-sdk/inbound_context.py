"""
Inbound call-back context — when a guest dials our number back (usually after seeing a
missed call from us), look the caller up in the campaign contact list and build the exact
opening instruction ("trigger") the agent should follow, e.g.:

    "Hi Rajesh! Thanks for calling back — I tried reaching you a little while ago
     on behalf of the hospitality team, but I believe you were unavailable..."

The trigger only ever tells the agent WHO is calling and WHAT happened on our last
attempt. It never scripts the actual message: that lives in the agent's own prompt
(see prompt_render), so a reminder agent gives its reminder and a logistics agent
confirms travel, each from the same inbound trigger.

All classification and date math happens HERE in Python — the model is never asked to
reason about timestamps or guess call history. Pure read-side helper: never raises; an
unknown number gets a safe generic-greeting trigger with NO invented context (a wrong
name or a made-up "we called you" is worse than a plain hello).
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import directory
import eo_db

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# "Carry on with your job" — the agent's prompt says what that job actually is.
_PROCEED = "continue with the purpose of your call as described in your instructions"

# Caller not in any campaign and not in the guest directory.
UNKNOWN_TRIGGER = (
    "[INBOUND CALL: someone has just called our number. You do NOT know who they are — "
    "never invent a name and never claim you called them. Greet them warmly, say who you "
    f"are calling on behalf of, and ask how you can help. If they say we called them, "
    f"apologise lightly for missing each other and {_PROCEED}.]"
)

# Outcomes meaning we never actually spoke to a person.
_UNANSWERED = ("voicemail", "not_reachable")
# Outcomes meaning the guest already gave us the answer we wanted.
_SETTLED = ("yes", "acknowledged", "confirmed")
# Outcomes meaning they declined.
_DECLINED = ("no", "declined")


def _when_phrase(last_attempt_iso, now=None):
    """Humanize an ISO timestamp into a spoken-style phrase, bucketed by IST calendar
    day: 'a little while ago' (<2h), 'earlier today', 'yesterday', 'a few days ago'."""
    try:
        dt = datetime.fromisoformat(str(last_attempt_iso))
    except (TypeError, ValueError):
        return "recently"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    delta = (now - dt).total_seconds()
    if delta < 0:
        return "recently"
    if delta < 2 * 3600:
        return "a little while ago"
    today = now.astimezone(_IST).date()
    then = dt.astimezone(_IST).date()
    days = (today - then).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    return "a few days ago"


def _first_name(cc, phone):
    """Campaign-contact name (the name we greeted them with on the outbound leg)
    wins over the guest directory; either may be empty."""
    name = str((cc or {}).get("name") or "").strip()
    if name:
        return name.split()[0]
    return directory.first_name_for(phone)


def _missed_trigger(first_name, when, agent_reason, member_line):
    """Trigger for the headline case: we tried them, failed, and they called back."""
    if first_name:
        return (
            f"[INBOUND CALL-BACK: {first_name} is calling US back — we tried calling them "
            f"{when} but {agent_reason}. Open with, in your own warm words keeping every fact: "
            f'"Hi {first_name}! Thanks for calling back — I tried reaching you {when}, '
            f'{member_line}." Then {_PROCEED}. Do NOT ask who they are, do NOT say "am I '
            f"speaking to…?\", and mention the missed call only this once.]"
        )
    return (
        f"[INBOUND CALL-BACK: the caller is a guest we tried calling {when} but "
        f"{agent_reason}. You were NOT given their name — never invent one. Open with, in "
        f'your own warm words: "Hello! Thanks for calling back — I tried reaching you {when}, '
        f'{member_line}." Then {_PROCEED}, and mention the missed call only this once.]'
    )


def build(raw_from, now=None):
    """Context for an inbound call from `raw_from` (Plivo `From`, any format).

    Returns {"phone": E.164, "name": first name or "", "campaign_id": int|None,
             "trigger": opening instruction for the agent}. Never raises.
    """
    phone = directory.normalize_phone(raw_from)
    out = {"phone": phone, "name": "", "campaign_id": None, "trigger": UNKNOWN_TRIGGER}
    if not phone:
        return out

    try:
        cc = eo_db.cc_find_recent_by_phone(phone)
    except Exception as e:
        logger.warning(f"Inbound context lookup failed for {phone}: {e}")
        cc = None

    first_name = _first_name(cc, phone)
    out["name"] = first_name

    if not cc:
        if first_name:  # in the guest directory, but never part of a campaign
            out["trigger"] = (
                f"[INBOUND CALL: {first_name} is calling our number. Greet them warmly by "
                f'name — "Hi {first_name}!" — say who you are calling on behalf of, and ask '
                f"how you can help. Then {_PROCEED}. Do NOT claim we called them.]"
            )
        return out

    out["campaign_id"] = cc.get("campaign_id")
    outcome = str(cc.get("rsvp_outcome") or "").strip().lower()
    attempts = int(cc.get("attempts") or 0)
    when = _when_phrase(cc.get("last_attempt_at"), now=now)
    who = first_name or "the caller"

    if outcome in _UNANSWERED:
        out["trigger"] = _missed_trigger(
            first_name, when,
            agent_reason="only reached their voicemail",
            member_line="but I'm afraid it went to your voicemail")
    elif outcome in _SETTLED:
        out["trigger"] = (
            f"[INBOUND CALL: {who} is calling us, and we have ALREADY had this conversation "
            f"with them — they gave us their answer on the earlier call. Thank them warmly "
            f"for calling and ask how you can help. Do NOT repeat your message from scratch "
            f"and do NOT re-record the outcome — only call record_outcome again if they "
            f"clearly change what they told us.]"
        )
    elif outcome in _DECLINED:
        out["trigger"] = (
            f"[INBOUND CALL: {who} is calling us; on our earlier call they declined. Thank "
            f"them warmly for calling and ask how you can help. Don't push — but if they say "
            f"they've changed their mind, delightedly record the new answer.]"
        )
    elif outcome == "callback":
        out["trigger"] = (
            f"[INBOUND CALL-BACK: {who} is calling US back — they had earlier asked us to "
            f'call them back. Thank them warmly for calling in ("so glad you called!"), then '
            f"{_PROCEED}. Do NOT ask who they are.]"
        )
    elif outcome == "do_not_contact":
        out["trigger"] = (
            f"[INBOUND CALL: {who} is calling us. NOTE: they had asked not to be contacted "
            f"again — but THEY called US, so simply be warm and helpful and answer their "
            f"questions. Do NOT deliver your message unless they themselves ask about it.]"
        )
    elif outcome == "wrong_number":
        out["trigger"] = UNKNOWN_TRIGGER   # number was confirmed not the guest's
        out["name"] = ""
        out["campaign_id"] = None
    elif attempts > 0:
        out["trigger"] = _missed_trigger(
            first_name, when,
            agent_reason="could not reach them",
            member_line="but I believe you were unavailable")
    else:
        out["trigger"] = (
            f"[INBOUND CALL: {who} is calling our number. They're on our list but we had NOT "
            f"called them yet — so do NOT claim we tried calling. Greet warmly, say who you "
            f"are calling on behalf of, and {_PROCEED}.]"
        )
    return out
