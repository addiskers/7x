"""Render an agent's prompt template against one call's wedding / event / guest rows.

Deliberately dependency-light: no eo_db import, no I/O. The caller passes rows that are
already fetched, which keeps SQLite off the render path and makes every rule here
unit-testable in isolation.

Two outputs per call, and the split matters:

* ``system_instruction`` — frozen into the Live session at connect time. Carries the
  persona plus every FACT (event, wedding, guest), so the model can *answer questions*
  about them rather than only reciting them.
* ``trigger`` — a text turn sent once the media stream opens. This is what makes the
  model start speaking at all (the Live API emits no audio until it receives input),
  so it carries the guest's name and "begin the opening now".

Missing-data policy: on the live call path an unresolved placeholder becomes an empty
string and is reported in ``missing``; it NEVER raises. A half-personalised call beats a
dropped one on the wedding morning. An *unknown* placeholder (a typo like ``{even_name}``)
is a different thing — an authoring bug — and is rejected when the agent is saved.
"""

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}")

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
    8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth",
    14: "fourteenth", 15: "fifteenth", 16: "sixteenth", 17: "seventeenth", 18: "eighteenth",
    19: "nineteenth", 20: "twentieth", 21: "twenty-first", 22: "twenty-second",
    23: "twenty-third", 24: "twenty-fourth", 25: "twenty-fifth", 26: "twenty-sixth",
    27: "twenty-seventh", 28: "twenty-eighth", 29: "twenty-ninth", 30: "thirtieth",
    31: "thirty-first",
}

_HOURS = {0: "twelve", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
          7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}

_MINUTES = {5: "five", 10: "ten", 15: "quarter", 20: "twenty", 25: "twenty-five",
            30: "half", 35: "twenty-five", 40: "twenty", 45: "quarter", 50: "ten", 55: "five"}


class PromptRenderError(ValueError):
    """Raised only by strict rendering (agent save / preview), never on a live call."""


# Every placeholder an agent template may use. Single source of truth for the save-time
# validator AND the UI's placeholder palette, so the two can never drift apart.
KNOWN_PLACEHOLDERS = frozenset({
    # derived
    "today_spoken", "today_iso", "now_time", "when_phrase", "days_until",
    # wedding
    "wedding_name", "groom_name", "bride_name", "groom_side_family", "bride_side_family",
    "hospitality_team", "placard_text", "contact_phone", "contact_name", "wedding_city",
    "wedding_start_date", "wedding_end_date",
    # guest
    "guest_name", "guest_full_name", "guest_phone", "side", "side_phrase", "dietary",
    "transport_mode", "transport_number", "flight_number", "train_number",
    "arrival_time", "departure_time", "hotel", "room_number", "guest_count",
    # event
    "event_name", "event_date", "event_date_spoken", "event_time", "event_end_time",
    "venue", "venue_address", "dress_code", "announcement", "audience",
    # the whole wedding's schedule, filtered to what THIS guest is invited to
    "schedule", "schedule_detail", "upcoming_schedule", "event_count",
})


def _spoken_date(value):
    """'2026-09-19' -> 'the nineteenth of September'. Blank on anything unparseable."""
    d = _as_date(value)
    if not d:
        return ""
    return f"the {_ORDINALS.get(d.day, str(d.day))} of {d.strftime('%B')}"


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).strip()[:10]).date()
    except (TypeError, ValueError):
        return None


