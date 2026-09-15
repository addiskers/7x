"""prompt_render.py — placeholder resolution, spoken time/date, and the cleanup that
stops a missing value from being read aloud as punctuation."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import prompt_render as pr

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 20, 8, 0, tzinfo=IST)

WEDDING = {
    "name": "Anant & Manya", "groom_name": "Anant", "bride_name": "Manya",
    "hospitality_team": "the Wedding Hospitality team",
    "placard_text": "Kapoor & Chopra Family Welcomes You",
    "contact_phone": "+91 98123 45678", "contact_name": "Rohit",
    "city": "Udaipur",
}
EVENT = {
    "id": 1, "name": "Saanth Ritual", "event_date": "2026-09-21", "start_time": "10:30",
    "venue": "The Imperial Ballroom", "audience": "groom", "dress_code": "Indian formals",
}
GUEST = {
    "name": "Rajesh Kumar", "phone": "+919876543210", "side": "groom",
    "dietary": "vegetarian", "transport_mode": "flight", "transport_number": "AI 456",
    "arrival_at": "2:30 PM on 19 September", "hotel": "Fairmont Udaipur",
}


# --------------------------------------------------------------------------- spoken forms
@pytest.mark.parametrize("raw,expected", [
    ("19:00", "seven in the evening"),
    ("10:30", "half past ten in the morning"),
    ("13:00", "one in the afternoon"),
    ("20:00", "eight at night"),
    ("12:30", "half past twelve in the afternoon"),
    ("18:45", "quarter to seven in the evening"),
    ("07:15", "quarter past seven in the morning"),
])
def test_times_are_spoken_never_digits(raw, expected):
    assert pr._spoken_time(raw) == expected


def test_prose_time_is_left_alone():
    # The client's schedule says "6:00 PM onwards" — don't mangle what is already prose.
    assert pr._spoken_time("6 PM onwards") == "6 PM onwards"
    assert pr._spoken_time("") == ""


def test_dates_are_spoken():
    assert pr._spoken_date("2026-09-19") == "the nineteenth of September"
    assert pr._spoken_date("2026-09-21") == "the twenty-first of September"
    assert pr._spoken_date("nonsense") == ""


def test_when_phrase_is_relative_to_today():
    today = NOW.date()
    assert pr._when_phrase("2026-09-20", today) == "today"
    assert pr._when_phrase("2026-09-21", today) == "tomorrow"
    assert pr._when_phrase("2026-09-25", today) == "on the twenty-fifth of September"


# ----------------------------------------------------------------------- resolution order
def test_event_wins_over_wedding_for_a_shared_key():
    ctx = pr.build_context(wedding=WEDDING, event=EVENT, guest=GUEST, now=NOW)
    assert ctx["venue"] == "The Imperial Ballroom"


def test_a_blank_on_the_more_specific_row_does_not_shadow():
    """Layering skips empty values: a blank on a later (more specific) layer must not wipe
    a real value an earlier layer supplied."""
    # extra is the highest layer; a blank there must not erase the event's venue.
    ctx = pr.build_context(wedding=WEDDING, event=EVENT, now=NOW, extra={"venue": ""})
    assert ctx["venue"] == "The Imperial Ballroom"
    # and an event with no venue simply leaves the key unset rather than blanking loudly
    ctx2 = pr.build_context(wedding=WEDDING, event={**EVENT, "venue": ""}, now=NOW)
    assert ctx2.get("venue", "") == ""


def test_extra_overrides_everything():
    ctx = pr.build_context(wedding=WEDDING, event=EVENT, guest=GUEST, now=NOW,
                           extra={"guest_name": "Sample Guest"})
    assert ctx["guest_name"] == "Sample Guest"


def test_guest_derivations():
    ctx = pr.build_context(wedding=WEDDING, event=EVENT, guest=GUEST, now=NOW)
    assert ctx["guest_name"] == "Rajesh"              # first name only
    assert ctx["guest_full_name"] == "Rajesh Kumar"
    assert ctx["side_phrase"] == "the groom's side"
    # flight_number/train_number alias transport_number by mode, so a template need not branch
    assert ctx["flight_number"] == "AI 456"
    assert ctx.get("train_number", "") == ""


def test_train_alias_only_fires_for_a_train():
    ctx = pr.build_context(guest={**GUEST, "transport_mode": "train",
                                  "transport_number": "12951"}, now=NOW)
    assert ctx["train_number"] == "12951"
    assert ctx.get("flight_number", "") == ""


def test_side_phrase_for_every_side():
    for side, expected in [("groom", "the groom's side"), ("bride", "the bride's side"),
                           ("both", "both families"), ("", "")]:
        ctx = pr.build_context(guest={"side": side}, now=NOW)
        assert ctx.get("side_phrase", "") == expected


# ------------------------------------------------------------------------- missing values
def test_missing_placeholder_blanks_and_is_reported_never_raises():
    out, missing = pr.render("Reminder: {event_name} at {venue}.", {"event_name": "Mehendi"})
    assert missing == ["venue"]
    assert "{venue}" not in out


def test_a_line_that_lost_its_only_fact_is_dropped_whole():
    """A bare "- Where:" would be read aloud verbatim, so the line goes instead."""
    out, _ = pr.render("- Function: {event_name}\n- Where: {venue}\n- When: {event_time}",
                       {"event_name": "Mehendi", "event_time": "one in the afternoon"})
    assert "- Function: Mehendi" in out
    assert "- When: one in the afternoon" in out
    assert "Where" not in out


def test_dangling_prepositions_are_removed():
    out, _ = pr.render("It begins at {event_time} at {venue}.", {})
    assert "at at" not in out and "at ." not in out


def test_author_prose_is_never_rewritten():
    """Only lines that actually lost a value get cleaned. A heading ending in a preposition
    ("...SPEAKING TO") and a line ending in a colon must survive untouched."""
    template = ("## WHO YOU ARE SPEAKING TO\n"
                "Branch on their reply:\n"
                "- Their name: {guest_name}\n"
                "Contact them at {contact_phone}.")
    out, _ = pr.render(template, {"guest_name": "Rajesh"})
    assert "## WHO YOU ARE SPEAKING TO" in out
    assert "Branch on their reply:" in out
    assert "- Their name: Rajesh" in out


def test_fully_resolved_text_is_unchanged():
    out, missing = pr.render("Full: {event_name} at {event_time} at {venue}.",
                             {"event_name": "Mehendi", "event_time": "one in the afternoon",
                              "venue": "Ivory Garden"})
    assert missing == []
    assert out == "Full: Mehendi at one in the afternoon at Ivory Garden."


# --------------------------------------------------------------------- unknown placeholders
def test_unknown_placeholder_is_an_authoring_bug_not_missing_data():
    assert pr.validate_template("Hello {even_name} and {guest_name}") == ["even_name"]
    assert pr.validate_template("Hello {guest_name}") == []
    with pytest.raises(pr.PromptRenderError):
        pr.render("Hello {even_name}", {}, strict=True)


def test_unknown_placeholder_does_not_break_a_live_call():
    out, _ = pr.render("Hello {even_name}!", {})       # strict=False, the live path
    assert "{even_name}" not in out


def test_shipped_templates_use_only_known_placeholders():
    import agent_seeds
    for seed in agent_seeds.SEEDS:
        assert pr.validate_template(seed["prompt_template"]) == [], seed["slug"]
        assert pr.validate_template(seed["trigger_template"]) == [], seed["slug"]


# ------------------------------------------------------------------------- render_prompt
def test_render_prompt_returns_instruction_and_trigger():
    agent = {"slug": "t", "prompt_template": "You are calling for {hospitality_team}.",
             "trigger_template": "[Say hello to {guest_name}.]"}
    r = pr.render_prompt(agent, wedding=WEDDING, event=EVENT, guest=GUEST, now=NOW)
    assert r["system_instruction"] == "You are calling for the Wedding Hospitality team."
    assert r["trigger"] == "[Say hello to Rajesh.]"
    assert r["missing"] == []


def test_an_agent_with_no_trigger_still_gets_one():
    """The Live API emits no audio until it receives a turn, so a call with an empty
    trigger would sit silent. Fall back to a name-aware opening."""
    r = pr.render_prompt({"prompt_template": "x", "trigger_template": ""}, guest=GUEST, now=NOW)
    assert "Rajesh" in r["trigger"]

    anon = pr.render_prompt({"prompt_template": "x", "trigger_template": ""}, now=NOW)
    assert anon["trigger"]
    assert "never invent one" in anon["trigger"]


def test_render_prompt_never_raises_on_a_missing_row():
    r = pr.render_prompt({"prompt_template": "Hi {guest_name}, {event_name} at {venue}."},
                         now=NOW)
    assert r["system_instruction"]
    assert set(r["missing"]) == {"guest_name", "event_name", "venue"}


# ------------------------------------------------------------------------------ schedule
SCHEDULE_EVENTS = [
    {"id": 1, "name": "Ghazal Night", "event_date": "2026-09-19", "start_time": "19:00",
     "venue": "Infinity Terrace", "audience": "all"},
    {"id": 2, "name": "Saanth", "event_date": "2026-09-21", "start_time": "11:00",
     "venue": "Imperial Terrace", "audience": "groom"},
    {"id": 3, "name": "Chuda Ceremony", "event_date": "2026-09-21", "start_time": "12:30",
     "venue": "Imperial Ballroom", "audience": "bride"},
    {"id": 4, "name": "Wedding Ceremony", "event_date": "2026-09-21", "start_time": "22:30",
     "venue": "Chand Baori", "audience": "all"},
]


def test_schedule_is_filtered_to_the_guests_own_side():
    """A bride-side guest must never be told about a groom-only ritual — it is not theirs
    to attend."""
    groom = pr.build_schedule(SCHEDULE_EVENTS, side="groom")
    assert "Saanth" in groom and "Chuda Ceremony" not in groom

    bride = pr.build_schedule(SCHEDULE_EVENTS, side="bride")
    assert "Chuda Ceremony" in bride and "Saanth" not in bride

    # everyone-events are on both
    for s in (groom, bride):
        assert "Ghazal Night" in s and "Wedding Ceremony" in s


def test_a_guest_with_no_recorded_side_still_gets_every_function():
    """Matches eo_db.guests_for_audience: a blank side in the sheet must not silently hide
    a guest's own schedule from them."""
    for blank in ("", None, "both"):
        s = pr.build_schedule(SCHEDULE_EVENTS, side=blank)
        assert "Saanth" in s and "Chuda Ceremony" in s, f"side={blank!r}"


