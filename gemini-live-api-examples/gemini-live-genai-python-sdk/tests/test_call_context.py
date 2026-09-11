"""The per-call context pipeline: which script a call speaks, and how that survives
every failure mode. dialer -> answer_url -> /plivo/answer -> _resolve_call_context ->
prewarm + trigger."""

import asyncio

import pytest

import dialer
import eo_db
import main


@pytest.fixture()
def wedding_world(fresh_eo_db, monkeypatch):
    """A wedding with two functions, one groom-side guest, and a reminder campaign."""
    eo_db = fresh_eo_db
    eo_db.init()
    main.invalidate_ctx_cache()          # the cache is process-wide; don't leak across tests
    admin = eo_db.create_user("a", "A", "h", "s", role="eo_admin")
    wid = eo_db.create_wedding(
        "Anant & Manya", created_by=admin, groom_name="Anant", bride_name="Manya",
        hospitality_team="the Wedding Hospitality team",
        placard_text="Kapoor & Chopra Family Welcomes You",
        contact_phone="+91 98123 45678", contact_name="Rohit")
    ghazal = eo_db.create_event(wid, "Ghazal Night", event_date="2026-09-19",
                                start_time="19:00", venue="Infinity Terrace", audience="all")
    saanth = eo_db.create_event(wid, "Saanth Ritual", event_date="2026-09-21",
                                start_time="10:30", venue="The Imperial Ballroom",
                                audience="groom")
    eo_db.bulk_upsert_contacts([("Rajesh Kumar", "+919876543210", "valid", {
        "side": "groom", "transport_mode": "flight", "transport_number": "AI 456",
        "hotel": "Fairmont Udaipur"})], created_by=admin, wedding_id=wid)
    guest = eo_db.guests_for_audience(wid, "groom", created_by=admin)[0]
    agent = eo_db.get_agent_by_slug("event_reminder")
    camp = eo_db.create_campaign("Saanth reminder", "2026-09-21T02:30:00+00:00", admin,
                                 4, 3, 1, wedding_id=wid, event_id=saanth,
                                 agent_id=agent["id"])
    return dict(eo_db=eo_db, admin=admin, wedding=wid, ghazal=ghazal, saanth=saanth,
                guest=guest, agent=agent, campaign=camp)


# ------------------------------------------------------------------ dialer -> answer_url
def test_context_ids_ride_the_answer_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("PLIVO_AUTH_ID", "x")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "y")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    captured = {}
    monkeypatch.setattr(dialer, "_place_call_sync",
                        lambda to, url: captured.setdefault("url", url) or "UUID")
    asyncio.run(dialer.place_call("+919876543210", name="Rajesh", campaign_id=7,
                                  agent_id=1, event_id=2, guest_id=3, wedding_id=4))
    url = captured["url"]
    assert "agent=1" in url and "event=2" in url and "guest=3" in url and "wedding=4" in url


def test_non_numeric_context_ids_are_dropped_not_injected(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("PLIVO_AUTH_ID", "x")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "y")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    captured = {}
    monkeypatch.setattr(dialer, "_place_call_sync",
                        lambda to, url: captured.setdefault("url", url) or "UUID")
    asyncio.run(dialer.place_call("+919876543210", agent_id="1&evil=1", event_id=None))
    assert "evil" not in captured["url"]
    assert "agent=" not in captured["url"]


# -------------------------------------------------------------------------- resolution
def test_resolves_the_campaigns_agent_event_and_guest(wedding_world):
    w = wedding_world
    ctx = main._resolve_call_context(
        agent_id=w["agent"]["id"], event_id=w["saanth"], guest_id=w["guest"]["id"],
        wedding_id=w["wedding"], campaign_id=w["campaign"], caller="+919876543210")
    assert ctx["agent"]["slug"] == "event_reminder"
    assert ctx["event"]["name"] == "Saanth Ritual"
    assert ctx["guest"]["name"] == "Rajesh Kumar"
    assert [t["name"] for t in ctx["tools"]] == ["record_outcome", "end_call"]


def test_the_rendered_prompt_carries_this_events_facts(wedding_world):
    w = wedding_world
    ctx = main._resolve_call_context(agent_id=w["agent"]["id"], event_id=w["saanth"],
                                     guest_id=w["guest"]["id"], wedding_id=w["wedding"])
    si = ctx["system_instruction"]
    assert "Saanth Ritual" in si
    assert "The Imperial Ballroom" in si
    assert "half past ten in the morning" in si      # spoken, never digits
    assert "10:30" not in si
    assert "the groom's side" in si


