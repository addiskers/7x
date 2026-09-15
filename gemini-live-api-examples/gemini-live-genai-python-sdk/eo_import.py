"""
Contacts import helpers for the EO Admin platform.

- `normalize_phone` — India-centric E.164 normaliser that rejects the classic
  Excel scientific-notation corruption (e.g. `9.17619E+11`).
- `parse_upload` — read an .xlsx (openpyxl) or .csv into (name, e164, status)
  rows, de-duplicated by phone within the batch.
- `build_template` — a real .xlsx sample with a Text-formatted Phone column so
  users don't re-introduce scientific notation.
"""

import csv
import io
import re

from openpyxl import Workbook, load_workbook

_MAX_ROWS = 100_000
_NAME_HEADERS = {"name", "full name", "contact name", "contact", "member", "guest",
                 "guest name"}
_PHONE_HINTS = ("phone", "mobile", "number", "contact no", "whatsapp", "cell", "msisdn")

# Wedding-guest columns, matched by header keyword. A sheet without any of these still
# imports — the fields simply stay blank, which means "not known" everywhere downstream.
_GUEST_HINTS = {
    "side": ("side", "party of", "belongs to"),
    "dietary": ("diet", "food", "meal", "veg"),
    "transport_mode": ("mode of travel", "travel mode", "transport mode", "travel by"),
    "transport_number": ("flight", "train", "pnr", "travel number", "transport number"),
    "arrival_at": ("arriv", "eta", "landing", "check in", "check-in"),
    "departure_at": ("depart", "etd", "return", "check out", "check-out"),
    "hotel": ("hotel", "stay", "accommodation", "property"),
    "room_number": ("room",),
    "guest_count": ("pax", "party size", "guest count", "accompanying"),
}

# Loose normalisation for the one field the campaign audience filter branches on.
_SIDE_WORDS = {
    "groom": "groom", "g": "groom", "boy": "groom", "groom side": "groom",
    "grooms side": "groom", "var": "groom", "var side": "groom",
    "bride": "bride", "b": "bride", "girl": "bride", "bride side": "bride",
    "brides side": "bride", "vadhu": "bride",
    "both": "both", "common": "both", "either": "both", "mutual": "both",
}


def normalize_side(raw):
    """Map a sheet's wording onto groom / bride / both.

    Anything unrecognised stays blank rather than guessing: a blank side is treated as
    "invite them to everything", whereas a wrong side would silently drop a guest from
    the reminder they were meant to get."""
    s = str(raw or "").strip().lower().rstrip(".")
    if not s:
        return ""
    s = s.replace("'", "")
    return _SIDE_WORDS.get(s, "")


def normalize_phone(raw):
    """Return (e164_or_None, is_valid). None => unparseable / rejected."""
    if raw is None:
        return None, False
    s = str(raw).strip()
    if not s:
        return None, False
    # reject float / scientific-notation corruption ("9.17619E+11", "917619000000.0")
    if re.search(r"[eE][+\-]?\d", s) or re.fullmatch(r"\d+\.\d+", s):
        return None, False
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None, False
    if plus:
        e164 = "+" + digits
    elif len(digits) == 10:
        e164 = "+91" + digits                       # bare Indian mobile
    elif len(digits) == 12 and digits.startswith("91"):
        e164 = "+" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        e164 = "+91" + digits[1:]                    # leading-0 domestic form
    else:
        e164 = "+" + digits
    ndig = len(e164) - 1
    return e164, (10 <= ndig <= 15)


def _pick_columns(header):
    """(name_idx, phone_idx, {guest_field: idx}, [unrecognised headers])."""
    name_idx = phone_idx = None
    guest_idx, unknown = {}, []
    for i, cell in enumerate(header):
        c = str(cell or "").strip().lower()
        if not c:
            continue
        if name_idx is None and c in _NAME_HEADERS:
            name_idx = i
            continue
        if phone_idx is None and any(k in c for k in _PHONE_HINTS):
            phone_idx = i
            continue
        matched = None
        for field, hints in _GUEST_HINTS.items():
            if field in guest_idx:
                continue
            if any(h in c for h in hints):
                matched = field
                break
        if matched:
            guest_idx[matched] = i
        else:
            unknown.append(str(cell).strip())
    return name_idx, phone_idx, guest_idx, unknown


def _rows_from_matrix(matrix):
    """matrix: list of row-tuples. Returns list of (name_raw, phone_raw)."""
    matrix = [r for r in matrix if r is not None and any(c not in (None, "") for c in r)]
    if not matrix:
        return []
    name_idx, phone_idx, guest_idx, unknown = _pick_columns(matrix[0])
    if phone_idx is not None:
        body = matrix[1:]                            # first row was a header
    else:
        ncol = max(len(r) for r in matrix)
        name_idx, phone_idx = (0, 1) if ncol >= 2 else (None, 0)
        guest_idx, unknown = {}, []
        body = matrix
    out = []
    for r in body:
        ph = r[phone_idx] if phone_idx is not None and phone_idx < len(r) else None
        nm = r[name_idx] if name_idx is not None and name_idx < len(r) else None
        if ph in (None, "") and nm in (None, ""):
            continue
        fields = {}
        for field, idx in guest_idx.items():
            val = r[idx] if idx < len(r) else None
            if val in (None, ""):
                continue
            fields[field] = normalize_side(val) if field == "side" else str(val).strip()
        out.append((nm, ph, fields))
    return out, unknown


