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
                 "Do you have any questions about any of these?", "hospitality desk",
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


def test_event_reminder_is_about_one_event_and_one_event_only():
    """Since 25 Sep 2026 the reminder is the family's fixed script: it no longer even
    carries the schedule to look another function up in."""
    t = _seed("event_reminder")["prompt_template"]
    assert "{schedule}" not in t and "{schedule_detail}" not in t
    assert "## THE ONE EVENT YOU ARE CALLING ABOUT" in t
    assert "## STRICT RULES" in t
    assert "Do NOT read the whole schedule out unless they actually ask" not in t


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


# ------------------------------------------------ --set-events on the live server's data
def _server_like(db):
    """Ved & Riya as it exists on the server: one bare event named slightly differently, a
    stray event, the five made-up sample guests, a real guest, and a scheduled campaign."""
    import seed_demo_wedding as sdw
    h, s = eo_auth.hash_password("pw123456")
    owner = db.create_user(username="boss", name="B", password_hash=h, password_salt=s,
                           role="eo_admin")
    wid = db.create_wedding(sdw.WEDDING_NAME, created_by=owner,
                            hospitality_team="Ved and Riya's Hospitality Team")
    bare = db.create_event(wid, "Sufi night", event_date="2026-09-25", start_time="19:00")
    stray = db.create_event(wid, "Test function", event_date="2026-09-25")
    rows = [(f"Fake {i}", p, "valid", {}) for i, p in enumerate(sdw.SAMPLE_PHONES)]
    rows.append(("Heeren Agrawal", "+917043020542", "valid", {}))
    db.bulk_upsert_contacts(rows, created_by=owner, wedding_id=wid)
    agent = db.get_agent_by_slug("event_reminder")
    cid = db.create_campaign("Live", "2026-09-25T11:00:00+00:00", owner, 4, 3, 1,
                             status="scheduled", wedding_id=wid, event_id=bare,
                             agent_id=agent["id"])
    return wid, bare, stray, cid


def test_set_events_fills_in_the_itinerary_without_touching_anything_else(fresh_eo_db):
    import seed_demo_wedding as sdw
    db = fresh_eo_db
    db.init()
    wid, bare, stray, _ = _server_like(db)
    sdw.set_events()
    events = {e["name"]: e for e in db.list_events(wid)}
    assert set(events) == {"Hi-Tea", "Sufi Night", "After Party", "Test function"}
    # the bare event is updated IN PLACE, so a campaign pointing at it stays valid
    assert events["Sufi Night"]["id"] == bare
    assert events["Sufi Night"]["venue"] == "Great Park"
    assert "Shadab Faridi" in events["Sufi Night"]["announcement"]
    assert events["After Party"]["start_time"] == "23:00"
    # without the opt-in flags nothing is deleted
    assert len(db.list_contacts(wedding_id=wid, limit=50)["items"]) == 6


def test_set_events_opt_ins_remove_extras_and_fake_guests_but_not_in_use_events(fresh_eo_db):
    import seed_demo_wedding as sdw
    db = fresh_eo_db
    db.init()
    wid, bare, stray, cid = _server_like(db)
    # a scheduled campaign now uses the stray event: it must survive --replace-events
    db.get_conn().execute("UPDATE campaigns SET event_id = ? WHERE id = ?", (stray, cid))
    db.get_conn().commit()
    sdw.set_events(replace=True, remove_sample_guests=True)
    assert "Test function" in {e["name"] for e in db.list_events(wid)}
    guests = [c["name"] for c in db.list_contacts(wedding_id=wid, limit=50)["items"]]
    assert guests == ["Heeren Agrawal"]


def test_a_switched_off_agent_leaves_create_campaign_and_cannot_be_used(fresh_eo_db, monkeypatch):
    """"delete other" — shipped agents cannot be deleted (init() re-creates any missing
    slug on every restart), so taking one out of use means switching it off."""
    import asyncio
    db = fresh_eo_db
    db.init()
    user, wid = _campaign_world(db)
    monkeypatch.setattr(eo_auth, "require_eo", lambda request: user)

    class _Req:
        query_params = {}

    def choices():
        return {a["slug"] for a in json.loads(asyncio.run(eo_api.agent_choices(_Req())).body)["items"]}

    reminder = db.get_agent_by_slug("event_reminder")
    db.update_agent(reminder["id"], active=0)
    assert "event_reminder" not in choices() and "wedding_schedule" in choices()
    with pytest.raises(HTTPException):
        eo_api._campaign_payload(user, _body(reminder["id"], wid))

    db.update_agent(reminder["id"], active=1)                 # reversible
    assert "event_reminder" in choices()