def _spoken_time(value):
    """'19:00' -> 'seven in the evening'; '10:30' -> 'half past ten in the morning'.

    Times must never reach the model as digits — it reads them out as digits."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{1,2})[:.](\d{2})", raw)
    if not m:
        return raw                      # already prose ("6 PM onwards") — leave it alone
    hour24, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour24 <= 23 and 0 <= minute <= 59):
        return raw
    if hour24 < 12:
        part = "in the morning"
    elif hour24 < 16:
        part = "in the afternoon"
    elif hour24 < 20:
        part = "in the evening"
    else:
        part = "at night"
    hour12 = hour24 % 12
    spoken_hour = _HOURS[hour12 if hour12 else 12]
    if minute == 0:
        return f"{spoken_hour} {part}"
    if minute == 15:
        return f"quarter past {spoken_hour} {part}"
    if minute == 30:
        return f"half past {spoken_hour} {part}"
    if minute == 45:
        nxt = _HOURS[(hour12 + 1) % 12 if (hour12 + 1) % 12 else 12]
        return f"quarter to {nxt} {part}"
    if minute in _MINUTES and minute < 30:
        return f"{_MINUTES[minute]} past {spoken_hour} {part}"
    if minute in _MINUTES and minute > 30:
        nxt = _HOURS[(hour12 + 1) % 12 if (hour12 + 1) % 12 else 12]
        return f"{_MINUTES[minute]} to {nxt} {part}"
    # Any other minute: "five twenty-seven", "five oh seven" — never the digits, which the
    # model reads out as digits ({now_time} hit this: "five 27 in the evening").
    return f"{spoken_hour} {_minute_words(minute)} {part}"


_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen")
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def _minute_words(minute):
    if minute < 10:
        return f"oh {_ONES[minute]}"
    if minute < 20:
        return _ONES[minute]
    tens, ones = divmod(minute, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def _when_phrase(event_date, today):
    """How the agent refers to the event day: 'today' / 'tomorrow' / 'on the 19th of Sept'."""
    d = _as_date(event_date)
    if not d or not today:
        return ""
    days = (d - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    spoken = _spoken_date(d)
    return f"on {spoken}" if spoken else ""


def _first_name(full):
    parts = str(full or "").strip().split()
    return parts[0] if parts else ""


def guest_can_attend(event, side):
    """Is this guest invited to this function?

    Matches eo_db.guests_for_audience deliberately: an 'all' event is for everyone, a
    side-specific one is for that side plus anyone marked 'both' — and a guest whose side
    was never recorded gets EVERYTHING. A blank side in the sheet must not silently hide a
    guest's own schedule from them."""
    audience = str((event or {}).get("audience") or "all").strip().lower()
    if audience not in ("groom", "bride"):
        return True
    side = str(side or "").strip().lower()
    if side in ("", "both"):
        return True
    return side == audience


def build_schedule(events, *, side=None, today=None, current_event_id=None):
    """The guest's functions as spoken-ready lines the agent can answer questions from.

        - Ghazal Night - tomorrow, seven in the evening, Infinity Terrace
        - Saanth - on the twenty-first of September, eleven in the morning, Imperial
          Terrace (a groom's-side ritual)

    Side-filtered per guest_can_attend. Times and dates go through the same spoken
    helpers as everything else — the model must never read a schedule out as digits."""
    lines = []
    for e in events or []:
        if not guest_can_attend(e, side):
            continue
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        parts = []
        when = _when_phrase(e.get("event_date"), today) if today else ""
        if not when:
            when = _spoken_date(e.get("event_date"))
            when = f"on {when}" if when else ""
        if when:
            parts.append(when)
        spoken_time = _spoken_time(e.get("start_time"))
        if spoken_time:
            parts.append(spoken_time)
        venue = str(e.get("venue") or "").strip()
        if venue:
            parts.append(venue)
        line = f"- {name}"
        if parts:
            line += " - " + ", ".join(parts)
        # Note the side only when it actually narrows who is invited (decision 2).
        audience = str(e.get("audience") or "all").strip().lower()
        if audience == "groom":
            line += " (a groom's-side function)"
        elif audience == "bride":
            line += " (a bride's-side function)"
        if current_event_id is not None and e.get("id") == current_event_id:
            line += "  <- the one you are calling about"
        lines.append(line)
    return "\n".join(lines)


