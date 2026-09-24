"""Clear the test data before go-live, from the Super admin page.

Three independent parts: call logs and recordings, guests + campaigns, and the weddings
themselves (with their events and per-wedding agent copies). Users, settings, the plan and
the shipped global agent scripts are never touched.

Nothing is lost for good: the database is copied and the call files are MOVED into
DATA_DIR/backups/before-reset-<time>/ first. The reset is then recorded in the audit log
(after the wipe, so it survives an audit-log wipe).
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import audit
import eo_db
import store

logger = logging.getLogger(__name__)

PARTS = ("calls", "outbound", "weddings", "audit")


def counts() -> dict:
    db = eo_db.data_counts()
    return {"calls": len(store.call_metas()), "recordings": store.recording_count(),
            "weddings": db["weddings"], "events": db["events"],
            "contacts": db["contacts"], "campaigns": db["campaigns"],
            "audit": db["audit"]}


def backup_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(eo_db._DB_PATH)), "backups")


async def reset(parts, user=None) -> dict:
    """Back up, then delete the chosen parts. Raises ValueError when nothing was chosen."""
    wanted = {str(p).strip().lower() for p in (parts or [])}
    chosen = [p for p in PARTS if p in wanted]
    if not chosen:
        raise ValueError("Pick at least one kind of data to delete")
    before = counts()
    stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(backup_root(), f"before-reset-{stamp}")
    # Always, even for a partial reset: the cheapest possible insurance against a mis-click.
    eo_db.backup_db(os.path.join(backup_dir, "eo.db"))
    moved = await store.archive_calls(backup_dir) if "calls" in chosen else {}
    deleted = eo_db.wipe_data(outbound="outbound" in chosen,
                              weddings="weddings" in chosen,
                              audit="audit" in chosen)
    # Written AFTER the wipe so that clearing the audit log cannot erase the record of it.
    audit.log("data_reset", user=user, target="data",
              detail={"parts": chosen, "before": before, "deleted": deleted,
                      "moved": moved, "backup": backup_dir})
    logger.warning("DATA RESET by %s: %s — backup in %s",
                   (user or {}).get("username") or "?", ", ".join(chosen), backup_dir)
    return {"parts": chosen, "before": before, "after": counts(), "backup": backup_dir}
