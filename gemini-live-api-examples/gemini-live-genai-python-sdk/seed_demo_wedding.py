"""Seed the Ved & Riya wedding from the client's 25-September brief.

Idempotent — re-running updates the same wedding and events rather than duplicating.

    python seed_demo_wedding.py                      # seed
    python seed_demo_wedding.py --phone +9198...     # seed + set the sample guest's number
    python seed_demo_wedding.py --preview sufi       # print the rendered script and exit
    python seed_demo_wedding.py --list               # show the event keys
    python seed_demo_wedding.py --delete-wedding 1   # remove a previous wedding + its guests
"""

import argparse
import sys

# --preview prints a rendered script, and those now contain Devanagari and Gujarati
# examples from the LANGUAGE block. A Windows console defaults to cp1252 and raises
# UnicodeEncodeError on them, so reconfigure before anything is printed — same guard as
# main._utf8_console, kept here because this script does not import main.
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass                      # a redirected or closed stream is not worth failing over

import eo_auth
import eo_db
import prompt_render

WEDDING_NAME = "Ved & Riya"

# The evening of 25 September. All three functions are open to every guest — this brief has
# no groom/bride-side split, so audience is "all" throughout and {schedule} shows all three
# to everyone.
#
# Times are 24h "HH:MM" and are converted to speech by prompt_render._spoken_time, so
# "23:00" becomes "eleven at night". Never write them as "11:00 PM" here.
#
# (key, name, date, start "HH:MM", venue, audience)
EVENTS = [
    ("hitea",      "Hi-Tea",      "2026-09-25", "16:00", "Harvest",    "all"),
    ("sufi",       "Sufi Night",  "2026-09-25", "19:00", "Great Park", "all"),
    ("afterparty", "After Party", "2026-09-25", "23:00", "Ballroom",   "all"),
]

# The highlights for each function. This is the ONLY per-event free text the agent speaks,
# and it is injected verbatim — so times inside it must be written longhand as prose,
# because _spoken_time never touches it.
#
# Note build_schedule() does NOT read this: an announcement is spoken in full only for the
# function the call is actually about. The other two remain answerable by name, time and
# venue from {schedule}.
ANNOUNCEMENTS = {
    "hitea": "Evening refreshments, light snacks and drinks, and a chance to meet the other "
             "guests. It runs from four until six.",
    "sufi":  "A grand welcome with an ittar shower and a gajra, a mocktail bar, and an "
             "interactive perfume-making experience. The couple enter at quarter past eight, "
             "and Shadab Faridi performs live Sufi music from half past eight. Dinner is "
             "served through the evening.",
    "afterparty": "A DJ night with DJ Alex performing live, a bar, and late-night "
                  "celebrations. Supper is served from half past eleven.",
}

# The phone numbers the sample GUESTS below (and the removed seed_ved_riya_campaign.py) put
# on a wedding. Made up — they may belong to real strangers — so a whole-wedding campaign,
# which ticks every guest, must never dial them.
SAMPLE_PHONES = ("+919876543210", "+919876543211", "+919876543212", "+919876543213",
                 "+919876543214")

# Sample guests so the Test panel has someone to render against, and so a dry-run
# campaign has a handful of rows. Replace with the real guest list via
# Create Campaign -> upload. --phone overrides the first one's number.
#
# (name, phone, side, dietary, transport_mode, transport_number, arrival, departure, hotel)
GUESTS = [
    ("Rajesh Kumar", "+919876543210", "", "vegetarian", "", "",
     "25 September", "26 September", ""),
    ("Priya Singh",   "+919876543211", "", "", "", "", "", "", ""),
    ("Amit Patel",    "+919876543212", "", "", "", "", "", "", ""),
    ("Neha Sharma",   "+919876543213", "", "", "", "", "", "", ""),
    ("Vikram Desai",  "+919876543214", "", "", "", "", "", "", ""),
]


def _owner():
    eo_auth.seed_admin()
    for u in eo_db.list_users():
        if u["role"] == "eo_admin":
            return u["id"]
    raise SystemExit("No admin user; check EO_ADMIN_USER / EO_ADMIN_PASS.")


