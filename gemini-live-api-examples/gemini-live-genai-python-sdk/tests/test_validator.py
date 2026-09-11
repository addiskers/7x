"""main.handle_record_outcome — outcome normalization against a per-agent vocabulary,
incl. the machine-answer coercions that stop a voicemail from ever becoming a phantom
'user requested callback'."""

import json

import pytest

from main import handle_record_outcome


# A reminder agent: the default 7x vocabulary.
REMINDER = {
    "id": 1,
    "name": "Event Reminder",
    "outcome_enum": json.dumps([
        {"value": "acknowledged"}, {"value": "callback"},
        {"value": "not_reachable"}, {"value": "wrong_number"},
    ]),
}

# A logistics agent: different vocabulary, plus an extra capture field.
LOGISTICS = {
    "id": 2,
    "name": "Logistics Concierge",
    "outcome_enum": json.dumps([
        {"value": "confirmed"}, {"value": "details_changed"},
        {"value": "callback"}, {"value": "not_reachable"}, {"value": "wrong_number"},
    ]),
    "extra_fields": json.dumps([
        {"name": "corrected_transport_number", "type": "string"},
        {"name": "pickup_time_text", "type": "string"},
    ]),
}


def _status(agent=REMINDER, **kwargs):
    return handle_record_outcome(agent=agent, **kwargs)["outcome_status"]


def test_valid_statuses_pass_through():
    for s in ("acknowledged", "callback", "not_reachable", "wrong_number"):
        assert _status(outcome_status=s) == s


def test_each_agent_keeps_its_own_vocabulary():
    assert _status(LOGISTICS, outcome_status="confirmed") == "confirmed"
    assert _status(LOGISTICS, outcome_status="details_changed") == "details_changed"
    # "confirmed" is not in the reminder agent's enum — it must not pass through.
    assert _status(REMINDER, outcome_status="confirmed") != "confirmed"


def test_unknown_status_with_machine_wording_coerces_to_not_reachable():
    assert _status(outcome_status="voicemail_detected") == "not_reachable"
    assert _status(outcome_status="voice mail") == "not_reachable"
    assert _status(outcome_status="unknown", note="reached an answering machine") == "not_reachable"


def test_unknown_status_falls_back_to_callback_not_a_final_answer():
    # An unrecognised outcome must stay recoverable — never silently close the contact out.
    assert _status(outcome_status="not_interested") == "callback"
    assert _status(outcome_status="") == "callback"


def test_callback_with_machine_note_and_no_time_is_not_reachable():
    # the exact old-prompt shape that created the phantom next-day-10am callbacks
    assert _status(outcome_status="callback", note="voicemail — no live answer") == "not_reachable"


def test_callback_with_machine_note_but_a_real_time_stays_callback():
    assert _status(outcome_status="callback", note="voicemail mentioned earlier",
                   callback_time_text="tomorrow evening") == "callback"
    assert _status(outcome_status="callback", note="voicemail",
                   callback_time_iso="2026-09-21T18:00:00+05:30") == "callback"


def test_genuine_guest_callback_untouched():
    assert _status(outcome_status="callback", note="busy driving") == "callback"
    assert _status(outcome_status="callback") == "callback"


def test_extra_fields_are_captured_only_when_declared_and_non_empty():
    r = handle_record_outcome(agent=LOGISTICS, outcome_status="details_changed",
                              corrected_transport_number="6E 2134",
                              pickup_time_text="around 3 pm",
                              undeclared_field="ignored me")
    assert r["outcome_extra"] == {"corrected_transport_number": "6E 2134",
                                  "pickup_time_text": "around 3 pm"}
    # An agent that declares no extras captures none.
    assert handle_record_outcome(agent=REMINDER, outcome_status="acknowledged",
                                 corrected_transport_number="6E 2134")["outcome_extra"] == {}
    # Empty values are not captured — a blank is not an answer.
    assert handle_record_outcome(agent=LOGISTICS, outcome_status="confirmed",
                                 pickup_time_text="")["outcome_extra"] == {}


def test_do_not_contact_flag_tracks_the_status():
    dnc = {"id": 3, "outcome_enum": json.dumps([{"value": "acknowledged"},
                                                {"value": "do_not_contact"}])}
    assert handle_record_outcome(agent=dnc, outcome_status="do_not_contact")["do_not_contact"] is True
    assert handle_record_outcome(agent=dnc, outcome_status="acknowledged")["do_not_contact"] is False


@pytest.mark.parametrize("agent", [None, {}, {"outcome_enum": "{not json"}])
def test_a_missing_or_corrupt_agent_row_still_records(agent):
    # A corrupt outcome_enum must never break a live call — it falls back to the defaults.
    r = handle_record_outcome(agent=agent, outcome_status="acknowledged")
    assert r["success"] is True
    assert r["outcome_status"] == "acknowledged"


def test_result_is_silent_and_carries_the_single_closing_instruction():
    r = handle_record_outcome(agent=REMINDER, outcome_status="acknowledged")
    assert r["silent"] is True
    # The instruction is what guarantees exactly one spoken closing on older SDKs.
    assert "produce no audio this turn" in r["instruction"].lower()
