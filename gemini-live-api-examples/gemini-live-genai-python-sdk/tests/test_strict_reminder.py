"""The strict Event Reminder (25 Sep 2026): the family's three-line script for one function,
no other function named, no question answered — and the next-function fallback a call with
no function needs."""

from datetime import datetime
from zoneinfo import ZoneInfo

import agent_seeds
import eo_db
import prompt_render

_IST = ZoneInfo("Asia/Kolkata")

WEDDING = {"name": "Ved & Riya", "hospitality_team": "Ved and Riya's Hospitality Team"}
HI_TEA = {"id": 1, "name": "Hi-Tea", "event_date": "2026-09-25", "start_time": "16:00",
          "venue": "Harvest", "audience": "all"}
SUFI = {"id": 2, "name": "Sufi Night", "event_date": "2026-09-25", "start_time": "19:00",
        "venue": "The Great Park", "audience": "all"}
AFTER = {"id": 3, "name": "After Party", "event_date": "2026-09-25", "start_time": "23:00",
         "venue": "The Ballroom", "audience": "all"}
EVENTS = [HI_TEA, SUFI, AFTER]
GUEST = {"name": "Apeksha Shah", "phone": "+919904240078", "side": "bride"}


def _seed():
    return next(s for s in agent_seeds.SEEDS if s["slug"] == "event_reminder")


def _render(guest=GUEST, event=SUFI):
    return prompt_render.render_prompt(_seed(), wedding=WEDDING, event=event, guest=guest,
                                       events=EVENTS)


def _at(hh, mm=0):
    return datetime(2026, 9, 25, hh, mm, tzinfo=_IST)


# ------------------------------------------------------------------ the script itself
def test_the_three_lines_are_in_the_prompt_word_for_word():
    si = _render()["system_instruction"]
    assert "Hello, I'm speaking from Ved and Riya's Hospitality Team." in si
    assert "Am I speaking to Apeksha?" in si
    assert ("I just wanted to inform you that Sufi Night will start at seven in the evening "
            "at The Great Park.") in si
    assert "Looking forward to seeing you." in si
    assert "19:00" not in si and "7:00" not in si            # spoken, never digits


def test_no_other_function_is_named_anywhere():
    """"Do not mention Hi tea (Strictly)" — the prompt carries ONE function and no
    schedule placeholder, so it cannot read another one out even when asked."""
    t = _seed()["prompt_template"]
    for ph in ("{schedule}", "{schedule_detail}", "{upcoming_schedule}", "{upcoming_names}"):
        assert ph not in t, ph
    si = _render()["system_instruction"]
    for other in ("Hi-Tea", "Harvest", "After Party", "Ballroom"):
        assert other not in si, other
    assert "Never mention any other function" in si


def test_nothing_beyond_the_script_is_offered():
    """"Nothing else to be mentioned other than what i have written above"."""
    t = _seed()["prompt_template"]
    for ph in ("{dress_code}", "{announcement}", "{hotel}", "{room_number}", "{side_phrase}"):
        assert ph not in t, ph
    si = _render()["system_instruction"]
    assert "Is there anything else I can help you with?" not in si
    assert "Do you have any questions about any of these?" not in si
    assert "guest support desks" not in si
    assert "excited to welcome" not in si.replace('no "excited to welcome you"', "").replace(
        "no \"we're excited to welcome you\"", "")
    assert "## STRICT RULES" in si


def test_a_question_gets_one_fixed_line_then_the_sign_off():
    """"AI should not reply to any other query" — but a guest with a question is never hung
    up on mid-sentence: one line, then the close."""
    si = _render()["system_instruction"]
    assert "The hospitality team will get back to you on that." in si
    assert "Never answer the question itself" in si
    assert "WHEN THEY ASK YOU SOMETHING ELSE" not in si      # the helpfulness block is gone


def test_three_languages_and_never_a_menu():
    """Gujarati was answered in Hindi, then the agent offered "two options", then three, then
    drifted into Tamil — all from a prompt that named ten languages and asked when unsure."""
    si = _render()["system_instruction"]
    assert "You understand English, Hindi and Gujarati — nothing else." in si
    assert "never in Hindi" in si                             # Gujarati is not Hindi
    assert "Never offer a choice of languages" in si
    assert "Would you prefer to continue in" not in si
    assert "Marathi, Punjabi, Bengali" not in si              # the ten-language sentence


