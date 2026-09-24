"""The client's plan: minutes purchased, the period, the licence and the ₹ rate.

Defaults come from .env (EO_PLAN_*); an override can be stored in the settings table by a
username listed in EO_SUPERADMIN_USERS (the service provider, not the client's admin).
Usage is computed from the call store: every PHONE call (campaign, inbound, "Call me" test)
that started inside the period, rounded UP to the next minute per call the way telecom
bills. Browser mic tests are free.

Display only. Nothing here blocks a call: a miscounted minute or a mistyped date must never
be able to stop a wedding-day campaign mid-event. Pure functions; nothing touches the line.
"""

import math
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import eo_auth
import eo_db
import store

IST = ZoneInfo("Asia/Kolkata")
# What the carrier charges us for. 'browser' is the admin mic test and is free; 'twilio' is
# the legacy source name kept so older call records still count.
BILLED_SOURCES = frozenset({"plivo", "plivo_inbound", "plivo_campaign", "twilio"})
PLAN_KEYS = ("name", "minutes", "start", "end", "licence_valid_till", "rate_inr_per_min")
SETTING_KEY = "plan"


def _date(s):
    try:
        return date.fromisoformat(str(s or "").strip()[:10]) if s else None
    except ValueError:
        return None


def _int(s, default=0):
    try:
        return int(float(str(s).strip()))
    except (TypeError, ValueError):
        return default


def _float(s, default=0.0):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return default


def env_defaults() -> dict:
    return {
        "name": (os.getenv("EO_PLAN_NAME") or "").strip(),
        "minutes": _int(os.getenv("EO_PLAN_MINUTES"), 0),
        "start": (os.getenv("EO_PLAN_START") or "").strip(),
        "end": (os.getenv("EO_PLAN_END") or "").strip(),
        "licence_valid_till": (os.getenv("EO_LICENCE_VALID_TILL") or "").strip(),
        "rate_inr_per_min": _float(os.getenv("EO_RATE_INR_PER_MIN"), 0.0),
    }


def override() -> dict:
    """The stored override, known keys only; {} when none (or the DB is unavailable)."""
    try:
        raw = eo_db.get_setting(SETTING_KEY) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in PLAN_KEYS if k in raw and raw[k] not in (None, "")}


def plan() -> dict:
    p = env_defaults()
    o = override()
    p.update(o)
    p["source"] = "db" if o else "env"
    return p


def validate(body: dict) -> dict:
    """Clean a plan override from the form. Raises ValueError with a sentence for the user."""
    body = body or {}
    out = {}
    name = str(body.get("name") or "").strip()
    if name:
        out["name"] = name[:80]
    if body.get("minutes") not in (None, ""):
        try:
            minutes = int(float(str(body["minutes"]).strip()))
        except (TypeError, ValueError):
            raise ValueError("Minutes must be a whole number")
        if minutes < 0 or minutes != float(str(body["minutes"]).strip()):
            raise ValueError("Minutes must be a whole number, 0 or more")
        out["minutes"] = minutes
    for key, label in (("start", "Start date"), ("end", "End date"),
                       ("licence_valid_till", "Licence valid till")):
        val = str(body.get(key) or "").strip()
        if val:
            if not _date(val):
                raise ValueError(f"{label} must be a date (YYYY-MM-DD)")
            out[key] = val[:10]
    if out.get("start") and out.get("end") and _date(out["end"]) < _date(out["start"]):
        raise ValueError("End date is before the start date")
    if body.get("rate_inr_per_min") not in (None, ""):
        try:
            rate = float(str(body["rate_inr_per_min"]).strip())
        except (TypeError, ValueError):
            raise ValueError("Rate must be a number (₹ per minute)")
        if rate < 0:
            raise ValueError("Rate cannot be negative")
        out["rate_inr_per_min"] = round(rate, 4)
    return out


def _ist_date(started_at):
    try:
        dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(IST).date()


def usage(plan_: dict, metas=None, now=None) -> dict:
    """Minutes, ₹ and the clock for one plan. `metas` defaults to the live call index."""
    now = now or datetime.now(IST)
    today = now.astimezone(IST).date() if now.tzinfo else now.date()
    start, end = _date(plan_.get("start")), _date(plan_.get("end"))
    if metas is None:
        metas = store.call_metas()
    calls = seconds = minutes = 0
    for m in metas:
        if m.get("source") not in BILLED_SOURCES:
            continue
        d = _ist_date(m.get("started_at"))
        if d is None or (start and d < start) or (end and d > end):
            continue
        secs = max(0, _int(m.get("duration_seconds"), 0))
        calls += 1
        seconds += secs
        minutes += math.ceil(secs / 60)          # telecom rounding: per-call ceil
    included = _int(plan_.get("minutes"), 0)     # 0 == unlimited
    rate = _float(plan_.get("rate_inr_per_min"), 0.0)
    licence_till = _date(plan_.get("licence_valid_till"))
    if not licence_till:
        licence_status = "unset"
    elif licence_till < today:
        licence_status = "expired"
    elif (licence_till - today).days <= 7:
        licence_status = "expiring"
    else:
        licence_status = "valid"
    return {
        "calls": calls,
        "seconds": seconds,
        "minutes_used": minutes,
        "minutes_included": included,
        "minutes_left": max(0, included - minutes) if included else None,
        "pct_used": min(100, round(100.0 * minutes / included)) if included else None,
        "rate_inr_per_min": rate,
        "amount_inr": round(minutes * rate, 2),
        "period": {
            "start": plan_.get("start") or "",
            "end": plan_.get("end") or "",
            "days_left": (end - today).days if end else None,
            "active": (not start or today >= start) and (not end or today <= end),
        },
        "licence": {
            "valid_till": plan_.get("licence_valid_till") or "",
            "days_left": (licence_till - today).days if licence_till else None,
            "status": licence_status,
        },
        "as_of": now.isoformat(),
    }


def snapshot(user=None) -> dict:
    p = plan()
    return {"plan": p, "usage": usage(p), "can_edit": eo_auth.is_superadmin(user),
            "source": p["source"]}