def build_schedule_detail(events, *, side=None, today=None, upcoming_only=False):
    """The guest's functions WITH their highlights, for a call that briefs all of them.

        Hi-Tea - on the twenty-fifth of September, four in the evening, at Harvest.
          Evening refreshments, with light snacks and drinks, from four until six.

    build_schedule is the lookup list ("what time is the Mehendi?"); this is the script
    for a call whose whole purpose is to walk through the evening. It is the only place
    an announcement is spoken for a function the call is not about, so a wedding gets
    richer calls purely by filling in announcements — no prompt edit.

    Same side filtering and the same spoken date/time helpers as build_schedule: a time
    must never reach the model as digits.

    upcoming_only drops functions dated before `today`, for a call that covers the whole
    wedding: a guest rung on the last day must not be told about a Mehendi that is over.
    Today's functions stay (one may be under way), and so do undated ones."""
    blocks = []
    for e in events or []:
        if not guest_can_attend(e, side):
            continue
        if upcoming_only and today:
            d = _as_date(e.get("event_date"))
            if d and d < today:
                continue
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        parts = []
        when = _when_phrase(e.get("event_date"), today) if today else ""
        if not when:
            when = _spoken_date(e.get("event_date"))
            when = f"on {when}" if when else ""
        if when:
            parts.append(when)
        spoken_time = _spoken_time(e.get("start_time"))
        if spoken_time:
            parts.append(spoken_time)
        venue = str(e.get("venue") or "").strip()
        if venue:
            parts.append(f"at {venue}")
        line = f"{name} - " + ", ".join(parts) + "." if parts else f"{name}."
        audience = str(e.get("audience") or "all").strip().lower()
        if audience == "groom":
            line += " (a groom's-side function)"
        elif audience == "bride":
            line += " (a bride's-side function)"
        block = [line]
        announcement = str(e.get("announcement") or "").strip()
        if announcement:
            block.append(f"  {announcement}")
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def _side_phrase(side):
    side = str(side or "").strip().lower()
    if side == "groom":
        return "the groom's side"
    if side == "bride":
        return "the bride's side"
    if side == "both":
        return "both families"
    return ""


def _merge(dst, src):
    """Layer src over dst, skipping empty values so a blank never shadows a real one."""
    for key, val in (src or {}).items():
        if val in (None, ""):
            continue
        dst[key] = str(val)


def build_context(*, wedding=None, event=None, guest=None, agent=None, now=None, extra=None,
                  events=None):
    """Flatten the rows into one {placeholder: str} map.

    Resolution order, each layer filling only what the previous left blank:
    derived -> wedding -> guest -> event -> extra. Event wins over guest wins over
    wedding because a key like ``venue`` can plausibly exist on more than one row and
    the event is the most specific.

    ``events`` is the wedding's whole function list; it becomes {schedule}, filtered to
    what this guest is invited to, so the agent can answer "what time is the Mehendi?"
    instead of deflecting."""
    now = now or datetime.now(_IST)
    today = now.date() if hasattr(now, "date") else None

    ctx = {}

    # 1. derived / global
    _merge(ctx, {
        "today_spoken": _spoken_date(today),
        "today_iso": today.isoformat() if today else "",
        "now_time": _spoken_time(now.strftime("%H:%M")) if hasattr(now, "strftime") else "",
    })

    # 2. wedding
    w = wedding or {}
    _merge(ctx, {
        "wedding_name": w.get("name"),
        "groom_name": w.get("groom_name"),
        "bride_name": w.get("bride_name"),
        "groom_side_family": w.get("groom_side_family"),
        "bride_side_family": w.get("bride_side_family"),
        "hospitality_team": w.get("hospitality_team"),
        "placard_text": w.get("placard_text"),
        "contact_phone": w.get("contact_phone"),
        "contact_name": w.get("contact_name"),
        "wedding_city": w.get("city"),
        "wedding_start_date": _spoken_date(w.get("start_date")),
        "wedding_end_date": _spoken_date(w.get("end_date")),
    })

    # 3. guest
    g = guest or {}
    mode = str(g.get("transport_mode") or "").strip().lower()
    number = g.get("transport_number")
    _merge(ctx, {
        "guest_name": _first_name(g.get("name")),
        "guest_full_name": g.get("name"),
        "guest_phone": g.get("phone"),
        "side": g.get("side"),
        "side_phrase": _side_phrase(g.get("side")),
        "dietary": g.get("dietary"),
        "transport_mode": mode,
        "transport_number": number,
        # Aliases so a template can say {flight_number} without branching on mode.
        "flight_number": number if mode == "flight" else "",
        "train_number": number if mode == "train" else "",
        "arrival_time": g.get("arrival_at"),
        "departure_time": g.get("departure_at"),
        "hotel": g.get("hotel"),
        "room_number": g.get("room_number"),
        "guest_count": g.get("guest_count"),
    })

    # 4. event (most specific of the rows)
    e = event or {}
    _merge(ctx, {
        "event_name": e.get("name"),
        "event_date": e.get("event_date"),
        "event_date_spoken": _spoken_date(e.get("event_date")),
        "event_time": _spoken_time(e.get("start_time")),
        "event_end_time": _spoken_time(e.get("end_time")),
        "venue": e.get("venue"),
        "venue_address": e.get("venue_address"),
        "dress_code": e.get("dress_code"),
        "announcement": e.get("announcement"),
        "audience": e.get("audience"),
        "when_phrase": _when_phrase(e.get("event_date"), today),
    })
    d = _as_date(e.get("event_date"))
    if d and today:
        _merge(ctx, {"days_until": str((d - today).days)})

    # 5. the guest's whole schedule, so an "and what about the Mehendi?" has an answer
    if events:
        side = g.get("side")
        attending = [ev for ev in events if guest_can_attend(ev, side)]
        _merge(ctx, {
            "schedule": build_schedule(events, side=side, today=today,
                                       current_event_id=e.get("id")),
            "schedule_detail": build_schedule_detail(events, side=side, today=today),
            "upcoming_schedule": build_schedule_detail(events, side=side, today=today,
                                                       upcoming_only=True),
            "event_count": str(len(attending)) if attending else "",
        })

    # 6. caller overrides (the Test panel's sample values)
    _merge(ctx, extra)

    return ctx