def test_the_shared_rules_it_still_needs_are_present():
    si = _render()["system_instruction"]
    for heading in ("## IF THE LINE IS BAD", "## IF A CALL-SCREENING ASSISTANT ANSWERS",
                    "## IF THEY ASK WHY YOU ARE CALLING", "## LISTENING SOUNDS ARE NOT GOODBYES",
                    "## THE GOLDEN RULE"):
        assert heading in si, heading
    assert "NEVER call end_call in a turn that asks a question" in si
    assert "Sir OR Ma'am" not in si                           # no honorific block at all


def test_the_trigger_is_the_opening_and_nothing_more():
    trig = _render()["trigger"]
    assert ("Hello, I'm speaking from Ved and Riya's Hospitality Team. Am I speaking to "
            "Apeksha?") in trig
    assert "Do NOT give the reminder" in trig


def test_a_nameless_call_never_asks_who_it_is_speaking_to():
    """An unknown inbound number: the name lines tidy away, the reminder line stays."""
    r = _render(guest=None)
    si = r["system_instruction"]
    assert "Am I speaking to" not in si
    assert "Hello, I'm speaking from Ved and Riya's Hospitality Team." in si
    assert "Sufi Night will start at seven in the evening at The Great Park." in si
    assert "Am I speaking to" not in r["trigger"]
    assert "never invent one" in r["trigger"]


def test_the_seed_row_fits_a_short_call():
    s = _seed()
    assert s["requires_event"] == 1
    assert 0 < s["listen_seconds"] <= 10                      # nothing to wait for after the close
    assert prompt_render.validate_template(s["prompt_template"]) == []
    assert prompt_render.validate_template(s["trigger_template"]) == []


def test_the_strict_prompt_is_current_and_the_old_reminder_is_stale():
    """stale_agent_reasons demanded a schedule placeholder and the escalation block — both
    absent from a script-only agent by design. A copy of the OLD reminder must still be
    flagged, or --refresh-agents would leave it speaking the conversational script."""
    assert eo_db.stale_agent_reasons(_seed()) == []
    old = ("Then, in the same breath, that you are excited to welcome them and have some "
           "details about this evening. {schedule} SPEAK TO A PERSON")
    assert any("have some details about this evening" in r
               for r in eo_db.stale_agent_reasons({"prompt_template": old}))


# ------------------------------------------------------------- the next function to start
def test_next_event_is_the_first_one_not_yet_started():
    assert prompt_render.next_event(EVENTS, now=_at(15, 59))["name"] == "Hi-Tea"
    assert prompt_render.next_event(EVENTS, now=_at(16, 0))["name"] == "Hi-Tea"
    assert prompt_render.next_event(EVENTS, now=_at(18, 0))["name"] == "Sufi Night"
    assert prompt_render.next_event(EVENTS, now=_at(19, 30))["name"] == "After Party"


def test_next_event_after_the_last_start_is_the_last_function():
    assert prompt_render.next_event(EVENTS, now=_at(23, 30))["name"] == "After Party"
    late = datetime(2026, 9, 26, 2, 0, tzinfo=_IST)
    assert prompt_render.next_event(EVENTS, now=late)["name"] == "After Party"


def test_next_event_orders_by_time_and_survives_bad_rows():
    shuffled = [AFTER, {"id": 9, "name": "Undated", "start_time": "10:00"}, SUFI, HI_TEA]
    assert prompt_render.next_event(shuffled, now=_at(18, 0))["name"] == "Sufi Night"
    assert prompt_render.next_event([], now=_at(18, 0)) is None
    assert prompt_render.next_event(None, now=_at(18, 0)) is None
    undated_only = [{"id": 9, "name": "Undated"}]
    assert prompt_render.next_event(undated_only, now=_at(18, 0))["name"] == "Undated"


def test_next_event_treats_a_naive_clock_as_ist():
    assert prompt_render.next_event(EVENTS, now=datetime(2026, 9, 25, 18, 0))["name"] == "Sufi Night"
