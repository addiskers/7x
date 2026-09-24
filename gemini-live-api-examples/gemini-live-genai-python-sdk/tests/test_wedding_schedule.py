"""The Wedding Schedule agent: one call, for the whole wedding, covering every function
still to come — and Event Reminder going back to a single function.

The team asked for "one agent that gives information of all events together" instead of a
campaign per function. The platform already supported event-less campaigns (the logistics
agent), so most of what these tests pin is the prompt and the few gaps around it.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import agent_seeds
import eo_api
import eo_auth
import main
import prompt_render as pr

IST = pr._IST


def _seed(slug):
    return {s["slug"]: s for s in agent_seeds.SEEDS}[slug]


# ------------------------------------------------------------- the upcoming-only schedule
def test_upcoming_only_drops_past_days_but_keeps_today_and_undated():
    """A guest rung on the last day must not be told about a Mehendi that is over. Today's
    functions stay — one may be under way — and so do undated ones."""
    today = date(2026, 9, 25)
    events = [
        {"name": "Mehendi", "event_date": "2026-09-23", "start_time": "12:00", "venue": "Garden"},
        {"name": "Hi-Tea", "event_date": "2026-09-25", "start_time": "16:00", "venue": "Harvest"},
        {"name": "Brunch", "event_date": "", "start_time": "", "venue": "Lawn"},
        {"name": "Vidaai", "event_date": "2026-09-26", "start_time": "00:30", "venue": "Chand Baori"},
    ]
    out = pr.build_schedule_detail(events, today=today, upcoming_only=True)
    assert "Mehendi" not in out
    for name in ("Hi-Tea", "Brunch", "Vidaai"):
        assert name in out
    # the default is unchanged, so Event Reminder's own lists are not affected
    assert "Mehendi" in pr.build_schedule_detail(events, today=today)


def test_the_upcoming_schedule_still_respects_the_guests_side():
    today = date(2026, 9, 25)
    events = [{"name": "Saanth", "event_date": "2026-09-25", "audience": "groom"},
              {"name": "Chuda", "event_date": "2026-09-25", "audience": "bride"}]
    out = pr.build_schedule_detail(events, side="groom", today=today, upcoming_only=True)
    assert "Saanth" in out and "Chuda" not in out


def test_the_context_offers_the_upcoming_schedule_alongside_the_full_one():
    now = datetime(2026, 9, 25, 16, 30, tzinfo=IST)
    events = [{"name": "Old Lunch", "event_date": "2026-09-20", "start_time": "13:00"},
              {"name": "Sufi Night", "event_date": "2026-09-25", "start_time": "19:00",
               "venue": "Great Park"}]
    ctx = pr.build_context(events=events, now=now)
    assert "Sufi Night" in ctx["upcoming_schedule"]
    assert "Old Lunch" not in ctx["upcoming_schedule"]
    assert "Old Lunch" in ctx["schedule"]                  # the lookup list is untouched
    assert "seven in the evening" in ctx["upcoming_schedule"]
    assert "19:00" not in ctx["upcoming_schedule"]


# --------------------------------------------- two rendering bugs this agent exposed
def test_a_multi_line_value_never_shifts_the_cleanup_onto_the_authors_prose():
    """render() recorded which TEMPLATE lines lost a value and cleaned those line numbers
    in the OUTPUT — but {schedule} expands to several lines, so every line after it moved
    and the cleanup hit prose instead: "attend and to what" lost its "and", "help with,"
    lost its "with". Pre-existing since {schedule}; the new agent's preview showed it."""
    template = "\n".join([
        "## THE FUNCTIONS",
        "{upcoming_schedule}",
        "Filtered to what they may attend and to what has not happened yet.",
        "Ask if there is anything else you can help with, then stop.",
        "- Their room: {room_number}",
    ])
    schedule = "\n".join(["Hi-Tea - four in the evening.", "  Snacks.",
                          "Sufi Night - seven in the evening.", "  Music."])
    out, missing = pr.render(template, {"upcoming_schedule": schedule})
    assert missing == ["room_number"]
    assert "attend and to what has not happened yet." in out
    assert "anything else you can help with, then stop." in out
    assert "Their room" not in out                 # the line that DID lose its value goes


@pytest.mark.parametrize("raw,expected", [
    ("17:27", "five twenty-seven in the evening"),
    ("09:07", "nine oh seven in the morning"),
    ("21:38", "nine thirty-eight at night"),
    ("12:41", "twelve forty-one in the afternoon"),
])
def test_an_odd_minute_is_spoken_never_digits(raw, expected):
    """{now_time} rendered "five 27 in the evening" — the model reads digits out as digits."""
    assert pr._spoken_time(raw) == expected


# ------------------------------------------------------------------- the new agent
def test_the_schedule_agent_needs_no_event_and_reads_the_upcoming_functions():
    s = _seed("wedding_schedule")
    assert s["requires_event"] == 0 and s["kind"] == "schedule"
    t = s["prompt_template"]
    assert "{upcoming_schedule}" in t
    # a campaign with no event has no event_* values — the prompt must not lean on them
    assert not re.findall(r"\{(event_[a-z_]+|venue|announcement|when_phrase|dress_code)\}", t)
    import eo_db
    assert eo_db.stale_agent_reasons({"prompt_template": t}) == []


