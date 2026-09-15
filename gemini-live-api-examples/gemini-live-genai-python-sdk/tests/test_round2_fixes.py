"""Second round of live-call fixes: caller-ID rotation, the escalation hangup, stale
per-wedding agent prompts, and travel-data mismatches.

Every test here pins a symptom the client reported on a real call."""

import asyncio

import dialer
import eo_db
import eo_import
from plivo_handler import _looks_like_closing


# --------------------------------------------------------------- caller-ID rotation
def test_a_single_number_still_works(monkeypatch):
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+918031321419")
    monkeypatch.setattr(dialer, "_from_rotation", 0)
    assert [dialer._pick_from_number() for _ in range(3)] == ["+918031321419"] * 3


def test_two_numbers_alternate(monkeypatch):
    """A wedding-day burst from one line gets flagged by carriers."""
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+918031704911,+918031704910")
    monkeypatch.setattr(dialer, "_from_rotation", 0)
    picked = [dialer._pick_from_number() for _ in range(4)]
    assert picked == ["+918031704911", "+918031704910"] * 2


def test_whitespace_and_blanks_are_tolerated(monkeypatch):
    monkeypatch.setenv("PLIVO_FROM_NUMBER", " +918031704911 , ,+918031704910 ")
    assert dialer.from_numbers() == ["+918031704911", "+918031704910"]


def test_no_number_configured_is_not_a_crash(monkeypatch):
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "")
    assert dialer._pick_from_number() == ""
    assert dialer.from_numbers() == []


def test_the_number_dialled_is_the_number_reported(monkeypatch):
    """When a carrier flags a line you need to know which one placed the call."""
    monkeypatch.setenv("PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("PLIVO_AUTH_ID", "x")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "y")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+918031704911,+918031704910")
    monkeypatch.setattr(dialer, "_from_rotation", 0)
    seen = []
    monkeypatch.setattr(dialer, "_place_call_sync",
                        lambda to, url, frm=None: seen.append(frm) or "UUID")
    r1 = asyncio.run(dialer.place_call("+919876543210"))
    r2 = asyncio.run(dialer.place_call("+919876543211"))
    assert seen == ["+918031704911", "+918031704910"]
    assert r1["from"] == seen[0] and r2["from"] == seen[1]


# ------------------------------------------------------- the escalation hangup (bug)
def test_promising_a_follow_up_is_not_a_goodbye():
    """The client's exact scenario: "koi person se baat krne ko bolu to call end ho jata
    hai". The escalation line paraphrases to "speak soon", which used to match the
    closing-phrase regex and trigger the bridge-side hangup."""
    for line in [
        "Certainly. I will notify the Wedding Hospitality team and "
        "someone will reach out to you shortly.",
        "Of course - someone will call you back shortly.",
        "I will inform the team and they will get back to you.",
        "We will speak soon about that.",
        "I'll pass this on and the team will contact you.",
    ]:
        assert _looks_like_closing(line) is False, line


def test_real_goodbyes_still_end_the_call():
    """The hangup path must keep working - an agent that never hangs up is its own bug."""
    for line in ["Thank you so much, goodbye!",
                 "Lovely - see you there! Take care.",
                 "Shukriya, aavjo!",
                 "Have a wonderful evening."]:
        assert _looks_like_closing(line) is True, line


def test_a_question_is_never_a_closing():
    assert _looks_like_closing("Anything else I can help with?") is False


# ------------------------------------------------------- stale per-wedding agents
def test_stale_reasons_name_what_is_wrong():
    old = ('You are calling on behalf of EO Gujarat about the AI First Mindset Workshop. '
           'Say "Hello Sir or Ma\'am, am I speaking with {guest_name}?"')
    reasons = eo_db.stale_agent_reasons({"prompt_template": old})
    joined = " ".join(reasons)
    assert "EO Gujarat" in joined
    assert "other functions" in joined          # missing {schedule}
    assert "asked for a person" in joined       # missing the escalation block