def test_schedule_times_are_spoken_never_digits():
    s = pr.build_schedule(SCHEDULE_EVENTS, side="groom")
    assert "seven in the evening" in s
    assert "half past ten at night" in s
    for digits in ("19:00", "11:00", "22:30"):
        assert digits not in s


def test_schedule_notes_the_side_only_when_it_narrows_who_is_invited():
    s = pr.build_schedule(SCHEDULE_EVENTS, side="")
    assert "Saanth" in s
    saanth_line = next(l for l in s.splitlines() if "Saanth" in l)
    ghazal_line = next(l for l in s.splitlines() if "Ghazal" in l)
    assert "groom's-side" in saanth_line
    assert "side" not in ghazal_line          # an all-guests function needs no label


def test_schedule_marks_the_event_this_call_is_about():
    s = pr.build_schedule(SCHEDULE_EVENTS, side="groom", current_event_id=2)
    saanth_line = next(l for l in s.splitlines() if "Saanth" in l)
    assert "calling about" in saanth_line


def test_schedule_uses_relative_days_when_today_is_known():
    from datetime import date
    s = pr.build_schedule(SCHEDULE_EVENTS, side="groom", today=date(2026, 9, 20))
    assert "yesterday" in s          # Ghazal on the 19th
    assert "tomorrow" in s           # Saanth on the 21st


