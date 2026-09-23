"""Create the Ved & Riya hospitality calling campaign for 25th September ~4:30 PM IST.

    python create_ved_riya_campaign.py
"""

import sys
from datetime import datetime, timezone, timedelta

import eo_db

def _now():
    return datetime.now(timezone.utc).isoformat()


def create_campaign():
    """Create the campaign for Sufi Night event."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    # Get the wedding
    cursor.execute("SELECT id FROM weddings WHERE name = ?", ("Ved & Riya",))
    wedding = cursor.fetchone()
    if not wedding:
        print("❌ Wedding 'Ved & Riya' not found. Run seed_ved_riya_campaign.py first.")
        return False
    wedding_id = wedding[0]

    # Get the Sufi Night event (main evening event)
    cursor.execute("""
        SELECT id FROM events WHERE wedding_id = ? AND name = 'Sufi Night'
    """, (wedding_id,))
    event = cursor.fetchone()
    if not event:
        print("❌ Event 'Sufi Night' not found.")
        return False
    event_id = event[0]

    # Get the hospitality agent
    cursor.execute("""
        SELECT id FROM agents WHERE wedding_id = ? AND name = 'Hospitality Greeting & Event Reminder'
    """, (wedding_id,))
    agent = cursor.fetchone()
    if not agent:
        print("❌ Agent 'Hospitality Greeting & Event Reminder' not found.")
        return False
    agent_id = agent[0]

    # Check if campaign already exists
    cursor.execute("""
        SELECT id, status FROM campaigns WHERE wedding_id = ? AND event_id = ? AND agent_id = ?
    """, (wedding_id, event_id, agent_id))
    existing = cursor.fetchone()

    if existing:
        campaign_id, status = existing
        print(f"ℹ️  Campaign already exists (id={campaign_id}, status={status})")
        return campaign_id

    # Create campaign with start_at = 25th Sept ~4:30 PM IST
    # IST is UTC+5:30, so 4:30 PM IST = 10:00 AM UTC
    campaign_start = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)

    now = _now()
    cursor.execute("""
        INSERT INTO campaigns (
            name, status, start_at, wedding_id, event_id, agent_id,
            contact_count, callback_delay_hours, callback_max_per_day, callback_days,
            call_start_min, call_end_min, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "Sufi Night Hospitality - 25th September 4:30 PM",
        "scheduled",
        campaign_start.isoformat(),
        wedding_id,
        event_id,
        agent_id,
        0,  # Will be set when contacts added
        4,  # Retry after 4 hours if unanswered
        3,  # Max 3 attempts per day
        1,  # Retry for 1 day
        540,   # Call start: 09:00 IST (540 min)
        1260,  # Call end: 21:00 IST (1260 min)
        now,
        now,
    ))
    conn.commit()
    campaign_id = cursor.lastrowid
    print(f"✅ Created campaign '{WEDDING_NAME} Hospitality' (id={campaign_id})")
    print(f"   Scheduled for: 25th September, 4:30 PM IST (10:00 AM UTC)")

    # Add all guests from this wedding to the campaign
    cursor.execute("""
        SELECT id, name, phone FROM contacts WHERE wedding_id = ? AND status = 'valid'
    """, (wedding_id,))
    guests = cursor.fetchall()

    if not guests:
        print("⚠️  No guests found for this wedding. Add guests and run this again.")
        return campaign_id

    contact_count = 0
    for contact_id, name, phone in guests:
        cursor.execute("""
            INSERT INTO campaign_contacts (
                campaign_id, contact_id, phone, name, call_status,
                attempts, day_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            campaign_id,
            contact_id,
            phone,
            name,
            "pending",
            0,
            0,
            now,
            now,
        ))
        contact_count += 1

    conn.commit()

    # Update campaign contact count
    cursor.execute("""
        UPDATE campaigns SET contact_count = ? WHERE id = ?
    """, (contact_count, campaign_id))
    conn.commit()

    print(f"   Added {contact_count} guests to campaign")
    print(f"\n✅ Campaign ready! Status: scheduled")
    print(f"   Campaign will start dialing on 25th September at 4:30 PM IST")

    return campaign_id


if __name__ == "__main__":
    WEDDING_NAME = "Ved & Riya"
    eo_db.init()
    print(f"\n📞 Creating {WEDDING_NAME} Hospitality Campaign\n")
    create_campaign()
    print()
