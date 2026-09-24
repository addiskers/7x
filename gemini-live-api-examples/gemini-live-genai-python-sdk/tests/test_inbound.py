"""Inbound call-back: caller lookup + context triggers, answer-webhook stash,
bridge per-call trigger, and RSVP reconciliation back onto the campaign."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import directory
import pytest

import inbound_context
import store
from plivo_handler import PlivoMediaBridge
from recorder import CallRecorder

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)   # 17:30 IST


def _ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def _seed(eo_db, phone, name="Amman Kumar", status="live", campaign="July Event", **cc_over):
    """One campaign + one contact; returns (campaign_id, cc_id)."""
    cid = eo_db.create_campaign(campaign, "2026-07-01T00:00:00+00:00", 1, 4, 3, 1, status=status)
    eo_db.add_campaign_contacts(cid, [{"id": None, "phone": phone, "name": name}])
    cc = eo_db._one(
        "SELECT * FROM campaign_contacts WHERE campaign_id = ? ORDER BY id DESC LIMIT 1", (cid,))
    if cc_over:
        eo_db.cc_update(cc["id"], **cc_over)
    return cid, cc["id"]


# _when_phrase: all date math stays in Python, bucketed in IST

def test_when_phrase_buckets():
    wp = inbound_context._when_phrase
    assert wp(_ago(minutes=30), now=NOW) == "a little while ago"
    assert wp(_ago(hours=6), now=NOW) == "earlier today"      # 11:30 IST, same IST day
    assert wp(_ago(hours=20), now=NOW) == "yesterday"          # 21:30 IST the day before
    assert wp(_ago(days=4), now=NOW) == "a few days ago"
    assert wp("not-a-date", now=NOW) == "recently"
    assert wp(None, now=NOW) == "recently"


# build(): trigger variants — never invented context

def test_build_unknown_number(fresh_eo_db, monkeypatch):
    fresh_eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    out = inbound_context.build("+917000000001")
    assert out["trigger"] == inbound_context.UNKNOWN_TRIGGER
    assert out["name"] == "" and out["campaign_id"] is None
    assert out["phone"] == "+917000000001"


def test_build_missed_no_answer_named(fresh_eo_db, monkeypatch):
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    cid, _ = _seed(eo_db, "+919824018000",
                   attempts=1, last_attempt_at=_ago(minutes=40), last_error="no answer")
    out = inbound_context.build("+919824018000", now=NOW)
    assert out["campaign_id"] == cid
    assert out["name"] == "Amman"                              # first token of the cc name
    t = out["trigger"]
    assert "Amman is calling US back" in t
    assert "Thanks for calling back" in t
    assert "a little while ago" in t
    assert "you were unavailable" in t


def test_build_voicemail_variant(fresh_eo_db, monkeypatch):
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    _seed(eo_db, "+919824018000",
          attempts=1, last_attempt_at=_ago(hours=6), rsvp_outcome="voicemail")
    t = inbound_context.build("+919824018000", now=NOW)["trigger"]
    assert "voicemail" in t
    assert "earlier today" in t


@pytest.mark.parametrize("outcome", ["yes", "acknowledged", "confirmed"])
def test_build_already_settled_outcome(fresh_eo_db, monkeypatch, outcome):
    """Every agent's positive outcome means "we already spoke to them" — the agent must
    not start its message over from scratch."""
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    _seed(eo_db, "+919824018000", attempts=1, rsvp_outcome=outcome)
    t = inbound_context.build("+919824018000", now=NOW)["trigger"]
    assert "ALREADY had this conversation" in t
    assert "do NOT re-record" in t
    assert "calling US back" not in t                          # no missed-call story


def test_build_not_yet_called_never_claims_a_missed_call(fresh_eo_db, monkeypatch):
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    _seed(eo_db, "+919824018000")                              # attempts=0, no outcome
    t = inbound_context.build("+919824018000", now=NOW)["trigger"]
    assert "NOT called them yet" in t
    assert "Thanks for calling back" not in t


def test_build_normalizes_bare_plivo_from(fresh_eo_db, monkeypatch):
    """Plivo may deliver `From` without '+' — a 10/12-digit form must still match."""
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    cid, _ = _seed(eo_db, "+919824018000", attempts=1, last_attempt_at=_ago(minutes=10))
    for raw in ("919824018000", "9824018000"):
        out = inbound_context.build(raw, now=NOW)
        assert out["phone"] == "+919824018000"
        assert out["campaign_id"] == cid


def test_build_prefers_active_campaign_over_more_recent_completed(fresh_eo_db, monkeypatch):
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    live_cid, _ = _seed(eo_db, "+919824018000", status="live",
                        attempts=1, last_attempt_at=_ago(minutes=30))
    done_cid, _ = _seed(eo_db, "+919824018000", status="completed", campaign="Old Event",
                        attempts=2, rsvp_outcome="no")          # newer row, inactive campaign
    out = inbound_context.build("+919824018000", now=NOW)
    assert out["campaign_id"] == live_cid != done_cid


def test_build_directory_member_without_campaign(fresh_eo_db, monkeypatch):
    fresh_eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {"+919824018000": "Pratik"})
    t = inbound_context.build("+919824018000", now=NOW)["trigger"]
    assert "Pratik" in t
    assert "Do NOT claim we called them" in t


# /plivo/answer webhook: inbound detection + meta stash (outbound path untouched)

def test_answer_webhook_inbound_stashes_direction_and_trigger(fresh_eo_db, monkeypatch):
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setattr(directory, "_MAP", {})
    cid, _ = _seed(eo_db, "+919824018000",
                   attempts=1, last_attempt_at=_ago(minutes=20), last_error="no answer")

    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.get("/plivo/answer", params={
        "Direction": "inbound", "From": "919824018000",
        "To": "912212345678", "CallUUID": "cu-inbound-test"})
    try:
        assert r.status_code == 200
        assert "<Stream" in r.text and "bidirectional" in r.text
        meta = main._pending_call_meta["cu-inbound-test"]
        assert meta["direction"] == "inbound"
        assert meta["caller"] == "+919824018000"                # normalized
        assert meta["name"] == "Amman"
        assert meta["campaign_id"] == str(cid)
        assert meta["trigger"].startswith("[INBOUND")
    finally:
        main._pending_call_meta.pop("cu-inbound-test", None)


def test_answer_webhook_outbound_leg_keeps_defaults(monkeypatch):
    import main
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.get("/plivo/answer", params={
        "caller": "+919824018000", "Direction": "outbound",
        "CallUUID": "cu-outbound-test"})
    try:
        assert r.status_code == 200
        meta = main._pending_call_meta["cu-outbound-test"]
        assert meta["direction"] == ""
        assert meta["trigger"] == ""
    finally:
        main._pending_call_meta.pop("cu-outbound-test", None)


# Bridge: a per-call trigger replaces both default openings; '' keeps them

class _WS:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_text(self):
        if not self._incoming:
            raise Exception((1000,))
        return self._incoming.pop(0)


def _start_msg():
    return json.dumps({"event": "start", "start": {"streamId": "s1", "callId": "cu1"}})


async def _run_start(resolve_trigger):
    b = PlivoMediaBridge(
        _WS([_start_msg()]), gemini_client=None, text_trigger="[default]",
        resolve_identity=lambda cid, c, n: ("+919824018000", "Pratik"),
        resolve_trigger=resolve_trigger)
    b._rec_on = False
    await b.handle_plivo_messages()
    if b._connect_tone_task:
        b._connect_tone_task.cancel()
        await asyncio.gather(b._connect_tone_task, return_exceptions=True)
    return b.text_input_queue.get_nowait()


def test_bridge_uses_per_call_trigger_over_named_opening():
    got = asyncio.run(_run_start(lambda cid: "[INBOUND CALL-BACK: test trigger]"))
    assert got == "[INBOUND CALL-BACK: test trigger]"


def test_bridge_empty_trigger_falls_back_to_named_opening():
    got = asyncio.run(_run_start(lambda cid: ""))
    assert "Am I speaking to Pratik?" in got


# Reconciliation: inbound RSVP → campaign contact settled + stale callbacks cancelled

def _inbound_recorder(**over):
    r = CallRecorder(model="test")
    r.call = {"id": "in1", "call_sid": "sid-in1", "source": "plivo_inbound",
              "caller": "+919824018000", "campaign_id": 7, "generation": 0,
              "origin_call_id": None, "booking_created": False,
              "transcript": [], "tool_calls": []}
    r.call.update(over)
    return r


def _run_reconcile(monkeypatch, rec):
    import eo_db
    cc_calls, cancels = [], []
    monkeypatch.setattr(eo_db, "cc_set_outcome_by_phone",
                        lambda cid, phone, oc, **kw: cc_calls.append((cid, phone, oc, kw)))

    async def fake_cancel(phone, exclude_call_id=None):
        cancels.append((phone, exclude_call_id))
        return 0

    monkeypatch.setattr(store, "cancel_pending_callbacks_for_phone", fake_cancel)
    asyncio.run(rec._reconcile_inbound())
    return cc_calls, cancels


def test_reconcile_inbound_positive_outcome_marks_contact_done(monkeypatch):
    cc_calls, cancels = _run_reconcile(
        monkeypatch, _inbound_recorder(rsvp_outcome_status="acknowledged"))
    assert cc_calls == [(7, "+919824018000", "acknowledged",
                         {"mark_done": True, "remark": None})]
    assert cancels == [("+919824018000", "in1")]


def test_reconcile_inbound_callback_keeps_retries_alive(monkeypatch):
    cc_calls, cancels = _run_reconcile(
        monkeypatch, _inbound_recorder(rsvp_outcome_status="callback"))
    assert cc_calls[0][3]["mark_done"] is False
    assert cancels == [("+919824018000", "in1")]                # old callbacks still cancelled


def test_reconcile_skips_outbound_and_rsvpless_calls(monkeypatch):
    cc_calls, cancels = _run_reconcile(
        monkeypatch, _inbound_recorder(source="plivo", rsvp_outcome_status="yes"))
    assert cc_calls == [] and cancels == []
    cc_calls, cancels = _run_reconcile(monkeypatch, _inbound_recorder())  # no RSVP captured
    assert cc_calls == [] and cancels == []


def test_reconcile_without_campaign_still_cancels_callbacks(monkeypatch):
    cc_calls, cancels = _run_reconcile(
        monkeypatch, _inbound_recorder(rsvp_outcome_status="yes", campaign_id=None))
    assert cc_calls == []
    assert cancels == [("+919824018000", "in1")]


# store.cancel_pending_callbacks_for_phone — digit-normalized, excludes the live call

def test_cancel_pending_callbacks_for_phone():
    async def run():
        mk = lambda i, phone: {"id": i, "callback": {"status": "pending", "to": phone},
                               "transcript": [], "tool_calls": []}
        await store.save_call(mk("cbt-match", "+919888800001"))
        await store.save_call(mk("cbt-other", "+919888800002"))
        await store.save_call(mk("cbt-self", "+919888800001"))     # the live call's own block
        n = await store.cancel_pending_callbacks_for_phone(
            "+919888800001", exclude_call_id="cbt-self")
        a = await store.load_call("cbt-match")
        b = await store.load_call("cbt-other")
        c = await store.load_call("cbt-self")
        return n, a["callback"]["status"], b["callback"]["status"], c["callback"]["status"]

    n, a, b, c = asyncio.run(run())
    assert n == 1
    assert a == "cancelled" and b == "pending" and c == "pending"


# ------------------------------------------------- cold inbound calls (no campaign history)
# A guest dials one of our numbers. Before this the call fell to Event Reminder with a
# generic "someone called us" opening, the wedding was never found (so the agent knew no
# functions), and an unknown caller got "Their first name is. … speaking with ?".
def _wedding_world(eo_db):
    import eo_auth
    eo_db.init()
    eo_auth.seed_admin()
    owner = [u for u in eo_db.list_users() if u["role"] == "eo_admin"][0]["id"]
    wid = eo_db.create_wedding("Ved & Riya", created_by=owner,
                               hospitality_team="Ved and Riya's Hospitality Team")
    eo_db.create_event(wid, "Sufi Night", event_date="2030-01-01", start_time="19:00",
                       venue="Great Park")
    eo_db.bulk_upsert_contacts([("Heeren Agrawal", "+917043020542", "valid", {})],
                               created_by=owner, wedding_id=wid)
    return wid


def _answer(monkeypatch, frm):
    import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(directory, "_MAP", {})
    main.invalidate_ctx_cache()
    r = TestClient(main.app).get("/plivo/answer", params={
        "Direction": "inbound", "From": frm, "To": "918031704911", "CallUUID": "cu-" + frm})
    assert r.status_code == 200
    return main._pending_call_meta.pop("cu-" + frm)


@pytest.mark.parametrize("frm", ["917043020542", "+917043020542", "7043020542",
                                 "07043020542", "0917043020542"])
def test_a_guest_calling_in_gets_the_schedule_agent_by_name_in_every_number_format(
        fresh_eo_db, monkeypatch, frm):
    _wedding_world(fresh_eo_db)
    meta = _answer(monkeypatch, frm)
    ctx = meta["ctx"]
    assert meta["caller"] == "+917043020542"
    assert meta["name"] == "Heeren"
    assert ctx["agent"]["slug"] == "wedding_schedule"
    assert "Heeren" in meta["trigger"] and "speaking with Heeren" in meta["trigger"]
    assert "Sufi Night" in ctx["system_instruction"]              # the wedding was found
    assert "Ved and Riya's Hospitality Team" in ctx["system_instruction"]


def test_an_unknown_caller_gets_the_schedule_with_a_nameless_opening(fresh_eo_db, monkeypatch):
    _wedding_world(fresh_eo_db)
    meta = _answer(monkeypatch, "919999900000")
    ctx = meta["ctx"]
    assert meta["name"] == ""
    assert ctx["agent"]["slug"] == "wedding_schedule"
    assert "first name is." not in meta["trigger"]
    assert "never ask 'am I speaking with" in meta["trigger"]
    assert "Sufi Night" in ctx["system_instruction"]              # the sole active wedding


def test_a_call_back_to_a_campaign_keeps_its_history_aware_opening(fresh_eo_db, monkeypatch):
    """The campaign path is untouched: a guest we rang and missed still hears
    "we tried calling you" from the campaign's own agent."""
    eo_db = fresh_eo_db
    eo_db.init()
    cid, _ = _seed(eo_db, "+919824018000", attempts=1, last_attempt_at=_ago(minutes=20),
                   last_error="no answer")
    meta = _answer(monkeypatch, "919824018000")
    assert meta["campaign_id"] == str(cid)
    assert meta["trigger"].startswith("[INBOUND")


def test_inbound_agent_can_be_chosen_by_env_and_falls_back_when_switched_off(
        fresh_eo_db, monkeypatch):
    import main
    eo_db = fresh_eo_db
    eo_db.init()
    monkeypatch.setenv("EO_INBOUND_AGENT_SLUG", "logistics_concierge")
    assert main._inbound_agent_id() == str(eo_db.get_agent_by_slug("logistics_concierge")["id"])
    eo_db.update_agent(eo_db.get_agent_by_slug("logistics_concierge")["id"], active=0)
    assert main._inbound_agent_id() == ""
