"""
SQLite storage for the 7x platform: users, weddings, events, agents, the guest pool,
campaigns and campaign_contacts. Calls stay as JSON files (store.py); they only gain a
`campaign_id` so the admin can filter/label per campaign.

The hierarchy is Wedding -> Event -> Campaign. Guests and agents belong to a wedding;
a campaign pairs one agent with one event (or with no event, for logistics calls that
are keyed to a guest's travel rather than a function).

Sync sqlite3 (WAL, check_same_thread=False) guarded by a lock — SQLite queries
here are tiny, so this stays off the event loop's critical path without an async
driver. Lives next to the JSON call store under DATA_DIR.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Bumped whenever the schema changes shape. Stamped into PRAGMA user_version so a DB
# can say what it is; 7x starts at 1 (the EO line kept no version at all).
SCHEMA_VERSION = 1

_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
_DB_PATH = os.path.join(_DATA_DIR, "eo.db")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(_DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    name          TEXT,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'eo_admin',   -- eo_admin | eo_agent
    active        INTEGER NOT NULL DEFAULT 1,
    provider      TEXT NOT NULL DEFAULT 'plivo',      -- telephony provider: plivo | enablex
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weddings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,                  -- "Anant & Manya"
    groom_name        TEXT NOT NULL DEFAULT '',
    bride_name        TEXT NOT NULL DEFAULT '',
    groom_side_family TEXT NOT NULL DEFAULT '',       -- "Kapoor Family"
    bride_side_family TEXT NOT NULL DEFAULT '',       -- "Chopra Family"
    start_date        TEXT,                           -- YYYY-MM-DD, first function
    end_date          TEXT,                           -- YYYY-MM-DD, last function
    city              TEXT NOT NULL DEFAULT '',
    -- Spoken identity: who the agent says it is calling on behalf of.
    hospitality_team  TEXT NOT NULL DEFAULT '',       -- "Anant and Manya's Wedding Hospitality team"
    placard_text      TEXT NOT NULL DEFAULT '',       -- "Kapoor & Chopra Family Welcomes You"
    contact_phone     TEXT NOT NULL DEFAULT '',       -- spoken fallback number
    contact_name      TEXT NOT NULL DEFAULT '',       -- "Rohit, our hospitality lead"
    notes             TEXT,
    status            TEXT NOT NULL DEFAULT 'active', -- active | archived
    created_by        INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weddings_status ON weddings(status);
CREATE INDEX IF NOT EXISTS idx_weddings_owner ON weddings(created_by);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wedding_id    INTEGER NOT NULL REFERENCES weddings(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,                      -- "Mehendi & Haldi"
    event_date    TEXT,                               -- YYYY-MM-DD (IST calendar day)
    start_time    TEXT NOT NULL DEFAULT '',           -- "19:00" 24h; rendered spoken
    end_time      TEXT NOT NULL DEFAULT '',
    venue         TEXT NOT NULL DEFAULT '',
    venue_address TEXT NOT NULL DEFAULT '',
    dress_code    TEXT NOT NULL DEFAULT '',
    audience      TEXT NOT NULL DEFAULT 'all',        -- all | groom | bride
    announcement  TEXT NOT NULL DEFAULT '',           -- extra free text the agent must convey
    notes         TEXT,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_wedding ON events(wedding_id, event_date, sort_order);

CREATE TABLE IF NOT EXISTS agents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wedding_id      INTEGER REFERENCES weddings(id) ON DELETE CASCADE, -- NULL = global template
    name            TEXT NOT NULL,                    -- "Event Reminder Specialist"
    slug            TEXT NOT NULL DEFAULT '',         -- stable key for seeding
    kind            TEXT NOT NULL DEFAULT 'custom',   -- reminder | logistics | custom
    description     TEXT NOT NULL DEFAULT '',
    prompt_template TEXT NOT NULL,                    -- placeholder-bearing system prompt
    trigger_template TEXT NOT NULL DEFAULT '',        -- placeholder-bearing FIRST-TURN trigger
    -- JSON, not child tables: read whole per call, never queried into. Always parsed
    -- defensively — a corrupt blob must never break a live call (see agent_tools).
    outcome_enum    TEXT NOT NULL DEFAULT '[]',       -- [{"value":..,"description":..}]
    extra_fields    TEXT NOT NULL DEFAULT '[]',       -- [{"name":..,"type":..,"description":..}]
    voice_name      TEXT NOT NULL DEFAULT '',         -- '' -> EO_VOICE_NAME env default
    -- Fixed for the whole session and cannot switch mid-call, so it belongs on the agent.
    speech_language_code TEXT NOT NULL DEFAULT '',
    language_mode   TEXT NOT NULL DEFAULT 'auto',     -- auto | english
    listen_seconds  INTEGER NOT NULL DEFAULT 0,       -- >0 -> announce-then-listen-N-then-hang-up
    requires_event  INTEGER NOT NULL DEFAULT 1,       -- 0 = event optional (logistics)
    active          INTEGER NOT NULL DEFAULT 1,
    created_by      INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_wedding ON agents(wedding_id, active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_slug ON agents(wedding_id, slug);

CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    phone      TEXT NOT NULL,                         -- E.164
    source     TEXT NOT NULL DEFAULT 'upload',        -- upload | manual | plivo
    status     TEXT NOT NULL DEFAULT 'valid',         -- valid | invalid
    remark     TEXT,
    -- Wedding-guest fields. Blank means "not known" — never rendered into a prompt.
    side             TEXT NOT NULL DEFAULT '',        -- groom | bride | both | ''
    dietary          TEXT NOT NULL DEFAULT '',
    transport_mode   TEXT NOT NULL DEFAULT '',        -- flight | train | car | ''
    transport_number TEXT NOT NULL DEFAULT '',        -- "6E 2134" / "12951"
    arrival_at       TEXT NOT NULL DEFAULT '',        -- ISO-ish or free text
    departure_at     TEXT NOT NULL DEFAULT '',
    hotel            TEXT NOT NULL DEFAULT '',
    room_number      TEXT NOT NULL DEFAULT '',
    guest_count      INTEGER,                         -- party size; NULL = unknown
    created_by INTEGER,                               -- owning user; always stamped on insert
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- A guest belongs to one wedding, so the same phone may legitimately appear in two
    -- weddings run by the same operator (a repeat client's shared relatives).
    --
    -- wedding_id is 0, never NULL, for "no wedding": SQLite treats NULLs as DISTINCT in a
    -- unique index, so a NULL here would stop ON CONFLICT from ever firing and every
    -- re-upload would silently duplicate the whole guest list.
    wedding_id       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(created_by, wedding_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_contacts_created ON contacts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);
CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone);
CREATE INDEX IF NOT EXISTS idx_contacts_wedding ON contacts(wedding_id, side);
-- idx_contacts_owner is created in init() AFTER the created_by migration: putting it
-- here would crash startup on a legacy DB whose contacts table predates the column.

CREATE TABLE IF NOT EXISTS campaigns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | live | completed | cancelled
    start_at            TEXT NOT NULL,                     -- ISO-8601 UTC
    created_by          INTEGER,
    -- What this campaign runs: which agent, for which function of which wedding.
    -- event_id is NULL for logistics campaigns (pickup/drop are keyed to the guest's
    -- travel, not to any one function) — see agents.requires_event.
    wedding_id          INTEGER,
    event_id            INTEGER,
    agent_id            INTEGER,
    contact_count       INTEGER NOT NULL DEFAULT 0,
    callback_delay_hours INTEGER NOT NULL DEFAULT 4,
    callback_max_per_day INTEGER NOT NULL DEFAULT 3,
    callback_days        INTEGER NOT NULL DEFAULT 1,
    -- calling hours (minutes-since-midnight IST): no auto dials outside [start, end)
    call_start_min       INTEGER NOT NULL DEFAULT 540,      -- 09:00
    call_end_min         INTEGER NOT NULL DEFAULT 1260,     -- 21:00
    -- progress counters (updated by the runner)
    done_count          INTEGER NOT NULL DEFAULT 0,
    failed_count        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaigns_created ON campaigns(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_campaigns_event ON campaigns(event_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_wedding ON campaigns(wedding_id);

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id      INTEGER,
    phone           TEXT NOT NULL,
    name            TEXT,
    call_status     TEXT NOT NULL DEFAULT 'pending',  -- pending|calling|done|failed|cancelled (no_answer: legacy, unused)
    attempts        INTEGER NOT NULL DEFAULT 0,
    day_attempts    INTEGER NOT NULL DEFAULT 0,
    day_key         TEXT,                              -- YYYY-MM-DD of last day_attempts window
    next_attempt_at TEXT,                              -- backoff/pacing gate (ISO)
    last_call_id    TEXT,
    last_attempt_at TEXT,
    last_error      TEXT,
    rsvp_outcome    TEXT,
    remark          TEXT,                              -- agent note / operator edit
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_campaign ON campaign_contacts(campaign_id);
CREATE INDEX IF NOT EXISTS idx_cc_due ON campaign_contacts(call_status, next_attempt_at);

-- Small operator settings that must survive a restart (the plan override, the client's
-- hidden pages). A generic key/JSON store rather than a column per setting.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT 'null',            -- JSON
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);

-- Who did what. Written for the actions worth being able to answer for later: clearing
-- data, changing the plan, changing what the client can see.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at DESC);
"""

