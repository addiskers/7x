"""
Single outbound-call entry point (multi-provider: Plivo + EnableX).

Used by the /call-me admin button, the campaign runner and the auto-callback
scheduler, so the dial logic lives in exactly one place. `place_call()` runs the
blocking provider SDK/REST call in a thread executor and returns a structured dict —
it never raises for normal API errors. The provider is chosen per call (the campaign
owner's user setting); when none is given, EO_DEFAULT_PROVIDER decides (default plivo).
"""

import asyncio
import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _base_url(base_url=None, request=None):
    """Resolve the public https base Plivo must reach for /plivo/answer."""
    public = (os.getenv("PUBLIC_URL", "") or "").rstrip("/")
    if public:
        return public
    if base_url:
        return base_url.rstrip("/")
    if request is not None:
        host = request.headers.get("host", "localhost")
        proto = ("https" if ("onrender.com" in host or "globalvoxinc.ai" in host)
                 else request.url.scheme)
        return f"{proto}://{host}"
    return ""


# Outbound caller-ID rotation. PLIVO_FROM_NUMBER accepts one number or a comma-separated
# list; a wedding-day burst from a single line gets flagged by carriers, so calls are spread
# across whatever is configured. A single value behaves exactly as before.
_from_rotation = 0


def from_numbers():
    """Configured caller-ID numbers, in order. Empty list when none is set."""
    raw = os.getenv("PLIVO_FROM_NUMBER", "") or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


def _pick_from_number():
    """The next caller-ID to dial from, round-robin (same rotation idea as the campaign
    runner's fair ordering). Returns "" when nothing is configured — the caller already
    rejects that case with a clear error."""
    global _from_rotation
    numbers = from_numbers()
    if not numbers:
        return ""
    if len(numbers) == 1:
        return numbers[0]
    picked = numbers[_from_rotation % len(numbers)]
    _from_rotation += 1
    return picked


def _place_call_sync(to_number, answer_url, from_number=None):
    import plivo
    client = plivo.RestClient(os.getenv("PLIVO_AUTH_ID"), os.getenv("PLIVO_AUTH_TOKEN"))
    resp = client.calls.create(
        # Passed in, not re-read here, so the number we LOG is the number we dialled.
        from_=from_number if from_number is not None else _pick_from_number(),
        to_=to_number,
        answer_url=answer_url,
        answer_method="GET",
    )
    return getattr(resp, "request_uuid", None) or (
        resp.get("request_uuid") if isinstance(resp, dict) else None)


# EnableX (developer.enablex.io/voice): outbound via REST, media over their
# bidirectional WebSocket stream (same base64 mulaw/8k frames as Plivo).
# NOTE: payload/response field names to be verified against the pinned EnableX docs
# before first live use (phase C of the rollout) — guarded so Plivo is never affected.

def _enablex_headers():
    import base64
    tok = base64.b64encode(
        f"{os.getenv('ENABLEX_APP_ID', '')}:{os.getenv('ENABLEX_APP_KEY', '')}".encode()).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


def _enablex_api(path):
    base = (os.getenv("ENABLEX_API_BASE", "https://api.enablex.io/voice/v1") or "").rstrip("/")
    return f"{base}{path}"