def seed(phone=None):
    eo_db.init()
    owner = _owner()

    # hospitality_team is spoken verbatim in the greeting — "Hey, I'm speaking from
    # {hospitality_team}" — so it must read as a name with no article in front of it and
    # no "&" (the agent would have to spell it out).
    fields = dict(
        groom_name="Ved", bride_name="Riya",
        start_date="2026-09-25", end_date="2026-09-25", city="",
        hospitality_team="Ved and Riya's Hospitality Team",
        placard_text="Ved & Riya Wedding — Welcome",
        contact_phone="", contact_name="",
        notes="Evening of 25 September 2026: Hi-Tea, Sufi Night, After Party.",
    )
    existing = [w for w in eo_db.list_weddings() if w["name"] == WEDDING_NAME]
    if existing:
        wid = existing[0]["id"]
        eo_db.update_wedding(wid, **fields)
        print(f"Wedding #{wid} '{WEDDING_NAME}' updated")
    else:
        wid = eo_db.create_wedding(WEDDING_NAME, created_by=owner, **fields)
        print(f"Wedding #{wid} '{WEDDING_NAME}' created")

    by_name = {e["name"]: e for e in eo_db.list_events(wid)}
    keys = {}
    for order, (key, name, edate, start, venue, audience) in enumerate(EVENTS):
        common = dict(event_date=edate, start_time=start, venue=venue,
                      audience=audience, sort_order=order,
                      announcement=ANNOUNCEMENTS.get(key, ""))
        if name in by_name:
            eid = by_name[name]["id"]
            eo_db.update_event(eid, **common)
        else:
            eid = eo_db.create_event(wid, name, **common)
        keys[key] = eid
    print(f"{len(EVENTS)} events seeded")

    rows = []
    for name, default_phone, side, diet, mode, number, arrival, departure, hotel in GUESTS:
        rows.append((name, phone or default_phone, "valid", {
            "side": side, "dietary": diet, "transport_mode": mode,
            "transport_number": number, "arrival_at": arrival,
            "departure_at": departure, "hotel": hotel}))
    added, updated = eo_db.bulk_upsert_contacts(rows, source="manual",
                                                created_by=owner, wedding_id=wid)
    print(f"guests: {added} added, {updated} updated"
          + (f" (phone set to {phone})" if phone else ""))

    reminder = eo_db.get_agent_by_slug("event_reminder")
    logistics = eo_db.get_agent_by_slug("logistics_concierge")
    if not reminder or not logistics:
        raise SystemExit("The shipped agents are missing from this database. "
                         "Run: python seed_demo_wedding.py --refresh-agents")
    print(f"agents: reminder=#{reminder['id']}  logistics=#{logistics['id']}")
    return wid, keys, owner, reminder, logistics


def delete_wedding(wid, assume_yes=False):
    """Remove a wedding, its events, its agent copies AND its guests.

    eo_db.delete_wedding cascades to events and agents (both carry a FK with ON DELETE
    CASCADE), but contacts and campaigns do NOT have a foreign key to weddings — they
    would be left behind with a dangling wedding_id, invisible in the UI and ready to
    collide with a fresh import on UNIQUE(created_by, wedding_id, phone). So the guests
    are deleted explicitly here.

    Matched by ID, never by name: the live row is 'Anant & Manya' while the old script
    looked for 'Manya & Anant', so a name match would silently delete nothing."""
    eo_db.init()
    wedding = eo_db.get_wedding(wid)
    if not wedding:
        raise SystemExit(f"No wedding with id {wid}. Use --list-weddings to see what exists.")

    # Mirror the API's guard (eo_api.py weddings_delete): deleting out from under a running
    # dialer would strand it with no script.
    live = [c for c in eo_db.active_campaigns() if c.get("wedding_id") == wid]
    if live:
        names = ", ".join(f"'{c['name']}' ({c['status']})" for c in live)
        raise SystemExit(f"Refusing to delete: campaign {names} is still active. Cancel it first.")

    events = eo_db.list_events(wid)
    agents = [a for a in eo_db.all_agents() if a.get("wedding_id") == wid]
    guests = eo_db.list_contacts(wedding_id=wid, limit=100000)["items"]

    print(f"About to permanently delete wedding #{wid} '{wedding['name']}':")
    print(f"  {len(events)} events, {len(agents)} per-wedding agent(s), {len(guests)} guest(s)")
    if not assume_yes:
        if input("Type the wedding name to confirm: ").strip() != wedding["name"]:
            raise SystemExit("Name did not match — nothing was deleted.")

    if guests:
        eo_db.delete_contacts([g["id"] for g in guests])
    eo_db.delete_wedding(wid)
    print(f"Deleted wedding #{wid} '{wedding['name']}', its {len(events)} events, "
          f"{len(agents)} agent copies and {len(guests)} guests.")