_SETTINGS_SQL = """CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT 'null', updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '')"""


_CONTACT_GUEST_COLUMNS = (
    ("remark", "TEXT"),
    ("wedding_id", "INTEGER NOT NULL DEFAULT 0"),
    ("side", "TEXT NOT NULL DEFAULT ''"),
    ("dietary", "TEXT NOT NULL DEFAULT ''"),
    ("transport_mode", "TEXT NOT NULL DEFAULT ''"),
    ("transport_number", "TEXT NOT NULL DEFAULT ''"),
    ("arrival_at", "TEXT NOT NULL DEFAULT ''"),
    ("departure_at", "TEXT NOT NULL DEFAULT ''"),
    ("hotel", "TEXT NOT NULL DEFAULT ''"),
    ("room_number", "TEXT NOT NULL DEFAULT ''"),
    ("guest_count", "INTEGER"),
)


def _contacts_ddl() -> str:
    """The CREATE TABLE for contacts, lifted out of SCHEMA so the rebuild and the fresh
    path can never drift apart. Splitting SCHEMA on ';' would not work — the column
    comments contain them."""
    start = SCHEMA.index("CREATE TABLE IF NOT EXISTS contacts")
    end = SCHEMA.index("\n);", start) + len("\n);")
    return SCHEMA[start:end]


def _rebuild_contacts_if_legacy(conn) -> bool:
    """Give a pre-7x contacts table the UNIQUE(created_by, wedding_id, phone) key.

    ALTER TABLE ADD COLUMN cannot add a column to a UNIQUE constraint, so a DB carrying
    the old UNIQUE(phone) (or UNIQUE(created_by, phone)) cannot be migrated additively —
    every guest upsert would look for an ON CONFLICT target that does not exist. The only
    fix SQLite offers is a table rebuild.

    Returns True if it rebuilt. Existing rows keep their data; legacy rows with no owner
    are stamped with the first admin, because NULLs are distinct in a unique index and a
    NULL owner would never upsert."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
    if not cols:
        return False                       # table does not exist yet — SCHEMA will create it
    # Does the current unique key already match what we need?
    for idx in conn.execute("PRAGMA index_list(contacts)").fetchall():
        if not idx["unique"]:
            continue
        key = [r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
        if key == ["created_by", "wedding_id", "phone"]:
            return False                   # already correct
    logger.warning("contacts: migrating a pre-7x table to UNIQUE(created_by, wedding_id, phone)")

    owner_row = conn.execute(
        "SELECT id FROM users WHERE role = 'eo_admin' ORDER BY id ASC LIMIT 1").fetchone()
    if owner_row is None:
        owner_row = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
    legacy_owner = int(owner_row["id"]) if owner_row is not None else 1

    keep = ["id", "name", "phone", "source", "status"]
    keep += [c for c, _ in _CONTACT_GUEST_COLUMNS if c in cols]
    keep += ["created_at", "updated_at"]
    owner_expr = "created_by" if "created_by" in cols else str(legacy_owner)

    # One real transaction: sqlite3's legacy autocommit commits before each DDL, so a
    # crash mid-rebuild would otherwise strand the pool in contacts_legacy.
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE contacts RENAME TO contacts_legacy")
        conn.execute(_contacts_ddl())
        cols_sql = ", ".join(keep)
        select_sql = ", ".join(
            owner_expr if c == "created_by" else
            (f"COALESCE({c}, 0)" if c == "wedding_id" else c)
            for c in keep)
        conn.execute(
            f"INSERT INTO contacts (created_by, {cols_sql}) "
            f"SELECT {owner_expr}, {select_sql} FROM contacts_legacy")
        conn.execute("DROP TABLE contacts_legacy")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation
    return True


def init() -> None:
    """Create tables (idempotent), migrate older databases, seed the shipped agents.

    Order matters: columns are added BEFORE executescript(SCHEMA), because SCHEMA creates
    indexes over the new columns and executescript aborts the whole batch on the first
    failure — an index on a column that does not exist yet would stop every statement
    after it from running."""
    conn = get_conn()
    with _lock:
        def _add_missing(table, columns):
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not have:
                return                       # table not created yet; SCHEMA will do it
            for col, ddl in columns:
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

        # 1. Bring existing tables up to the current column set.
        _add_missing("campaign_contacts", [
            ("last_error", "TEXT"),
            ("remark", "TEXT"),
        ])
        _add_missing("campaigns", [
            ("call_start_min", "INTEGER NOT NULL DEFAULT 540"),
            ("call_end_min", "INTEGER NOT NULL DEFAULT 1260"),
            ("wedding_id", "INTEGER"),
            ("event_id", "INTEGER"),
            ("agent_id", "INTEGER"),
        ])
        _add_missing("users", [("provider", "TEXT NOT NULL DEFAULT 'plivo'")])
        _add_missing("contacts", _CONTACT_GUEST_COLUMNS)

        # 2. A unique key cannot be altered in place — rebuild if this DB predates 7x.
        _rebuild_contacts_if_legacy(conn)

        # 3. Now every column exists, so the schema's indexes can be created safely.
        conn.executescript(SCHEMA)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(created_by)")
        conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
        conn.commit()
    _seed_agents()
    # Seeding never overwrites an existing row, so a redeploy can leave old prompt text in
    # the database while the code is current. Say so on every boot rather than waiting for
    # a client to hear it on a call.
    warn_about_stale_agents()


def schema_version() -> int:
    """The DB's stamped schema version (0 on a pre-7x database)."""
    r = get_conn().execute("PRAGMA user_version").fetchone()
    return int(r[0]) if r else 0