def test_a_restart_does_not_switch_an_agent_back_on(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    logistics = db.get_agent_by_slug("logistics_concierge")
    db.update_agent(logistics["id"], active=0)
    db.init()                                                  # what every restart runs
    assert db.get_agent_by_slug("logistics_concierge")["active"] == 0


def test_set_events_refuses_to_guess_between_two_weddings_with_the_same_name(fresh_eo_db):
    import seed_demo_wedding as sdw
    db = fresh_eo_db
    db.init()
    db.create_wedding(sdw.WEDDING_NAME)
    db.create_wedding(sdw.WEDDING_NAME)
    with pytest.raises(SystemExit):
        sdw.set_events()


# ------------------------------------------------- never miss a function (last test round)
def test_the_schedule_turn_is_a_checklist_with_the_count_and_the_names():
    """"it is only informing about the one event ... sometimes it misses one or more": the
    agent was told to 'go through the list' with nothing to check itself against."""
    now = datetime(2026, 9, 24, 18, 0, tzinfo=IST)
    events = [{"name": "Hi-Tea", "event_date": "2026-09-25", "start_time": "16:00"},
              {"name": "Sufi Night", "event_date": "2026-09-25", "start_time": "19:00"},
              {"name": "After Party", "event_date": "2026-09-25", "start_time": "23:00"},
              {"name": "Old Lunch", "event_date": "2026-09-20", "start_time": "13:00"}]
    out = pr.render_prompt(_seed("wedding_schedule"), events=events, now=now,
                           wedding={"hospitality_team": "Ved and Riya's Hospitality Team"})
    si = out["system_instruction"]
    assert "The functions to tell them about, three in all: Hi-Tea, Sufi Night and After Party." in si
    assert "never stop after the first" in si
    assert "Old Lunch" not in si


def test_the_schedule_turn_gives_every_detail_at_once():
    """"i want all event details at once" — the team's tested prompt gave every function with
    all its highlights in one go; ours held the highlights back until asked."""
    t = _seed("wedding_schedule")["prompt_template"]
    assert "ALL of its highlights" in t
    assert "Do not hold anything back for later" in t
    assert "Keep the rest of the highlights for when they ask" not in t


def test_a_single_function_still_reads_as_a_sentence():
    """"There are one functions" — the count is now phrased so any number reads correctly."""
    now = datetime(2026, 9, 24, 18, 0, tzinfo=IST)
    out = pr.render_prompt(_seed("wedding_schedule"), now=now,
                           events=[{"name": "Sufi Night", "event_date": "2026-09-25"}])
    si = out["system_instruction"]
    assert "The functions to tell them about, one in all: Sufi Night." in si
    assert "one functions" not in si


def test_every_agent_forbids_asking_for_approval():
    """The agent ended its turn with "Does that sound good?" — an RSVP-style question the
    call is not meant to ask."""
    for slug, seed in {s["slug"]: s for s in agent_seeds.SEEDS}.items():
        t = seed["prompt_template"]
        assert "Never ask for their approval or agreement" in t, slug
        assert '"does that sound good?"' in t, slug


def test_why_are_you_calling_is_a_question_not_a_busy_signal():
    """Live test: the guest cut in with "aap ye btae aapne call kis liye kra h" and the agent
    replied "Should I call you later?" — then offered the callback AGAIN after "नहीं"."""
    for slug, seed in {s["slug"]: s for s in agent_seeds.SEEDS}.items():
        t = seed["prompt_template"]
        assert "## IF THEY ASK WHY YOU ARE CALLING" in t, slug
        assert "kis liye call kiya" in t, slug
        assert "Offer a callback at most ONCE" in t, slug


def test_an_assistant_taking_notes_gets_the_full_schedule_and_no_early_goodbye():
    """Shivi's call: "you are speaking with Shivi's assistant, I can take notes" got only
    name/time/place and a goodbye, and the agent ended the call on its own question."""
    t = _seed("wedding_schedule")["prompt_template"]
    assert "an assistant offering to take a message or notes" in t
    assert "give THE SCHEDULE exactly as you would to the guest" in t
    assert "that is note-taking, not a goodbye" in t
    for slug, seed in {s["slug"]: s for s in agent_seeds.SEEDS}.items():
        assert "NEVER call end_call in a turn that asks a question" in seed["prompt_template"], slug


def test_every_agent_handles_a_bad_line_and_a_call_screening_assistant():
    """Mansi's phone answered with "If you record your name and reason for calling, I'll see
    if this person is available", and her line then broke up — the agent gave up and booked
    a callback."""
    for slug, seed in {s["slug"]: s for s in agent_seeds.SEEDS}.items():
        t = seed["prompt_template"]
        assert "## IF THE LINE IS BAD" in t, slug
        assert "A bad line is NOT a reason to end the call or book a callback" in t, slug
        assert "## IF A CALL-SCREENING ASSISTANT ANSWERS" in t, slug
        assert "record your name and reason for calling" in t, slug


# ------------------------------------------------ callbacks on a cancelled campaign
def _tick_world(monkeypatch, campaign_status):
    """A pending callback on campaign 97, with every outside call stubbed."""
    import asyncio
    import callbacks
    import dialer
    import eo_db
    import live
    import scheduler
    import store
    call = {"id": "c1", "callback": {"status": "pending", "to": "+919773127146",
                                     "campaign_id": 97, "attempts": 0}}
    dialed = []

    async def pending(now):
        return [{"id": "c1"}]

    async def load(cid):
        return call

    async def save(c):
        return None

    async def place(to, **kw):
        dialed.append((to, kw.get("agent_id"), kw.get("event_id")))
        return {"call_uuid": "x"}

    async def not_paused(now):
        return False

    monkeypatch.setattr(store, "list_pending_callbacks", pending)
    monkeypatch.setattr(store, "load_call", load)
    monkeypatch.setattr(store, "save_call", save)
    monkeypatch.setattr(dialer, "place_call", place)
    monkeypatch.setattr(scheduler, "_is_paused", not_paused)
    monkeypatch.setattr(callbacks, "in_call_window", lambda a, b: True)
    monkeypatch.setattr(live, "room", lambda: 10)
    monkeypatch.setattr(eo_db, "user_provider", lambda uid: None)
    monkeypatch.setattr(eo_db, "get_campaign", lambda cid: {
        "id": cid, "status": campaign_status, "call_start_min": 0, "call_end_min": 1439,
        "agent_id": 9, "event_id": 25, "wedding_id": 4, "created_by": 1})
    asyncio.run(scheduler._tick())
    return call, dialed


def test_a_callback_on_a_cancelled_campaign_is_cancelled_not_dialled(monkeypatch):
    """Campaign 97 ran on the wrong script and booked callbacks for 10:00 the next day;
    they redial with the campaign's agent and event, cancelled or not."""
    call, dialed = _tick_world(monkeypatch, "cancelled")
    assert dialed == []
    assert call["callback"]["status"] == "cancelled"


def test_a_callback_on_a_completed_campaign_still_rings(monkeypatch):
    """A guest who said "call me later" is still owed that call after the campaign's
    first pass finishes."""
    call, dialed = _tick_world(monkeypatch, "completed")
    assert dialed == [("+919773127146", 9, 25)]


# --------------------------------------------- guests always land on a wedding (Contacts page)
# Live: an upload from the Contacts page reported "1 read · 0 added · 1 updated" and the list
# said "No guests yet". The page sent no wedding, so the row was filed under wedding 0 while
# the list showed wedding 4. Both writes now require a wedding, and the UI supplies one.
def _guard_world(db):
    h, s = eo_auth.hash_password("pw123456")
    uid = db.create_user(username="boss", name="B", password_hash=h, password_salt=s, role="eo_admin")
    wid = db.create_wedding("W", created_by=uid)
    return db.get_user(uid), wid


class _Req:
    def __init__(self, body=None, form=None):
        self._b, self._f = body or {}, form or {}
        self.query_params, self.headers, self.client = {}, {}, None
    async def json(self): return self._b
    async def form(self): return self._f


def test_adding_a_guest_without_a_wedding_is_refused(fresh_eo_db, monkeypatch):
    import asyncio
    db = fresh_eo_db
    db.init()
    user, wid = _guard_world(db)
    monkeypatch.setattr(eo_auth, "require_eo", lambda request: user)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(eo_api.contacts_add(_Req({"name": "Nandini", "phone": "+918320286829"})))
    assert exc.value.status_code == 400 and "wedding" in exc.value.detail.lower()
    assert db.list_contacts(limit=10)["total"] == 0                       # nothing filed under 0

    asyncio.run(eo_api.contacts_add(_Req({"name": "Nandini", "phone": "+918320286829", "wedding_id": wid})))
    rows = db.list_contacts(wedding_id=wid, limit=10)["items"]
    assert [r["name"] for r in rows] == ["Nandini"]                        # visible on ITS wedding


def test_uploading_a_guest_list_without_a_wedding_is_refused(fresh_eo_db, monkeypatch):
    import asyncio, io
    from openpyxl import Workbook
    db = fresh_eo_db
    db.init()
    user, wid = _guard_world(db)
    monkeypatch.setattr(eo_auth, "require_eo", lambda request: user)
    wb = Workbook(); ws = wb.active
    ws.append(["Name", "Phone"]); ws.append(["Nandini", "+91 83202 86829"])
    buf = io.BytesIO(); wb.save(buf)

    class _Up:
        filename = "guests.xlsx"
        async def read(self): return buf.getvalue()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(eo_api.contacts_import(_Req(form={}), file=_Up()))
    assert exc.value.status_code == 400
    r = asyncio.run(eo_api.contacts_import(_Req(form={"wedding_id": str(wid)}), file=_Up()))
    assert (r["added"], r["updated"]) == (1, 0)
    assert db.list_contacts(wedding_id=wid, limit=10)["total"] == 1
