"""
EO Admin API — `/api/eo/*`. Session-gated (bearer token), COST-FREE (no gemini /
twilio / total cost anywhere). Reuses store.py / scheduler.py for calls +
callbacks; adds contacts, campaigns, and users (later phases). The Super-Admin
`/api/admin/*` endpoints are untouched and keep cost.
"""

import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import agent_tools
import callbacks
import eo_auth
import eo_db
import eo_import
import prompt_render
import scheduler
import store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/eo")


def _invalidate_call_cache():
    """Drop main's 60s agent/event/wedding row cache after an edit, so the next call
    speaks the new text rather than the old. Imported lazily: main imports this module."""
    try:
        import main
        main.invalidate_ctx_cache()
    except Exception:                      # never let a cache miss break a write
        logger.debug("could not invalidate the call context cache", exc_info=True)

# Cost / token fields that must NEVER reach an EO user.
_CALL_COST_KEYS = {"gemini_cost_usd", "twilio", "total_cost_usd", "cost_estimated", "tokens", "gemini_model"}
_SUMMARY_COST_KEYS = {
    "total_cost_usd", "gemini_cost_usd", "twilio_cost_usd", "avg_cost_per_call",
    "projected_month_cost", "pending_twilio_price",
}


def _filters_from_request(request: Request) -> dict:
    qp = request.query_params
    return {
        "source": qp.get("source") or None,
        "from": qp.get("from") or None,
        "to": qp.get("to") or None,
        "q": qp.get("q") or None,
        "booking": qp.get("booking"),
        "campaign_id": qp.get("campaign_id") or None,
        "limit": qp.get("limit"),
        "offset": qp.get("offset"),
    }


def _strip_summary(s: dict, include_cost: bool = False) -> dict:
    """Dashboard summary. eo_admin gets cost; eo_agent gets a cost-free view."""
    s = dict(s)
    if include_cost:
        return s
    for k in _SUMMARY_COST_KEYS:
        s.pop(k, None)
    if isinstance(s.get("this_month"), dict):
        s["this_month"] = {"calls": s["this_month"].get("calls")}
    s["by_day"] = [{"date": d.get("date"), "calls": d.get("calls")} for d in s.get("by_day", [])]
    return s


def _campaign_label(call, meta_by_id):
    """Campaign name for a call — but ONLY if the call started at/after that campaign was
    created. Guards against a call record (which survived a DB reset) being mislabelled by a
    NEW campaign that reused the old campaign's numeric id."""
    cid = call.get("campaign_id")
    if not cid:
        return None
    m = meta_by_id.get(int(cid))
    if not m or (call.get("started_at") or "") < (m.get("created_at") or ""):
        return None
    return m["name"]


def _campaign_since(filters: dict) -> dict:
    """When filtering by ONE campaign, set `since` = its created_at so calls that predate it
    (stale records with a reused campaign id after a DB reset) don't show in that campaign."""
    cid = filters.get("campaign_id")
    if cid:
        camp = eo_db.get_campaign(cid)
        if camp:
            filters["since"] = camp.get("created_at")
    return filters


def _with_name_search(filters: dict) -> dict:
    """Call records store only the phone, so a name search resolves to phones first:
    any caller whose contact name (pool or campaign recipient) matches `q` also hits."""
    if filters.get("q"):
        filters["q_phones"] = eo_db.phones_by_name_query(filters["q"])
    return filters


def _label_and_strip(items: list[dict], include_cost: bool = False) -> list[dict]:
    """Attach campaign_name + contact_name; strip cost fields unless include_cost (eo_admin).
    Call records store only the phone (`caller`), so the person's name is resolved by phone —
    preferring the campaign contact name we greeted with, then the global contacts pool."""
    meta = eo_db.campaign_meta([c.get("campaign_id") for c in items if c.get("campaign_id")])
    cc_names = eo_db.names_by_campaign_phone(
        [(c["campaign_id"], c["caller"]) for c in items if c.get("campaign_id") and c.get("caller")])
    phone_names = eo_db.names_by_phone([c.get("caller") for c in items if c.get("caller")])
    out = []
    for c in items:
        c = dict(c) if include_cost else {k: v for k, v in c.items() if k not in _CALL_COST_KEYS}
        c["campaign_name"] = _campaign_label(c, meta)
        cid, phone = c.get("campaign_id"), c.get("caller")
        c["contact_name"] = ((cc_names.get((int(cid), str(phone))) if cid and phone else None)
                             or (phone_names.get(str(phone)) if phone else None) or "")
        c["has_recording"] = store.has_recording(c.get("call_sid"))
        c["rsvp_outcome_label"] = _rsvp_label(c.get("rsvp_outcome_status"))
        out.append(c)
    return out


def _strip_full(call: dict, include_cost: bool = False) -> dict:
    """Full call record (for the transcript drawer). eo_admin keeps cost."""
    if include_cost:
        c = dict(call)
        c.setdefault("messages", c.get("transcript") or [])
        cid = c.get("campaign_id")
        if cid:
            c["campaign_name"] = _campaign_label(c, eo_db.campaign_meta([cid]))
        c["has_recording"] = store.has_recording(c.get("call_sid"))
        return c
    c = {k: v for k, v in call.items() if k not in _CALL_COST_KEYS}
    for k in ("cost", "pricing"):
        c.pop(k, None)
    if isinstance(c.get("summary"), dict):
        c["summary"] = {k: v for k, v in c["summary"].items() if k not in _SUMMARY_COST_KEYS}
    # normalise transcript key for the SPA (it reads `messages` or `transcript`)
    c.setdefault("messages", c.get("transcript") or [])
    cid = c.get("campaign_id")
    if cid:
        c["campaign_name"] = _campaign_label(c, eo_db.campaign_meta([cid]))
    c["has_recording"] = store.has_recording(c.get("call_sid"))
    return c


# Per-role data scoping
def _scope_ids(user):
    """Campaign-id strings this user may see, or None for full access (eo_admin =
    Superadmin). An eo_agent (Admin) only sees campaigns they created + the calls
    from those campaigns."""
    if user["role"] == "eo_admin":
        return None
    return {str(i) for i in eo_db.campaign_ids_by_owner(user["id"])}


def _owns_or_admin(user, campaign) -> bool:
    return bool(campaign) and (user["role"] == "eo_admin" or campaign.get("created_by") == user["id"])


def _wedding_or_404(user, wedding_id):
    """The wedding, if this user may see it. 404 (not 403) so an id probe reveals nothing."""
    w = eo_db.get_wedding(wedding_id)
    if not w or not (user["role"] == "eo_admin" or w.get("created_by") == user["id"]):
        raise HTTPException(status_code=404, detail="Wedding not found")
    return w


def _event_or_404(user, event_id):
    e = eo_db.get_event(event_id)
    if not e:
        raise HTTPException(status_code=404, detail="Event not found")
    _wedding_or_404(user, e["wedding_id"])          # inherits the wedding's visibility
    return e