def template_for(agent, seeds):
    """The shipped template a per-wedding copy was made from, or None.

    The slug alone is not enough: duplicating into a wedding that already holds a copy
    gives the new one a suffix ('event_reminder-2'), and an operator can rename it. So
    try the exact slug, then the slug without a trailing -N, then the agent's kind —
    the kind survives both a duplicate and an edit, and each shipped template has its
    own."""
    import re
    by_slug = {s["slug"]: s for s in seeds}
    slug = str(agent.get("slug") or "")
    if slug in by_slug:
        return by_slug[slug]
    base = re.sub(r"-\d+$", "", slug)
    if base in by_slug:
        return by_slug[base]
    by_kind = {}
    for s in seeds:
        by_kind.setdefault(s.get("kind"), []).append(s)
    matches = by_kind.get(agent.get("kind")) or []
    return matches[0] if len(matches) == 1 else None


def _apply_seed(row, seed):
    eo_db.update_agent(row["id"],
                       name=seed["name"], kind=seed["kind"],
                       description=seed.get("description", ""),
                       prompt_template=seed["prompt_template"],
                       trigger_template=seed.get("trigger_template", ""),
                       outcome_enum=seed.get("outcome_enum", "[]"),
                       extra_fields=seed.get("extra_fields", "[]"),
                       listen_seconds=int(seed.get("listen_seconds", 0)),
                       requires_event=int(seed.get("requires_event", 1)))


def refresh_agents(force_all=False):
    """Update the shipped global agent templates in place from agent_seeds.SEEDS.

    init()'s seeding is idempotent by slug and deliberately never overwrites an existing
    row, so a deploy does NOT pick up prompt changes. This does — for the global
    (wedding_id IS NULL) rows by default.

    A wedding's own duplicate is the operator's and is NOT updated, but it IS checked: a
    copy made before a fix keeps speaking the old text forever, and because a call resolves
    its agent by id, that copy is what guests actually hear. Stale ones are named here, and
    force_all=True updates them too (discarding any wording the operator changed)."""
    import agent_seeds
    eo_db.init()
    by_slug = {s["slug"]: s for s in agent_seeds.SEEDS}

    for slug, seed in by_slug.items():
        row = eo_db.get_agent_by_slug(slug)                  # global row only
        if not row or row.get("wedding_id") is not None:
            print(f"  {slug}: no global row, skipped")
            continue
        _apply_seed(row, seed)
        print(f"  {slug}: updated (#{row['id']}, "
              f"listen_seconds={seed.get('listen_seconds', 0)})")

    customs = [a for a in eo_db.all_agents() if a.get("wedding_id")]
    if not customs:
        return
    for a in customs:
        problems = eo_db.stale_agent_reasons(a)
        if not problems:
            print(f"  kept: '{a['name']}' (#{a['id']}) — per-wedding copy, looks current")
            continue
        seed = template_for(a, agent_seeds.SEEDS)
        if force_all and seed:
            _apply_seed(a, seed)
            print(f"  FORCED: '{a['name']}' (#{a['id']}) — was stale ({', '.join(problems)}); "
                  f"now the shipped {seed['slug']} script")
        elif force_all:
            print(f"  STALE:  '{a['name']}' (#{a['id']}) — {', '.join(problems)}")
            print(f"          cannot tell which shipped script it was copied from "
                  f"(slug '{a.get('slug')}', kind '{a.get('kind')}'), so it was left alone. "
                  f"Point the campaign at a shipped agent instead.")
        else:
            print(f"  STALE:  '{a['name']}' (#{a['id']}) — {', '.join(problems)}")
            print(f"          guests on this agent still hear the old script. "
                  f"Re-run with --force-all to update it.")