def _parse_xlsx(data):
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    matrix = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= _MAX_ROWS:
            break
        matrix.append(row)
    wb.close()
    return _rows_from_matrix(matrix)


def _parse_csv(data):
    text = data.decode("utf-8-sig", errors="replace")
    matrix = [tuple(r) for r in csv.reader(io.StringIO(text))]
    return _rows_from_matrix(matrix[:_MAX_ROWS])


def parse_upload(filename, data):
    """Return (rows, rejected, total, unknown_headers).

    rows: list of (name, e164, status, guest_fields) — the 4th element carries whatever
    wedding columns the sheet had (side, hotel, flight...), an empty dict when it had
    none. unknown_headers is reported back so a mis-named column is visible rather than
    silently ignored; call transport_mismatches(rows) for travel-data warnings."""
    name = (filename or "").lower()
    raw_rows, unknown = _parse_csv(data) if name.endswith(".csv") else _parse_xlsx(data)
    seen = {}                                        # e164 -> (name, status, fields)
    rejected = 0
    for nm, ph, fields in raw_rows:
        e164, valid = normalize_phone(ph)
        if not e164:
            rejected += 1
            continue
        nm = (str(nm).strip() if nm not in (None, "") else "")
        status = "valid" if valid else "invalid"
        prev = seen.get(e164)
        # keep a name if we have one; prefer valid status; merge the guest fields
        keep_name = nm or (prev[0] if prev else "")
        keep_status = "valid" if (status == "valid" or (prev and prev[1] == "valid")) else "invalid"
        merged = {**(prev[2] if prev else {}), **fields}
        seen[e164] = (keep_name, keep_status, merged)
    rows = [(nm, ph, st, f) for ph, (nm, st, f) in seen.items()]
    return rows, rejected, len(raw_rows), unknown


# An airline code: a carrier prefix then 3-4 digits — "6E 2134", "AI456", "UK 955".
# The prefix MUST contain a letter: a bare "12951" is an Indian train number, and an
# earlier [A-Z0-9]{2} pattern flagged every train as a flight.
_FLIGHT_CODE_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{2}\s?\d{3,4}$", re.I)
# An Indian train number is five bare digits — "12951".
_TRAIN_CODE_RE = re.compile(r"^\d{5}$")


def transport_mismatches(rows):
    """Rows whose travel mode disagrees with the shape of their travel number.

    The agent reads {transport_mode} out loud, so a sheet saying "train" beside a flight
    code makes it tell a guest their "train number" while they hold a boarding pass. Only
    reported — never auto-corrected, because the number could equally be the wrong one."""
    out = []
    for row in rows:
        fields = row[3] if len(row) > 3 else None
        if not fields:
            continue
        mode = str(fields.get("transport_mode") or "").strip().lower()
        number = str(fields.get("transport_number") or "").strip()
        if not mode or not number:
            continue
        looks_flight = bool(_FLIGHT_CODE_RE.match(number))
        looks_train = bool(_TRAIN_CODE_RE.match(number))
        if mode == "train" and looks_flight:
            out.append(f"{row[1]}: mode says 'train' but '{number}' looks like a flight")
        elif mode == "flight" and looks_train:
            out.append(f"{row[1]}: mode says 'flight' but '{number}' looks like a train")
    return out


def build_template():
    """A minimal .xlsx sample: headers Name/Phone, phone column Text-formatted."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Guests"
    headers = ["Name", "Phone", "Side", "Dietary", "Travel mode", "Flight/Train no",
               "Arrival", "Departure", "Hotel", "Room", "Pax"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=1, column=i, value=h)
    samples = [
        ("Rajesh Kumar", "9876543210", "Groom", "Vegetarian", "Flight", "AI 456",
         "2:30 PM on 19 September", "22 September", "Fairmont Udaipur", "412", 2),
        ("Priya Patel", "+919812345678", "Bride", "", "Train", "12951",
         "20 September", "", "Taj Lake Palace", "", 1),
    ]
    for r, row in enumerate(samples, start=2):
        for i, val in enumerate(row, start=1):
            c = ws.cell(row=r, column=i, value=val)
            if i == 2:
                c.number_format = "@"                # Text — preserves leading digits
    for col, width in zip("ABCDEFGHIJK", (22, 18, 10, 14, 12, 16, 24, 18, 20, 8, 6)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
