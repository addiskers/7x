"""Seed the Ved & Riya wedding (25th September) with hospitality calling campaign.

Campaign: Outbound hospitality calls to guests on 25th September ~4:30 PM IST
Events: Hi-Tea (4-6 PM), Sufi Night (7 PM+), After Party (11 PM+)

Idempotent — re-running updates the same wedding and events rather than duplicating.

    python seed_ved_riya_campaign.py                    # seed
    python seed_ved_riya_campaign.py --phone +919876... # seed + add sample guest
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

import eo_db
import eo_auth

WEDDING_NAME = "Ved & Riya"

# Events for 25th September as per ticket
EVENTS = [
    ("hitea",     "Hi-Tea",
     "2026-09-25", "16:00", "Harvest", "all"),

    ("sufi",      "Sufi Night",
     "2026-09-25", "19:00", "Great Park", "all"),

    ("afterparty", "After Party",
     "2026-09-25", "23:00", "Ballroom", "all"),
]

# Hospitality agent prompt with multi-language support and event knowledge
HOSPITALITY_AGENT_PROMPT = """You are a warm, enthusiastic member of Ved and Riya's Hospitality Team,
welcoming guests to their wedding celebrations on 25th September.

## Your Personality
- Warm, friendly, celebratory tone
- Speak naturally and conversationally
- Sound like a wedding hospitality concierge, NOT robotic or transactional
- Show genuine excitement about the celebrations
- Be personalized and attentive to each guest

## IMPORTANT: Deliver All Event Details Upfront
In your opening message AFTER greeting the guest by name, immediately share comprehensive details about ALL THREE events in a clear, organized way. Do not wait for the guest to ask about other events - provide all information proactively in your first response.

## Opening Greeting & Event Overview
After greeting, say something like:

"Hi [Guest Name]! I'm calling from Ved and Riya's Hospitality Team. We wanted to share details about today's three evening celebrations:

**Hi-Tea** happens from 4:00 PM to 6:00 PM at Harvest. It's a wonderful time for evening refreshments, light snacks and beverages, and to mingle with other guests.

Then at **7:00 PM, Sufi Night** begins at Great Park. This is our main evening celebration with a grand welcome, ittar shower, gajra welcome, a mocktail bar, an interactive perfume-making experience, and at 8:15 PM the couple will make their entry. We'll have a live Sufi performance by Shadab Faridi at 8:30 PM, followed by dinner service.

And finally, our **After Party** starts at 11:00 PM at the Ballroom with supper service from 11:30 PM onwards. We have DJ Alex performing live, a full bar, and late-night celebrations to keep the party going.

Do you have any questions about any of these events?"

## Auto-Detect Language
Listen carefully to the guest's first response. If they respond in Hindi, Gujarati, Marathi, Punjabi, Bengali, Tamil, Telugu, Kannada, or Malayalam, continue the ENTIRE conversation in that language naturally. If uncertain, ask: "Would you prefer to continue in English, Hindi, or Gujarati?"

## Event Details (Reference)

### Hi-Tea (4:00 PM – 6:00 PM)
- **Venue**: Harvest
- **Highlights**: Evening refreshments, light snacks and beverages, guest interactions

### Sufi Night (7:00 PM onwards)
- **Venue**: Great Park
- **Key Timings**:
  - 8:15 PM – Couple Entry
  - 8:30 PM – Live Performance by Shadab Faridi
- **Highlights**:
  - Grand guest welcome
  - Ittar shower experience
  - Gajra welcome
  - Mocktail bar
  - Interactive perfume-making experience
  - Couple entry
  - Live Sufi performance
  - Dinner service

### After Party (11:00 PM onwards)
- **Venue**: Ballroom
- **Supper Service**: 11:30 PM onwards
- **Highlights**:
  - DJ Night
  - Live Performance by DJ Alex
  - Bar Service
  - Late Night Celebrations

## Hospitality Information
Always be ready to mention:
- Hospitality assistance is available throughout the event
- Guest support desks are operational
- Venue navigation assistance is available
- Logistics and transfer support can be coordinated through the hospitality team if required

## Q&A Support
Answer guest questions accurately about:
- Event times and venues
- Couple entry timing (8:15 PM at Sufi Night)
- Food service times
- Activities and highlights
- Transportation and logistics support

## Call Guidelines
- Open with warm greeting and full event overview (1-2 minutes)
- Answer any guest questions (1-2 minutes)
- Keep total call duration between 2–4 minutes
- Speak clearly and confidently
- Deliver information warmly and conversationally

## Do NOT
- Take RSVPs
- Modify guest information
- Confirm room allocations
- Commit hospitality arrangements
- Make schedule changes
- Handle unrelated queries
- Sound robotic or transactional
- Provide incorrect dates or venues

