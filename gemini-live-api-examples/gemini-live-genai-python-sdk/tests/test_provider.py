"""Per-user telephony provider: users.provider column + dialer dispatch."""

import asyncio

import dialer


def test_users_provider_migration_idempotent_and_defaults(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    eo_db.init()   # migration must be re-runnable
    uid = eo_db.create_user("a", "A", "h", "s", role="eo_admin")
    assert eo_db.get_user(uid)["provider"] == "plivo"
    assert any(u["provider"] == "plivo" for u in eo_db.list_users())


def test_set_and_resolve_user_provider(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    uid = eo_db.create_user("k", "K", "h", "s", role="eo_agent", provider="enablex")
    assert eo_db.user_provider(uid) == "enablex"
    eo_db.set_user_provider(uid, "plivo")
    assert eo_db.user_provider(uid) == "plivo"
    assert eo_db.user_provider(None) == ""       # no owner -> caller falls back to default
    assert eo_db.user_provider(99999) == ""


def _dispatch_env(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://eo.example.com")
    monkeypatch.setenv("PLIVO_AUTH_ID", "id")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+910000000000")
    monkeypatch.setenv("ENABLEX_APP_ID", "app")
    monkeypatch.setenv("ENABLEX_APP_KEY", "key")
    monkeypatch.setenv("ENABLEX_FROM_NUMBER", "+918645595009")


def test_place_call_routes_by_provider(monkeypatch):
    _dispatch_env(monkeypatch)
    hits = []
    monkeypatch.setattr(dialer, "_place_call_sync", lambda to, url: hits.append(("plivo", url)) or "p-1")
    monkeypatch.setattr(dialer, "_place_call_sync_enablex", lambda to, url: hits.append(("enablex", url)) or "e-1")

    res = asyncio.run(dialer.place_call("+911111111111", provider="enablex", name="Shivi", campaign_id=7))
    assert res == {"success": True, "call_uuid": "e-1", "to": "+911111111111", "provider": "enablex"}
    res = asyncio.run(dialer.place_call("+911111111111", provider="plivo"))
    assert res["provider"] == "plivo" and res["call_uuid"] == "p-1"

    assert hits[0][0] == "enablex"
    assert hits[0][1].startswith("wss://eo.example.com/enablex/media-stream?caller=")
    assert "name=Shivi" in hits[0][1] and "campaign=7" in hits[0][1]
    assert hits[1][0] == "plivo"


def test_place_call_env_default_and_unknown_fallback(monkeypatch):
    _dispatch_env(monkeypatch)
    hits = []
    monkeypatch.setattr(dialer, "_place_call_sync", lambda to, url: hits.append("plivo") or "p-1")
    monkeypatch.setattr(dialer, "_place_call_sync_enablex", lambda to, url: hits.append("enablex") or "e-1")

    monkeypatch.setenv("EO_DEFAULT_PROVIDER", "enablex")
    assert asyncio.run(dialer.place_call("+911111111111"))["provider"] == "enablex"
    monkeypatch.delenv("EO_DEFAULT_PROVIDER")
    assert asyncio.run(dialer.place_call("+911111111111"))["provider"] == "plivo"
    assert asyncio.run(dialer.place_call("+911111111111", provider="bogus"))["provider"] == "plivo"
    assert hits == ["enablex", "plivo", "plivo"]


def test_place_call_enablex_requires_credentials(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://eo.example.com")
    monkeypatch.delenv("ENABLEX_APP_ID", raising=False)
    monkeypatch.delenv("ENABLEX_APP_KEY", raising=False)
    res = asyncio.run(dialer.place_call("+911111111111", provider="enablex"))
    assert "EnableX credentials not configured" in res["error"]