def test_schedule_reaches_the_context_and_counts_only_attendable_events():
    ctx = pr.build_context(wedding=WEDDING, event=SCHEDULE_EVENTS[1], guest=GUEST,
                           now=NOW, events=SCHEDULE_EVENTS)
    assert "Saanth" in ctx["schedule"]
    assert "Chuda" not in ctx["schedule"]      # GUEST is groom-side
    assert ctx["event_count"] == "3"           # 2 all-guests + 1 groom


def test_no_events_leaves_schedule_missing_rather_than_blank_text():
    r = pr.render_prompt({"prompt_template": "Schedule:\n{schedule}"}, guest=GUEST, now=NOW)
    assert "schedule" in r["missing"]


def test_guest_can_attend_matches_the_db_audience_rule():
    for audience, side, expected in [
        ("all", "groom", True), ("all", "", True),
        ("groom", "groom", True), ("groom", "bride", False),
        ("bride", "bride", True), ("bride", "groom", False),
        ("groom", "both", True), ("bride", "both", True),
        ("groom", "", True), ("bride", "", True),        # unknown side sees everything
    ]:
        assert pr.guest_can_attend({"audience": audience}, side) is expected, (audience, side)


# ------------------------------------------------- shipped templates: client regressions
# Each of these pins a specific complaint from the client's live test calls. They assert
# on the template TEXT because that is where every one of these bugs actually lived.
def _shipped():
    import agent_seeds
    return {s["slug"]: s for s in agent_seeds.SEEDS}


