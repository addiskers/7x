"""Super admin: the service-provider tier above eo_admin, the go-live data reset, and the
subscription plan.

The reset is the most destructive thing in the product, so these tests pin what it keeps
as hard as what it removes.
"""

import pytest

import eo_api
import eo_auth
import subscription


# ------------------------------------------------------------------ the superadmin tier
def test_nobody_is_a_superadmin_until_the_env_names_them(monkeypatch):
    """A fresh install must not be able to wipe itself. Blank = nobody, including the
    seeded eo_admin."""
    monkeypatch.delenv("EO_SUPERADMIN_USERS", raising=False)
    assert eo_auth.superadmin_usernames() == set()
    assert not eo_auth.is_superadmin({"role": "eo_admin", "username": "eoadmin"})


def test_the_allow_list_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("EO_SUPERADMIN_USERS", " EOadmin , ops ")
    assert eo_auth.superadmin_usernames() == {"eoadmin", "ops"}
    assert eo_auth.is_superadmin({"role": "eo_admin", "username": "eoAdmin"})


def test_a_client_admin_named_in_the_env_is_still_not_a_superadmin(monkeypatch):
    """eo_agent is the client-facing role. Being listed must not promote it."""
    monkeypatch.setenv("EO_SUPERADMIN_USERS", "client")
    assert not eo_auth.is_superadmin({"role": "eo_agent", "username": "client"})


# --------------------------------------------------------------------- the data reset
def _world(db):
    """A wedding with an event, a guest, a campaign and a per-wedding agent copy."""
    eo_auth.seed_admin()
    owner = [u for u in db.list_users() if u["role"] == "eo_admin"][0]["id"]
    wid = db.create_wedding("W", created_by=owner)
    eid = db.create_event(wid, "Sufi Night", event_date="2026-09-25", start_time="19:00")
    db.bulk_upsert_contacts([("A", "+919000000001", "valid", {})],
                            created_by=owner, wedding_id=wid)
    guests = db.guests_for_audience(wid, "all", created_by=owner)
    agent = db.get_agent_by_slug("event_reminder")
    cid = db.create_campaign("C", "2026-09-25T11:00:00+00:00", owner, 4, 3, 1,
                             wedding_id=wid, event_id=eid, agent_id=agent["id"])
    db.add_campaign_contacts(cid, guests)
    db.create_agent("copy", agent["prompt_template"], wedding_id=wid,
                    created_by=owner, slug="copy-1")
    return owner, wid


def test_a_full_reset_keeps_users_and_the_shipped_agents(fresh_eo_db):
    """The two global templates ARE the product. A go-live reset must never take them,
    or every future campaign has no script."""
    db = fresh_eo_db
    db.init()
    _world(db)
    db.wipe_data(outbound=True, weddings=True)
    counts = db.data_counts()
    assert counts["weddings"] == 0 and counts["events"] == 0
    assert counts["contacts"] == 0 and counts["campaigns"] == 0
    slugs = sorted(a["slug"] for a in db.all_agents())
    assert slugs == ["event_reminder", "logistics_concierge"]
    assert all(a["wedding_id"] is None for a in db.all_agents())
    assert len(db.list_users()) >= 1


