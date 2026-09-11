"""eo_db.py — per-user contacts: legacy migration, (owner, phone) upsert, scoped access."""


def _seed_users(eo_db):
    admin = eo_db.create_user("admin", "Admin", "h", "s", role="eo_admin")
    agent = eo_db.create_user("agent", "Agent", "h", "s", role="eo_agent")
    return admin, agent


def test_fresh_schema_has_every_table_and_is_stamped(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    conn = eo_db.get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert {"users", "weddings", "events", "agents",
            "contacts", "campaigns", "campaign_contacts"} <= tables
    assert eo_db.schema_version() == eo_db.SCHEMA_VERSION
    eo_db.init()                                     # idempotent
    assert eo_db.schema_version() == eo_db.SCHEMA_VERSION


def test_shipped_agents_are_seeded_once(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    slugs = sorted(a["slug"] for a in eo_db.list_agents())
    assert slugs == ["event_reminder", "logistics_concierge"]
    # the seeds are global so every wedding can use them without copying
    assert all(a["wedding_id"] is None for a in eo_db.list_agents())
    eo_db.init()
    assert len(eo_db.list_agents()) == 2             # never duplicated


def test_seeding_never_clobbers_an_operator_edit(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    agent = eo_db.get_agent_by_slug("event_reminder")
    eo_db.update_agent(agent["id"], prompt_template="MY EDITED PROMPT")
    eo_db.init()
    assert eo_db.get_agent_by_slug("event_reminder")["prompt_template"] == "MY EDITED PROMPT"


def test_a_wedding_agent_overrides_the_global_of_the_same_slug(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    admin, _ = _seed_users(eo_db)
    wid = eo_db.create_wedding("W", created_by=admin)
    eo_db.create_agent("Custom Reminder", "OVERRIDE", wedding_id=wid, slug="event_reminder")
    assert eo_db.get_agent_by_slug("event_reminder", wedding_id=wid)["prompt_template"] == "OVERRIDE"
    # other weddings still get the global template
    assert eo_db.get_agent_by_slug("event_reminder")["prompt_template"] != "OVERRIDE"


def test_same_phone_lives_in_two_weddings_under_one_owner(fresh_eo_db):
    """A repeat-client planner may have the same aunt at two weddings; the second import
    must not move or overwrite the first."""
    eo_db = fresh_eo_db
    eo_db.init()
    admin, _ = _seed_users(eo_db)
    w1 = eo_db.create_wedding("W1", created_by=admin)
    w2 = eo_db.create_wedding("W2", created_by=admin)
    eo_db.bulk_upsert_contacts([("Aunt", "+919000000099", "valid")], created_by=admin, wedding_id=w1)
    eo_db.bulk_upsert_contacts([("Aunt", "+919000000099", "valid")], created_by=admin, wedding_id=w2)
    assert eo_db.count_contacts(created_by=admin) == 2


def test_reupload_updates_rather_than_duplicating(fresh_eo_db):
    """SQLite treats NULLs as distinct in a unique index, so wedding_id uses 0 rather than
    NULL — otherwise ON CONFLICT never fires and every re-upload doubles the guest list."""
    eo_db = fresh_eo_db
    eo_db.init()
    admin, _ = _seed_users(eo_db)
    wid = eo_db.create_wedding("W", created_by=admin)
    for _ in range(3):
        eo_db.bulk_upsert_contacts([("Rajesh", "+919000000098", "valid")],
                                   created_by=admin, wedding_id=wid)
    assert eo_db.count_contacts(created_by=admin) == 1
    # and with no wedding at all
    for _ in range(3):
        eo_db.bulk_upsert_contacts([("Loose", "+919000000097", "valid")], created_by=admin)
    assert eo_db.count_contacts(created_by=admin) == 2


def test_guest_fields_persist_and_a_blank_never_wipes_them(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    admin, _ = _seed_users(eo_db)
    wid = eo_db.create_wedding("W", created_by=admin)
    eo_db.bulk_upsert_contacts([("Rajesh", "+919000000096", "valid", {
        "side": "groom", "dietary": "vegetarian", "transport_mode": "flight",
        "transport_number": "AI 456", "hotel": "Fairmont", "guest_count": 3})],
        created_by=admin, wedding_id=wid)
    g = eo_db.guests_for_audience(wid, "groom", created_by=admin)[0]
    assert (g["side"], g["dietary"], g["transport_number"], g["hotel"], g["guest_count"]) == (
        "groom", "vegetarian", "AI 456", "Fairmont", 3)
    # a later sheet missing the hotel column must not blank the hotel we already have
    eo_db.bulk_upsert_contacts([("Rajesh", "+919000000096", "valid", {"side": "groom"})],
                               created_by=admin, wedding_id=wid)
    assert eo_db.guests_for_audience(wid, "groom", created_by=admin)[0]["hotel"] == "Fairmont"


def test_audience_filter_includes_both_and_unknown_sides(fresh_eo_db):
    """A guest whose side was never recorded must still get every reminder — a blank must
    not silently drop them from a function they were meant to attend."""
    eo_db = fresh_eo_db
    eo_db.init()
    admin, _ = _seed_users(eo_db)
    wid = eo_db.create_wedding("W", created_by=admin)
    eo_db.bulk_upsert_contacts([
        ("G", "+919000000090", "valid", {"side": "groom"}),
        ("B", "+919000000091", "valid", {"side": "bride"}),
        ("Both", "+919000000092", "valid", {"side": "both"}),
        ("Unknown", "+919000000093", "valid", {}),
    ], created_by=admin, wedding_id=wid)

    def names(aud):
        return sorted(g["name"] for g in eo_db.guests_for_audience(wid, aud, created_by=admin))

    assert names("all") == ["B", "Both", "G", "Unknown"]
    assert names("groom") == ["Both", "G", "Unknown"]
    assert names("bride") == ["B", "Both", "Unknown"]


def test_same_phone_lives_in_two_pools_and_upserts_within_one(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    admin, agent = _seed_users(eo_db)

    id_a, created_a = eo_db.add_contact("Raj", "+919825227503", created_by=admin)
    id_b, created_b = eo_db.add_contact("Raj bhai", "+919825227503", created_by=agent)
    assert created_a and created_b and id_a != id_b   # per-owner pools

    id_a2, created_a2 = eo_db.add_contact("Raj Updated", "+919825227503", created_by=admin)
    assert id_a2 == id_a and created_a2 is False      # same-pool upsert, no duplicate

    assert eo_db.count_contacts(created_by=admin) == 1
    assert eo_db.count_contacts(created_by=agent) == 1


def test_bulk_upsert_counts_and_conflicts_per_owner(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    admin, agent = _seed_users(eo_db)

    added, updated = eo_db.bulk_upsert_contacts(
        [("A", "+919000000001", "valid"), ("B", "+919000000002", "valid")], created_by=agent)
    assert (added, updated) == (2, 0)
    added, updated = eo_db.bulk_upsert_contacts(
        [("A2", "+919000000001", "valid"), ("C", "+919000000003", "valid")], created_by=agent)
    assert (added, updated) == (1, 1)
    # the ADMIN importing the same phone lands in the admin pool, not the agent's
    added, _ = eo_db.bulk_upsert_contacts([("A", "+919000000001", "valid")], created_by=admin)
    assert added == 1
    assert eo_db.count_contacts(created_by=agent) == 3


def test_scoped_get_delete_and_list(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    admin, agent = _seed_users(eo_db)
    id_admin, _ = eo_db.add_contact("AdminC", "+919000000010", created_by=admin)
    id_agent, _ = eo_db.add_contact("AgentC", "+919000000011", created_by=agent)

    # the IDOR fix: an agent asking for the admin's contact id gets nothing
    assert eo_db.get_contacts_by_ids([id_admin], created_by=agent) == []
    assert len(eo_db.get_contacts_by_ids([id_admin, id_agent], created_by=agent)) == 1
    assert len(eo_db.get_contacts_by_ids([id_admin, id_agent], created_by=None)) == 2

    # scoped delete: the agent cannot delete the admin's row
    assert eo_db.delete_contacts([id_admin], created_by=agent) == 0
    assert eo_db.delete_contacts([id_agent], created_by=agent) == 1
    assert eo_db.count_contacts() == 1

    assert eo_db.list_contacts(created_by=agent)["total"] == 0
    assert eo_db.list_contacts(created_by=None)["total"] == 1


def test_contact_remark_setter(fresh_eo_db):
    eo_db = fresh_eo_db
    eo_db.init()
    admin, _ = _seed_users(eo_db)
    cid, _ = eo_db.add_contact("R", "+919000000020", created_by=admin)
    eo_db.set_contact_remark(cid, "VIP member")
    assert eo_db.get_contact(cid)["remark"] == "VIP member"
