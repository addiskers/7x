"""Call analytics and campaign reporting for 7x platform.

Provides metrics and insights for wedding hospitality campaigns:
- Total calls, connected calls, successful deliveries
- Failed calls and reasons
- Call duration statistics
- Language distribution
- Callback requests
- Completion rates
"""

import logging
from datetime import datetime, timezone
import eo_db
import store

logger = logging.getLogger(__name__)


def get_campaign_analytics(campaign_id: int) -> dict:
    """Get comprehensive analytics for a campaign."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    # Get campaign info
    campaign = eo_db.get_campaign(campaign_id)
    if not campaign:
        return {}

    # Get contact statistics
    cursor.execute("""
        SELECT
            COUNT(*) as total_contacts,
            SUM(CASE WHEN call_status = 'done' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN call_status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN call_status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN call_status = 'calling' THEN 1 ELSE 0 END) as calling,
            MAX(attempts) as max_attempts,
            AVG(attempts) as avg_attempts
        FROM campaign_contacts
        WHERE campaign_id = ?
    """, (campaign_id,))

    contact_stats = cursor.fetchone()
    if not contact_stats:
        contact_stats = (0, 0, 0, 0, 0, 0, 0)

    total_contacts = contact_stats[0] or 0
    completed = contact_stats[1] or 0
    failed = contact_stats[2] or 0
    pending = contact_stats[3] or 0
    calling = contact_stats[4] or 0
    max_attempts = contact_stats[5] or 0
    avg_attempts = float(contact_stats[6] or 0)

    # Get callback requests
    cursor.execute("""
        SELECT COUNT(*) FROM campaign_contacts
        WHERE campaign_id = ? AND rsvp_outcome = 'callback_requested'
    """, (campaign_id,))
    callback_requested = cursor.fetchone()[0] or 0

    # Get call data from JSON store
    call_records = store.list_calls_for_campaign(campaign_id)

    total_call_duration = 0
    connected_calls = 0
    language_distribution = {}
    call_outcomes = {}

    for call_record in call_records:
        connected_calls += 1

        # Accumulate duration
        duration = call_record.get("duration_seconds", 0) or 0
        total_call_duration += duration

        # Track language
        language = call_record.get("language_detected", "unknown")
        language_distribution[language] = language_distribution.get(language, 0) + 1

        # Track outcomes
        outcome = call_record.get("outcome", "unknown")
        call_outcomes[outcome] = call_outcomes.get(outcome, 0) + 1

    avg_call_duration = total_call_duration / connected_calls if connected_calls > 0 else 0

    # Calculate completion rate
    completion_rate = (completed / total_contacts * 100) if total_contacts > 0 else 0

    return {
        "campaign": {
            "id": campaign_id,
            "name": campaign.get("name"),
            "status": campaign.get("status"),
            "started_at": campaign.get("start_at"),
        },
        "contacts": {
            "total": total_contacts,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "calling": calling,
        },
        "calls": {
            "connected": connected_calls,
            "total_duration_seconds": int(total_call_duration),
            "average_duration_seconds": round(avg_call_duration, 2),
        },
        "languages": language_distribution,
        "outcomes": call_outcomes,
        "callbacks": {
            "requested": callback_requested,
        },
        "metrics": {
            "completion_rate_percent": round(completion_rate, 2),
            "average_attempts_per_contact": round(avg_attempts, 2),
            "max_attempts": max_attempts,
        },
    }


def get_wedding_analytics(wedding_id: int) -> dict:
    """Get analytics for all campaigns in a wedding."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    # Get all campaigns for this wedding
    cursor.execute("""
        SELECT id FROM campaigns WHERE wedding_id = ? ORDER BY created_at DESC
    """, (wedding_id,))

    campaigns = cursor.fetchall()
    campaign_analytics = []

    for (campaign_id,) in campaigns:
        analytics = get_campaign_analytics(campaign_id)
        if analytics:
            campaign_analytics.append(analytics)

    # Aggregate stats
    total_contacts = sum(c["contacts"]["total"] for c in campaign_analytics)
    total_completed = sum(c["contacts"]["completed"] for c in campaign_analytics)
    total_connected = sum(c["calls"]["connected"] for c in campaign_analytics)
    total_duration = sum(c["calls"]["total_duration_seconds"] for c in campaign_analytics)

    # Aggregate languages
    all_languages = {}
    for campaign in campaign_analytics:
        for lang, count in campaign["languages"].items():
            all_languages[lang] = all_languages.get(lang, 0) + count

    # Aggregate outcomes
    all_outcomes = {}
    for campaign in campaign_analytics:
        for outcome, count in campaign["outcomes"].items():
            all_outcomes[outcome] = all_outcomes.get(outcome, 0) + count

    overall_completion_rate = (total_completed / total_contacts * 100) if total_contacts > 0 else 0
    avg_duration = total_duration / total_connected if total_connected > 0 else 0

    return {
        "wedding_id": wedding_id,
        "total_campaigns": len(campaign_analytics),
        "campaigns": campaign_analytics,
        "aggregate": {
            "contacts": {
                "total": total_contacts,
                "completed": total_completed,
            },
            "calls": {
                "connected": total_connected,
                "total_duration_seconds": int(total_duration),
                "average_duration_seconds": round(avg_duration, 2),
            },
            "languages": all_languages,
            "outcomes": all_outcomes,
            "metrics": {
                "overall_completion_rate_percent": round(overall_completion_rate, 2),
            },
        },
    }


def track_call_outcome(call_id: str, campaign_id: int, phone: str, outcome: str,
                      language: str = None, duration_seconds: int = 0) -> bool:
    """Track a call outcome in the campaign."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    # Update campaign contact with call info
    cursor.execute("""
        UPDATE campaign_contacts
        SET
            call_status = ?,
            last_call_id = ?,
            last_attempt_at = ?,
            attempts = attempts + 1,
            day_attempts = day_attempts + 1,
            updated_at = ?
        WHERE campaign_id = ? AND phone = ?
    """, (
        "done" if outcome in ("answered", "completed", "callback_requested") else "failed",
        call_id,
        datetime.now(timezone.utc).isoformat(),
        datetime.now(timezone.utc).isoformat(),
        campaign_id,
        phone,
    ))

    conn.commit()

    # Store call record (handled by store.py)
    return True


def get_pending_calls(campaign_id: int, limit: int = 50) -> list:
    """Get pending calls for a campaign."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, phone, name, attempts, next_attempt_at
        FROM campaign_contacts
        WHERE campaign_id = ? AND call_status IN ('pending', 'calling')
        ORDER BY next_attempt_at ASC, attempts ASC
        LIMIT ?
    """, (campaign_id, limit))

    results = cursor.fetchall()
    return [
        {
            "id": row[0],
            "phone": row[1],
            "name": row[2],
            "attempts": row[3],
            "next_attempt_at": row[4],
        }
        for row in results
    ]


def get_completed_calls(campaign_id: int, limit: int = 50) -> list:
    """Get completed calls for a campaign."""
    conn = eo_db.get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, phone, name, attempts, last_attempt_at, last_call_id, rsvp_outcome
        FROM campaign_contacts
        WHERE campaign_id = ? AND call_status = 'done'
        ORDER BY last_attempt_at DESC
        LIMIT ?
    """, (campaign_id, limit))

    results = cursor.fetchall()
    return [
        {
            "id": row[0],
            "phone": row[1],
            "name": row[2],
            "attempts": row[3],
            "last_attempt_at": row[4],
            "call_id": row[5],
            "outcome": row[6],
        }
        for row in results
    ]
