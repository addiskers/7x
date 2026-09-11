"""Round-robin fairness across concurrent campaigns.

The global dial budget (live.room()) is shared by every live campaign and consumed in
iteration order. With one campaign that never mattered; with six reminder batches on the
wedding afternoon, a fixed order means campaign #1 takes the whole budget every tick and
#2-#6 never dial at all. This is the bug the old one-campaign-at-a-time rule was hiding.
"""

import campaign_runner


def test_rotation_gives_each_campaign_the_first_claim_in_turn(monkeypatch):
    campaigns = [{"id": 1}, {"id": 2}, {"id": 3}]
    seen_first = []
    for tick in range(6):
        monkeypatch.setattr(campaign_runner, "_tick_count", tick)
        seen_first.append(campaign_runner._fair_order(campaigns)[0]["id"])
    # every campaign leads the queue an equal number of times
    assert seen_first == [1, 2, 3, 1, 2, 3]


def test_rotation_keeps_every_campaign_in_the_list(monkeypatch):
    """Rotating must never drop a campaign — only reorder."""
    campaigns = [{"id": i} for i in range(1, 7)]
    for tick in range(12):
        monkeypatch.setattr(campaign_runner, "_tick_count", tick)
        order = campaign_runner._fair_order(campaigns)
        assert sorted(c["id"] for c in order) == [1, 2, 3, 4, 5, 6]
        assert len(order) == 6


def test_a_single_campaign_is_left_alone(monkeypatch):
    monkeypatch.setattr(campaign_runner, "_tick_count", 7)
    assert campaign_runner._fair_order([{"id": 9}]) == [{"id": 9}]
    assert campaign_runner._fair_order([]) == []


def _six_campaign_world(eo_db):
    """Six live campaigns, 20 guests each — the wedding-afternoon shape."""
    from datetime import datetime, timezone
    eo_db.init()
    admin = eo_db.create_user("a", "A", "h", "s", role="eo_admin")
    wid = eo_db.create_wedding("W", created_by=admin)
    ev = eo_db.create_event(wid, "E", event_date="2026-09-21", start_time="10:30",
                            audience="all")
    agent = eo_db.get_agent_by_slug("event_reminder")
    past = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
    for i in range(1, 7):
        cid = eo_db.create_campaign(f"C{i}", past, admin, 4, 3, 1, status="live",
                                    call_start_min=0, call_end_min=1439,
                                    wedding_id=wid, event_id=ev, agent_id=agent["id"])
        eo_db.add_campaign_contacts(cid, [
            {"id": None, "phone": f"+919{i}{n:07d}", "name": f"G{n}"} for n in range(20)])


def _run_ticks(eo_db, monkeypatch, ticks=10, rotate=True):
    """Drive the real _tick loop with a contended live-call budget, returning which
    campaigns actually got dialled."""
    import asyncio

    import live
    import scheduler

    monkeypatch.setenv("MAX_LIVE_CALLS", "2")
    monkeypatch.setenv("EO_CAMPAIGN_MAX_PER_TICK", "5")
    # _process_campaign refuses to dial without telephony configured
    monkeypatch.setenv("PLIVO_AUTH_ID", "test")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "test")
    monkeypatch.setenv("PLIVO_FROM_NUMBER", "+10000000000")
    monkeypatch.setattr(scheduler, "is_enabled", lambda: True)
    if not rotate:
        monkeypatch.setattr(campaign_runner, "_fair_order", lambda cs: cs)

    dialled = []

    async def fake_place_call(phone, **kw):
        dialled.append(kw.get("campaign_id"))
        live.inc()                       # the call connects and HOLDS a slot
        return {"success": True, "call_uuid": "u"}

    monkeypatch.setattr(campaign_runner.dialer, "place_call", fake_place_call)

    async def run():
        for _ in range(ticks):
            await campaign_runner._tick()
            while live.count():          # last tick's calls hang up, freeing the slots
                live.dec()

    asyncio.run(run())
    return dialled


def test_a_fixed_order_starves_every_campaign_but_the_first_few(fresh_eo_db, monkeypatch):
    """The bug the one-campaign-at-a-time rule was hiding. Kept as a test so nobody
    'simplifies' the rotation away."""
    _six_campaign_world(fresh_eo_db)
    dialled = _run_ticks(fresh_eo_db, monkeypatch, rotate=False)
    starved = {1, 2, 3, 4, 5, 6} - set(dialled)
    assert starved, "expected the fixed order to starve the tail campaigns"


def test_rotation_lets_every_campaign_dial(fresh_eo_db, monkeypatch):
    """Six reminder batches on the wedding afternoon must all make progress."""
    _six_campaign_world(fresh_eo_db)
    dialled = _run_ticks(fresh_eo_db, monkeypatch, rotate=True)
    assert set(dialled) == {1, 2, 3, 4, 5, 6}, f"starved: {{1,2,3,4,5,6}} - {set(dialled)}"


def test_the_active_cap_is_configurable(monkeypatch):
    monkeypatch.delenv("EO_MAX_ACTIVE_CAMPAIGNS", raising=False)
    assert campaign_runner._max_active_campaigns() == 6      # a full wedding day
    monkeypatch.setenv("EO_MAX_ACTIVE_CAMPAIGNS", "12")
    assert campaign_runner._max_active_campaigns() == 12
    monkeypatch.setenv("EO_MAX_ACTIVE_CAMPAIGNS", "nonsense")
    assert campaign_runner._max_active_campaigns() == 6      # never crash on a bad value
    monkeypatch.setenv("EO_MAX_ACTIVE_CAMPAIGNS", "0")
    assert campaign_runner._max_active_campaigns() == 1      # always allow at least one


def test_the_api_and_the_runner_share_one_cap(monkeypatch):
    """Two entry points create campaigns (POST /campaigns and Call-now); they must not
    disagree about the limit."""
    import eo_api
    monkeypatch.setenv("EO_MAX_ACTIVE_CAMPAIGNS", "4")
    assert eo_api._max_active_campaigns() == campaign_runner._max_active_campaigns() == 4