def test_no_agent_ever_says_sir_or_maam_aloud():
    """"Sir/Ma'am dono bol raha hai" — the prompt used to literally instruct it to."""
    for slug, seed in _shipped().items():
        opening = seed["prompt_template"].split("## THE OPENING")[-1][:400]
        assert "Sir or Ma'am, am I speaking" not in opening, slug
        assert "Sir or Ma'am" not in seed["trigger_template"], slug


def test_the_reminder_agent_may_discuss_other_functions():
    """"event details vala kisi or event ki details nahi de raha" — caused by an explicit
    prohibition in the prompt."""
    seed = _shipped()["event_reminder"]
    assert "Do NOT volunteer details about any other function" not in seed["prompt_template"]
    assert "{schedule}" in seed["prompt_template"]


def test_both_agents_escalate_instead_of_hanging_up():
    """"koi person ke sath baat karane ko bolu to end ho jata hai" and "out of context
    puchta hai to call end ho jata hai"."""
    for slug, seed in _shipped().items():
        t = seed["prompt_template"]
        assert "I will notify the team" in t, slug
        assert "SPEAK TO A PERSON" in t, slug
        assert "NONE of these is a reason to end the call" in t, slug


def test_no_agent_offers_a_phone_number_to_ring_back():
    """"is there anything u can call on this number 98945..?" — the contact fields were
    blank, so the agent was offering a number it did not have. We call guests; they never
    need to ring us."""
    for slug, seed in _shipped().items():
        t = seed["prompt_template"]
        assert "{contact_phone}" not in t, slug
        assert "{contact_name}" not in t, slug


def test_every_agent_introduces_itself_as_7x_on_behalf_of_the_wedding():
    """7x is the caller on every wedding, so it is fixed; the couple comes from the row."""
    for slug, seed in _shipped().items():
        t = seed["prompt_template"]
        assert "This is 7x, calling on behalf of {wedding_name}'s wedding." in t, slug
        assert "{hospitality_team}" not in t, slug


def test_the_logistics_agent_can_answer_about_the_guests_stay():
    """"room no or stay k regarding, it is not able to answer"."""
    t = _shipped()["logistics_concierge"]["prompt_template"]
    assert "{room_number}" in t and "{hotel}" in t
    assert "THEIR STAY" in t
    assert "Do NOT list the functions" not in t


def test_the_reminder_agent_listens_long_enough_to_hear_a_question():
    """At 6s it hung up while guests were still asking."""
    assert _shipped()["event_reminder"]["listen_seconds"] >= 12