def _place_call_sync_enablex(to_number, stream_wss_url):
    import json as _json
    import urllib.request
    payload = {
        "from": os.getenv("ENABLEX_FROM_NUMBER", ""),
        "to": to_number,
        "action_on_connect": {
            "stream": {"url": stream_wss_url, "bidirectional": True},
        },
    }
    req = urllib.request.Request(_enablex_api("/call"), data=_json.dumps(payload).encode(),
                                 headers=_enablex_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = _json.loads(r.read().decode() or "{}")
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    return body.get("voice_id") or body.get("call_id") or result.get("voice_id")


async def place_call(to_number, *, base_url=None, request=None, gen=0, origin_call_id=None,
                     name="", campaign_id=None, provider=None,
                     agent_id=None, event_id=None, guest_id=None, wedding_id=None):
    """
    Place one outbound call (Plivo or EnableX) that bridges to the agent.

    Returns {"success": True, "call_uuid": ..., "to": ..., "provider": ...} or {"error": "..."}.
    `gen` / `origin_call_id` are threaded into the answer/stream URL so a re-dialed call
    knows it is a callback (used by the scheduler to cap callback generations).
    `name`, when given, personalises the agent's greeting for this call.
    `provider` comes from the campaign owner's user setting; None → EO_DEFAULT_PROVIDER.

    `agent_id` / `event_id` / `guest_id` / `wedding_id` say WHICH script this call speaks:
    /plivo/answer resolves them into a rendered prompt before the media stream opens
    (see main._resolve_call_context). They are plain ints, so no quoting is needed and
    there is no injection surface. The campaign row is the authority — these are the fast
    path, and a missing one is backfilled from the campaign at answer time.
    """
    # Ints only: anything unparseable is dropped rather than put in a URL.
    def _ctx_params():
        parts = ""
        for key, val in (("agent", agent_id), ("event", event_id),
                         ("guest", guest_id), ("wedding", wedding_id)):
            if val in (None, ""):
                continue
            try:
                parts += f"&{key}={int(val)}"
            except (TypeError, ValueError):
                logger.warning("place_call: ignoring non-numeric %s=%r", key, val)
        return parts
    if not to_number:
        return {"error": "Missing 'to' number"}
    provider = (provider or os.getenv("EO_DEFAULT_PROVIDER", "plivo") or "plivo").strip().lower()
    if provider not in ("plivo", "enablex"):
        logger.warning(f"Unknown provider '{provider}'; falling back to plivo")
        provider = "plivo"

    base = _base_url(base_url=base_url, request=request)
    if not base:
        return {"error": "No PUBLIC_URL configured; cannot build answer_url"}

    if provider == "enablex":
        if not os.getenv("ENABLEX_APP_ID") or not os.getenv("ENABLEX_APP_KEY"):
            return {"error": "EnableX credentials not configured (ENABLEX_APP_ID/ENABLEX_APP_KEY)"}
        if not os.getenv("ENABLEX_FROM_NUMBER"):
            return {"error": "ENABLEX_FROM_NUMBER not configured"}
        # Context rides the stream URL's query params — the /enablex/media-stream WS
        # handler reads them directly (EnableX has no Plivo-style answer_url fetch).
        wss_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        stream_url = f"{wss_base}/enablex/media-stream?caller={quote(to_number)}"
        if name:
            stream_url += f"&name={quote(str(name))}"
        if gen:
            stream_url += f"&gen={int(gen)}"
        if origin_call_id:
            stream_url += f"&origin={quote(str(origin_call_id))}"
        if campaign_id:
            stream_url += f"&campaign={int(campaign_id)}"
        stream_url += _ctx_params()
        try:
            loop = asyncio.get_running_loop()
            voice_id = await loop.run_in_executor(
                None, _place_call_sync_enablex, to_number, stream_url)
            logger.info(f"Outbound EnableX call initiated: {voice_id} to {to_number} (gen={gen})")
            return {"success": True, "call_uuid": voice_id, "to": to_number, "provider": "enablex"}
        except Exception as e:
            logger.error(f"Failed to initiate EnableX call to {to_number}: {e}")
            return {"error": str(e)}

    if not os.getenv("PLIVO_AUTH_ID") or not os.getenv("PLIVO_AUTH_TOKEN"):
        return {"error": "Plivo credentials not configured"}
    if not os.getenv("PLIVO_FROM_NUMBER"):
        return {"error": "PLIVO_FROM_NUMBER not configured"}

    answer_url = f"{base}/plivo/answer?caller={quote(to_number)}"
    if name:
        answer_url += f"&name={quote(str(name))}"
    if gen:
        answer_url += f"&gen={int(gen)}"
    if origin_call_id:
        answer_url += f"&origin={quote(str(origin_call_id))}"
    if campaign_id:
        answer_url += f"&campaign={int(campaign_id)}"
    answer_url += _ctx_params()

    # Chosen here, not inside the executor, so the number we report is the number we dialled —
    # when a carrier flags a line you need to know which one placed the call.
    from_number = _pick_from_number()

    try:
        loop = asyncio.get_running_loop()
        request_uuid = await loop.run_in_executor(
            None, _place_call_sync, to_number, answer_url, from_number)
        logger.info(f"Outbound Plivo call initiated: {request_uuid} to {to_number} "
                    f"from {from_number} (gen={gen})")
        return {"success": True, "call_uuid": request_uuid, "to": to_number,
                "from": from_number, "provider": "plivo"}
    except Exception as e:
        logger.error(f"Failed to initiate Plivo call to {to_number} from {from_number}: {e}")
        return {"error": str(e)}


def _hangup_sync(call_uuid):
    import plivo
    client = plivo.RestClient(os.getenv("PLIVO_AUTH_ID"), os.getenv("PLIVO_AUTH_TOKEN"))
    client.calls.delete(call_uuid)


def _hangup_sync_enablex(call_uuid):
    import urllib.request
    req = urllib.request.Request(_enablex_api(f"/call/{call_uuid}"),
                                 headers=_enablex_headers(), method="DELETE")
    with urllib.request.urlopen(req, timeout=15):
        pass


async def hangup_call(call_uuid, provider="plivo"):
    """Hang up a live call by its provider call id (Plivo CallUUID / EnableX voice_id)."""
    if not call_uuid:
        return {"error": "no call_uuid"}
    provider = (provider or "plivo").strip().lower()
    if provider == "enablex":
        if not os.getenv("ENABLEX_APP_ID") or not os.getenv("ENABLEX_APP_KEY"):
            return {"error": "EnableX credentials not configured"}
        sync_fn = _hangup_sync_enablex
    else:
        if not os.getenv("PLIVO_AUTH_ID") or not os.getenv("PLIVO_AUTH_TOKEN"):
            return {"error": "Plivo credentials not configured"}
        sync_fn = _hangup_sync
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, sync_fn, call_uuid), timeout=15)
        logger.info(f"Hung up {provider} call {call_uuid}")
        return {"success": True}
    except asyncio.TimeoutError:
        logger.warning(f"Hangup timed out for {call_uuid}")
        return {"error": "hangup timeout"}
    except Exception as e:
        if "not found" in str(e).lower():
            # normal: the caller already hung up, so the call no longer exists on
            # Plivo's side — our best-effort hangup has nothing left to do
            logger.info(f"Hangup skipped for {call_uuid}: call already ended")
        else:
            logger.error(f"Hangup failed for {call_uuid}: {e}")
        return {"error": str(e)}
