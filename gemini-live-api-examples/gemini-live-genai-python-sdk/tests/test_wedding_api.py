"""The 7x API surface: weddings/events/agents CRUD, agent preview + test tokens, and
campaign creation with audience preselect, the fatigue warning and the active cap."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import eo_auth
import eo_db
import main


def _soon(minutes=5):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture()
def api(fresh_eo_db, monkeypatch):
    """A logged-in client over a fresh DB, with a wedding, two events and three guests."""
    eo_db = fresh_eo_db
    eo_db.init()
    eo_auth.seed_admin()
    main.invalidate_ctx_cache()
    client = TestClient(main.app)
    tok = client.post("/api/eo/login",
                      json={"username": "eoadmin", "password": "eoadmin123"}).json()["token"]
    headers = {"Authorization": f"Bearer {tok}"}

    wedding = client.post("/api/eo/weddings", headers=headers, json={
        "name": "Anant & Manya", "groom_name": "Anant", "bride_name": "Manya",
        "hospitality_team": "the Wedding Hospitality team",
        "placard_text": "Kapoor & Chopra Family Welcomes You",
        "contact_phone": "+91 98123 45678", "contact_name": "Rohit"}).json()
    saanth = client.post("/api/eo/events", headers=headers, json={
        "wedding_id": wedding["id"], "name": "Saanth Ritual", "event_date": "2026-09-21",
        "start_time": "10:30", "venue": "The Imperial Ballroom", "audience": "groom"}).json()
    ghazal = client.post("/api/eo/events", headers=headers, json={
        "wedding_id": wedding["id"], "name": "Ghazal Night", "event_date": "2026-09-19",
        "start_time": "19:00", "venue": "Infinity Terrace", "audience": "all"}).json()
    owner = eo_db.get_user_by_username("eoadmin")["id"]
    eo_db.bulk_upsert_contacts([
        ("Rajesh Kumar", "+919876543210", "valid", {"side": "groom"}),
        ("Priya Sharma", "+919876543211", "valid", {"side": "bride"}),
        ("Amit Shah", "+919876543212", "valid", {"side": "groom"}),
    ], created_by=owner, wedding_id=wedding["id"])
    return dict(client=client, h=headers, eo_db=eo_db, wedding=wedding,
                saanth=saanth, ghazal=ghazal, owner=owner,
                reminder=eo_db.get_agent_by_slug("event_reminder"),
                logistics=eo_db.get_agent_by_slug("logistics_concierge"))


# ------------------------------------------------------------------------------ weddings
def test_wedding_crud(api):
    c, h = api["client"], api["h"]
    wid = api["wedding"]["id"]
    assert c.get("/api/eo/weddings", headers=h).json()["total"] == 1
    detail = c.get(f"/api/eo/weddings/{wid}", headers=h).json()
    assert detail["placard_text"] == "Kapoor & Chopra Family Welcomes You"
    assert len(detail["events"]) == 2
    assert detail["guest_count"] == 3

    c.patch(f"/api/eo/weddings/{wid}", headers=h, json={"city": "Udaipur"})
    assert c.get(f"/api/eo/weddings/{wid}", headers=h).json()["city"] == "Udaipur"


def test_a_wedding_with_a_live_campaign_cannot_be_deleted(api):
    """Deleting cascades to events, agents and guests — that would strand a running dialer."""
    c, h = api["client"], api["h"]
    c.post("/api/eo/campaigns", headers=h, json={
        "name": "Saanth reminder", "start_at": _soon(), "agent_id": api["reminder"]["id"],
        "event_id": api["saanth"]["id"], "wedding_id": api["wedding"]["id"]})
    r = c.delete(f"/api/eo/weddings/{api['wedding']['id']}", headers=h)
    assert r.status_code == 409
    assert "Cancel it first" in r.json()["detail"]


# -------------------------------------------------------------------------------- events
def test_events_come_back_in_running_order(api):
    items = api["client"].get(f"/api/eo/weddings/{api['wedding']['id']}/events",
                              headers=api["h"]).json()["items"]
    assert [e["name"] for e in items] == ["Ghazal Night", "Saanth Ritual"]   # by date


def test_audience_is_validated(api):
    r = api["client"].post("/api/eo/events", headers=api["h"], json={
        "wedding_id": api["wedding"]["id"], "name": "X", "audience": "cousins"})
    assert r.status_code == 400
    assert "audience must be one of" in r.json()["detail"]


# -------------------------------------------------------------------------------- agents
def test_agents_list_ships_the_placeholder_vocabulary(api):
    """The UI palette and the save-time validator must read one source of truth."""
    import prompt_render
    data = api["client"].get("/api/eo/agents", headers=api["h"]).json()
    assert {a["slug"] for a in data["items"]} == {"event_reminder", "logistics_concierge"}
    assert set(data["placeholders"]) == set(prompt_render.KNOWN_PLACEHOLDERS)


def test_an_unknown_placeholder_is_rejected_at_save_time(api):
    """A typo is an authoring bug — catching it now beats the agent speaking a gap."""
    r = api["client"].post("/api/eo/agents", headers=api["h"], json={
        "name": "Typo", "wedding_id": api["wedding"]["id"],
        "prompt_template": "Reminder about {even_name} at {venue}."})
    assert r.status_code == 400
    assert "{even_name}" in r.json()["detail"]


def test_a_prompt_naming_a_nonexistent_tool_is_rejected(api):
    r = api["client"].post("/api/eo/agents", headers=api["h"], json={
        "name": "Stale", "wedding_id": api["wedding"]["id"],
        "prompt_template": "When done, call record_rsvp with the outcome."})
    assert r.status_code == 400
    assert "record_outcome" in r.json()["detail"]


def test_shipped_templates_cannot_be_deleted_only_duplicated(api):
    c, h = api["client"], api["h"]
    r = c.delete(f"/api/eo/agents/{api['reminder']['id']}", headers=h)
    assert r.status_code == 400

    dup = c.post(f"/api/eo/agents/{api['reminder']['id']}/duplicate", headers=h,
                 json={"wedding_id": api["wedding"]["id"], "name": "Our Reminder"}).json()
    assert dup["wedding_id"] == api["wedding"]["id"]
    assert dup["prompt_template"] == api["reminder"]["prompt_template"]
    assert c.delete(f"/api/eo/agents/{dup['id']}", headers=h).status_code == 200


def test_editing_an_agent_changes_the_next_call(api):
    c, h = api["client"], api["h"]
    dup = c.post(f"/api/eo/agents/{api['reminder']['id']}/duplicate", headers=h,
                 json={"wedding_id": api["wedding"]["id"], "name": "Ours"}).json()
    c.patch(f"/api/eo/agents/{dup['id']}", headers=h,
            json={"prompt_template": "Reminder about {event_name}."})
    ctx = main._resolve_call_context(agent_id=dup["id"], event_id=api["saanth"]["id"],
                                     wedding_id=api["wedding"]["id"])
    assert ctx["system_instruction"] == "Reminder about Saanth Ritual."


# ------------------------------------------------------------------------------- preview
def test_preview_returns_the_exact_text_and_flags_gaps(api):
    r = api["client"].post(f"/api/eo/agents/{api['reminder']['id']}/preview", headers=api["h"],
                           json={"event_id": api["saanth"]["id"],
                                 "wedding_id": api["wedding"]["id"]}).json()
    assert "Saanth Ritual" in r["system_instruction"]
    assert "half past ten in the morning" in r["system_instruction"]
    assert "10:30" not in r["system_instruction"]
    assert "dress_code" in r["missing"]              # not set on this event
    assert [t["name"] for t in r["tools"]] == ["record_outcome", "end_call"]


def test_a_test_token_is_short_lived_and_names_one_agent(api):
    """/ws is unauthenticated, so it must never take an agent id from the query string."""
    r = api["client"].post(f"/api/eo/agents/{api['reminder']['id']}/test-token",
                           headers=api["h"], json={"event_id": api["saanth"]["id"]}).json()
    claims = eo_auth.verify_test_token(r["token"])
    assert claims["agent_id"] == api["reminder"]["id"]
    assert claims["event_id"] == api["saanth"]["id"]
    assert r["ttl_seconds"] <= 3600
    # a session token must not double as a test token, or vice versa
    assert eo_auth.verify_token(r["token"]) is None


# ----------------------------------------------------------------------------- campaigns
def test_preflight_preselects_guests_by_the_events_audience(api):
    c, h = api["client"], api["h"]
    base = {"name": "R", "start_at": _soon(), "agent_id": api["reminder"]["id"],
            "wedding_id": api["wedding"]["id"]}
    groom = c.post("/api/eo/campaigns/preflight", headers=h,
                   json={**base, "event_id": api["saanth"]["id"]}).json()
    assert groom["audience"]["audience"] == "groom"
    assert {g["name"] for g in groom["audience"]["guests"]} == {"Rajesh Kumar", "Amit Shah"}

    everyone = c.post("/api/eo/campaigns/preflight", headers=h,
                      json={**base, "event_id": api["ghazal"]["id"]}).json()
    assert everyone["audience"]["matched"] == 3


def test_an_explicit_guest_selection_overrides_the_audience(api):
    """The preselect is editable — an operator may add or drop an individual."""
    r = api["client"].post("/api/eo/campaigns/preflight", headers=api["h"], json={
        "name": "R", "start_at": _soon(), "agent_id": api["reminder"]["id"],
        "event_id": api["saanth"]["id"], "wedding_id": api["wedding"]["id"],
        "contact_ids": [1]}).json()
    assert r["audience"]["matched"] == 1


def test_campaign_records_its_agent_and_event(api):
    r = api["client"].post("/api/eo/campaigns", headers=api["h"], json={
        "name": "Saanth reminder", "start_at": _soon(),
        "agent_id": api["reminder"]["id"], "event_id": api["saanth"]["id"],
        "wedding_id": api["wedding"]["id"]})
    assert r.status_code == 201
    c = r.json()
    assert (c["agent_id"], c["event_id"], c["wedding_id"]) == (
        api["reminder"]["id"], api["saanth"]["id"], api["wedding"]["id"])
    assert c["contact_count"] == 2                   # groom side only


def test_a_reminder_agent_requires_an_event_a_logistics_agent_does_not(api):
    c, h = api["client"], api["h"]
    bad = c.post("/api/eo/campaigns", headers=h, json={
        "name": "No event", "start_at": _soon(), "agent_id": api["reminder"]["id"],
        "wedding_id": api["wedding"]["id"], "contact_ids": [1]})
    assert bad.status_code == 400
    assert "pick an event" in bad.json()["detail"].lower()

    ok = c.post("/api/eo/campaigns", headers=h, json={
        "name": "Airport pickup", "start_at": _soon(), "agent_id": api["logistics"]["id"],
        "wedding_id": api["wedding"]["id"], "contact_ids": [1, 2, 3]})
    assert ok.status_code == 201
    assert ok.json()["event_id"] is None


def test_fatigue_warns_then_proceeds_on_acknowledgement(api):
    """The client's own schedule needs six calls to some guests on the wedding day, so
    this is a warning the operator must see — never a block."""
    c, h, db = api["client"], api["h"], api["eo_db"]
    base = {"name": "First", "start_at": _soon(), "agent_id": api["reminder"]["id"],
            "event_id": api["saanth"]["id"], "wedding_id": api["wedding"]["id"]}
    first = c.post("/api/eo/campaigns", headers=h, json=base).json()
    now = datetime.now(timezone.utc).isoformat()
    for cc in db.list_campaign_contacts(first["id"])["items"]:
        db.cc_update(cc["id"], last_attempt_at=now)

    pf = c.post("/api/eo/campaigns/preflight", headers=h, json={**base, "name": "Second"}).json()
    assert pf["fatigue"]["count"] == 2

    blocked = c.post("/api/eo/campaigns", headers=h, json={**base, "name": "Second"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "fatigue_warning"

    ok = c.post("/api/eo/campaigns", headers=h,
                json={**base, "name": "Second", "acknowledge_fatigue": True})
    assert ok.status_code == 201


def test_several_campaigns_may_run_at_once_up_to_the_cap(api, monkeypatch):
    """Six reminder batches on the wedding afternoon is the normal case, not an error."""
    monkeypatch.setenv("EO_MAX_ACTIVE_CAMPAIGNS", "3")
    c, h = api["client"], api["h"]
    base = {"start_at": _soon(), "agent_id": api["reminder"]["id"],
            "event_id": api["saanth"]["id"], "wedding_id": api["wedding"]["id"],
            "acknowledge_fatigue": True}
    for i in range(3):
        assert c.post("/api/eo/campaigns", headers=h,
                      json={**base, "name": f"C{i}"}).status_code == 201
    over = c.post("/api/eo/campaigns", headers=h, json={**base, "name": "C4"})
    assert over.status_code == 409
    assert "limit 3" in over.json()["detail"]


# ------------------------------------------------------------------------------ scoping
def test_an_agent_user_cannot_see_another_owners_wedding(api):
    db = api["eo_db"]
    other = db.create_user("agent2", "Other", *eo_auth.hash_password("pw12345678"),
                           role="eo_agent")
    client = TestClient(main.app)
    tok = client.post("/api/eo/login",
                      json={"username": "agent2", "password": "pw12345678"}).json()["token"]
    h2 = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/eo/weddings", headers=h2).json()["total"] == 0
    # a direct id probe returns 404, not 403 — it must not confirm the row exists
    assert client.get(f"/api/eo/weddings/{api['wedding']['id']}", headers=h2).status_code == 404
    assert client.patch(f"/api/eo/weddings/{api['wedding']['id']}", headers=h2,
                        json={"city": "X"}).status_code == 404