def _name_key(name):
    """'Hi-Tea', 'Hi Tea' and 'hi tea' are the same function."""
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def set_events(wedding_id=None, replace=False, remove_sample_guests=False):
    """Write EVENTS + ANNOUNCEMENTS onto an EXISTING wedding, for a live server.

    Unlike the plain seed this touches nothing else: no sample guests, no wedding fields,
    and it never creates a wedding. An event already there under the same name (ignoring
    case and punctuation) is updated in place, so campaigns pointing at it stay valid.
    Other events are only reported — removed with replace=True, and even then never one a
    scheduled or live campaign is using."""
    eo_db.init()
    if wedding_id:
        wedding = eo_db.get_wedding(int(wedding_id))
    else:
        matches = [w for w in eo_db.list_weddings() if w["name"] == WEDDING_NAME]
        if len(matches) > 1:
            raise SystemExit(f"{len(matches)} weddings are named '{WEDDING_NAME}'. "
                             f"Pick one with --wedding ID (see --list-weddings).")
        wedding = matches[0] if matches else None
    if not wedding:
        raise SystemExit("Wedding not found. Use --list-weddings, then --wedding ID.")
    wid = wedding["id"]
    print(f"Wedding #{wid} '{wedding['name']}'")

    existing = {_name_key(e["name"]): e for e in eo_db.list_events(wid)}
    wanted = set()
    for order, (key, name, edate, start, venue, audience) in enumerate(EVENTS):
        fields = dict(name=name, event_date=edate, start_time=start, venue=venue,
                      audience=audience, sort_order=order,
                      announcement=ANNOUNCEMENTS.get(key, ""))
        k = _name_key(name)
        wanted.add(k)
        if k in existing:
            eo_db.update_event(existing[k]["id"], **fields)
            print(f"  updated  #{existing[k]['id']:<4} {name} — {start} at {venue}")
        else:
            eid = eo_db.create_event(wid, name, **{f: v for f, v in fields.items() if f != "name"})
            print(f"  added    #{eid:<4} {name} — {start} at {venue}")

    in_use = {c.get("event_id") for c in eo_db.active_campaigns() if c.get("event_id")}
    for k, e in existing.items():
        if k in wanted:
            continue
        if replace and e["id"] not in in_use:
            eo_db.delete_event(e["id"])
            print(f"  removed  #{e['id']:<4} {e['name']}")
        elif replace:
            print(f"  KEPT     #{e['id']:<4} {e['name']} — a scheduled/live campaign uses it")
        else:
            print(f"  extra    #{e['id']:<4} {e['name']} — not in the itinerary; guests will "
                  f"hear about it too. Re-run with --replace-events to remove it.")

    fakes = [c for c in eo_db.list_contacts(wedding_id=wid, limit=100000)["items"]
             if c["phone"] in SAMPLE_PHONES]
    if fakes and remove_sample_guests:
        eo_db.delete_contacts([c["id"] for c in fakes])
        print(f"  removed {len(fakes)} sample guest(s) with made-up numbers")
    elif fakes:
        print(f"\n  WARNING: {len(fakes)} sample guest(s) with made-up numbers are on this "
              f"wedding: " + ", ".join(f"{c['name']} {c['phone']}" for c in fakes))
        print("  A whole-schedule campaign ticks every guest and would ring them. "
              "Re-run with --remove-sample-guests to delete them.")
    print("\nCheck what the agent will say:  python seed_demo_wedding.py --preview schedule")


def preview_schedule(wedding_id=None):
    """Render the Wedding Schedule agent for a wedding with NO event, exactly as a
    whole-schedule campaign call would. Read-only: unlike --preview EVENT_KEY it does not
    seed, so it never touches events edited in the UI."""
    eo_db.init()
    weddings = eo_db.list_weddings()
    if wedding_id:
        wedding = eo_db.get_wedding(int(wedding_id))
    else:
        wedding = (next((w for w in weddings if w["name"] == WEDDING_NAME), None)
                   or (weddings[0] if weddings else None))
    if not wedding:
        raise SystemExit("No wedding to preview. Use --list-weddings.")
    agent = eo_db.get_agent_by_slug("wedding_schedule")
    if not agent:
        raise SystemExit("The Wedding Schedule agent is not in this database yet — restart the "
                         "app once so it is seeded, or run --refresh-agents.")
    guests = eo_db.list_contacts(wedding_id=wedding["id"], limit=1)["items"]
    r = prompt_render.render_prompt(agent, wedding=wedding, event=None,
                                    guest=guests[0] if guests else None,
                                    events=eo_db.list_events(wedding["id"]))
    print(f"Wedding #{wedding['id']} '{wedding['name']}' — no event (whole schedule)")
    print("=" * 70)
    print(r["system_instruction"])
    print("=" * 70)
    print("TRIGGER:", r["trigger"])
    if r["missing"]:
        print("\nMISSING (blank in the data, left out of the prompt):", r["missing"])