End the call warmly: "Thank you so much for being part of our celebrations. We look forward to seeing you at the Hi-Tea at 4 PM, then Sufi Night at 7 PM, and the After Party at 11 PM. If you have any questions, our hospitality desk is always here to help!"
"""

# First turn trigger (agent opens conversation)
HOSPITALITY_TRIGGER = "Hey, I'm speaking from Ved and Riya's Hospitality Team. We're excited to welcome you to today's wedding celebrations and wanted to share some important information regarding this evening's events. Am I speaking with {guest_name}? We have some wonderful events planned for you this evening, and I wanted to share all the details with you!"


def _now():
    return datetime.now(timezone.utc).isoformat()


def seed_wedding():
    """Create or update the Ved & Riya wedding."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id FROM weddings WHERE name = ?
    """, (WEDDING_NAME,))
    row = cursor.fetchone()

    if row:
        wedding_id = row[0]
        print(f"✓ Wedding '{WEDDING_NAME}' already exists (id={wedding_id})")
        return wedding_id

    now = _now()
    cursor.execute("""
        INSERT INTO weddings (
            name, groom_name, bride_name, start_date, end_date, city,
            hospitality_team, placard_text, contact_phone, contact_name,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        WEDDING_NAME,
        "Ved",
        "Riya",
        "2026-09-25",
        "2026-09-25",
        "Multiple Venues",
        "Ved and Riya's Hospitality Team",
        "Welcoming You to Our Celebrations",
        "+91-98765-43210",
        "Hospitality Desk",
        "active",
        now,
        now,
    ))
    conn.commit()
    wedding_id = cursor.lastrowid
    print(f"✓ Created wedding '{WEDDING_NAME}' (id={wedding_id})")
    return wedding_id


def seed_events(wedding_id):
    """Create or update events for the wedding."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    now = _now()

    for key, name, date, start_time, venue, audience in EVENTS:
        cursor.execute("""
            SELECT id FROM events WHERE wedding_id = ? AND name = ?
        """, (wedding_id, name))
        row = cursor.fetchone()

        if row:
            event_id = row[0]
            print(f"  ✓ Event '{name}' already exists (id={event_id})")
            continue

        cursor.execute("""
            INSERT INTO events (
                wedding_id, name, event_date, start_time, venue, audience,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            wedding_id,
            name,
            date,
            start_time,
            venue,
            audience,
            now,
            now,
        ))
        conn.commit()
        event_id = cursor.lastrowid
        print(f"  ✓ Created event '{name}' (id={event_id})")


def seed_hospitality_agent(wedding_id):
    """Create the hospitality greeting agent."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    agent_name = "Hospitality Greeting & Event Reminder"

    cursor.execute("""
        SELECT id FROM agents WHERE wedding_id = ? AND name = ?
    """, (wedding_id, agent_name))
    row = cursor.fetchone()

    if row:
        agent_id = row[0]
        print(f"✓ Agent '{agent_name}' already exists (id={agent_id})")
        return agent_id

    now = _now()
    cursor.execute("""
        INSERT INTO agents (
            wedding_id, name, slug, kind, description,
            prompt_template, trigger_template,
            speech_language_code, language_mode,
            voice_name, requires_event, active,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        wedding_id,
        agent_name,
        "hospitality-greeting",
        "reminder",
        "Warm hospitality greeting and event information delivery for Ved & Riya's wedding",
        HOSPITALITY_AGENT_PROMPT,
        HOSPITALITY_TRIGGER,
        "en-IN",  # Indian English
        "auto",   # Auto-detect language
        "",       # Use default voice from env
        0,        # Event optional (can be used for any event)
        1,        # Active
        now,
        now,
    ))
    conn.commit()
    agent_id = cursor.lastrowid
    print(f"✓ Created agent '{agent_name}' (id={agent_id})")
    return agent_id


def seed_sample_guests(wedding_id, sample_phone=None):
    """Add sample guests for testing."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    # Sample guest list
    sample_guests = [
        ("Rajesh Kumar", "+919876543210"),
        ("Priya Singh", "+919876543211"),
        ("Amit Patel", "+919876543212"),
        ("Neha Sharma", "+919876543213"),
        ("Vikram Desai", "+919876543214"),
    ]

    if sample_phone:
        sample_guests = [(sample_guests[0][0], sample_phone)]

    now = _now()
    user_id = 1  # Default admin user

    for name, phone in sample_guests:
        cursor.execute("""
            SELECT id FROM contacts WHERE wedding_id = ? AND phone = ?
        """, (wedding_id, phone))
        row = cursor.fetchone()

        if row:
            print(f"  ✓ Guest '{name}' ({phone}) already exists")
            continue

        cursor.execute("""
            INSERT INTO contacts (
                wedding_id, name, phone, source, status,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            wedding_id,
            name,
            phone,
            "manual",
            "valid",
            user_id,
            now,
            now,
        ))
        conn.commit()
        print(f"  ✓ Added guest '{name}' ({phone})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", help="Sample guest phone number (E.164 format)")
    args = parser.parse_args()

    eo_db.init()

    print(f"\n🔔 Seeding Ved & Riya Wedding Campaign\n")

    # Seed wedding
    wedding_id = seed_wedding()

    # Seed events
    print(f"\n📅 Adding Events:")
    seed_events(wedding_id)

    # Seed hospitality agent
    print(f"\n🎤 Setting up Hospitality Agent:")
    seed_hospitality_agent(wedding_id)

    # Seed sample guests
    print(f"\n👥 Adding Sample Guests:")
    seed_sample_guests(wedding_id, args.phone)

    print(f"\n✅ Seeding complete!\n")


if __name__ == "__main__":
    main()
