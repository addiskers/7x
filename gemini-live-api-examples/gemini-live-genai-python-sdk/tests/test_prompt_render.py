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