def test_the_schedule_agent_keeps_what_the_team_tested():
    t = _seed("wedding_schedule")["prompt_template"]
    for rule in ("Take an RSVP", "room allocation", "Change, or promise a change to, the schedule",
                 "Would you like more detail on any of them", "hospitality desk",
                 "guest support desks"):
        assert rule in t, rule


def test_the_schedule_agent_drops_what_would_break_on_a_call():
    """Script-detection markers (the model hears audio, not text), hardcoded times as
    digits and markdown (both read aloud verbatim), and a hardcoded couple."""
    t = _seed("wedding_schedule")["prompt_template"]
    assert "Gujarati script characters" not in t
    assert not re.search(r"\d{1,2}:\d{2}\s*(AM|PM)", t)
    assert "**" not in t
    assert "Ved" not in t and "Riya" not in t


def test_event_reminder_is_about_one_event_again():
    t = _seed("event_reminder")["prompt_template"]
    assert "{schedule}" in t and "{schedule_detail}" not in t
    assert "## THE ONE EVENT YOU ARE CALLING ABOUT" in t
    assert "Do NOT read the whole schedule out unless they actually ask" in t


# ------------------------------------------------------------ a call with no event
def test_a_whole_schedule_call_hears_every_upcoming_function(fresh_eo_db):
    """Dates are relative to today, so this keeps passing after the wedding."""
    db = fresh_eo_db
    db.init()
    main.invalidate_ctx_cache()
    admin = db.create_user("a", "A", "h", "s", role="eo_admin")
    today = datetime.now(IST).date()
    wid = db.create_wedding("Ved & Riya", created_by=admin,
                            hospitality_team="Ved and Riya's Hospitality Team")
    db.create_event(wid, "Yesterday Brunch", event_date=str(today - timedelta(days=1)),
                    start_time="11:00", venue="Lawn")
    db.create_event(wid, "Sufi Night", event_date=str(today), start_time="19:00",
                    venue="Great Park", announcement="Shadab Faridi performs live.")
    db.create_event(wid, "After Party", event_date=str(today + timedelta(days=1)),
                    start_time="23:00", venue="Ballroom")
    db.bulk_upsert_contacts([("Heeren Agrawal", "+917043020542", "valid", {})],
                            created_by=admin, wedding_id=wid)
    agent = db.get_agent_by_slug("wedding_schedule")

    ctx = main._resolve_call_context(agent_id=agent["id"], event_id=None, wedding_id=wid,
                                     caller="+917043020542")
    si = ctx["system_instruction"]
    assert "Sufi Night" in si and "After Party" in si and "Shadab Faridi" in si
    assert "Yesterday Brunch" not in si
    assert "19:00" not in si
    assert "Heeren" in ctx["trigger"]
    assert "Ved and Riya's Hospitality Team" in ctx["trigger"]


# ------------------------------------------------------------- creating the campaign
def _campaign_world(db):
    h, s = eo_auth.hash_password("pw123456")
    uid = db.create_user(username="boss", name="Boss", password_hash=h, password_salt=s,
                         role="eo_admin")
    wid = db.create_wedding("W", created_by=uid)
    db.create_event(wid, "Sufi Night", event_date="2030-01-01", start_time="19:00")
    db.bulk_upsert_contacts([("Groom Guest", "+919000000001", "valid", {"side": "groom"}),
                             ("Bride Guest", "+919000000002", "valid", {"side": "bride"})],
                            created_by=uid, wedding_id=wid)
    return db.get_user(uid), wid


def _body(agent_id, wid):
    return {"name": "Evening", "agent_id": agent_id, "wedding_id": wid,
            "start_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}


def test_a_whole_schedule_campaign_preselects_every_guest(fresh_eo_db):
    """No event means no audience to pre-select from; for this agent that means everyone."""
    db = fresh_eo_db
    db.init()
    user, wid = _campaign_world(db)
    agent = db.get_agent_by_slug("wedding_schedule")
    p = eo_api._campaign_payload(user, _body(agent["id"], wid))
    assert p["event"] is None
    assert {c["name"] for c in p["contacts"]} == {"Groom Guest", "Bride Guest"}


def test_event_reminder_still_refuses_a_campaign_with_no_event(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    user, wid = _campaign_world(db)
    agent = db.get_agent_by_slug("event_reminder")
    with pytest.raises(HTTPException):
        eo_api._campaign_payload(user, _body(agent["id"], wid))


def test_the_campaign_agent_list_never_carries_a_prompt(fresh_eo_db, monkeypatch):
    """Create Campaign must work for a Staff login, but the scripts stay admin-only."""
    import asyncio
    db = fresh_eo_db
    db.init()
    user, _wid = _campaign_world(db)
    monkeypatch.setattr(eo_auth, "require_eo", lambda request: user)

    class _Req:
        query_params = {}

    resp = asyncio.run(eo_api.agent_choices(_Req()))
    items = json.loads(resp.body)["items"]
    assert {a["slug"] for a in items} >= {"event_reminder", "wedding_schedule", "logistics_concierge"}
    for a in items:
        assert "prompt_template" not in a and "trigger_template" not in a