def _agent_or_404(user, agent_id):
    a = eo_db.get_agent(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    if a.get("wedding_id"):
        _wedding_or_404(user, a["wedding_id"])
    return a


def _clean_str(body, key, *, max_len=500, required=False, label=None):
    val = str(body.get(key) or "").strip()
    if required and not val:
        raise HTTPException(status_code=400, detail=f"{label or key} is required")
    if len(val) > max_len:
        raise HTTPException(status_code=400,
                            detail=f"{label or key} is too long (max {max_len} characters)")
    return val


# Display statuses (labels only — raw enums stay for logic/actions)
# variant maps to the SPA pill palette: green | blue | amber | red
# Display labels for every outcome any shipped agent can record. Agents define their own
# vocabulary (agent_tools), so this is the union across agent kinds — an outcome with no entry
# here falls back to a humanised form of the raw value (see _rsvp_label).
_RSVP_LABELS = {
    # reminder agent
    "acknowledged": ("Acknowledged", "green"),
    "not_reachable": ("Not reachable", "amber"),
    # logistics agent
    "confirmed": ("Confirmed", "green"),
    "details_changed": ("Details changed", "amber"),
    # shared
    "callback": ("Callback requested", "amber"),
    "do_not_contact": ("Do not contact", "red"),
    "wrong_number": ("Wrong number", "red"),
    # legacy RSVP vocabulary — kept so historic call records still label correctly
    "yes": ("Attending", "green"),
    "no": ("Declined", "red"),
    "voicemail": ("Voicemail", "amber"),
}
_CALLBACK_LABELS = {
    "pending": ("Scheduled", "amber"),
    "in_flight": ("Dialing", "blue"),
    "completed": ("Called back", "green"),
    "failed": ("Failed", "red"),
    "cancelled": ("Cancelled", "red"),
}


def _rsvp_label(value):
    """Display label for an rsvp outcome; unknown values pass through raw."""
    if value in _RSVP_LABELS:
        return _RSVP_LABELS[value][0]
    return value or None


def _contact_display(cc, campaign=None, scheduler_on=True, now=None, now_min=None):
    """Human status for a campaign_contacts row: (display_status, display_variant).
    Explains WHY a past-due retry isn't dialing (campaign not live / scheduler off /
    outside calling hours) instead of showing a stale 'Pending'."""
    st = cc.get("call_status")
    outcome = cc.get("rsvp_outcome")
    if st == "calling":
        return ("In progress", "blue")
    if st == "cancelled":
        return ("Cancelled", "red")
    if st == "failed":
        return ("Unreachable — max attempts", "red")
    if st == "done":
        if outcome in _RSVP_LABELS:
            return _RSVP_LABELS[outcome]
        if outcome:
            return (str(outcome), "green")
        return ("Answered — no RSVP captured", "amber")
    # pending (and the legacy, never-written 'no_answer')
    if int(cc.get("attempts") or 0) == 0:
        return ("Queued", "amber")
    # rsvp_outcome='voicemail' is the typed source of truth; last_error text is a fallback for older rows
    reason = ("voicemail" if outcome == "voicemail"
              or "voicemail" in (cc.get("last_error") or "").lower() else "no answer")
    nxt = _parse_iso(cc.get("next_attempt_at"))
    if nxt and nxt > (now or datetime.now(timezone.utc)):
        return (f"Retry scheduled — {reason}", "amber")
    # past due: explain what's holding the dial
    if campaign and campaign.get("status") != "live":
        return ("Waiting — campaign not active", "amber")
    if not scheduler_on:
        return ("Waiting — scheduler off", "amber")
    if campaign is not None:
        try:
            start_min, end_min = callbacks.campaign_window(campaign)
            if not callbacks.in_call_window(start_min, end_min, now_min=now_min):
                return ("Waiting for calling hours", "amber")
        except Exception:
            pass
    return (f"Due now — {reason}", "amber")


def _attach_contact_display(items, campaign=None, scheduler_on=True):
    now = datetime.now(timezone.utc)            # per-request constants, not per-row
    now_min = callbacks.now_ist_min()
    for cc in items:
        camp = campaign
        if camp is None and cc.get("campaign_status") is not None:
            # queue rows carry their campaign fields inline (cc_upcoming join)
            camp = {"status": cc.get("campaign_status"),
                    "call_start_min": cc.get("campaign_call_start_min"),
                    "call_end_min": cc.get("campaign_call_end_min")}
        label, variant = _contact_display(cc, camp, scheduler_on, now=now, now_min=now_min)
        cc["display_status"] = label
        cc["display_variant"] = variant
        cc["rsvp_outcome_label"] = _rsvp_label(cc.get("rsvp_outcome"))
    return items


def _clean_remark(body) -> str:
    remark = str((body or {}).get("remark") or "").strip()
    if len(remark) > 500:
        raise HTTPException(status_code=400, detail="Remark is too long (max 500 characters)")
    return remark


# Auth
@router.post("/login")
async def login(request: Request):
    body = await request.json()
    user = eo_auth.authenticate((body.get("username") or "").strip(), body.get("password") or "")
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = eo_auth.issue_token(user)
    return {"ok": True, "token": token,
            "user": {"id": user["id"], "username": user["username"], "name": user.get("name"), "role": user["role"]}}


@router.post("/logout")
async def logout(request: Request):
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = eo_auth.require_eo(request)
    return {"ok": True, "user": {"id": user["id"], "username": user["username"], "name": user.get("name"), "role": user["role"]}}


@router.post("/me/password")
async def me_password(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    if not eo_auth.verify_password(body.get("current") or "", user["password_hash"], user["password_salt"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new = body.get("new") or ""
    if len(new) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    h, s = eo_auth.hash_password(new)
    eo_db.update_user_password(user["id"], h, s)
    return {"ok": True}


# Users (eo_admin only)
@router.get("/users")
async def users_list(request: Request):
    eo_auth.require_eo_admin(request)
    return JSONResponse({"items": eo_db.list_users()})


@router.post("/users")
async def users_create(request: Request):
    eo_auth.require_eo_admin(request)
    body = await request.json()
    username = (body.get("username") or "").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if eo_db.get_user_by_username(username):
        raise HTTPException(status_code=409, detail="That username already exists")
    password = body.get("password") or ""
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    role = body.get("role") if body.get("role") in ("eo_admin", "eo_agent") else "eo_agent"
    provider = body.get("provider") if body.get("provider") in ("plivo", "enablex") else "plivo"
    h, s = eo_auth.hash_password(password)
    uid = eo_db.create_user(username, (body.get("name") or "").strip(), h, s, role, provider=provider)
    return {"ok": True, "id": uid}


@router.patch("/users/{user_id}")
async def users_update(user_id: int, request: Request):
    admin = eo_auth.require_eo_admin(request)
    body = await request.json()
    if "active" in body:
        if int(user_id) == int(admin["id"]) and not body["active"]:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        eo_db.set_user_active(int(user_id), bool(body["active"]))
    if "provider" in body:
        if body["provider"] not in ("plivo", "enablex"):
            raise HTTPException(status_code=400, detail="Provider must be 'plivo' or 'enablex'")
        eo_db.set_user_provider(int(user_id), body["provider"])
    return {"ok": True}


# Dashboard + call logs (cost for Superadmin; Admins see only their calls)
@router.get("/summary")
async def eo_summary(request: Request):
    user = eo_auth.require_eo(request)
    include_cost = user["role"] == "eo_admin"
    filters = _filters_from_request(request)
    scope = _scope_ids(user)
    if scope is not None:
        filters["campaign_ids"] = scope
    s = await store.summary(filters)
    # Unique RSVP rollup: deduped by phone WITHIN each campaign, summed across campaigns.
    uniq = eo_db.rsvp_yes_unique_totals(scope)
    s["unique_yes"] = uniq["yes"]
    s["unique_responded"] = uniq["responded"]
    s["unique_yes_rate"] = round(uniq["yes"] / uniq["responded"], 4) if uniq["responded"] else 0.0
    return JSONResponse(_strip_summary(s, include_cost))


@router.get("/calls")
async def eo_calls(request: Request):
    user = eo_auth.require_eo(request)
    include_cost = user["role"] == "eo_admin"
    filters = _with_name_search(_campaign_since(_filters_from_request(request)))
    scope = _scope_ids(user)
    if scope is not None:
        filters["campaign_ids"] = scope
    if filters.get("limit") is None:
        filters["limit"] = 500
    data = await store.list_calls(filters)
    data["items"] = _label_and_strip(data["items"], include_cost)
    return JSONResponse(data)


@router.get("/calls.csv")
async def eo_calls_csv(request: Request):
    user = eo_auth.require_eo(request)
    include_cost = user["role"] == "eo_admin"
    filters = _with_name_search(_campaign_since(_filters_from_request(request)))
    scope = _scope_ids(user)
    if scope is not None:
        filters["campaign_ids"] = scope
    filters["limit"] = None
    data = await store.list_calls(filters)
    items = _label_and_strip(data["items"], include_cost)
    buf = io.StringIO()
    # Deliberately limited export columns — everything else is on the grid.
    cols = ["contact_name", "caller", "started_at", "status", "rsvp_outcome_status",
            "duration_seconds", "remark"]
    headers = ["Name", "Phone", "Date/Time", "Status", "Outcome", "Duration (s)", "Remark"]
    w = csv.writer(buf)
    w.writerow(headers)
    for c in items:
        w.writerow([c.get(k) for k in cols])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=call_logs.csv"})


@router.get("/calls/{call_id}")
async def eo_call_detail(call_id: str, request: Request):
    user = eo_auth.require_eo(request)
    include_cost = user["role"] == "eo_admin"
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    scope = _scope_ids(user)
    if scope is not None and str(call.get("campaign_id") or "") not in scope:
        raise HTTPException(status_code=404, detail="Call not found")
    return JSONResponse(_strip_full(call, include_cost))


@router.get("/calls/{call_id}/audio")
async def eo_call_audio(call_id: str, request: Request):
    """Stream a call's recording (WAV). Same auth + per-agent scoping as the transcript."""
    user = eo_auth.require_eo(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    scope = _scope_ids(user)
    if scope is not None and str(call.get("campaign_id") or "") not in scope:
        raise HTTPException(status_code=404, detail="Call not found")
    sid = call.get("call_sid")
    if not store.has_recording(sid):
        raise HTTPException(status_code=404, detail="Recording not found")
    return FileResponse(store.recording_path(sid), media_type="audio/wav",
                        filename=f"call-{call_id}.wav")


# Campaigns
def _whole_int(v, name, lo, hi):
    """Accept only a whole number in [lo, hi]; reject decimals / junk with a 400."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number")
    if f != int(f):
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number (no decimals)")
    n = int(f)
    if n < lo or n > hi:
        raise HTTPException(status_code=400, detail=f"{name} must be between {lo} and {hi}")
    return n


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@router.get("/campaigns")
async def campaigns_list(request: Request):
    user = eo_auth.require_eo(request)
    qp = request.query_params
    data = eo_db.list_campaigns(
        q=qp.get("q") or None,
        sort=qp.get("sort") or "created_at",
        direction=qp.get("dir") or "desc",
        limit=int(qp.get("limit") or 50),
        offset=int(qp.get("offset") or 0),
        created_by=None if user["role"] == "eo_admin" else user["id"],
    )
    for c in data["items"]:
        c["progress"] = eo_db.campaign_progress(c["id"])
    return JSONResponse(data)


@router.get("/campaigns/active")
async def campaign_active(request: Request):
    user = eo_auth.require_eo(request)
    c = eo_db.active_campaign()
    if c and not _owns_or_admin(user, c):
        c = None                       # Admins only see their own live campaign
    if c:
        c["progress"] = eo_db.campaign_progress(c["id"])
    return JSONResponse({"campaign": c})


# ---------------------------------------------------------------------------------------
# Weddings
# ---------------------------------------------------------------------------------------
@router.get("/weddings")
async def weddings_list(request: Request):
    user = eo_auth.require_eo(request)
    scope = None if user["role"] == "eo_admin" else int(user["id"])
    items = eo_db.list_weddings(created_by=scope,
                                status=request.query_params.get("status") or None)
    for w in items:
        w["event_count"] = len(eo_db.list_events(w["id"]))
    return JSONResponse({"items": items, "total": len(items)})


@router.post("/weddings")
async def weddings_create(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    name = _clean_str(body, "name", max_len=120, required=True, label="Wedding name")
    fields = {k: _clean_str(body, k, max_len=300) for k in eo_db.WEDDING_FIELDS
              if k not in ("name", "status") and k in body}
    wid = eo_db.create_wedding(name, created_by=user["id"], **fields)
    return JSONResponse(eo_db.get_wedding(wid), status_code=201)


@router.get("/weddings/{wedding_id}")
async def weddings_detail(wedding_id: int, request: Request):
    user = eo_auth.require_eo(request)
    w = _wedding_or_404(user, wedding_id)
    w["events"] = eo_db.list_events(wedding_id)
    w["guest_count"] = len(eo_db.guests_for_audience(
        wedding_id, "all", created_by=_contact_owner_scope(user)))
    return JSONResponse(w)


@router.patch("/weddings/{wedding_id}")
async def weddings_update(wedding_id: int, request: Request):
    user = eo_auth.require_eo(request)
    _wedding_or_404(user, wedding_id)
    body = await request.json()
    fields = {k: _clean_str(body, k, max_len=300) for k in eo_db.WEDDING_FIELDS if k in body}
    if "name" in fields and not fields["name"]:
        raise HTTPException(status_code=400, detail="Wedding name is required")
    eo_db.update_wedding(wedding_id, **fields)
    _invalidate_call_cache()
    return JSONResponse(eo_db.get_wedding(wedding_id))


@router.delete("/weddings/{wedding_id}")
async def weddings_delete(wedding_id: int, request: Request):
    user = eo_auth.require_eo(request)
    _wedding_or_404(user, wedding_id)
    # Deleting cascades to this wedding's events, agents and guests, so refuse while a
    # campaign is still live — that would strand a running dialer with no script.
    for c in eo_db.active_campaigns():
        if c.get("wedding_id") == wedding_id:
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{c['name']}' is still {c['status']}. Cancel it first.")
    eo_db.delete_wedding(wedding_id)
    _invalidate_call_cache()
    return {"ok": True}


# ---------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------
@router.get("/weddings/{wedding_id}/events")
async def events_list(wedding_id: int, request: Request):
    user = eo_auth.require_eo(request)
    _wedding_or_404(user, wedding_id)
    items = eo_db.list_events(wedding_id)
    return JSONResponse({"items": items, "total": len(items)})


@router.post("/events")
async def events_create(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    wedding_id = body.get("wedding_id")
    if not wedding_id:
        raise HTTPException(status_code=400, detail="wedding_id is required")
    _wedding_or_404(user, wedding_id)
    name = _clean_str(body, "name", max_len=160, required=True, label="Event name")
    fields = _event_fields(body)
    eid = eo_db.create_event(wedding_id, name, **fields)
    return JSONResponse(eo_db.get_event(eid), status_code=201)


@router.patch("/events/{event_id}")
async def events_update(event_id: int, request: Request):
    user = eo_auth.require_eo(request)
    _event_or_404(user, event_id)
    body = await request.json()
    fields = _event_fields(body)
    if "name" in body:
        fields["name"] = _clean_str(body, "name", max_len=160, required=True,
                                    label="Event name")
    eo_db.update_event(event_id, **fields)
    _invalidate_call_cache()
    return JSONResponse(eo_db.get_event(event_id))


@router.delete("/events/{event_id}")
async def events_delete(event_id: int, request: Request):
    user = eo_auth.require_eo(request)
    _event_or_404(user, event_id)
    for c in eo_db.active_campaigns():
        if c.get("event_id") == event_id:
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{c['name']}' is still {c['status']}. Cancel it first.")
    eo_db.delete_event(event_id)
    _invalidate_call_cache()
    return {"ok": True}


def _event_fields(body):
    """Validated event fields present in the body. Times stay 'HH:MM' — prompt_render
    converts them to spoken form at call time."""
    fields = {}
    for key in ("event_date", "start_time", "end_time", "venue", "venue_address",
                "dress_code", "announcement", "notes"):
        if key in body:
            fields[key] = _clean_str(body, key, max_len=1000)
    if "audience" in body:
        aud = str(body.get("audience") or "all").strip().lower()
        if aud not in eo_db.AUDIENCES:
            raise HTTPException(status_code=400,
                                detail=f"audience must be one of: {', '.join(eo_db.AUDIENCES)}")
        fields["audience"] = aud
    if "sort_order" in body:
        fields["sort_order"] = _whole_int(body.get("sort_order", 0), "Sort order", 0, 999)
    return fields


# ---------------------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------------------
@router.get("/agents")
async def agents_list(request: Request):
    user = eo_auth.require_eo(request)
    wedding_id = request.query_params.get("wedding_id") or None
    if wedding_id:
        _wedding_or_404(user, wedding_id)
    items = eo_db.list_agents(wedding_id=wedding_id, active_only=False)
    return JSONResponse({"items": items, "total": len(items),
                         "placeholders": sorted(prompt_render.KNOWN_PLACEHOLDERS)})


@router.get("/agents/{agent_id}")
async def agents_detail(agent_id: int, request: Request):
    user = eo_auth.require_eo(request)
    return JSONResponse(_agent_or_404(user, agent_id))


@router.post("/agents")
async def agents_create(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    if body.get("wedding_id"):
        _wedding_or_404(user, body["wedding_id"])
    name = _clean_str(body, "name", max_len=120, required=True, label="Agent name")
    prompt = str(body.get("prompt_template") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    fields = _agent_fields(body)
    aid = eo_db.create_agent(name, prompt, wedding_id=body.get("wedding_id"),
                             created_by=user["id"], **fields)
    _invalidate_call_cache()
    return JSONResponse(eo_db.get_agent(aid), status_code=201)


@router.patch("/agents/{agent_id}")
async def agents_update(agent_id: int, request: Request):
    user = eo_auth.require_eo(request)
    agent = _agent_or_404(user, agent_id)
    body = await request.json()
    fields = _agent_fields(body, agent)
    if "name" in body:
        fields["name"] = _clean_str(body, "name", max_len=120, required=True,
                                    label="Agent name")
    if "prompt_template" in body:
        prompt = str(body.get("prompt_template") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt is required")
        fields["prompt_template"] = prompt
    eo_db.update_agent(agent_id, **fields)
    _invalidate_call_cache()
    return JSONResponse(eo_db.get_agent(agent_id))


@router.post("/agents/{agent_id}/duplicate")
async def agents_duplicate(agent_id: int, request: Request):
    """Copy an agent — the way a wedding customises a shipped template without editing
    the global row every other wedding uses."""
    user = eo_auth.require_eo(request)
    src = _agent_or_404(user, agent_id)
    body = await request.json() if await request.body() else {}
    wedding_id = body.get("wedding_id") or src.get("wedding_id")
    if wedding_id:
        _wedding_or_404(user, wedding_id)
    name = _clean_str(body, "name", max_len=120) or f"{src['name']} (copy)"
    slug = _clean_str(body, "slug", max_len=60) or src.get("slug") or ""
    if slug and eo_db.get_agent_by_slug(slug, wedding_id=wedding_id) and not wedding_id:
        slug = ""            # don't collide with the global template's unique (NULL, slug)
    aid = eo_db.create_agent(
        name, src["prompt_template"], wedding_id=wedding_id, created_by=user["id"],
        slug=slug, kind=src.get("kind"), description=src.get("description"),
        trigger_template=src.get("trigger_template"), outcome_enum=src.get("outcome_enum"),
        extra_fields=src.get("extra_fields"), voice_name=src.get("voice_name"),
        speech_language_code=src.get("speech_language_code"),
        language_mode=src.get("language_mode"), listen_seconds=src.get("listen_seconds"),
        requires_event=src.get("requires_event"))
    return JSONResponse(eo_db.get_agent(aid), status_code=201)


@router.delete("/agents/{agent_id}")
async def agents_delete(agent_id: int, request: Request):
    user = eo_auth.require_eo(request)
    agent = _agent_or_404(user, agent_id)
    if agent.get("wedding_id") is None:
        raise HTTPException(status_code=400,
                            detail="Shipped agent templates cannot be deleted. "
                                   "Duplicate one to make your own, or deactivate it.")
    for c in eo_db.active_campaigns():
        if c.get("agent_id") == agent_id:
            raise HTTPException(
                status_code=409,
                detail=f"Campaign '{c['name']}' is still {c['status']}. Cancel it first.")
    eo_db.delete_agent(agent_id)
    _invalidate_call_cache()
    return {"ok": True}


def _agent_fields(body, existing=None):
    """Validated agent fields present in the body.

    The prompt is validated against the known placeholder vocabulary here, at save time —
    an unknown placeholder is an authoring typo, and catching it now beats discovering it
    when the agent speaks a gap to a guest."""
    fields = {}
    for key in ("slug", "kind", "description", "voice_name", "speech_language_code",
                "language_mode"):
        if key in body:
            fields[key] = _clean_str(body, key, max_len=200)
    if "trigger_template" in body:
        fields["trigger_template"] = str(body.get("trigger_template") or "").strip()
    if "listen_seconds" in body:
        fields["listen_seconds"] = _whole_int(body.get("listen_seconds", 0),
                                              "Listen seconds", 0, 120)
    for flag in ("requires_event", "active"):
        if flag in body:
            fields[flag] = 1 if body.get(flag) else 0

    for key in ("outcome_enum", "extra_fields"):
        if key in body:
            fields[key] = _json_list(body.get(key), key)

    # Validate the templates: unknown placeholders, and any tool name the agent cannot call.
    merged = {**(existing or {}), **fields}
    if "prompt_template" in body:
        merged["prompt_template"] = body.get("prompt_template") or ""
    for tkey in ("prompt_template", "trigger_template"):
        text = merged.get(tkey) if tkey in body or tkey in fields else None
        if not text:
            continue
        unknown = prompt_render.validate_template(text)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail="Unknown placeholder(s): " + ", ".join("{%s}" % u for u in unknown)
                       + ". Check the spelling against the placeholder list.")
    prompt_text = merged.get("prompt_template") or ""
    if prompt_text:
        allowed = set(agent_tools.tool_names(merged))
        for bad in ("record_rsvp",):
            if bad in prompt_text and bad not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"The prompt tells the agent to call '{bad}', which does not "
                           f"exist. Use '{'record_outcome'}' instead.")
    return fields


def _json_list(value, label):
    """outcome_enum / extra_fields arrive as a list (or a JSON string). Stored as TEXT."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{label} must be valid JSON")
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{label} must be a list")
    if label == "outcome_enum":
        values = []
        for item in value:
            v = (item.get("value") if isinstance(item, dict) else str(item) or "").strip()
            if not v:
                raise HTTPException(status_code=400,
                                    detail="Every outcome needs a value")
            values.append(v)
        if values and len(set(values)) != len(values):
            raise HTTPException(status_code=400, detail="Duplicate outcome values")
        missing_labels = [v for v in values if v not in _RSVP_LABELS]
        if missing_labels:
            logger.warning("agent outcomes with no display label: %s", missing_labels)
    return json.dumps(value)


# ---------------------------------------------------------------------------------------
# Agent preview / test
# ---------------------------------------------------------------------------------------
@router.post("/agents/{agent_id}/preview")
async def agents_preview(agent_id: int, request: Request):
    """Render this agent against a chosen event + guest and return the exact text the
    model would be given — the main way an operator spots 'this event has no venue'."""
    user = eo_auth.require_eo(request)
    agent = _agent_or_404(user, agent_id)
    body = await request.json() if await request.body() else {}

    event = _event_or_404(user, body["event_id"]) if body.get("event_id") else None
    wedding_id = body.get("wedding_id") or (event or {}).get("wedding_id") or agent.get("wedding_id")
    wedding = _wedding_or_404(user, wedding_id) if wedding_id else None

    guest = None
    if body.get("guest_id"):
        guest = eo_db.get_contact(int(body["guest_id"]))
        if guest and not _contact_visible(user, guest):
            raise HTTPException(status_code=404, detail="Guest not found")

    rendered = prompt_render.render_prompt(
        agent, wedding=wedding, event=event, guest=guest,
        # the guest's whole schedule, so the preview shows exactly what a call would know
        events=eo_db.list_events(wedding_id) if wedding_id else None,
        extra=body.get("sample") if isinstance(body.get("sample"), dict) else None)
    return JSONResponse({
        "system_instruction": rendered["system_instruction"],
        "trigger": rendered["trigger"],
        "missing": rendered["missing"],
        "tools": agent_tools.build_tools(agent),
        "guest": guest,
        "event": event,
    })


@router.post("/agents/{agent_id}/test-token")
async def agents_test_token(agent_id: int, request: Request):
    """Short-lived token for a browser-mic test on /ws.

    /ws is unauthenticated, so it must never take an agent id straight from the query
    string — anyone could then render any wedding's prompt and hear its guests' phone
    numbers and hotel rooms. The token names exactly one agent/event/guest and expires."""
    user = eo_auth.require_eo(request)
    agent = _agent_or_404(user, agent_id)
    body = await request.json() if await request.body() else {}
    event = _event_or_404(user, body["event_id"]) if body.get("event_id") else None
    wedding_id = body.get("wedding_id") or (event or {}).get("wedding_id") or agent.get("wedding_id")
    if wedding_id:
        _wedding_or_404(user, wedding_id)
    guest_id = body.get("guest_id")
    if guest_id:
        guest = eo_db.get_contact(int(guest_id))
        if not guest or not _contact_visible(user, guest):
            raise HTTPException(status_code=404, detail="Guest not found")
    token = eo_auth.issue_test_token(user, agent_id=agent_id,
                                     event_id=(event or {}).get("id"),
                                     guest_id=guest_id, wedding_id=wedding_id)
    return {"token": token, "ttl_seconds": eo_auth._TEST_TOKEN_TTL}


@router.post("/agents/{agent_id}/test-call")
async def agents_test_call(agent_id: int, request: Request):
    """Ring a number and let this agent speak — the phone half of the Test panel."""
    user = eo_auth.require_eo(request)
    agent = _agent_or_404(user, agent_id)
    body = await request.json()
    # normalize_phone returns (e164, is_valid) — a bare tuple is truthy, so unpack it.
    phone, phone_valid = eo_import.normalize_phone(body.get("phone"))
    if not phone or not phone_valid:
        raise HTTPException(status_code=400, detail="A valid phone number is required")
    event = _event_or_404(user, body["event_id"]) if body.get("event_id") else None
    wedding_id = body.get("wedding_id") or (event or {}).get("wedding_id") or agent.get("wedding_id")
    if wedding_id:
        _wedding_or_404(user, wedding_id)
    guest_id = body.get("guest_id")
    if guest_id:
        guest = eo_db.get_contact(int(guest_id))
        if not guest or not _contact_visible(user, guest):
            raise HTTPException(status_code=404, detail="Guest not found")

    import dialer
    res = await dialer.place_call(
        phone, base_url=os.getenv("PUBLIC_URL"), name=_clean_str(body, "name", max_len=80),
        agent_id=agent_id, event_id=(event or {}).get("id"), guest_id=guest_id,
        wedding_id=wedding_id,
        provider=eo_db.user_provider(user["id"]) or None)
    if res.get("error"):
        raise HTTPException(status_code=502, detail=res["error"])
    return JSONResponse(res)


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: int, request: Request):
    user = eo_auth.require_eo(request)
    c = eo_db.get_campaign_full(campaign_id)
    if not c or not _owns_or_admin(user, c):
        raise HTTPException(status_code=404, detail="Campaign not found")
    c["rsvp_yes_unique"] = eo_db.campaign_rsvp_yes_unique(campaign_id)
    return JSONResponse(c)


def _max_active_campaigns():
    """A wedding day legitimately runs several campaigns at once (six reminder batches on
    the same afternoon), so the old one-at-a-time rule is a cap, not a singleton."""
    try:
        return max(1, int(os.getenv("EO_MAX_ACTIVE_CAMPAIGNS", "6")))
    except (TypeError, ValueError):
        return 6


def _fatigue_window_hours():
    try:
        return max(0, int(os.getenv("EO_FATIGUE_WINDOW_HOURS", "12")))
    except (TypeError, ValueError):
        return 12


def _campaign_payload(user, body):
    """Validate a create/preflight payload. Returns the resolved pieces or raises 400."""
    name = _clean_str(body, "name", max_len=160, required=True, label="Campaign name")

    start_dt = _parse_iso(body.get("start_at"))
    if not start_dt:
        raise HTTPException(status_code=400, detail="Valid start date/time is required")
    if start_dt < datetime.now(timezone.utc) - timedelta(minutes=2):
        raise HTTPException(status_code=400,
                            detail="Start time is in the past. Pick the current time or later.")

    agent = _agent_or_404(user, body["agent_id"]) if body.get("agent_id") else None
    if not agent:
        raise HTTPException(status_code=400, detail="Pick an agent for this campaign")
    event = _event_or_404(user, body["event_id"]) if body.get("event_id") else None
    if agent.get("requires_event") and not event:
        raise HTTPException(status_code=400,
                            detail=f"'{agent['name']}' calls about a specific function — "
                                   f"pick an event for this campaign.")
    wedding_id = body.get("wedding_id") or (event or {}).get("wedding_id") or agent.get("wedding_id")
    if wedding_id:
        _wedding_or_404(user, wedding_id)

    delay_h = _whole_int(body.get("callback_delay_hours", 4), "Call-back hours", 0, 720)
    max_day = _whole_int(body.get("callback_max_per_day", 3), "Attempts per day", 1, 10)
    days = _whole_int(body.get("callback_days", 1), "Call-back days", 1, 10)

    def _hhmm(v, default):
        try:
            hh, mm = str(v).split(":")
            return max(0, min(1439, (int(hh) % 24) * 60 + (int(mm) % 60)))
        except Exception:
            return default
    call_start_min = _hhmm(body.get("call_start", "09:00"), 540)
    call_end_min = _hhmm(body.get("call_end", "21:00"), 1260)

    # Guests: an explicit selection wins; otherwise pre-select by the event's audience.
    scope = _contact_owner_scope(user)
    ids = body.get("contact_ids")
    if ids:
        contacts = [c for c in eo_db.get_contacts_by_ids(ids, created_by=scope)
                    if c.get("status") == "valid"]
    elif event and wedding_id:
        contacts = eo_db.guests_for_audience(wedding_id, event.get("audience"), created_by=scope)
    else:
        contacts = []
    # dedupe by phone: duplicate rows would double-dial and the reap (keyed on
    # campaign_id+phone) cross-stamps both
    seen = set()
    contacts = [c for c in contacts
                if c.get("phone") not in seen and not seen.add(c.get("phone"))]

    return dict(name=name, start_dt=start_dt, agent=agent, event=event,
                wedding_id=wedding_id, contacts=contacts, delay_h=delay_h,
                max_day=max_day, days=days, call_start_min=call_start_min,
                call_end_min=call_end_min)


def _fatigue_report(contacts):
    """Guests already called inside the fatigue window. A warning, never a block — the
    client's own schedule needs six calls to some guests on the wedding day."""
    hours = _fatigue_window_hours()
    if not hours or not contacts:
        return {"hours": hours, "count": 0, "guests": []}
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    recent = eo_db.recent_call_counts([c["phone"] for c in contacts], since)
    guests = [{"id": c.get("id"), "name": c.get("name"), "phone": c.get("phone"),
               "calls": recent[c["phone"]]["calls"],
               "last_called_at": recent[c["phone"]]["last_attempt_at"]}
              for c in contacts if c.get("phone") in recent]
    guests.sort(key=lambda g: g["last_called_at"] or "", reverse=True)
    return {"hours": hours, "count": len(guests), "guests": guests[:50]}


@router.post("/campaigns/preflight")
async def campaign_preflight(request: Request):
    """Everything the confirm dialog needs, creating nothing: who would be called, who
    was called recently, and whether the prompt has gaps."""
    user = eo_auth.require_eo(request)
    body = await request.json()
    p = _campaign_payload(user, body)

    rendered = prompt_render.render_prompt(
        p["agent"], wedding=eo_db.get_wedding(p["wedding_id"]) if p["wedding_id"] else None,
        event=p["event"], guest=p["contacts"][0] if p["contacts"] else None,
        events=eo_db.list_events(p["wedding_id"]) if p["wedding_id"] else None)

    active = eo_db.active_campaigns()
    return JSONResponse({
        "audience": {
            "matched": len(p["contacts"]),
            "audience": (p["event"] or {}).get("audience"),
            "guests": [{"id": c.get("id"), "name": c.get("name"), "phone": c.get("phone"),
                        "side": c.get("side")} for c in p["contacts"]],
        },
        "fatigue": _fatigue_report(p["contacts"]),
        "prompt_warnings": {"missing": rendered["missing"]},
        "capacity": {"active": len(active), "max": _max_active_campaigns()},
    })


@router.post("/campaigns")
async def campaign_create(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    p = _campaign_payload(user, body)

    active = eo_db.active_campaigns()
    cap = _max_active_campaigns()
    if len(active) >= cap:
        raise HTTPException(
            status_code=409,
            detail=f"{len(active)} campaigns are already scheduled or live (limit {cap}). "
                   f"Cancel one, or raise EO_MAX_ACTIVE_CAMPAIGNS.")

    if not p["contacts"]:
        raise HTTPException(status_code=400, detail="Select at least one valid guest")

    # Fatigue is a warning the operator must have seen — re-checked here so an API client
    # cannot skip the dialog by posting straight to create.
    fatigue = _fatigue_report(p["contacts"])
    if fatigue["count"] and not body.get("acknowledge_fatigue"):
        raise HTTPException(
            status_code=409,
            detail={"error": "fatigue_warning",
                    "message": f"{fatigue['count']} of these guests were already called in "
                               f"the last {fatigue['hours']} hours. Re-submit with "
                               f"acknowledge_fatigue to proceed.",
                    "fatigue": fatigue})

    now = datetime.now(timezone.utc)
    status = "live" if p["start_dt"] <= now else "scheduled"
    cid = eo_db.create_campaign(
        name=p["name"], start_at=p["start_dt"].astimezone(timezone.utc).isoformat(),
        created_by=user["id"], callback_delay_hours=p["delay_h"],
        callback_max_per_day=p["max_day"], callback_days=p["days"], status=status,
        call_start_min=p["call_start_min"], call_end_min=p["call_end_min"],
        wedding_id=p["wedding_id"], event_id=(p["event"] or {}).get("id"),
        agent_id=p["agent"]["id"],
    )
    eo_db.add_campaign_contacts(cid, p["contacts"])
    return JSONResponse(eo_db.get_campaign_full(cid), status_code=201)


@router.post("/campaigns/{campaign_id}/cancel")
async def campaign_cancel(campaign_id: int, request: Request):
    user = eo_auth.require_eo(request)
    if not _owns_or_admin(user, eo_db.get_campaign(campaign_id)):
        raise HTTPException(status_code=404, detail="Campaign not found")
    ok = eo_db.cancel_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Campaign is not cancellable (already completed or cancelled)")
    return {"ok": True}


@router.get("/campaigns/{campaign_id}/contacts")
async def campaign_contacts(campaign_id: int, request: Request):
    user = eo_auth.require_eo(request)
    campaign = eo_db.get_campaign(campaign_id)
    if not _owns_or_admin(user, campaign):
        raise HTTPException(status_code=404, detail="Campaign not found")
    qp = request.query_params
    data = eo_db.list_campaign_contacts(
        campaign_id, status=qp.get("status") or None, q=qp.get("q") or None,
        limit=int(qp.get("limit") or 500), offset=int(qp.get("offset") or 0))
    _attach_contact_display(data["items"], campaign, scheduler.is_enabled())
    return JSONResponse(data)


@router.get("/scheduler/campaign-queue")
async def scheduler_campaign_queue(request: Request):
    """Upcoming campaign dials for the Scheduler page — pending/calling contacts in
    live/scheduled campaigns, soonest first. Scoped: a Superadmin sees every campaign;
    an Admin sees only the campaigns they created."""
    user = eo_auth.require_eo(request)
    ids = _scope_ids(user)                       # None → full access (Superadmin)
    campaign_ids = None if ids is None else [int(i) for i in ids]
    limit = int(request.query_params.get("limit") or 200)
    data = eo_db.cc_upcoming(campaign_ids, limit=limit)
    sched_on = scheduler.is_enabled()
    _attach_contact_display(data["items"], None, sched_on)
    data["scheduler_enabled"] = sched_on
    active = eo_db.active_campaign()
    data["active_campaign"] = ({"id": active["id"], "name": active["name"], "status": active["status"]}
                               if active and _owns_or_admin(user, active) else None)
    return JSONResponse(data)


@router.post("/campaigns/{campaign_id}/contacts/{cc_id}/retry")
async def campaign_contact_retry(campaign_id: int, cc_id: int, request: Request):
    """'Call now' — dial this recipient immediately (promotes a scheduled campaign to live)."""
    import campaign_runner
    user = eo_auth.require_eo(request)
    if not _owns_or_admin(user, eo_db.get_campaign(campaign_id)):
        raise HTTPException(status_code=404, detail="Campaign not found")
    res = await campaign_runner.dial_contact_now(campaign_id, cc_id)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/contacts/{cc_id}/cancel")
async def campaign_contact_cancel(campaign_id: int, cc_id: int, request: Request):
    """Cancel a PENDING retry for this recipient — no more automatic dials. History
    (done/failed) and in-progress calls are untouchable; Call now can revive a
    cancelled contact deliberately."""
    user = eo_auth.require_eo(request)
    if not _owns_or_admin(user, eo_db.get_campaign(campaign_id)):
        raise HTTPException(status_code=404, detail="Campaign not found")
    cc = eo_db.get_campaign_contact(cc_id)
    if not cc or int(cc.get("campaign_id") or 0) != int(campaign_id):
        raise HTTPException(status_code=404, detail="Recipient not found")
    st = cc.get("call_status")
    if st == "calling":
        raise HTTPException(status_code=409, detail="A call to this recipient is in progress")
    if st != "pending":
        raise HTTPException(status_code=400, detail="Nothing to cancel — this recipient has no pending retry")
    eo_db.cc_update(int(cc_id), call_status="cancelled", next_attempt_at=None)
    return {"ok": True}


# Contacts pool (per-user: an Admin sees only their own; Superadmin sees all)
def _contact_owner_scope(user):
    """None → all pools (Superadmin); otherwise the caller's own pool."""
    return None if user["role"] == "eo_admin" else int(user["id"])


def _contact_visible(user, contact) -> bool:
    return user["role"] == "eo_admin" or contact.get("created_by") == user["id"]


@router.get("/contacts")
async def contacts_list(request: Request):
    user = eo_auth.require_eo(request)
    qp = request.query_params
    return JSONResponse(eo_db.list_contacts(
        q=qp.get("q") or None,
        source=qp.get("source") or None,
        status=qp.get("status") or None,
        sort=qp.get("sort") or "created_at",
        direction=qp.get("dir") or "desc",
        limit=int(qp.get("limit") or 25),
        offset=int(qp.get("offset") or 0),
        created_by=_contact_owner_scope(user),
        wedding_id=qp.get("wedding_id") or None,
        side=qp.get("side") or None,
    ))


@router.post("/contacts")
async def contacts_add(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    e164, valid = eo_import.normalize_phone(body.get("phone"))
    if not e164:
        raise HTTPException(status_code=400, detail="Invalid phone number")
    wedding_id = body.get("wedding_id") or None
    if wedding_id:
        _wedding_or_404(user, wedding_id)
    guest = {k: body[k] for k in eo_db.GUEST_FIELDS if k in body}
    if "side" in guest:
        guest["side"] = eo_import.normalize_side(guest["side"])
    cid, created = eo_db.add_contact(
        (body.get("name") or "").strip(), e164,
        source="manual", status="valid" if valid else "invalid",
        created_by=user["id"], wedding_id=wedding_id, **guest,
    )
    return {"ok": True, "id": cid, "created": created, "phone": e164, "valid": valid}


@router.post("/contacts/import")
async def contacts_import(request: Request, file: UploadFile = File(...)):
    user = eo_auth.require_eo(request)
    form = await request.form()
    wedding_id = form.get("wedding_id") or None
    if wedding_id:
        _wedding_or_404(user, wedding_id)
    data = await file.read()
    try:
        rows, rejected, total, unknown = eo_import.parse_upload(file.filename, data)
    except Exception as e:
        logger.warning("Guest import parse failed: %s", e)
        raise HTTPException(status_code=400, detail="Could not read that file. Use the sample .xlsx / .csv format.")
    added, updated = eo_db.bulk_upsert_contacts(rows, source="upload", created_by=user["id"],
                                                wedding_id=wedding_id)
    invalid = sum(1 for r in rows if r[2] == "invalid")
    mapped = sorted({k for r in rows for k in (r[3] or {})})
    return {"ok": True, "rows_read": total, "added": added, "updated": updated,
            "rejected": rejected, "invalid": invalid,
            # So a mis-named column is visible rather than silently dropped.
            "mapped_fields": mapped, "unknown_headers": unknown[:12],
            # The agent reads the travel mode aloud, so a sheet saying "train" beside a
            # flight code tells a guest the wrong thing on the call. Flag it at upload.
            "transport_warnings": eo_import.transport_mismatches(rows)[:12]}


@router.post("/contacts/delete")
async def contacts_delete(request: Request):
    user = eo_auth.require_eo(request)
    body = await request.json()
    n = eo_db.delete_contacts(body.get("ids") or [], created_by=_contact_owner_scope(user))
    return {"ok": True, "deleted": n}


@router.get("/contacts/template")
async def contacts_template(request: Request):
    eo_auth.require_eo(request)
    data = eo_import.build_template()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=contacts_template.xlsx"},
    )


# Scheduler / callbacks (reuse super-admin logic)
@router.get("/callbacks")
async def eo_callbacks(request: Request):
    user = eo_auth.require_eo(request)
    include_cost = user["role"] == "eo_admin"
    qp = request.query_params
    statuses = set(s.strip() for s in qp["status"].split(",")) if qp.get("status") else None
    items = await store.list_callbacks(statuses)
    scope = _scope_ids(user)
    if scope is not None:
        items = [c for c in items if str(c.get("campaign_id") or "") in scope]
    items = _label_and_strip(items, include_cost)
    for c in items:
        cb = c.get("callback") or {}
        # the callback row inherits the call's remark until a callback-specific remark is written
        c["callback"] = {**cb, "remark": cb.get("remark") or c.get("remark") or None}
        label, variant = _CALLBACK_LABELS.get(cb.get("status"), (cb.get("status") or "—", "amber"))
        c["display_status"] = label
        c["display_variant"] = variant
        c["result_outcome_label"] = _rsvp_label(cb.get("result_outcome"))
    state = await store.load_scheduler_state()
    return JSONResponse({"items": items, "scheduler_enabled": scheduler.is_enabled(),
                         "paused_until": state.get("paused_until")})


async def _owned_callback(user, call_id):
    """Load a call that has a callback block, enforcing per-agent ownership.
    Superadmin: any. Agent: only if the call's campaign belongs to them."""
    call = await store.load_call(call_id)
    if not call or not call.get("callback"):
        raise HTTPException(status_code=404, detail="Callback not found")
    if user["role"] != "eo_admin":
        cid = call.get("campaign_id")
        if not cid or not _owns_or_admin(user, eo_db.get_campaign(cid)):
            raise HTTPException(status_code=404, detail="Callback not found")
    return call


@router.post("/callbacks/{call_id}/cancel")
async def eo_callback_cancel(call_id: str, request: Request):
    user = eo_auth.require_eo(request)
    call = await _owned_callback(user, call_id)
    st = call["callback"].get("status")
    if st == "in_flight":
        raise HTTPException(status_code=409, detail="This callback is being dialed right now")
    if st != "pending":
        # completed / failed / cancelled are HISTORY — never rewrite what already happened
        raise HTTPException(status_code=400, detail="This callback already finished — history can't be cancelled")
    call["callback"]["status"] = "cancelled"
    await store.save_call(call)
    return {"ok": True}


@router.post("/callbacks/{call_id}/call-now")
async def eo_callback_call_now(call_id: str, request: Request):
    user = eo_auth.require_eo(request)
    call = await _owned_callback(user, call_id)
    cb = call["callback"]
    if cb.get("status") in ("in_flight", "completed"):
        return {"ok": False, "error": f"callback is {cb.get('status')}"}
    cb.update({"status": "pending", "due_at": datetime.now(timezone.utc).isoformat(),
               "next_retry_at": None, "attempts": 0, "last_error": None})
    await store.save_call(call)
    return {"ok": True}


@router.post("/callbacks/{call_id}/reschedule")
async def eo_callback_reschedule(call_id: str, request: Request):
    """Change a scheduled callback's due time to a chosen future moment."""
    user = eo_auth.require_eo(request)
    call = await _owned_callback(user, call_id)
    body = await request.json()
    due = _parse_iso(body.get("due_at"))
    if not due:
        raise HTTPException(status_code=400, detail="A valid date/time is required")
    if due < datetime.now(timezone.utc) - timedelta(minutes=2):
        raise HTTPException(status_code=400, detail="Pick a time in the future")
    cb = call["callback"]
    if cb.get("status") in ("in_flight", "completed"):
        return {"ok": False, "error": f"callback is {cb.get('status')}"}
    cb.update({"status": "pending", "due_at": due.astimezone(timezone.utc).isoformat(),
               "next_retry_at": None, "last_error": None})
    await store.save_call(call)
    return {"ok": True}


# Manual RSVP overwrite
_FINAL_OUTCOMES = ("yes", "no", "do_not_contact", "wrong_number")


def _apply_manual_outcome(call: dict, outcome: str, username: str) -> dict:
    """Overwrite a call's system-captured RSVP with a human decision. Mutates the dict
    (caller persists it). Keeps the original outcome once for audit, keeps the
    dashboards honest (booking_created), and cancels a pending member-callback when
    the outcome moves away from 'callback'."""
    prev = call.get("rsvp_outcome_status")
    if prev and "rsvp_outcome_original" not in call:
        call["rsvp_outcome_original"] = prev
    call["rsvp_outcome_status"] = outcome
    call["booking_created"] = outcome == "yes"
    call["rsvp_source"] = "manual"
    call["rsvp_edited_by"] = username or ""
    call["rsvp_edited_at"] = datetime.now(timezone.utc).isoformat()
    cb = call.get("callback")
    if cb and cb.get("status") == "pending" and outcome != "callback":
        cb["status"] = "cancelled"
    return call


@router.patch("/calls/{call_id}/outcome")
async def eo_call_outcome(call_id: str, request: Request):
    """Let a user overwrite the system-captured RSVP. Final outcomes also finalise the
    campaign contact (stops pending retries)."""
    user = eo_auth.require_eo(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    scope = _scope_ids(user)
    if scope is not None and str(call.get("campaign_id") or "") not in scope:
        raise HTTPException(status_code=404, detail="Call not found")
    if call.get("status") == "in_progress":
        raise HTTPException(status_code=409, detail="Call is still in progress — edit the RSVP once it ends")
    body = await request.json()
    outcome = str((body or {}).get("outcome") or "").strip().lower()
    if outcome not in _RSVP_LABELS:
        raise HTTPException(status_code=400, detail="Pick a valid RSVP outcome")
    _apply_manual_outcome(call, outcome, user.get("username") or "")
    await store.save_call(call)
    cid, caller = call.get("campaign_id"), (call.get("caller") or "").strip()
    if cid and caller:
        eo_db.cc_set_outcome_by_phone(int(cid), caller, outcome,
                                      mark_done=outcome in _FINAL_OUTCOMES)
    return {"ok": True, "outcome": outcome, "label": _rsvp_label(outcome),
            "edited_by": call["rsvp_edited_by"]}


# Remarks (user-editable note on every grid row)
@router.patch("/calls/{call_id}/remark")
async def eo_call_remark(call_id: str, request: Request):
    user = eo_auth.require_eo(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    scope = _scope_ids(user)
    if scope is not None and str(call.get("campaign_id") or "") not in scope:
        raise HTTPException(status_code=404, detail="Call not found")
    if call.get("status") == "in_progress":
        # the recorder rewrites the whole record at close — an edit now would be lost
        raise HTTPException(status_code=409, detail="Call is still in progress — add the remark once it ends")
    call["remark"] = _clean_remark(await request.json())
    await store.save_call(call)
    return {"ok": True, "remark": call["remark"]}


@router.patch("/callbacks/{call_id}/remark")
async def eo_callback_remark(call_id: str, request: Request):
    user = eo_auth.require_eo(request)
    call = await _owned_callback(user, call_id)
    if call["callback"].get("status") == "in_flight":
        # the scheduler is mid-claim; a whole-file save could revert in_flight and cause a double dial
        raise HTTPException(status_code=409, detail="This callback is being dialed right now — try again in a minute")
    call["callback"]["remark"] = _clean_remark(await request.json())
    await store.save_call(call)
    return {"ok": True, "remark": call["callback"]["remark"]}


@router.patch("/campaigns/{campaign_id}/contacts/{cc_id}/remark")
async def eo_campaign_contact_remark(campaign_id: int, cc_id: int, request: Request):
    user = eo_auth.require_eo(request)
    if not _owns_or_admin(user, eo_db.get_campaign(campaign_id)):
        raise HTTPException(status_code=404, detail="Campaign not found")
    cc = eo_db.get_campaign_contact(cc_id)
    if not cc or int(cc.get("campaign_id") or 0) != int(campaign_id):
        raise HTTPException(status_code=404, detail="Recipient not found")
    remark = _clean_remark(await request.json())
    eo_db.cc_update(int(cc_id), remark=remark)
    return {"ok": True, "remark": remark}


@router.patch("/contacts/{contact_id}/remark")
async def eo_contact_remark(contact_id: int, request: Request):
    user = eo_auth.require_eo(request)
    contact = eo_db.get_contact(contact_id)
    if not contact or not _contact_visible(user, contact):
        raise HTTPException(status_code=404, detail="Contact not found")
    remark = _clean_remark(await request.json())
    eo_db.set_contact_remark(int(contact_id), remark)
    return {"ok": True, "remark": remark}


@router.post("/scheduler/toggle")
async def eo_scheduler_toggle(request: Request):
    eo_auth.require_eo_admin(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = bool(body.get("enabled", not scheduler.is_enabled()))
    scheduler.set_override(enabled)
    return {"ok": True, "enabled": enabled}


# ---- Campaign Analytics ----

@router.get("/campaigns/{campaign_id}/analytics")
async def campaign_analytics(campaign_id: int, request: Request):
    """Get analytics for a specific campaign."""
    user = eo_auth.require_eo(request)
    campaign = eo_db.get_campaign(campaign_id)
    if not campaign or not _owns_or_admin(user, campaign):
        raise HTTPException(status_code=404, detail="Campaign not found")

    import call_analytics
    analytics = call_analytics.get_campaign_analytics(campaign_id)
    return analytics


@router.get("/weddings/{wedding_id}/analytics")
async def wedding_analytics(wedding_id: int, request: Request):
    """Get aggregated analytics for all campaigns in a wedding."""
    user = eo_auth.require_eo(request)
    wedding = eo_db.get_wedding(wedding_id)
    if not wedding:
        raise HTTPException(status_code=404, detail="Wedding not found")

    import call_analytics
    analytics = call_analytics.get_wedding_analytics(wedding_id)
    return analytics


@router.get("/campaigns/{campaign_id}/pending")
async def campaign_pending_calls(campaign_id: int, request: Request):
    """Get pending calls for a campaign."""
    user = eo_auth.require_eo(request)
    campaign = eo_db.get_campaign(campaign_id)
    if not campaign or not _owns_or_admin(user, campaign):
        raise HTTPException(status_code=404, detail="Campaign not found")

    import call_analytics
    pending = call_analytics.get_pending_calls(campaign_id)
    return {"pending_calls": pending}


@router.get("/campaigns/{campaign_id}/completed")
async def campaign_completed_calls(campaign_id: int, request: Request):
    """Get completed calls for a campaign."""
    user = eo_auth.require_eo(request)
    campaign = eo_db.get_campaign(campaign_id)
    if not campaign or not _owns_or_admin(user, campaign):
        raise HTTPException(status_code=404, detail="Campaign not found")

    import call_analytics
    completed = call_analytics.get_completed_calls(campaign_id)
    return {"completed_calls": completed}