def test_wiping_weddings_alone_leaves_no_orphans(fresh_eo_db):
    """campaigns.wedding_id/event_id/agent_id and contacts.wedding_id are bare INTEGERs
    with NO foreign key, so SQLite will not clean them up — they would be left pointing at
    rows that no longer exist and would resurface in the UI as ghost campaigns."""
    db = fresh_eo_db
    db.init()
    _world(db)
    db.wipe_data(weddings=True)
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM campaigns WHERE wedding_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM campaign_contacts").fetchone()[0] == 0
    # the guest survives, but unlinked — and to 0, not NULL: UNIQUE(created_by,
    # wedding_id, phone) stops matching if a NULL creeps in, so re-import would duplicate.
    assert conn.execute("SELECT COUNT(*) FROM contacts WHERE wedding_id <> 0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_a_reset_of_nothing_is_refused(fresh_eo_db):
    import asyncio
    import data_reset
    fresh_eo_db.init()
    with pytest.raises(ValueError):
        asyncio.run(data_reset.reset([]))


def test_the_reset_records_itself_after_wiping_the_audit_log(fresh_eo_db, tmp_path,
                                                             monkeypatch):
    """Ordering matters: the audit row is written AFTER the wipe, so clearing the audit
    log cannot erase the evidence that someone cleared it."""
    import asyncio
    import data_reset
    db = fresh_eo_db
    db.init()
    db.add_audit(username="old", action="something_earlier")
    asyncio.run(data_reset.reset(["audit"], {"id": 1, "username": "boss"}))
    rows = db.list_audit()["items"]
    assert [r["action"] for r in rows] == ["data_reset"]
    assert rows[0]["username"] == "boss"


def test_the_reset_backs_the_database_up_first(fresh_eo_db):
    import asyncio
    import os
    import data_reset
    db = fresh_eo_db
    db.init()
    _world(db)
    result = asyncio.run(data_reset.reset(["weddings"], {"username": "boss"}))
    assert os.path.isfile(os.path.join(result["backup"], "eo.db"))
    assert result["before"]["weddings"] == 1 and result["after"]["weddings"] == 0


# ----------------------------------------------------------------------- subscription
def test_usage_bills_phone_calls_and_rounds_each_up_to_the_minute():
    """Telecom rounding is per call, not on the total: two 30s calls are 2 minutes."""
    plan = {"minutes": 100, "start": "2026-09-01", "end": "2026-09-30",
            "rate_inr_per_min": 2.0}
    metas = [
        {"source": "plivo", "started_at": "2026-09-25T10:00:00+05:30", "duration_seconds": 30},
        {"source": "plivo", "started_at": "2026-09-25T11:00:00+05:30", "duration_seconds": 30},
    ]
    u = subscription.usage(plan, metas=metas)
    assert u["calls"] == 2 and u["minutes_used"] == 2
    assert u["amount_inr"] == 4.0
    assert u["minutes_left"] == 98


def test_browser_mic_tests_are_free():
    plan = {"minutes": 100}
    metas = [{"source": "browser", "started_at": "2026-09-25T10:00:00+05:30",
              "duration_seconds": 600}]
    assert subscription.usage(plan, metas=metas)["minutes_used"] == 0


def test_calls_outside_the_plan_period_do_not_count():
    plan = {"minutes": 100, "start": "2026-09-20", "end": "2026-09-25"}
    metas = [{"source": "plivo", "started_at": "2026-09-19T10:00:00+05:30", "duration_seconds": 120},
             {"source": "plivo", "started_at": "2026-09-22T10:00:00+05:30", "duration_seconds": 120}]
    assert subscription.usage(plan, metas=metas)["calls"] == 1


def test_no_minute_cap_means_unlimited_not_zero_left():
    """minutes == 0 is 'no cap'. Reporting 0 left would read as 'out of minutes'."""
    u = subscription.usage({"minutes": 0}, metas=[])
    assert u["minutes_left"] is None and u["pct_used"] is None


def test_a_bad_plan_is_rejected_with_a_sentence_a_human_can_act_on():
    with pytest.raises(ValueError, match="whole number"):
        subscription.validate({"minutes": "lots"})
    with pytest.raises(ValueError, match="before the start"):
        subscription.validate({"start": "2026-09-30", "end": "2026-09-01"})
    with pytest.raises(ValueError, match="date"):
        subscription.validate({"start": "next tuesday"})


def test_the_licence_clock_reports_expiry(monkeypatch):
    from datetime import datetime
    now = datetime(2026, 9, 24, tzinfo=subscription.IST)
    assert subscription.usage({"licence_valid_till": "2026-09-20"}, metas=[],
                              now=now)["licence"]["status"] == "expired"
    assert subscription.usage({"licence_valid_till": "2026-09-28"}, metas=[],
                              now=now)["licence"]["status"] == "expiring"
    assert subscription.usage({"licence_valid_till": "2027-01-01"}, metas=[],
                              now=now)["licence"]["status"] == "valid"
    assert subscription.usage({}, metas=[], now=now)["licence"]["status"] == "unset"


def test_the_plan_never_blocks_a_call():
    """Display only, by decision: a mistyped date must not be able to halt a wedding-day
    campaign. Nothing in the dialling path may import subscription."""
    import inspect
    import campaign_runner
    import dialer
    for mod in (dialer, campaign_runner):
        assert "subscription" not in inspect.getsource(mod), mod.__name__


# ------------------------------------------------------------------- endpoint guards
@pytest.mark.parametrize("fn_name", ["superadmin_get", "superadmin_pages",
                                     "superadmin_reset_data", "subscription_put"])
def test_the_dangerous_endpoints_need_the_service_provider(fn_name):
    import inspect
    src = inspect.getsource(getattr(eo_api, fn_name))
    assert "require_superadmin(request)" in src, fn_name


def test_reading_the_plan_is_open_to_any_signed_in_user():
    import inspect
    src = inspect.getsource(eo_api.subscription_get)
    assert "require_eo(request)" in src


# ------------------------------------------------------ the client's menu, EPP-style
def test_the_super_admin_sees_every_tab_but_is_told_which_the_client_cannot(monkeypatch):
    """The sidebar tags hidden tabs for the service provider instead of hiding them from
    them; everyone else simply does not get those tabs."""
    monkeypatch.setenv("EO_SUPERADMIN_USERS", "boss")
    monkeypatch.setenv("EO_HIDDEN_PAGES", "agents,audit")
    monkeypatch.setattr(eo_api.eo_db, "get_setting", lambda *a, **k: None)
    boss = eo_api.ui_config({"role": "eo_admin", "username": "boss"})
    assert boss["superadmin"] is True and boss["hidden_pages"] == []
    assert boss["client_hidden_pages"] == ["agents", "audit"]
    client = eo_api.ui_config({"role": "eo_admin", "username": "client"})
    assert client["superadmin"] is False and client["hidden_pages"] == ["agents", "audit"]


def test_every_sidebar_page_key_is_one_the_server_knows():
    """A tab whose key is not in UI_PAGES could never be hidden, and a key with no tab
    would be a toggle that does nothing — the mismatch the first port shipped with."""
    import re
    layout = open("admin/src/components/Layout.jsx", encoding="utf-8").read()
    nav_pages = set(re.findall(r"page: '([a-z-]+)'", layout))
    assert nav_pages == set(eo_api.UI_PAGES)


@pytest.mark.parametrize("fn_name", ["audit_list", "audit_actions"])
def test_the_audit_log_needs_an_admin_login(fn_name):
    import inspect
    assert "require_eo_admin(request)" in inspect.getsource(getattr(eo_api, fn_name))


def test_the_audit_log_filters_by_action_and_text(fresh_eo_db):
    db = fresh_eo_db
    db.init()
    db.add_audit(username="boss", action="wedding_created", target="Ved & Riya")
    db.add_audit(username="boss", action="login")
    db.add_audit(username="client", action="login_failed", target="client")
    assert db.list_audit(action="login")["total"] == 1
    assert db.list_audit(q="Ved")["total"] == 1
    assert db.audit_actions() == ["login", "login_failed", "wedding_created"]


# ------------------------------------------------------------- changing a user's role
def test_a_role_can_be_changed_but_never_your_own(fresh_eo_db):
    """The client's login was created as Staff (own campaigns only) and there was no way to
    promote it; an admin demoting themself would lose Users and Settings."""
    import asyncio
    from fastapi import HTTPException
    db = fresh_eo_db
    db.init()
    h, s = eo_auth.hash_password("pw123456")
    boss = db.create_user(username="boss", name="Boss", password_hash=h, password_salt=s, role="eo_admin")
    client = db.create_user(username="client", name="C", password_hash=h, password_salt=s, role="eo_agent")

    class _Req:
        def __init__(self, body): self._b = body
        async def json(self): return self._b
        headers, client, query_params = {}, None, {}

    real = eo_auth.require_eo_admin
    eo_auth.require_eo_admin = lambda request: db.get_user(boss)
    try:
        asyncio.run(eo_api.users_update(client, _Req({"role": "eo_admin"})))
        assert db.get_user(client)["role"] == "eo_admin"
        with pytest.raises(HTTPException):
            asyncio.run(eo_api.users_update(boss, _Req({"role": "eo_agent"})))
        with pytest.raises(HTTPException):
            asyncio.run(eo_api.users_update(client, _Req({"role": "superuser"})))
    finally:
        eo_auth.require_eo_admin = real
    assert db.get_user(boss)["role"] == "eo_admin"