# Text that should not survive in any live agent prompt. The first group is branding from
# the EO build this platform was converted from; the rest are sections every current agent
# must carry. A row missing them predates a fix and is still speaking the old script.
_STALE_MARKERS = ("EO Gujarat", "Raj Goodman", "AI First Mindset", "DoubleTree",
                  "Sir or Ma'am, am I speaking")
_REQUIRED_FRAGMENTS = (
    # Either the lookup list or the detailed walk-through counts: both give the agent
    # every function, which is what "kisi or event ki details nahi de raha" was about.
    (("{schedule}", "{schedule_detail}", "{upcoming_schedule}"), "cannot answer about other functions"),
    (("SPEAK TO A PERSON",), "hangs up when asked for a person"),
)


def stale_agent_reasons(agent) -> list:
    """Why this agent's prompt looks out of date. Empty list = current.

    Checked on every boot and by --refresh-agents, because a stale row is invisible: the
    code is new, the prompt in the database is old, and only a guest on a live call finds
    out."""
    text = (agent or {}).get("prompt_template") or ""
    if not text:
        return []
    reasons = [f"still says '{m}'" for m in _STALE_MARKERS if m in text]
    reasons += [why for frags, why in _REQUIRED_FRAGMENTS
                if not any(f in text for f in frags)]
    return reasons


def warn_about_stale_agents() -> list:
    """Log a loud warning for every agent row carrying old prompt text. Returns them."""
    stale = []
    try:
        for a in all_agents():
            reasons = stale_agent_reasons(a)
            if reasons:
                stale.append(a)
                logger.warning(
                    "STALE AGENT PROMPT: '%s' (id=%s, wedding_id=%s) — %s. "
                    "Guests on this agent hear the OLD script. Fix with: "
                    "python seed_demo_wedding.py --refresh-agents%s",
                    a.get("name"), a.get("id"), a.get("wedding_id"), "; ".join(reasons),
                    " --force-all" if a.get("wedding_id") else "")
    except Exception:
        logger.debug("stale-agent check failed", exc_info=True)
    return stale


def _seed_agents() -> None:
    """Insert the shipped agent templates as global rows (wedding_id IS NULL).

    Idempotent by slug: re-running init() never duplicates a seed, and never clobbers an
    operator's edits to one. A wedding needing a variant duplicates the agent into its own
    row rather than editing the global."""
    import agent_seeds
    conn = get_conn()
    with _lock:
        for seed in agent_seeds.SEEDS:
            existing = conn.execute(
                "SELECT id FROM agents WHERE wedding_id IS NULL AND slug = ?",
                (seed["slug"],)).fetchone()
            if existing:
                continue
            now = _now()
            conn.execute(
                "INSERT INTO agents (wedding_id, name, slug, kind, description, prompt_template, "
                "trigger_template, outcome_enum, extra_fields, voice_name, speech_language_code, "
                "language_mode, listen_seconds, requires_event, active, created_by, created_at, updated_at) "
                "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, '', '', 'auto', ?, ?, 1, NULL, ?, ?)",
                (seed["name"], seed["slug"], seed["kind"], seed.get("description", ""),
                 seed["prompt_template"], seed.get("trigger_template", ""),
                 seed.get("outcome_enum", "[]"), seed.get("extra_fields", "[]"),
                 int(seed.get("listen_seconds", 0)), int(seed.get("requires_event", 1)),
                 now, now))
        conn.commit()


# Generic helpers
def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _one(sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params: tuple = ()) -> int:
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


# Users
def count_users() -> int:
    r = _one("SELECT COUNT(*) c FROM users")
    return int(r["c"]) if r else 0


def create_user(username: str, name: str, password_hash: str, password_salt: str,
                role: str = "eo_admin", provider: str = "plivo") -> int:
    now = _now()
    return _exec(
        "INSERT INTO users (username, name, password_hash, password_salt, role, active, provider, created_at, updated_at) "
        "VALUES (?,?,?,?,?,1,?,?,?)",
        (username, name, password_hash, password_salt, role, provider, now, now),
    )


def get_user_by_username(username: str) -> dict | None:
    return _one("SELECT * FROM users WHERE username = ?", (username,))


def get_user(user_id: int) -> dict | None:
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def list_users() -> list[dict]:
    return _rows("SELECT id, username, name, role, active, provider, created_at FROM users ORDER BY created_at DESC")


def set_user_active(user_id: int, active: bool) -> None:
    _exec("UPDATE users SET active = ?, updated_at = ? WHERE id = ?", (1 if active else 0, _now(), user_id))


