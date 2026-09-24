"""Audit log: who did what, to which record, from where.

One helper, called from the API layer for the actions worth being able to answer for
later — clearing data before go-live, changing the billing plan, changing what the
client's admins are allowed to see.

Never raises: an audit failure must not turn a successful action into an error.
"""

import json
import logging

import eo_db

logger = logging.getLogger(__name__)


def _ip(request):
    if request is None:
        return ""
    try:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


def log(action, user=None, target="", detail=None, request=None):
    """Record one audit row. `detail` is any JSON-serialisable value (kept small)."""
    try:
        payload = ""
        if detail not in (None, "", {}):
            payload = json.dumps(detail, ensure_ascii=False, default=str)[:2000]
        eo_db.add_audit(
            user_id=(user or {}).get("id"),
            username=(user or {}).get("username") or "",
            action=str(action or "")[:80],
            target=str(target or "")[:200],
            detail=payload,
            ip=_ip(request)[:64],
        )
    except Exception:
        logger.debug("audit log write failed", exc_info=True)
