"""Per-agent Gemini tool declarations.

In the EO build the tool list was a module constant built once at import time from
EO_CALL_MODE. 7x is multi-agent: every agent row carries its own outcome vocabulary
(``outcome_enum``) and its own extra capture fields (``extra_fields``), so the
declarations must be built per call from that row.

Two tools are produced:

* ``record_outcome`` — silent bookkeeping. Its ``outcome_status`` enum and any extra
  properties come from the agent row. NON_BLOCKING where the SDK supports it, so the
  tool result never forces a turn (that was the doubled-closing bug).
* ``end_call`` — universal, identical for every agent.

The JSON columns are operator-editable, so every parse is defensive: a malformed
``outcome_enum`` must degrade to the built-in default set, never break a live call.
"""

import json
import logging

logger = logging.getLogger(__name__)

try:                                        # google-genai >= 2.x
    from google.genai import types
    _NONBLOCKING_BEHAVIOR = types.Behavior.NON_BLOCKING
except (ImportError, AttributeError):       # older SDK: prompt-level mitigation only
    _NONBLOCKING_BEHAVIOR = None


# Outcomes every agent understands even with an empty/corrupt outcome_enum. These are the
# statuses the dialer and campaign runner branch on, so the set must always be dialable.
DEFAULT_OUTCOMES = [
    {"value": "acknowledged",
     "description": "the guest heard and understood the message"},
    {"value": "callback",
     "description": "a LIVE person asked to be called later, or is busy/driving right now "
                    "(NEVER for a bad line, a repeated 'hello?', an unclear reply, or a machine "
                    "— ask again instead)"},
    {"value": "not_reachable",
     "description": "an answering machine or voicemail picked up, or no live person was reached "
                    "(never use 'callback' for a machine)"},
    {"value": "wrong_number",
     "description": "confirmed wrong number / not the intended guest (leave guest_name empty)"},
]

# Neither of these is ever re-dialed by the campaign runner.
TERMINAL_OUTCOMES = frozenset({"wrong_number", "do_not_contact"})

_ALLOWED_FIELD_TYPES = frozenset({"string", "integer", "number", "boolean"})

END_CALL_DECLARATION = {
    "name": "end_call",
    "description": (
        "Hang up the phone call. Call this ONCE, silently, immediately AFTER you have spoken "
        "your final goodbye, when the conversation is complete (the outcome is recorded and any "
        "final question answered). This ends the call."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _loads(raw, what, agent_id):
    """Parse a JSON column. Returns [] on anything unexpected — never raises."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("agent %s: %s is not valid JSON (%s); falling back to defaults", agent_id, what, exc)
        return []
    if not isinstance(parsed, list):
        logger.warning("agent %s: %s is %s, expected a list; falling back to defaults",
                       agent_id, what, type(parsed).__name__)
        return []
    return parsed


def outcome_values(agent):
    """The outcome_status enum for this agent, as a list of strings.

    Falls back to DEFAULT_OUTCOMES when the row is empty or unparseable, so the value
    returned is always safe to compare campaign-runner branches against."""
    agent = agent or {}
    raw = _loads(agent.get("outcome_enum"), "outcome_enum", agent.get("id"))
    values = []
    for item in raw:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("value", "")).strip()
        else:
            continue
        if value and value not in values:
            values.append(value)
    if not values:
        return [o["value"] for o in DEFAULT_OUTCOMES]
    return values


def _outcome_description(agent):
    """Human-readable enum guidance, built from whichever shape the row holds."""
    agent = agent or {}
    raw = _loads(agent.get("outcome_enum"), "outcome_enum", agent.get("id")) or DEFAULT_OUTCOMES
    parts = []
    for item in raw:
        if isinstance(item, dict):
            value = str(item.get("value", "")).strip()
            desc = str(item.get("description", "")).strip()
            if value:
                parts.append(f"{value}={desc}" if desc else value)
        elif isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return ", ".join(parts)


def extra_properties(agent):
    """The agent's extra capture fields as JSON-schema properties.

    Skips anything malformed or colliding with a base property — an operator typo in the
    Agents tab must not shadow outcome_status."""
    agent = agent or {}
    raw = _loads(agent.get("extra_fields"), "extra_fields", agent.get("id"))
    props = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in _BASE_PROPERTY_NAMES or name in props:
            continue
        ftype = str(item.get("type", "string")).strip().lower()
        if ftype not in _ALLOWED_FIELD_TYPES:
            ftype = "string"
        prop = {"type": ftype, "description": str(item.get("description", "")).strip()}
        enum = item.get("enum")
        if isinstance(enum, list):
            values = [str(v).strip() for v in enum if str(v).strip()]
            if values:
                prop["enum"] = values
        props[name] = prop
    return props


def _base_properties(agent):
    return {
        "outcome_status": {
            "type": "string",
            "enum": outcome_values(agent),
            "description": _outcome_description(agent),
        },
        "callback_time_text": {
            "type": "string",
            "description": "For outcome_status='callback': the guest's preferred callback time in "
                           "their own words (e.g. 'tomorrow evening', 'after 5 pm'). Empty if none given.",
        },
        "callback_time_iso": {
            "type": "string",
            "description": "For outcome_status='callback' when a time is implied: that time as "
                           "ISO-8601 in India Standard Time computed from today's date "
                           "(e.g. '2026-09-21T18:00:00+05:30'). Empty if no specific time.",
        },
        "guest_name": {
            "type": "string",
            "description": "The guest's name if they shared it, otherwise empty.",
        },
        "note": {
            "type": "string",
            "description": "Anything notable the guest mentioned, in a few words.",
        },
    }


_BASE_PROPERTY_NAMES = frozenset(
    {"outcome_status", "callback_time_text", "callback_time_iso", "guest_name", "note"}
)


def build_tools(agent):
    """Gemini function declarations for one agent row.

    Returns the list that goes into ``GeminiLive(tools=[{"function_declarations": ...}])``."""
    agent = agent or {}
    props = _base_properties(agent)
    props.update(extra_properties(agent))

    record = {
        "name": "record_outcome",
        "description": (
            f"Record the outcome of this {agent.get('name') or 'hospitality'} call. Silent "
            "bookkeeping — it produces no speech; never react to it or speak because of it. "
            "Call it exactly once, in the same turn as your closing, after you have spoken it."
        ),
        "parameters": {"type": "object", "properties": props, "required": ["outcome_status"]},
    }
    # NON_BLOCKING: the result must not force a turn, or the agent speaks its closing twice.
    if _NONBLOCKING_BEHAVIOR is not None:
        record["behavior"] = _NONBLOCKING_BEHAVIOR

    return [record, END_CALL_DECLARATION]


def tool_names(agent):
    """Every tool name this agent can call — used to validate prompt templates on save."""
    return [t["name"] for t in build_tools(agent)]