# Prepositions/conjunctions that are left dangling when the value after them blanks out.
# "begins at {event_time} at {venue}" with both missing must not become "begins at at."
_DANGLING = r"(?:at|on|in|from|to|by|for|with|and|near|until|till)"


def _tidy_line(line):
    """Clean one line that actually lost a value. Returns '' if nothing is left to say."""
    out = re.sub(r"[ \t]{2,}", " ", line)
    # Collapse runs of dangling prepositions, then drop a trailing one, repeatedly:
    # "begins at at ." -> "begins at ." -> "begins ."
    for _ in range(4):
        before = out
        out = re.sub(rf"\s+{_DANGLING}(?=\s+{_DANGLING}\b)", "", out, flags=re.I)
        out = re.sub(rf"\s+{_DANGLING}\s*(?=[.,;:!?]|$)", "", out, flags=re.I)
        out = re.sub(r"[ \t]+([,.;:!?])", r"\1", out)
        out = re.sub(r"([,;:])(\s*[,;:])+", r"\1", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        if out == before:
            break
    stripped = out.strip()
    if not stripped:
        return ""
    # Punctuation-only leftovers, and bullet labels whose value vanished ("- Where:").
    if re.fullmatch(r"[-*•:,;.\s]+", stripped):
        return ""
    if re.fullmatch(r"[-*•]\s*[A-Za-z][\w' ()/,]{0,60}\s*[:—-]\s*[.,;]?", stripped):
        return ""
    return out.rstrip()


def _tidy(text, touched_lines=None):
    """Clean up what an empty substitution leaves behind.

    A blank placeholder does not just leave a gap — it strands the words around it. The
    model reads "begins at at." and "- Where:" aloud verbatim, so those have to go before
    the prompt is ever spoken. A fact line that lost its only fact is dropped whole rather
    than left as a bare label.

    Only lines that ACTUALLY lost a placeholder are rewritten (``touched_lines``). Prose
    the template author wrote is never touched — otherwise a heading like
    "## WHO YOU ARE SPEAKING TO" loses its trailing "TO", and a line ending in a colon
    ("Branch on their reply:") gets deleted as an empty label."""
    lines = []
    for idx, raw in enumerate(text.split("\n")):
        if not raw.strip():
            lines.append("")
            continue
        if touched_lines is not None and idx not in touched_lines:
            lines.append(raw.rstrip())
            continue
        cleaned = _tidy_line(raw)
        if cleaned:
            lines.append(cleaned)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def render(template, ctx, *, strict=False):
    """Substitute {placeholders}. Returns (rendered_text, sorted_missing_names).

    strict=True raises PromptRenderError instead of blanking — used by the save-time
    validator and the preview, never by the live call path."""
    template = template or ""
    missing = set()
    unknown = set()
    # Which OUTPUT lines lost a value — only those get the punctuation cleanup, so the
    # author's own prose is never rewritten. Counted in the output as it is built, not in
    # the template: {schedule} and friends expand to several lines, so a template line
    # number points at the wrong output line once one of them has been substituted — the
    # cleanup then stripped words ("and", "with") from untouched prose further down.
    touched = set()
    pieces = []
    out_line = 0
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        literal = template[pos:match.start()]
        pieces.append(literal)
        out_line += literal.count("\n")
        name = match.group(1)
        if name not in KNOWN_PLACEHOLDERS:
            unknown.add(name)
            value = match.group(0) if strict else ""
            if not strict:
                touched.add(out_line)
        else:
            value = ctx.get(name, "")
            if value in (None, ""):
                missing.add(name)
                touched.add(out_line)
                value = ""
            value = str(value)
        pieces.append(value)
        out_line += value.count("\n")
        pos = match.end()
    pieces.append(template[pos:])
    out = "".join(pieces)
    if strict and unknown:
        raise PromptRenderError(
            "unknown placeholder(s): " + ", ".join(f"{{{n}}}" for n in sorted(unknown)))
    if unknown:
        # Not fatal on the live path, but it means an agent was saved before validation
        # existed — surface it loudly rather than silently speaking a gap.
        logger.warning("prompt_render: unknown placeholders ignored: %s", sorted(unknown))

    return _tidy(out, touched), sorted(missing)


def validate_template(template):
    """Placeholders in a template that are not in the known vocabulary. Empty = valid."""
    return sorted({n for n in _PLACEHOLDER_RE.findall(template or "")
                   if n not in KNOWN_PLACEHOLDERS})


def render_prompt(agent, *, wedding=None, event=None, guest=None, now=None, extra=None,
                  events=None):
    """The one function the call path uses.

    Returns {"system_instruction", "trigger", "missing", "context"}. Never raises: a
    missing placeholder is reported, not fatal."""
    agent = agent or {}
    ctx = build_context(wedding=wedding, event=event, guest=guest, agent=agent,
                        now=now, extra=extra, events=events)

    system_instruction, missing_prompt = render(agent.get("prompt_template") or "", ctx)
    trigger, missing_trigger = render(agent.get("trigger_template") or "", ctx)

    if not trigger:
        # Every agent needs SOMETHING to open with — the Live API produces no audio until
        # it receives a turn. Fall back to a name-aware generic opening.
        name = ctx.get("guest_name") or ""
        if name:
            trigger = (f"[The guest has just answered. Their first name is {name}. Begin your "
                       f'opening: your first turn is EXACTLY "Hello Sir or Ma\'am, am I speaking '
                       f'with {name}?" — say ONLY that, then STOP and wait.]')
        else:
            trigger = ("[The guest has just answered. You were NOT given their name — never "
                       "invent one. Greet them warmly, say who you are calling on behalf of, "
                       "and continue with the purpose of your call.]")

    missing = sorted(set(missing_prompt) | set(missing_trigger))
    if missing:
        logger.warning("prompt_render: agent=%s event=%s unresolved placeholders: %s",
                       agent.get("slug") or agent.get("id"),
                       (event or {}).get("id"), missing)

    return {
        "system_instruction": system_instruction,
        "trigger": trigger,
        "missing": missing,
        "context": ctx,
    }