def set_user_role(user_id: int, role: str) -> None:
    _exec("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, _now(), int(user_id)))


def set_user_provider(user_id: int, provider: str) -> None:
    _exec("UPDATE users SET provider = ?, updated_at = ? WHERE id = ?", (provider, _now(), int(user_id)))


def user_provider(user_id) -> str:
    """Telephony provider assigned to a user ('' when unknown → caller falls back to the default)."""
    if not user_id:
        return ""
    u = get_user(int(user_id))
    return (u or {}).get("provider") or ""


def update_user_password(user_id: int, password_hash: str, password_salt: str) -> None:
    _exec("UPDATE users SET password_hash = ?, password_salt = ?, updated_at = ? WHERE id = ?",
          (password_hash, password_salt, _now(), int(user_id)))


# Contacts (global pool)
_CONTACT_SORTS = {"name", "phone", "source", "status", "created_at"}


# Wedding-guest columns an import or a manual add may set. Order matters: it drives the
# INSERT column list and the ON CONFLICT update clause below.
GUEST_FIELDS = ("side", "dietary", "transport_mode", "transport_number",
                "arrival_at", "departure_at", "hotel", "room_number", "guest_count")


def _wid(wedding_id) -> int:
    """Normalise a wedding id for the contacts table. 0 means "no wedding" — never NULL,
    because SQLite treats NULLs as distinct in a unique index and ON CONFLICT would
    then never fire, silently duplicating the guest list on every re-upload."""
    try:
        return int(wedding_id) if wedding_id not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _guest_values(fields):
    """Normalise a guest-field dict into GUEST_FIELDS order. Blank/absent stays blank
    (meaning 'not known'), and guest_count stays NULL rather than becoming 0."""
    fields = fields or {}
    out = []
    for key in GUEST_FIELDS:
        val = fields.get(key)
        if key == "guest_count":
            try:
                out.append(int(val) if val not in (None, "") else None)
            except (TypeError, ValueError):
                out.append(None)
        else:
            out.append(str(val).strip() if val not in (None, "") else "")
    return out


def add_contact(name: str, phone: str, source: str = "manual", status: str = "valid",
                created_by=None, wedding_id=None, **guest):
    """Upsert one guest by (owner, wedding, phone). Returns (id, created_bool)."""
    now = _now()
    owner = int(created_by) if created_by is not None else None
    wid = _wid(wedding_id)
    existing = _one(
        "SELECT id FROM contacts WHERE created_by IS ? AND wedding_id = ? AND phone = ?",
        (owner, wid, phone))
    values = _guest_values(guest)
    if existing:
        # Only overwrite a guest field when the caller actually supplied one — a blank in a
        # re-upload must not wipe a detail someone already has.
        sets = ["name = COALESCE(NULLIF(?, ''), name)", "status = ?", "updated_at = ?"]
        params = [name or "", status, now]
        for key, val in zip(GUEST_FIELDS, values):
            if val not in (None, ""):
                sets.append(f"{key} = ?")
                params.append(val)
        _exec(f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?",
              tuple(params) + (existing["id"],))
        return existing["id"], False
    cols = ", ".join(GUEST_FIELDS)
    holes = ", ".join("?" * len(GUEST_FIELDS))
    cid = _exec(
        f"INSERT INTO contacts (name, phone, source, status, created_by, wedding_id, "
        f"{cols}, created_at, updated_at) VALUES (?,?,?,?,?,?,{holes},?,?)",
        (name, phone, source, status, owner, wid, *values, now, now),
    )
    return cid, True


def bulk_upsert_contacts(rows, source: str = "upload", created_by=None, wedding_id=None):
    """rows: iterable of (name, phone, status) or (name, phone, status, guest_fields_dict).
    Upserts into the OWNER's pool for ONE wedding. Returns (added, updated)."""
    rows = list(rows)
    if not rows:
        return 0, 0
    now = _now()
    owner = int(created_by) if created_by is not None else None
    wid = _wid(wedding_id)

    def _split(row):
        nm, ph, st = row[0], row[1], row[2]
        return nm, ph, st, (row[3] if len(row) > 3 else None)

    # A blank in the sheet must not wipe a detail already on the row, so each guest column
    # updates only when the incoming value is non-empty.
    guest_updates = ", ".join(
        f"{k} = CASE WHEN excluded.{k} IS NULL OR excluded.{k} = '' THEN contacts.{k} "
        f"ELSE excluded.{k} END" for k in GUEST_FIELDS)
    cols = ", ".join(GUEST_FIELDS)
    holes = ", ".join("?" * len(GUEST_FIELDS))

    conn = get_conn()
    with _lock:
        existing = {r["phone"] for r in conn.execute(
            "SELECT phone FROM contacts WHERE created_by IS ? AND wedding_id = ?",
            (owner, wid)).fetchall()}
        conn.executemany(
            f"INSERT INTO contacts (name, phone, source, status, created_by, wedding_id, "
            f"{cols}, created_at, updated_at) VALUES (?,?,?,?,?,?,{holes},?,?) "
            f"ON CONFLICT(created_by, wedding_id, phone) DO UPDATE SET "
            f"name = COALESCE(NULLIF(excluded.name, ''), contacts.name), "
            f"status = excluded.status, updated_at = excluded.updated_at, {guest_updates}",
            [(nm, ph, source, st, owner, wid, *_guest_values(g), now, now)
             for (nm, ph, st, g) in (_split(r) for r in rows)],
        )
        conn.commit()
    added = sum(1 for r in rows if r[1] not in existing)
    return added, len(rows) - added


def list_contacts(q=None, source=None, status=None, sort="created_at", direction="desc",
                  limit=25, offset=0, created_by=None, wedding_id=None, side=None):
    """created_by=None → all pools (Superadmin); an int scopes to that owner's pool.
    wedding_id scopes to one wedding's guest list."""
    where, params = [], []
    if q:
        where.append("(name LIKE ? OR phone LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if source:
        where.append("source = ?")
        params.append(source)
    if status:
        where.append("status = ?")
        params.append(status)
    if created_by is not None:
        where.append("created_by = ?")
        params.append(int(created_by))
    if wedding_id is not None:
        where.append("wedding_id = ?")
        params.append(_wid(wedding_id))
    if side:
        where.append("side = ?")
        params.append(str(side))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    col = sort if sort in _CONTACT_SORTS else "created_at"
    dir_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    total = _one(f"SELECT COUNT(*) c FROM contacts {wsql}", tuple(params))["c"]
    rows = _rows(
        f"SELECT * FROM contacts {wsql} ORDER BY {col} {dir_sql} LIMIT ? OFFSET ?",
        tuple(params) + (int(limit), int(offset)),
    )
    return {"items": rows, "total": int(total)}


def get_contacts_by_ids(ids, created_by=None):
    """created_by=None → any pool (Superadmin); an int restricts to that owner's rows
    (an agent can never attach another user's contacts to a campaign)."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    sql = f"SELECT * FROM contacts WHERE id IN ({ph})"
    params: tuple = tuple(ids)
    if created_by is not None:
        sql += " AND created_by = ?"
        params += (int(created_by),)
    return _rows(sql, params)


def delete_contacts(ids, created_by=None) -> int:
    ids = [int(i) for i in ids if i]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    sql = f"DELETE FROM contacts WHERE id IN ({ph})"
    params: tuple = tuple(ids)
    if created_by is not None:
        sql += " AND created_by = ?"
        params += (int(created_by),)
    conn = get_conn()
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def count_contacts(created_by=None) -> int:
    if created_by is not None:
        r = _one("SELECT COUNT(*) c FROM contacts WHERE created_by = ?", (int(created_by),))
    else:
        r = _one("SELECT COUNT(*) c FROM contacts")
    return int(r["c"]) if r else 0


def get_contact(contact_id: int) -> dict | None:
    return _one("SELECT * FROM contacts WHERE id = ?", (int(contact_id),))


def contact_by_phone(phone: str, wedding_id=None) -> dict | None:
    """Look a guest up by number — the inbound path and /call-me tests have no id.

    Prefers the given wedding so an aunt who appears at two weddings is greeted with the
    right one's details; falls back to the most recent row for that number."""
    phone = str(phone or "").strip()
    if not phone:
        return None
    if wedding_id not in (None, ""):
        hit = _one("SELECT * FROM contacts WHERE phone = ? AND wedding_id = ? "
                   "ORDER BY id DESC LIMIT 1", (phone, _wid(wedding_id)))
        if hit:
            return hit
    return _one("SELECT * FROM contacts WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,))


def set_contact_remark(contact_id: int, remark: str) -> None:
    _exec("UPDATE contacts SET remark = ?, updated_at = ? WHERE id = ?",
          (remark, _now(), int(contact_id)))


# Campaigns
_CAMPAIGN_SORTS = {"name", "status", "start_at", "contact_count", "created_at"}


# ---------------------------------------------------------------------------------------
# Weddings
# ---------------------------------------------------------------------------------------
WEDDING_FIELDS = ("name", "groom_name", "bride_name", "groom_side_family", "bride_side_family",
                  "start_date", "end_date", "city", "hospitality_team", "placard_text",
                  "contact_phone", "contact_name", "notes", "status")


def create_wedding(name: str, created_by=None, **fields) -> int:
    now = _now()
    fields = {**fields, "name": name}
    cols, vals = ["created_by", "created_at", "updated_at"], [created_by, now, now]
    for key in WEDDING_FIELDS:
        if key in fields and fields[key] is not None:
            cols.append(key)
            vals.append(fields[key])
    return _exec(f"INSERT INTO weddings ({', '.join(cols)}) "
                 f"VALUES ({', '.join('?' * len(cols))})", tuple(vals))


def get_wedding(wedding_id) -> dict | None:
    if wedding_id in (None, ""):
        return None
    return _one("SELECT * FROM weddings WHERE id = ?", (int(wedding_id),))


def list_weddings(created_by=None, status=None) -> list[dict]:
    """created_by=None → every wedding (Superadmin); an int scopes to that owner's."""
    where, params = [], []
    if created_by is not None:
        where.append("created_by = ?")
        params.append(int(created_by))
    if status:
        where.append("status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return _rows(f"SELECT * FROM weddings {clause} ORDER BY "
                 f"COALESCE(start_date, '9999') DESC, id DESC", tuple(params))


def update_wedding(wedding_id: int, **fields) -> int:
    sets, params = [], []
    for key in WEDDING_FIELDS:
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if not sets:
        return 0
    sets.append("updated_at = ?")
    params.append(_now())
    return _exec(f"UPDATE weddings SET {', '.join(sets)} WHERE id = ?",
                 tuple(params) + (int(wedding_id),))


def delete_wedding(wedding_id: int) -> int:
    """Cascades to the wedding's events, agents and guests (FK ON DELETE CASCADE)."""
    return _exec("DELETE FROM weddings WHERE id = ?", (int(wedding_id),))


# ---------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------
EVENT_FIELDS = ("name", "event_date", "start_time", "end_time", "venue", "venue_address",
                "dress_code", "audience", "announcement", "notes", "sort_order")

AUDIENCES = ("all", "groom", "bride")


def create_event(wedding_id: int, name: str, **fields) -> int:
    now = _now()
    fields = {**fields, "name": name}
    cols, vals = ["wedding_id", "created_at", "updated_at"], [int(wedding_id), now, now]
    for key in EVENT_FIELDS:
        if key in fields and fields[key] is not None:
            cols.append(key)
            vals.append(fields[key])
    return _exec(f"INSERT INTO events ({', '.join(cols)}) "
                 f"VALUES ({', '.join('?' * len(cols))})", tuple(vals))


def get_event(event_id) -> dict | None:
    if event_id in (None, ""):
        return None
    return _one("SELECT * FROM events WHERE id = ?", (int(event_id),))


def list_events(wedding_id) -> list[dict]:
    """A wedding's functions in running order — by date, then the operator's sort_order."""
    return _rows(
        "SELECT * FROM events WHERE wedding_id = ? "
        "ORDER BY COALESCE(event_date, '9999'), sort_order, start_time, id",
        (int(wedding_id),))


def update_event(event_id: int, **fields) -> int:
    sets, params = [], []
    for key in EVENT_FIELDS:
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if not sets:
        return 0
    sets.append("updated_at = ?")
    params.append(_now())
    return _exec(f"UPDATE events SET {', '.join(sets)} WHERE id = ?",
                 tuple(params) + (int(event_id),))


def delete_event(event_id: int) -> int:
    return _exec("DELETE FROM events WHERE id = ?", (int(event_id),))


def guests_for_audience(wedding_id, audience: str, created_by=None) -> list[dict]:
    """The guests a campaign for this event should pre-select.

    'all' takes everyone; 'groom'/'bride' take that side plus anyone marked 'both' and
    anyone whose side was never recorded — a blank side must not silently drop a guest
    from a reminder they were meant to get."""
    audience = (audience or "all").strip().lower()
    where = ["wedding_id = ?", "status = 'valid'"]
    params: list = [_wid(wedding_id)]
    if audience in ("groom", "bride"):
        where.append("(side = ? OR side = 'both' OR side = '' OR side IS NULL)")
        params.append(audience)
    if created_by is not None:
        where.append("created_by = ?")
        params.append(int(created_by))
    return _rows(f"SELECT * FROM contacts WHERE {' AND '.join(where)} ORDER BY name, id",
                 tuple(params))


# ---------------------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------------------
AGENT_FIELDS = ("name", "slug", "kind", "description", "prompt_template", "trigger_template",
                "outcome_enum", "extra_fields", "voice_name", "speech_language_code",
                "language_mode", "listen_seconds", "requires_event", "active")


def create_agent(name: str, prompt_template: str, wedding_id=None, created_by=None, **fields) -> int:
    now = _now()
    fields = {**fields, "name": name, "prompt_template": prompt_template}
    cols = ["wedding_id", "created_by", "created_at", "updated_at"]
    vals = [int(wedding_id) if wedding_id is not None else None, created_by, now, now]
    for key in AGENT_FIELDS:
        if key in fields and fields[key] is not None:
            cols.append(key)
            vals.append(fields[key])
    return _exec(f"INSERT INTO agents ({', '.join(cols)}) "
                 f"VALUES ({', '.join('?' * len(cols))})", tuple(vals))


def get_agent(agent_id) -> dict | None:
    if agent_id in (None, ""):
        return None
    return _one("SELECT * FROM agents WHERE id = ?", (int(agent_id),))


def get_agent_by_slug(slug: str, wedding_id=None) -> dict | None:
    """A wedding's own override of a slug wins over the global template of the same slug."""
    if wedding_id is not None:
        own = _one("SELECT * FROM agents WHERE wedding_id = ? AND slug = ?",
                   (int(wedding_id), slug))
        if own:
            return own
    return _one("SELECT * FROM agents WHERE wedding_id IS NULL AND slug = ?", (slug,))


def list_agents(wedding_id=None, active_only=True) -> list[dict]:
    """The agents usable by a wedding: its own rows plus the global templates."""
    where = ["(wedding_id IS NULL"]
    params = []
    if wedding_id is not None:
        where[0] += " OR wedding_id = ?"
        params.append(int(wedding_id))
    where[0] += ")"
    if active_only:
        where.append("active = 1")
    return _rows(f"SELECT * FROM agents WHERE {' AND '.join(where)} "
                 f"ORDER BY wedding_id IS NULL DESC, kind, name", tuple(params))


def all_agents() -> list[dict]:
    """EVERY agent row across every wedding.

    list_agents() answers "what may THIS wedding use" and so returns only the globals plus
    one wedding's own. Maintenance work — the stale-prompt scan, --refresh-agents — has to
    see every row, including a copy belonging to a wedding it was not asked about."""
    return _rows("SELECT * FROM agents ORDER BY wedding_id IS NULL DESC, wedding_id, kind, name")


def update_agent(agent_id: int, **fields) -> int:
    sets, params = [], []
    for key in AGENT_FIELDS:
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if not sets:
        return 0
    sets.append("updated_at = ?")
    params.append(_now())
    return _exec(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?",
                 tuple(params) + (int(agent_id),))


def delete_agent(agent_id: int) -> int:
    return _exec("DELETE FROM agents WHERE id = ?", (int(agent_id),))


def fallback_agent() -> dict | None:
    """The agent a call falls back to when its own could not be resolved — the seeded
    reminder template. Returning None makes GeminiLive use its identity-free prompt."""
    return get_agent_by_slug("event_reminder")


# ---------------------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------------------
def active_campaign() -> dict | None:
    """The most recent campaign currently scheduled or live."""
    return _one("SELECT * FROM campaigns WHERE status IN ('scheduled','live') ORDER BY created_at DESC LIMIT 1")


def active_campaigns() -> list[dict]:
    """Every scheduled/live campaign. A wedding day legitimately runs several at once
    (six reminder campaigns on the same afternoon), so callers cap on a budget rather
    than assuming there is only one."""
    return _rows("SELECT * FROM campaigns WHERE status IN ('scheduled','live') "
                 "ORDER BY start_at, id")


def create_campaign(name, start_at, created_by, callback_delay_hours,
                    callback_max_per_day, callback_days, status="scheduled",
                    call_start_min=540, call_end_min=1260,
                    wedding_id=None, event_id=None, agent_id=None) -> int:
    now = _now()
    return _exec(
        "INSERT INTO campaigns (name, status, start_at, created_by, contact_count, "
        "callback_delay_hours, callback_max_per_day, callback_days, call_start_min, call_end_min, "
        "wedding_id, event_id, agent_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, status, start_at, created_by, 0,
         int(callback_delay_hours), int(callback_max_per_day), int(callback_days),
         int(call_start_min), int(call_end_min),
         int(wedding_id) if wedding_id is not None else None,
         int(event_id) if event_id is not None else None,
         int(agent_id) if agent_id is not None else None,
         now, now),
    )


def add_campaign_contacts(campaign_id: int, contacts) -> int:
    """contacts: iterable of rows/dicts with id, phone, name. Returns final count."""
    contacts = list(contacts)
    now = _now()
    conn = get_conn()
    with _lock:
        conn.executemany(
            "INSERT INTO campaign_contacts (campaign_id, contact_id, phone, name, call_status, "
            "attempts, day_attempts, created_at, updated_at) VALUES (?,?,?,?, 'pending', 0, 0, ?, ?)",
            [(campaign_id, c.get("id"), c.get("phone"), c.get("name"), now, now) for c in contacts],
        )
        n = conn.execute("SELECT COUNT(*) FROM campaign_contacts WHERE campaign_id = ?", (campaign_id,)).fetchone()[0]
        conn.execute("UPDATE campaigns SET contact_count = ?, updated_at = ? WHERE id = ?", (n, now, campaign_id))
        conn.commit()
    return n


def list_campaigns(q=None, sort="created_at", direction="desc", limit=50, offset=0, created_by=None):
    where, params = [], []
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    if created_by is not None:
        where.append("created_by = ?")
        params.append(int(created_by))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    col = sort if sort in _CAMPAIGN_SORTS else "created_at"
    dir_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    total = _one(f"SELECT COUNT(*) c FROM campaigns {wsql}", tuple(params))["c"]
    rows = _rows(
        f"SELECT * FROM campaigns {wsql} ORDER BY {col} {dir_sql} LIMIT ? OFFSET ?",
        tuple(params) + (int(limit), int(offset)),
    )
    return {"items": rows, "total": int(total)}


def campaign_ids_by_owner(user_id) -> list:
    return [r["id"] for r in _rows("SELECT id FROM campaigns WHERE created_by = ?", (int(user_id),))]


def campaign_progress(campaign_id: int) -> dict:
    rows = _rows(
        "SELECT call_status, COUNT(*) n FROM campaign_contacts WHERE campaign_id = ? GROUP BY call_status",
        (int(campaign_id),),
    )
    return {r["call_status"]: r["n"] for r in rows}


# A "success" outcome differs per agent: a reminder agent records 'acknowledged', a logistics
# agent 'confirmed', and the legacy RSVP vocabulary used 'yes'. The dashboard counts any of them.
POSITIVE_OUTCOMES = ("yes", "acknowledged", "confirmed")
_POSITIVE_SQL = "rsvp_outcome IN ({})".format(",".join("?" * len(POSITIVE_OUTCOMES)))


def campaign_rsvp_yes_unique(campaign_id: int) -> int:
    """Unique-by-phone positive-outcome count for one campaign (a repeat from the same number
    counts once)."""
    r = _rows(
        "SELECT COUNT(DISTINCT phone) n FROM campaign_contacts "
        f"WHERE campaign_id = ? AND {_POSITIVE_SQL}",
        (int(campaign_id), *POSITIVE_OUTCOMES),
    )
    return int(r[0]["n"]) if r else 0


def rsvp_yes_unique_totals(campaign_ids=None) -> dict:
    """Dashboard rollup: unique-by-phone-within-each-campaign counts, summed across campaigns
    (the same number in two campaigns counts once per campaign). campaign_ids=None → all campaigns."""
    where, params = "", []
    if campaign_ids is not None:
        ids = [int(i) for i in campaign_ids]
        if not ids:
            return {"yes": 0, "responded": 0}
        where = f" AND campaign_id IN ({','.join('?' * len(ids))})"
        params = ids

    def _count(cond: str, cond_params=()) -> int:
        r = _rows(
            "SELECT COUNT(*) n FROM (SELECT campaign_id, phone FROM campaign_contacts "
            f"WHERE {cond}{where} GROUP BY campaign_id, phone)",
            (*cond_params, *params),
        )
        return int(r[0]["n"]) if r else 0

    return {
        "yes": _count(_POSITIVE_SQL, POSITIVE_OUTCOMES),
        "responded": _count("rsvp_outcome IS NOT NULL AND rsvp_outcome <> ''"),
    }


def get_campaign_full(campaign_id: int) -> dict | None:
    c = get_campaign(campaign_id)
    if not c:
        return None
    c["progress"] = campaign_progress(campaign_id)
    return c


def set_campaign_status(campaign_id: int, status: str) -> None:
    _exec("UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), int(campaign_id)))


def cancel_campaign(campaign_id: int) -> bool:
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE campaigns SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('scheduled','live')",
            (_now(), int(campaign_id)),
        )
        conn.commit()
        return cur.rowcount > 0


# Campaign runner support (used by campaign_runner.py)
def promote_due_campaigns(now_iso: str) -> int:
    """Flip scheduled campaigns whose start time has arrived to 'live'."""
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE campaigns SET status = 'live', updated_at = ? WHERE status = 'scheduled' AND start_at <= ?",
            (_now(), now_iso),
        )
        conn.commit()
        return cur.rowcount


def live_campaigns() -> list[dict]:
    return _rows("SELECT * FROM campaigns WHERE status = 'live' ORDER BY created_at ASC")


def cc_pending_due(campaign_id: int, now_iso: str, limit: int) -> list[dict]:
    return _rows(
        "SELECT * FROM campaign_contacts WHERE campaign_id = ? AND call_status = 'pending' "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id ASC LIMIT ?",
        (int(campaign_id), now_iso, int(limit)),
    )


def cc_by_status(campaign_id: int, status: str) -> list[dict]:
    return _rows(
        "SELECT * FROM campaign_contacts WHERE campaign_id = ? AND call_status = ? ORDER BY id ASC",
        (int(campaign_id), status),
    )


def list_campaign_contacts(campaign_id, status=None, limit=500, offset=0, q=None):
    where, params = ["campaign_id = ?"], [int(campaign_id)]
    if status:
        where.append("call_status = ?")
        params.append(status)
    if q:
        where.append("(name LIKE ? OR phone LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    wsql = "WHERE " + " AND ".join(where)
    total = _one(f"SELECT COUNT(*) c FROM campaign_contacts {wsql}", tuple(params))["c"]
    rows = _rows(
        f"SELECT * FROM campaign_contacts {wsql} ORDER BY id ASC LIMIT ? OFFSET ?",
        tuple(params) + (int(limit), int(offset)),
    )
    return {"items": rows, "total": int(total)}


def cc_open_count(campaign_id: int) -> int:
    r = _one(
        "SELECT COUNT(*) c FROM campaign_contacts WHERE campaign_id = ? AND call_status IN ('pending','calling')",
        (int(campaign_id),),
    )
    return int(r["c"]) if r else 0


def cc_upcoming(campaign_ids=None, limit=200):
    """The "Callback attempts" grid on the Scheduler: contacts DIALED at least once
    (attempts>0) — the automatic no-answer retries and their history — across ANY campaign
    status. Contacts never dialed yet (attempts=0) are excluded; they enter this view once
    their first dial happens. Open items (pending retries) sort first (soonest next-attempt),
    then done/failed history by most-recent attempt. campaign_ids=None → all campaigns
    (Superadmin); an explicit (possibly empty) list scopes to an owner's campaigns."""
    where = ["cc.attempts > 0"]
    params = []
    if campaign_ids is not None:
        if not campaign_ids:
            return {"items": [], "total": 0}
        ph = ",".join("?" for _ in campaign_ids)
        where.append(f"cc.campaign_id IN ({ph})")
        params.extend(int(i) for i in campaign_ids)
    base = ("FROM campaign_contacts cc JOIN campaigns c ON c.id = cc.campaign_id "
            "WHERE " + " AND ".join(where))
    total = _one(f"SELECT COUNT(*) AS n {base}", tuple(params))["n"]
    rows = _rows(
        "SELECT cc.*, c.name AS campaign_name, c.status AS campaign_status, "
        "c.start_at AS campaign_start_at, c.callback_max_per_day AS campaign_max_per_day, "
        "c.callback_days AS campaign_days, "
        "c.call_start_min AS campaign_call_start_min, c.call_end_min AS campaign_call_end_min "
        f"{base} "
        "ORDER BY (cc.call_status IN ('pending','calling')) DESC, "
        "(cc.next_attempt_at IS NULL) DESC, cc.next_attempt_at ASC, "
        "cc.last_attempt_at DESC, cc.id ASC LIMIT ?",
        tuple(params) + (int(limit),))
    return {"items": rows, "total": int(total)}


def get_campaign_contact(cc_id) -> dict | None:
    return _one("SELECT * FROM campaign_contacts WHERE id = ?", (int(cc_id),))


def cc_update(cc_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE campaign_contacts SET {cols} WHERE id = ?", tuple(fields.values()) + (int(cc_id),))


def cc_set_outcome_by_phone(campaign_id: int, phone: str, outcome: str,
                            mark_done: bool = False, remark=None) -> int:
    """Overwrite rsvp_outcome for the most-recent campaign_contacts row matching
    (campaign_id, phone). Used by callback-result back-propagation and by manual RSVP
    edits. mark_done=True also finalises the contact (call_status='done', retries
    stopped) — used when an admin sets a FINAL outcome by hand. `remark` fills the
    contact's remark ONLY when it is empty (a human edit is never overwritten).
    Returns rowcount (0 if no match)."""
    conn = get_conn()
    with _lock:
        row = conn.execute(
            "SELECT id, remark FROM campaign_contacts WHERE campaign_id = ? AND phone = ? "
            "ORDER BY id DESC LIMIT 1", (int(campaign_id), phone)).fetchone()
        if not row:
            return 0
        sets, params = ["rsvp_outcome = ?", "updated_at = ?"], [outcome, _now()]
        if mark_done:
            sets += ["call_status = 'done'", "next_attempt_at = NULL"]
        if remark and not (row["remark"] or "").strip():
            sets.append("remark = ?")
            params.append(str(remark))
        cur = conn.execute(
            f"UPDATE campaign_contacts SET {', '.join(sets)} WHERE id = ?",
            tuple(params) + (int(row["id"]),))
        conn.commit()
        return cur.rowcount


def recent_call_counts(phones, since_iso: str) -> dict:
    """{phone: {calls, last_attempt_at}} for numbers dialled since `since_iso`.

    One grouped query rather than a lookup per guest — this runs while the operator waits
    on the confirm dialog with the whole guest list selected."""
    phones = [str(p) for p in phones if p]
    if not phones or not since_iso:
        return {}
    out = {}
    # SQLite caps host parameters (999 by default), so chunk a large guest list.
    for i in range(0, len(phones), 500):
        chunk = phones[i:i + 500]
        holes = ",".join("?" * len(chunk))
        rows = _rows(
            f"SELECT phone, COUNT(*) calls, MAX(last_attempt_at) last_attempt_at "
            f"FROM campaign_contacts "
            f"WHERE phone IN ({holes}) AND last_attempt_at IS NOT NULL AND last_attempt_at >= ? "
            f"GROUP BY phone",
            (*chunk, str(since_iso)))
        for r in rows:
            out[r["phone"]] = {"calls": int(r["calls"]),
                               "last_attempt_at": r["last_attempt_at"]}
    return out


def cc_find_recent_by_phone(phone: str, now=None) -> dict | None:
    """Most-relevant campaign contact for a phone number — the inbound caller-ID
    lookup ("who is calling us back, and what happened on our last attempt?").
    Prefers a contact in an ACTIVE (scheduled/live) campaign, then the most recent
    row overall. Returns the contact joined with its campaign's name/status.

    A CANCELLED campaign is not a conversation we had — its contacts never count. And a
    call-back is only a call-back for a while: a row older than EO_INBOUND_HISTORY_DAYS
    (default 7) is ignored, so a guest we rang in a test weeks ago is greeted with the
    normal opening, not "we already spoke"."""
    try:
        days = float(os.getenv("EO_INBOUND_HISTORY_DAYS", "7") or 7)
    except ValueError:
        days = 7.0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    return _one(
        "SELECT cc.*, c.name AS campaign_name, c.status AS campaign_status "
        "FROM campaign_contacts cc JOIN campaigns c ON c.id = cc.campaign_id "
        "WHERE cc.phone = ? AND c.status <> 'cancelled' "
        "AND COALESCE(cc.last_attempt_at, cc.updated_at, cc.created_at) >= ? "
        "ORDER BY (c.status IN ('scheduled','live')) DESC, cc.id DESC LIMIT 1",
        (str(phone), cutoff),
    )


# Campaigns: read helpers for call-log labelling
def get_campaign(campaign_id: int) -> dict | None:
    return _one("SELECT * FROM campaigns WHERE id = ?", (int(campaign_id),))


def campaign_names(ids) -> dict:
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = _rows(f"SELECT id, name FROM campaigns WHERE id IN ({placeholders})", tuple(ids))
    return {r["id"]: r["name"] for r in rows}


def campaign_meta(ids) -> dict:
    """id -> {name, created_at}. Used to label a call with its campaign ONLY when the
    call happened at/after the campaign was created — so call records that survived a DB
    reset don't get mislabelled by a new campaign that reused their old id."""
    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = _rows(f"SELECT id, name, created_at FROM campaigns WHERE id IN ({placeholders})", tuple(ids))
    return {r["id"]: {"name": r["name"], "created_at": r["created_at"]} for r in rows}


def names_by_campaign_phone(pairs) -> dict:
    """(campaign_id, phone) -> contact name (the name used to greet), for the given pairs.
    Newest row wins per pair. Batch lookup so a call list resolves in one query."""
    pairs = [(int(c), str(p)) for c, p in pairs if c and p]
    if not pairs:
        return {}
    cids = sorted({c for c, _ in pairs})
    phones = sorted({p for _, p in pairs})
    cph = ",".join("?" * len(cids))
    pph = ",".join("?" * len(phones))
    rows = _rows(
        f"SELECT campaign_id, phone, name FROM campaign_contacts "
        f"WHERE campaign_id IN ({cph}) AND phone IN ({pph}) ORDER BY id ASC",
        tuple(cids) + tuple(phones))
    wanted, out = set(pairs), {}
    for r in rows:                                    # ORDER BY id ASC → later row overwrites = newest
        key = (int(r["campaign_id"]), str(r["phone"]))
        if key in wanted and (r.get("name") or "").strip():
            out[key] = r["name"]
    return out


def phones_by_name_query(q: str) -> set:
    """Phones whose contact NAME matches q, across the contacts pool and every campaign's
    recipient names. Powers name search on call grids (call records store only the phone)."""
    q = (q or "").strip()
    if not q:
        return set()
    like = f"%{q}%"
    rows = _rows(
        "SELECT phone FROM contacts WHERE name LIKE ? "
        "UNION SELECT phone FROM campaign_contacts WHERE name LIKE ?",
        (like, like))
    return {r["phone"] for r in rows if r.get("phone")}


def names_by_phone(phones) -> dict:
    """phone -> contact name, looked up across ALL pools (display labelling only — call
    visibility itself is campaign-scoped). With per-user pools a phone can exist in several
    pools; newest row wins deterministically (ORDER BY id ASC → later overwrite = newest)."""
    phones = sorted({str(p) for p in phones if p})
    if not phones:
        return {}
    ph = ",".join("?" * len(phones))
    rows = _rows(f"SELECT phone, name FROM contacts WHERE phone IN ({ph}) ORDER BY id ASC", tuple(phones))
    return {r["phone"]: r["name"] for r in rows if (r.get("name") or "").strip()}


# ---------------------------------------------------------------------------------------
# Settings (key -> JSON), audit log, and the go-live data reset
# ---------------------------------------------------------------------------------------
_settings_ready_conn = None


def _ensure_settings_table() -> None:
    """The table exists after init(); this covers code paths (tests, tools) that never ran it."""
    global _settings_ready_conn
    conn = get_conn()
    if _settings_ready_conn is conn:
        return
    with _lock:
        conn.execute(_SETTINGS_SQL)
        conn.commit()
    _settings_ready_conn = conn


def get_setting(key: str, default=None):
    _ensure_settings_table()
    r = _one("SELECT value FROM settings WHERE key = ?", (str(key),))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value, updated_by: str = "") -> None:
    _ensure_settings_table()
    _exec("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
          "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
          "updated_by = excluded.updated_by",
          (str(key), json.dumps(value, ensure_ascii=False, default=str), _now(), updated_by or ""))


def delete_setting(key: str) -> None:
    _ensure_settings_table()
    _exec("DELETE FROM settings WHERE key = ?", (str(key),))


def add_audit(user_id=None, username="", action="", target="", detail="", ip="") -> int:
    return _exec(
        "INSERT INTO audit_log (user_id, username, action, target, detail, ip, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, username or "", action or "", target or "", detail or "", ip or "", _now()))


def list_audit(q=None, action=None, user_id=None, date_from=None, date_to=None,
               limit=100, offset=0) -> dict:
    where, params = [], []
    if q:
        like = f"%{q}%"
        where.append("(username LIKE ? OR action LIKE ? OR target LIKE ? OR detail LIKE ?)")
        params += [like] * 4
    if action:
        where.append("action = ?")
        params.append(str(action))
    if user_id not in (None, ""):
        where.append("user_id = ?")
        params.append(int(user_id))
    if date_from:
        where.append("substr(created_at,1,10) >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        where.append("substr(created_at,1,10) <= ?")
        params.append(str(date_to)[:10])
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT COUNT(*) c FROM audit_log {wsql}", tuple(params))["c"]
    rows = _rows(f"SELECT * FROM audit_log {wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
                 tuple(params) + (int(limit), int(offset)))
    return {"items": rows, "total": int(total)}


def audit_actions() -> list:
    return [r["action"] for r in _rows("SELECT DISTINCT action FROM audit_log ORDER BY action")]


def data_counts() -> dict:
    """What a go-live reset would remove, table by table."""
    def n(table):
        return int(_one(f"SELECT COUNT(*) c FROM {table}")["c"])
    return {"weddings": n("weddings"), "events": n("events"), "contacts": n("contacts"),
            "campaigns": n("campaigns"), "campaign_contacts": n("campaign_contacts"),
            "audit": n("audit_log")}


def backup_db(dest_path: str) -> None:
    """A consistent copy of the whole database (SQLite's online backup), taken before a reset."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    src = get_conn()
    with _lock:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()


def wipe_data(outbound=False, weddings=False, audit=False) -> dict:
    """Delete test data before go-live, in one transaction. ALWAYS keeps users, settings
    and the shipped global agents. Returns the rows removed per table.

    Ordering matters. events and per-wedding agents carry a real FK to weddings
    (ON DELETE CASCADE), but campaigns.wedding_id/event_id/agent_id and contacts.wedding_id
    are bare INTEGERs with NO foreign key, so SQLite will not clean them up — they would be
    left pointing at rows that no longer exist. Children go first, and contacts.wedding_id
    resets to 0 rather than NULL because UNIQUE(created_by, wedding_id, phone) relies on it
    (a NULL there stops ON CONFLICT ever firing)."""
    conn = get_conn()
    out, sequences = {}, []
    with _lock:
        try:
            if outbound:
                out["campaign_contacts"] = conn.execute("DELETE FROM campaign_contacts").rowcount
                out["campaigns"] = conn.execute("DELETE FROM campaigns").rowcount
                out["contacts"] = conn.execute("DELETE FROM contacts").rowcount
                sequences += ["campaign_contacts", "campaigns", "contacts"]
            if weddings:
                # any campaign still pointing at a wedding we are about to remove
                if not outbound:
                    conn.execute("DELETE FROM campaign_contacts WHERE campaign_id IN "
                                 "(SELECT id FROM campaigns WHERE wedding_id IS NOT NULL)")
                    conn.execute("DELETE FROM campaigns WHERE wedding_id IS NOT NULL")
                    conn.execute("UPDATE contacts SET wedding_id = 0 WHERE wedding_id <> 0")
                # per-wedding agent copies go with their wedding; the global (wedding_id IS
                # NULL) templates are the product and must survive.
                out["agents"] = conn.execute(
                    "DELETE FROM agents WHERE wedding_id IS NOT NULL").rowcount
                out["events"] = conn.execute("DELETE FROM events").rowcount
                out["weddings"] = conn.execute("DELETE FROM weddings").rowcount
                sequences += ["events", "weddings"]
            if audit:
                out["audit"] = conn.execute("DELETE FROM audit_log").rowcount
                sequences.append("audit_log")
            if sequences:
                try:                                   # ids start again at 1 (campaign #1, not #37)
                    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({','.join('?' * len(sequences))})",
                                 tuple(sequences))
                except sqlite3.OperationalError:
                    pass                               # no AUTOINCREMENT row written yet
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return out
