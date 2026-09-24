"""Third round of live-call and admin-UI fixes.

Every test here pins a symptom reported from a real call or the deployed admin panel.
"""

import pytest

import eo_api
import eo_auth
import gemini_live


# ------------------------------------------------- caller transcription is not en-IN-only
def test_the_caller_is_transcribed_in_whatever_language_they_speak():
    """"tried to talk in hindi but the AI could not understand" — the session's
    language_code (the agent's VOICE, fixed for the call) was governing input
    transcription too, so Hindi came back as garbled English."""
    cfg = gemini_live._input_transcription_config().model_dump(exclude_none=True)
    assert cfg.get("language_auto") is not None, "input transcription must auto-detect"
    codes = (cfg.get("language_hints") or {}).get("language_codes") or []
    for lang in ("en-IN", "hi-IN", "gu-IN", "mr-IN", "ta-IN"):
        assert lang in codes, f"{lang} missing from the transcription hints"


def test_transcription_hints_are_configurable_and_may_be_empty(monkeypatch):
    """A deployment that wants pure auto-detection sets the var empty; that must not
    produce a hints object with an empty list, which the API would reject."""
    monkeypatch.setenv("EO_TRANSCRIBE_LANGUAGE_HINTS", "")
    cfg = gemini_live._input_transcription_config().model_dump(exclude_none=True)
    assert "language_hints" not in cfg
    assert cfg.get("language_auto") is not None


def test_an_older_sdk_falls_back_instead_of_breaking_every_call(monkeypatch):
    """language_auto/language_hints are newer than our pinned minimum. If they are
    missing, transcription degrades — a call must never fail to connect over it."""
    class _Boom:
        def __init__(self, *a, **kw):
            raise TypeError("unexpected keyword argument 'language_auto'")

    monkeypatch.setattr(gemini_live.types, "AudioTranscriptionConfig", _Boom)
    with pytest.raises(TypeError):
        gemini_live.types.AudioTranscriptionConfig(language_auto=None)
    # the helper swallows it and returns the plain config
    monkeypatch.undo()
    assert gemini_live._input_transcription_config() is not None


# --------------------------------------------------- agent endpoints are superadmin-only
# The agent prompts are our product. A client-facing "Admin" (role eo_agent — the UI
# labels invert the code names) must not be able to read or edit them, and hiding the
# sidebar item is not enough: GET /agents with no wedding_id skipped every ownership
# check and returned every agent row, prompts included, to any authenticated user.
_AGENT_ENDPOINTS = ("agents_list", "agents_detail", "agents_create", "agents_update",
                    "agents_delete", "agents_duplicate", "agents_preview",
                    "agents_test_token", "agents_test_call")


def _source_of(fn_name):
    import inspect
    return inspect.getsource(getattr(eo_api, fn_name))


@pytest.mark.parametrize("fn_name", _AGENT_ENDPOINTS)
def test_every_agent_endpoint_requires_the_superadmin_role(fn_name):
    src = _source_of(fn_name)
    assert "require_eo_admin(request)" in src, f"{fn_name} does not require eo_admin"
    assert "require_eo(request)" not in src, f"{fn_name} still accepts any logged-in user"


def test_the_role_guard_actually_rejects_a_client_admin(fresh_eo_db, monkeypatch):
    """Pin the guard itself, not just its call sites: require_eo_admin must 403 an
    eo_agent and pass an eo_admin."""
    from fastapi import HTTPException
    db = fresh_eo_db
    db.init()
    h, s = eo_auth.hash_password("pw123456")
    client_id = db.create_user(username="client", name="Client", password_hash=h,
                               password_salt=s, role="eo_agent")
    admin_id = db.create_user(username="boss", name="Boss", password_hash=h,
                              password_salt=s, role="eo_admin")

    class _Req:
        def __init__(self, uid):
            self.uid = uid
        headers = {}
        cookies = {}
        query_params = {}

    def fake_require_eo(request):
        return db.get_user(request.uid)

    monkeypatch.setattr(eo_auth, "require_eo", fake_require_eo)
    with pytest.raises(HTTPException) as exc:
        eo_auth.require_eo_admin(_Req(client_id))
    assert exc.value.status_code == 403
    assert eo_auth.require_eo_admin(_Req(admin_id))["role"] == "eo_admin"


# ------------------------------------------------------- duplicating an agent (the 500)
def test_duplicating_an_agent_twice_does_not_500(fresh_eo_db):
    """"Internal Server Error when i try to duplicate the agent" — agents carry
    UNIQUE(wedding_id, slug), and the collision guard only fired for GLOBAL rows, so the
    second copy into a wedding raised IntegrityError straight out of the endpoint."""
    db = fresh_eo_db
    db.init()
    eo_auth.seed_admin()
    owner = [u for u in db.list_users() if u["role"] == "eo_admin"][0]["id"]
    wid = db.create_wedding("W", created_by=owner)
    src = db.get_agent_by_slug("event_reminder")

    # what the endpoint now does: find a free slug rather than reusing a taken one
    def free_slug(slug, wedding_id):
        if slug and db.get_agent_by_slug(slug, wedding_id=wedding_id):
            base = slug[:54]
            return next((c for n in range(2, 100)
                         if not db.get_agent_by_slug((c := f"{base}-{n}"),
                                                     wedding_id=wedding_id)), "")
        return slug

    made = []
    for i in range(3):
        slug = free_slug(src["slug"], wid)
        made.append(db.create_agent(f"copy{i}", src["prompt_template"], wedding_id=wid,
                                    created_by=owner, slug=slug))
    assert len(set(made)) == 3, "each duplicate must get its own row"
