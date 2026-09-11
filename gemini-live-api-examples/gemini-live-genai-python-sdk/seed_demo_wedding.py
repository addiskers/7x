"""Seed the Anant & Manya demo wedding: events, one guest, and reminder campaigns.

Development helper for testing P2 before the Agents/Events UI exists (P4). Idempotent —
re-running updates the same wedding rather than creating a second one.

    python seed_demo_wedding.py                      # seed only
    python seed_demo_wedding.py --phone +9198...     # seed, then set that guest's number
    python seed_demo_wedding.py --preview saanth     # print the rendered prompt and exit
"""

import argparse
import sys

import eo_auth
import eo_db
import prompt_render

WEDDING_NAME = "Anant & Manya"

# The client's schedule. audience: all | groom | bride.
EVENTS = [
    # (key,          name,                              date,         start,  venue,                            audience)
    ("ghazal",   "Ghazal Night",                     "2026-09-19", "19:00", "Infinity Terrace",               "all"),
    ("mehendi",  "Mehendi & Haldi followed by Lunch", "2026-09-20", "13:00", "Ivory Garden & Pool",            "all"),
    ("saanth",   "Saanth ritual followed by Lunch",   "2026-09-21", "10:30", "The Imperial Ballroom & Terrace", "groom"),
    ("chuda",    "Chuda Ceremony followed by Lunch",  "2026-09-21", "12:30", "The Imperial Ballroom & Terrace", "bride"),
    ("baraat",   "Hi Tea & Safa Bandhai followed by Baraat Procession",
                                                     "2026-09-21", "18:00", "The Sheesh Mahal",               "groom"),
    ("swagat",   "Safa Bandhai followed by Baraat Swagat",
                                                     "2026-09-21", "19:00", "The Jashn Palace",               "bride"),
    ("cocktail", "Cocktail followed by Dinner",       "2026-09-21", "20:00", "The Jewel",                      "all"),
    ("wedding",  "Wedding Ceremony",                  "2026-09-21", "22:00", "Chand Baori",                    "all"),
]

GUESTS = [
    # name,           phone,            side,    dietary,      mode,     number,   arrival,                      hotel
    ("Rajesh Kumar", "+919876543210", "groom", "vegetarian", "flight", "AI 456", "2:30 PM on 19 September", "Fairmont Udaipur"),
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

    existing = [w for w in eo_db.list_weddings() if w["name"] == WEDDING_NAME]
    fields = dict(
        groom_name="Anant", bride_name="Manya",
        groom_side_family="Kapoor Family", bride_side_family="Chopra Family",
        start_date="2026-09-19", end_date="2026-09-22", city="Udaipur",
        hospitality_team="Anant and Manya's Wedding Hospitality team",
        placard_text="Kapoor & Chopra Family Welcomes You",
        contact_phone="+91 98123 45678", contact_name="Rohit, our hospitality lead",
    )
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
        if name in by_name:
            eid = by_name[name]["id"]
            eo_db.update_event(eid, event_date=edate, start_time=start, venue=venue,
                               audience=audience, sort_order=order)
        else:
            eid = eo_db.create_event(wid, name, event_date=edate, start_time=start,
                                     venue=venue, audience=audience, sort_order=order)
        keys[key] = eid
    print(f"{len(EVENTS)} events seeded")

    rows = []
    for name, default_phone, side, diet, mode, number, arrival, hotel in GUESTS:
        rows.append((name, phone or default_phone, "valid", {
            "side": side, "dietary": diet, "transport_mode": mode,
            "transport_number": number, "arrival_at": arrival, "hotel": hotel}))
    added, updated = eo_db.bulk_upsert_contacts(rows, source="manual",
                                                created_by=owner, wedding_id=wid)
    print(f"guests: {added} added, {updated} updated"
          + (f" (phone set to {phone})" if phone else ""))

    reminder = eo_db.get_agent_by_slug("event_reminder")
    logistics = eo_db.get_agent_by_slug("logistics_concierge")
    print(f"agents: reminder=#{reminder['id']}  logistics=#{logistics['id']}")
    return wid, keys, owner, reminder, logistics


def preview(wid, event_id, agent, owner):
    guests = eo_db.guests_for_audience(wid, "all", created_by=owner)
    r = prompt_render.render_prompt(agent, wedding=eo_db.get_wedding(wid),
                                    event=eo_db.get_event(event_id),
                                    guest=guests[0] if guests else None)
    print("=" * 70)
    print(r["system_instruction"])
    print("=" * 70)
    print("TRIGGER:", r["trigger"])
    if r["missing"]:
        print("\nMISSING (blank in the data, left out of the prompt):", r["missing"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", help="set the demo guest's number, e.g. +919876543210")
    ap.add_argument("--preview", metavar="EVENT_KEY",
                    help="print the rendered prompt for one event: "
                         + ", ".join(k for k, *_ in EVENTS))
    ap.add_argument("--logistics", action="store_true",
                    help="with --preview, render the logistics agent instead")
    args = ap.parse_args()

    wid, keys, owner, reminder, logistics = seed(args.phone)

    if args.preview:
        if args.preview not in keys:
            raise SystemExit(f"Unknown event '{args.preview}'. Try: {', '.join(keys)}")
        preview(wid, keys[args.preview], logistics if args.logistics else reminder, owner)
        return

    guests = eo_db.guests_for_audience(wid, "all", created_by=owner)
    print("\nTo hear it on your phone, with the server running:")
    print(f"""
  curl -X POST http://localhost:8000/call-me \\
    -H "Content-Type: application/json" \\
    -d '{{"phone": "{guests[0]['phone'] if guests else '+91XXXXXXXXXX'}",
          "agent_id": {reminder['id']},
          "event_id": {keys['saanth']},
          "guest_id": {guests[0]['id'] if guests else 1},
          "wedding_id": {wid}}}'
""")
    print("Event ids: " + ", ".join(f"{k}={v}" for k, v in keys.items()))


if __name__ == "__main__":
    sys.exit(main())
