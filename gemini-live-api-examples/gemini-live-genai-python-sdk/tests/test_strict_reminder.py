"""The one-function Event Reminder (25 Sep 2026): the family's opening and reminder lines,
warmly, answering what it knows about THAT function — and never naming another. Plus the
next-function fallback a call with no function needs, and the script-only call-back trigger."""

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


# ------------------------------------------------------------------ the family's lines
def test_the_familys_lines_are_in_the_prompt_word_for_word():
    si = _render()["system_instruction"]
    assert "Hello, I'm speaking from Ved and Riya's Hospitality Team. Am I speaking to Apeksha?" in si
    assert ("Hi Apeksha! I just wanted to inform you that Sufi Night will start at seven in the "
            "evening at The Great Park.") in si
    assert "looking forward to seeing them there" in si
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
    assert "## ONE FUNCTION ONLY" in si
    assert "Never mention any other function" in si
    assert "the team will share those details with them separately" in si


def test_it_is_warm_but_shares_nothing_beyond_the_script():
    """The first strict cut deflected "what time?" and hung up; the warm cut then confirmed
    Jain food on its own. The family's line: the manner is warm, the CONTENT is the script —
    time and venue may be repeated, anything else goes to the team, nothing is confirmed."""
    t = _seed()["prompt_template"]
    for ph in ("{dress_code}", "{announcement}", "{hotel}", "{room_number}", "{side_phrase}"):
        assert ph not in t, ph
    si = _render()["system_instruction"]
    assert "acknowledge them warmly, by name" in si
    assert "You may repeat the time and the venue." in si
    assert "I will tell the team to get back to you on that" in si
    assert "Never confirm that something will be provided or arranged — not Jain food" in si
    assert "Noted — I'll pass that on to the team" in si
    assert "Acknowledging is not confirming" in si
    assert "put it in the note when you record the outcome" in si
    assert "## WHEN THEY ASK YOU SOMETHING ELSE" in si         # never hangs up on a question
    assert "Is there anything else I can help you with?" in si
    assert "guest support desks" not in si
    assert "That is the whole of what you may say about it." in si


def test_three_languages_no_menu_and_the_goodbye_stays_in_the_calls_language():
    """Gujarati was answered in Hindi, a menu of "two options" was offered, and after a
    switch the closing came back in English."""
    si = _render()["system_instruction"]
    assert "You understand English, Hindi and Gujarati — nothing else." in si
    assert "never in Hindi" in si
    assert "Never offer a choice of languages" in si
    assert "Would you prefer to continue in" not in si
    assert "Marathi, Punjabi, Bengali" not in si
    assert "Say everything — the reminder, your answers and your goodbye — in that language" in si
    assert "Never switch back to English for the closing" in si


def test_no_outcome_before_the_reminder_and_announcements_are_not_the_guest():
    """Devvrat's call opened on the carrier's "Your call has been forwarded"; the model
    recorded an outcome on it, the bridge's mute-record nudge asked for "your ONE short
    closing", and the guest heard "Looking forward to seeing you" with no reminder."""
    si = _render()["system_instruction"]
    assert "your call has been forwarded" in si
    assert "is the network, not the guest" in si
    assert "never call record_outcome before you have said THE REMINDER to a person" in si


def test_the_shared_rules_it_still_needs_are_present():
    si = _render()["system_instruction"]
    for heading in ("## IF THE LINE IS BAD", "## IF A CALL-SCREENING ASSISTANT ANSWERS",
                    "## IF THEY ASK WHY YOU ARE CALLING", "## LISTENING SOUNDS ARE NOT GOODBYES",
                    "## THE GOLDEN RULE", "## ENDING THE CALL"):
        assert heading in si, heading
    assert "NEVER call end_call in a turn that asks a question" in si
    assert "Sir OR Ma'am" in si                               # the honorific rule is back too


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
    assert "Sufi Night will start at seven in the evening at The Great Park." in si
    assert "Am I speaking to" not in r["trigger"]
    assert "never invent one" in r["trigger"]


def test_the_seed_row():
    s = _seed()
    assert s["requires_event"] == 1
    assert s["listen_seconds"] >= 12                          # long enough to hear a question
    assert prompt_render.validate_template(s["prompt_template"]) == []
    assert prompt_render.validate_template(s["trigger_template"]) == []


def test_the_reminder_is_current_and_the_old_one_is_stale():
    """stale_agent_reasons demanded a schedule placeholder — absent from a one-function
    agent by design. A copy of the OLD reminder must still be flagged, or --refresh-agents
    would leave it speaking the schedule."""
    assert eo_db.stale_agent_reasons(_seed()) == []
    old = ("Then, in the same breath, that you are excited to welcome them and have some "
           "details about this evening. {schedule} SPEAK TO A PERSON")
    assert any("have some details about this evening" in r
               for r in eo_db.stale_agent_reasons({"prompt_template": old}))
    # one function only, but no escalation block: still stale
    assert eo_db.stale_agent_reasons({"prompt_template": "## ONE FUNCTION ONLY\nno escalation"})


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


# ------------------------------------------------- call-backs: script-only vs conversational
def test_a_call_back_on_a_script_only_agent_gets_its_script_not_how_can_i_help():
    """A script-only agent (## STRICT RULES) cannot help with anything, so a call-back is
    told to greet, thank them and say its script. The shipped reminder is conversational
    again and keeps the history-aware trigger."""
    import main
    strict = {"prompt_template": "## STRICT RULES\n- say only the script"}
    history = ("[INBOUND CALL: Devvrat is calling us, and we have ALREADY had this conversation "
               "with them … Thank them warmly for calling and ask how you can help.]")
    t = main._inbound_trigger_for(strict, "Devvrat", "Ved and Riya's Hospitality Team", history)
    assert t != history
    assert "Hello Devvrat, I'm speaking from Ved and Riya's Hospitality Team" in t
    assert "THE REMINDER and THE CLOSE" in t
    assert "Do NOT ask who they are and do NOT ask how you can help" in t

    nameless = main._inbound_trigger_for(strict, "", "Ved and Riya's Hospitality Team", "")
    assert "never invent one" in nameless and "Am I speaking" not in nameless

    reminder = _seed()
    assert not eo_db.is_strict_agent(reminder)
    assert main._inbound_trigger_for(reminder, "Devvrat", "team", history) == history
    assert main._inbound_trigger_for(reminder, "Devvrat", "team", "") == ""
