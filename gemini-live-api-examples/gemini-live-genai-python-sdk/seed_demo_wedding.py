"""Seed the Manya & Anant wedding (Fairmont, Udaipur) from the client's itinerary.

Idempotent — re-running updates the same wedding and events rather than duplicating.

    python seed_demo_wedding.py                      # seed
    python seed_demo_wedding.py --phone +9198...     # seed + set the sample guest's number
    python seed_demo_wedding.py --preview saanth     # print the rendered script and exit
    python seed_demo_wedding.py --list               # show the event keys
"""

import argparse
import sys

import eo_auth
import eo_db
import prompt_render

WEDDING_NAME = "Manya & Anant"

# From the client's itinerary. Only functions guests should be REMINDED about are here:
# breakfasts, hi-teas, arrivals and check-outs are hotel logistics, not call-worthy, and
# ringing someone about breakfast three mornings running is how a guest list gets annoyed.
#
# (key, name, date, start "HH:MM", venue, audience)
EVENTS = [
    # --- 19 September ---
    ("ghazal",    "Ghazal Night & Dinner",
     "2026-09-19", "19:00", "Infinity Terrace", "all"),

    # --- 20 September ---
    ("mehendi",   "Mehendi & Haldi followed by Lunch",
     "2026-09-20", "12:00", "Ivory Garden & Pool", "all"),
    ("cocktail",  "Cocktail followed by Dinner",
     "2026-09-20", "19:30", "The Jewel", "all"),

    # --- 21 September, the wedding day ---
    ("saanth",    "Saanth",
     "2026-09-21", "11:00", "Imperial Terrace", "groom"),
    ("chuda",     "Chuda Ceremony",
     "2026-09-21", "12:30", "Imperial Ballroom & Terrace", "bride"),
    ("sehrabandhi", "Hi-Tea & Sehrabandhi",
     "2026-09-21", "18:00", "Sheesh Mahal", "groom"),
    ("baraat",    "Baraat Procession",
     "2026-09-21", "18:15", "Panther Patio", "groom"),
    ("swagat",    "Safa Bandhi & Baraat Swagat",
     "2026-09-21", "19:30", "Jashn Palace Garden", "bride"),
    ("milni",     "Milni",
     "2026-09-21", "19:45", "Jashn Palace Garden", "all"),
    ("varmala",   "Varmala",
     "2026-09-21", "20:00", "Jashn Palace Garden", "all"),
    ("reception", "Reception",
     "2026-09-21", "20:45", "Jashn Palace Garden", "all"),
    ("wedding",   "Wedding Ceremony",
     "2026-09-21", "22:30", "Chand Baori", "all"),
    ("vidaai",    "Vidaai",
     "2026-09-22", "00:30", "Chand Baori", "all"),
]

# Announcements for the few functions where the itinerary says more than time + venue.
ANNOUNCEMENTS = {
    "mehendi":  "Lunch follows the Mehendi and Haldi.",
    "cocktail": "Dinner follows at nine, and there is an after-party later at the same venue.",
    "saanth":   "Lunch follows at half past twelve in the Imperial Ballroom.",
    "chuda":    "Lunch follows at half past twelve in the Imperial Ballroom.",
    "vidaai":   "The Vidaai is just after midnight, at the close of the wedding night.",
}

# One sample guest so the Test panel has someone to render against. Replace with the
# real guest list via Create Campaign -> upload.
GUESTS = [
    ("Rajesh Kumar", "+919876543210", "groom", "vegetarian", "flight", "AI 456",
     "2:30 PM on 19 September", "22 September", "Fairmont Udaipur"),
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

    fields = dict(
        groom_name="Anant", bride_name="Manya",
        start_date="2026-09-19", end_date="2026-09-22", city="Udaipur",
        hospitality_team="Manya and Anant's Wedding Hospitality team",
        placard_text="Manya & Anant Wedding — Welcome",
        contact_phone="", contact_name="",
        notes="Fairmont, Udaipur. 19-22 September 2026.",
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
    print(f"agents: reminder=#{reminder['id']}  logistics=#{logistics['id']}")
    return wid, keys, owner, reminder, logistics


def refresh_agents():
    """Update the shipped global agent templates in place from agent_seeds.SEEDS.

    init()'s seeding is idempotent by slug and deliberately never overwrites an existing
    row, so a deploy does NOT pick up prompt changes. This does — but only for the global
    (wedding_id IS NULL) rows. A wedding's own duplicate is the operator's, and stays."""
    import agent_seeds
    eo_db.init()
    for seed in agent_seeds.SEEDS:
        row = eo_db.get_agent_by_slug(seed["slug"])          # global row only
        if not row or row.get("wedding_id") is not None:
            print(f"  {seed['slug']}: no global row, skipped")
            continue
        eo_db.update_agent(row["id"],
                           name=seed["name"], kind=seed["kind"],
                           description=seed.get("description", ""),
                           prompt_template=seed["prompt_template"],
                           trigger_template=seed.get("trigger_template", ""),
                           outcome_enum=seed.get("outcome_enum", "[]"),
                           extra_fields=seed.get("extra_fields", "[]"),
                           listen_seconds=int(seed.get("listen_seconds", 0)),
                           requires_event=int(seed.get("requires_event", 1)))
        print(f"  {seed['slug']}: updated (#{row['id']}, "
              f"listen_seconds={seed.get('listen_seconds', 0)})")
    customs = [a for a in eo_db.list_agents(active_only=False) if a.get("wedding_id")]
    if customs:
        print(f"  left untouched: {len(customs)} per-wedding agent(s) — "
              f"{', '.join(a['name'] for a in customs)}")


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
    ap.add_argument("--preview", metavar="EVENT_KEY", help="print the rendered prompt")
    ap.add_argument("--logistics", action="store_true",
                    help="with --preview, render the logistics agent instead")
    ap.add_argument("--list", action="store_true", help="list the event keys and exit")
    ap.add_argument("--refresh-agents", action="store_true",
                    help="update the shipped agent templates from agent_seeds.py "
                         "(per-wedding copies are left alone)")
    args = ap.parse_args()

    if args.refresh_agents:
        print("Refreshing the shipped agent templates:")
        refresh_agents()
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
    print("\nNext: open /admin -> Weddings -> Manya & Anant, then Agents -> "
          "Event Reminder -> Show the script.")


if __name__ == "__main__":
    sys.exit(main())
