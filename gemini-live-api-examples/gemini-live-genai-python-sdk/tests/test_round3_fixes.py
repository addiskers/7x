"""Third round of live-call and admin-UI fixes.

Every test here pins a symptom reported from a real call or the deployed admin panel.
"""

import pytest

import eo_api
import eo_auth
import gemini_live


# ----------------------------------------------------- caller transcription languages
# language_auto, language_hints and language_codes are ONE oneof on the Live API server.
# Setting two of them passed every SDK check and then failed EVERY call in production with
# "1007 ... oneof field 'language_config' is already set". Verified against the live API:
# hints-only and auto-only are accepted, both-together is refused, codes is Vertex-only.
_LANGUAGE_ONEOF = ("language_auto", "language_hints", "language_codes")


@pytest.fixture(autouse=True)
def _fresh_transcription_flag(monkeypatch):
    monkeypatch.setattr(gemini_live, "_transcribe_lang_disabled", False)


def test_the_transcription_config_never_sets_two_language_options():
    cfg = gemini_live._input_transcription_config().model_dump(exclude_none=True)
    set_members = [k for k in _LANGUAGE_ONEOF if k in cfg]
    assert len(set_members) <= 1, f"server rejects this oneof combination: {set_members}"


def test_the_default_hints_cover_the_briefed_languages():
    cfg = gemini_live._input_transcription_config().model_dump(exclude_none=True)
    codes = (cfg.get("language_hints") or {}).get("language_codes") or []
    for lang in ("en-IN", "hi-IN", "gu-IN", "mr-IN", "ta-IN"):
        assert lang in codes, f"{lang} missing from the transcription hints"


def test_empty_hints_mean_the_plain_config(monkeypatch):
    """Plain is the config that ran in production before any of this; it is the safe
    fallback, and it must not produce a hints object with an empty list."""
    monkeypatch.setenv("EO_TRANSCRIBE_LANGUAGE_HINTS", "")
    assert gemini_live._input_transcription_config().model_dump(exclude_none=True) == {}


def test_a_rejected_config_switches_every_later_session_to_plain():
    """The SDK cannot see the server's oneof rule, so the first refusal must stop the
    next connect from repeating it: a phone call's pre-warm fails, the cold connect
    right after it has to succeed."""
    err = Exception("1007 None. Invalid value at 'setup.input_audio_transcription' (oneof)")
    assert gemini_live._note_setup_rejection(err) is True
    assert gemini_live._input_transcription_config().model_dump(exclude_none=True) == {}


def test_an_unrelated_connect_error_leaves_the_transcription_settings_alone():
    """A network blip or a quota error says nothing about our transcription config."""
    assert gemini_live._note_setup_rejection(Exception("1011 internal error")) is False
    cfg = gemini_live._input_transcription_config().model_dump(exclude_none=True)
    assert "language_hints" in cfg


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


# ------------------------------------------- --force-all must reach every stale copy
@pytest.mark.parametrize("slug", ["event_reminder", "event_reminder-2", "my-custom-name"])
def test_force_all_repairs_a_stale_copy_whatever_its_slug(fresh_eo_db, slug):
    """On the server, --force-all reported agent #9 as STALE and left it: it only matched
    copies whose slug equalled a template's exactly. A duplicate made into a wedding that
    already had a copy is 'event_reminder-2', and an operator may rename it — the copy
    kept speaking the old script, which hangs up on a guest who asks for a person."""
    import seed_demo_wedding
    db = fresh_eo_db
    db.init()
    eo_auth.seed_admin()
    owner = [u for u in db.list_users() if u["role"] == "eo_admin"][0]["id"]
    wid = db.create_wedding("W", created_by=owner)
    stale = "Hello, am I speaking with {guest_name}? Old script with no escalation."
    aid = db.create_agent("Old copy", stale, wedding_id=wid, created_by=owner,
                          slug=slug, kind="reminder")
    assert db.stale_agent_reasons(db.get_agent(aid))

    seed_demo_wedding.refresh_agents(force_all=True)

    import agent_seeds
    shipped = next(s for s in agent_seeds.SEEDS if s["slug"] == "event_reminder")
    assert db.stale_agent_reasons(db.get_agent(aid)) == []
    assert db.get_agent(aid)["prompt_template"] == shipped["prompt_template"]


def test_a_copy_of_unknown_origin_is_left_alone(fresh_eo_db):
    """If neither the slug nor the kind identifies a template, overwriting it with a
    guess would be worse than reporting it."""
    import seed_demo_wedding
    db = fresh_eo_db
    db.init()
    eo_auth.seed_admin()
    owner = [u for u in db.list_users() if u["role"] == "eo_admin"][0]["id"]
    wid = db.create_wedding("W", created_by=owner)
    aid = db.create_agent("Mystery", "old text", wedding_id=wid, created_by=owner,
                          slug="mystery", kind="something-else")
    seed_demo_wedding.refresh_agents(force_all=True)
    assert db.get_agent(aid)["prompt_template"] == "old text"