def preview(wid, event_id, agent, owner):
    guests = eo_db.guests_for_audience(wid, "all", created_by=owner)
    r = prompt_render.render_prompt(agent, wedding=eo_db.get_wedding(wid),
                                    event=eo_db.get_event(event_id),
                                    guest=guests[0] if guests else None,
                                    events=eo_db.list_events(wid))
    print("=" * 70)
    print(r["system_instruction"])
    print("=" * 70)
    print("TRIGGER:", r["trigger"])
    if r["missing"]:
        print("\nMISSING (blank in the data, left out of the prompt):", r["missing"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", help="set the sample guest's number, e.g. +919876543210")
    ap.add_argument("--preview", metavar="EVENT_KEY",
                    help="print the rendered prompt; 'schedule' renders the Wedding Schedule "
                         "agent with no event, read-only")
    ap.add_argument("--wedding", metavar="ID", type=int,
                    help="with --preview schedule or --set-events, the wedding to use "
                         "(default: the one named in WEDDING_NAME)")
    ap.add_argument("--set-events", action="store_true",
                    help="write the itinerary onto an EXISTING wedding — events only, no "
                         "sample guests, no wedding changes; safe on the live server")
    ap.add_argument("--replace-events", action="store_true",
                    help="with --set-events, also remove events not in the itinerary "
                         "(never one a scheduled/live campaign uses)")
    ap.add_argument("--remove-sample-guests", action="store_true",
                    help="with --set-events, delete the made-up sample guests from the wedding")
    ap.add_argument("--logistics", action="store_true",
                    help="with --preview, render the logistics agent instead")
    ap.add_argument("--list", action="store_true", help="list the event keys and exit")
    ap.add_argument("--refresh-agents", action="store_true",
                    help="update the shipped agent templates from agent_seeds.py "
                         "(per-wedding copies are checked and reported, not changed)")
    ap.add_argument("--force-all", action="store_true",
                    help="with --refresh-agents, ALSO overwrite stale per-wedding copies "
                         "— this discards any wording edited in the Agents tab")
    ap.add_argument("--delete-wedding", metavar="ID", type=int,
                    help="permanently delete a wedding with its events, agents and guests")
    ap.add_argument("--list-weddings", action="store_true",
                    help="show every wedding with its id, and exit")
    ap.add_argument("--yes", action="store_true",
                    help="with --delete-wedding, skip the confirmation prompt")
    args = ap.parse_args()

    if args.set_events:
        set_events(args.wedding, replace=args.replace_events,
                   remove_sample_guests=args.remove_sample_guests)
        return

    if args.preview == "schedule":
        preview_schedule(args.wedding)
        return

    if args.list_weddings:
        eo_db.init()
        for w in eo_db.list_weddings():
            events = len(eo_db.list_events(w["id"]))
            guests = eo_db.list_contacts(wedding_id=w["id"], limit=100000)["total"]
            print(f"  #{w['id']:<4} {w['name']:<24} {events} events, {guests} guests")
        return

    if args.delete_wedding:
        delete_wedding(args.delete_wedding, assume_yes=args.yes)
        return

    if args.refresh_agents or args.force_all:
        print("Refreshing the shipped agent templates:")
        refresh_agents(force_all=args.force_all)
        return

    if args.list:
        for key, name, edate, start, venue, audience in EVENTS:
            print(f"  {key:12} {edate}  {start}  {audience:6}  {name} — {venue}")
        return

    wid, keys, owner, reminder, logistics = seed(args.phone)

    if args.preview:
        if args.preview not in keys:
            raise SystemExit(f"Unknown event '{args.preview}'. Try: {', '.join(keys)}")
        preview(wid, keys[args.preview], logistics if args.logistics else reminder, owner)
        return

    print("\nEvent ids: " + ", ".join(f"{k}={v}" for k, v in keys.items()))
    print(f"\nNext: open /admin -> Weddings -> {WEDDING_NAME}, then Create Campaign "
          "-> Sufi Night, agent 'Event Reminder', start 2026-09-25 16:30 IST.")


if __name__ == "__main__":
    sys.exit(main())