def test_two_events_produce_two_different_prompts(wedding_world):
    """The whole point of the rebuild: one agent, many events."""
    w = wedding_world
    saanth = main._resolve_call_context(agent_id=w["agent"]["id"], event_id=w["saanth"],
                                        wedding_id=w["wedding"])["system_instruction"]
    ghazal = main._resolve_call_context(agent_id=w["agent"]["id"], event_id=w["ghazal"],
                                        wedding_id=w["wedding"])["system_instruction"]
    assert "Saanth Ritual" in saanth and "Ghazal Night" not in saanth
    assert "Ghazal Night" in ghazal and "Saanth Ritual" not in ghazal
    assert "seven in the evening" in ghazal


def test_the_campaign_row_backfills_missing_params(wedding_world):
    """The query params are the fast path; the campaign is the authority."""
    w = wedding_world
    ctx = main._resolve_call_context(campaign_id=w["campaign"], caller="+919876543210")
    assert ctx["agent"]["slug"] == "event_reminder"
    assert ctx["event"]["name"] == "Saanth Ritual"


def test_a_guest_with_no_id_is_found_by_phone(wedding_world):
    """Inbound calls and /call-me tests arrive with only a number."""
    w = wedding_world
    ctx = main._resolve_call_context(agent_id=w["agent"]["id"], wedding_id=w["wedding"],
                                     caller="+919876543210")
    assert ctx["guest"]["name"] == "Rajesh Kumar"


def test_the_trigger_names_the_guest_so_the_agent_starts_speaking(wedding_world):
    """The Live API emits no audio until it receives a turn — the trigger IS that turn."""
    w = wedding_world
    ctx = main._resolve_call_context(agent_id=w["agent"]["id"], event_id=w["saanth"],
                                     guest_id=w["guest"]["id"], wedding_id=w["wedding"])
    assert ctx["trigger"]
    assert "Rajesh" in ctx["trigger"]


# --------------------------------------------------------------------------- fallbacks
def test_no_ids_at_all_still_connects(wedding_world):
    ctx = main._resolve_call_context()
    assert ctx["agent"]["slug"] == "event_reminder"
    assert ctx["system_instruction"]


def test_an_unknown_agent_id_falls_back_rather_than_dropping_the_call(wedding_world):
    w = wedding_world
    ctx = main._resolve_call_context(agent_id=999999, event_id=w["saanth"])
    assert ctx["agent"]["slug"] == "event_reminder"
    assert ctx["system_instruction"]


def test_a_db_failure_never_raises(wedding_world, monkeypatch):
    """A half-personalised call beats a dropped one on the wedding morning."""
    def boom(*a, **kw):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(eo_db, "get_agent", boom)
    monkeypatch.setattr(eo_db, "fallback_agent", boom)
    main.invalidate_ctx_cache()
    ctx = main._resolve_call_context(agent_id=1, event_id=2)
    assert ctx["agent"] is None                     # GeminiLive uses its identity-free prompt
    assert ctx["system_instruction"] is None


def test_a_logistics_campaign_needs_no_event(wedding_world):
    """Pickup/drop calls are keyed to the guest's travel, not to any one function."""
    w = wedding_world
    logistics = eo_db.get_agent_by_slug("logistics_concierge")
    ctx = main._resolve_call_context(agent_id=logistics["id"], event_id=None,
                                     guest_id=w["guest"]["id"], wedding_id=w["wedding"])
    assert ctx["event"] is None
    assert "AI 456" in ctx["system_instruction"]                 # the guest's flight
    assert "Kapoor & Chopra Family Welcomes You" in ctx["system_instruction"]


# ------------------------------------------------------------------------ prewarm safety
def test_a_prewarmed_session_is_never_handed_to_a_different_call():
    """The session carries one guest's name, hotel and flight number; handing it to
    another call would read those out to the wrong person."""
    main._prewarm_sessions["call-A"] = {"handle": object(), "at": 0.0, "call_uuid": "call-A"}
    assert main._claim_prewarm("call-A") is not None

    # A session mis-keyed for any reason must be discarded, not reused.
    sentinel = object()
    main._prewarm_sessions["call-B"] = {"handle": sentinel, "at": 0.0, "call_uuid": "call-OTHER"}
    assert main._claim_prewarm("call-B") is None
    main._prewarm_sessions.clear()


# --------------------------------------------------------------------------- cache
def test_editing_an_agent_takes_effect_after_invalidation(wedding_world):
    w = wedding_world
    first = main._resolve_call_context(agent_id=w["agent"]["id"], event_id=w["saanth"],
                                       wedding_id=w["wedding"])["system_instruction"]
    eo_db.update_agent(w["agent"]["id"], prompt_template="BRAND NEW PROMPT")
    main.invalidate_ctx_cache()
    second = main._resolve_call_context(agent_id=w["agent"]["id"], event_id=w["saanth"],
                                        wedding_id=w["wedding"])["system_instruction"]
    assert first != second
    assert second == "BRAND NEW PROMPT"