def test_a_current_shipped_prompt_is_not_stale():
    import agent_seeds
    for seed in agent_seeds.SEEDS:
        assert eo_db.stale_agent_reasons(seed) == [], seed["slug"]


def test_refresh_reports_a_stale_wedding_copy_and_force_updates_it(fresh_eo_db, capsys):
    """A copy made in the Agents tab before a fix keeps speaking the old script forever,
    and a call resolves its agent by id - so that copy is what guests actually hear."""
    import agent_seeds
    import seed_demo_wedding as sd
    db = fresh_eo_db
    db.init()
    wid = db.create_wedding("W")
    dup = db.create_agent("Our Reminder", "Calling from EO Gujarat.",
                          wedding_id=wid, slug="event_reminder")

    sd.refresh_agents()                      # default: report only
    out = capsys.readouterr().out
    assert "STALE" in out and "Our Reminder" in out
    assert db.get_agent(dup)["prompt_template"] == "Calling from EO Gujarat."

    sd.refresh_agents(force_all=True)
    shipped = next(s for s in agent_seeds.SEEDS if s["slug"] == "event_reminder")
    assert db.get_agent(dup)["prompt_template"] == shipped["prompt_template"]


def test_all_agents_sees_every_wedding_not_just_one(fresh_eo_db):
    """list_agents() answers "what may THIS wedding use" and hid exactly the rows the
    stale scan needed to find."""
    db = fresh_eo_db
    db.init()
    w1, w2 = db.create_wedding("One"), db.create_wedding("Two")
    db.create_agent("A", "x", wedding_id=w1)
    db.create_agent("B", "x", wedding_id=w2)
    everything = db.all_agents()
    assert {a["name"] for a in everything} >= {"A", "B"}
    assert len(db.list_agents(wedding_id=w1, active_only=False)) < len(everything)


# ------------------------------------------------------- travel-data mismatch
def test_a_flight_code_under_a_train_mode_is_flagged():
    """"isne flight ka mention kiya tha but bola usne train number" - the agent reads the
    mode aloud, so a wrong mode tells the guest the wrong thing."""
    rows = [("Rajesh", "+919876543210", "valid",
             {"transport_mode": "train", "transport_number": "6E 2134"})]
    assert "looks like a flight" in " ".join(eo_import.transport_mismatches(rows))


def test_a_train_number_under_a_flight_mode_is_flagged():
    rows = [("Priya", "+919876543211", "valid",
             {"transport_mode": "flight", "transport_number": "12951"})]
    assert "looks like a train" in " ".join(eo_import.transport_mismatches(rows))


def test_consistent_or_absent_travel_data_is_not_flagged():
    for fields in [
        {"transport_mode": "flight", "transport_number": "AI 456"},
        {"transport_mode": "train", "transport_number": "12951"},
        {"transport_mode": "", "transport_number": "6E 2134"},   # unknown mode, not a clash
        {"transport_mode": "car", "transport_number": "MH 01 AB 1234"},
        {},
    ]:
        rows = [("X", "+919876543210", "valid", fields)]
        assert eo_import.transport_mismatches(rows) == [], fields


# ------------------------------------------------------- prompt guards
def test_the_delay_block_asks_for_the_number_and_reads_it_back():
    """"flight postpone hone pr flight number galat note kr raha hai" - the old block only
    asked when they land, never re-confirming the number."""
    import agent_seeds
    t = next(s for s in agent_seeds.SEEDS
             if s["slug"] == "logistics_concierge")["prompt_template"]
    assert "READING BACK" in t
    assert "character by character" in t
    assert "have I got that right" in t


def test_the_prompt_refuses_to_assert_a_mismatched_travel_mode():
    import agent_seeds
    t = next(s for s in agent_seeds.SEEDS
             if s["slug"] == "logistics_concierge")["prompt_template"]
    assert "IF THE MODE AND THE NUMBER DO NOT MATCH" in t
    assert "your travel booking" in t
