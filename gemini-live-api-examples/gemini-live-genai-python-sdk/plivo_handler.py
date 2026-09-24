"""
Plivo Voice <-> Gemini Live bridge.

Handles:
- Plivo bidirectional Audio Streaming WebSocket (mulaw 8kHz)
- Audio conversion: mulaw 8kHz <-> PCM 16kHz (Gemini input) / PCM 24kHz (Gemini output)
- Bridges the two in real-time, with 20ms-paced outbound frames + barge-in (clearAudio)
- Pure Python audio conversion (no audioop, works on Python 3.13+)

Plivo media-stream protocol (JSON text frames):
  inbound  start : {"event":"start","start":{"streamId","callId","mediaFormat":{...}},"extra_headers":"k=v,k=v"}
  inbound  media : {"event":"media","media":{"payload":"<base64 mulaw>","track":"inbound"}}
  inbound  stop  : {"event":"stop", ...}
  outbound play  : {"event":"playAudio","streamId":"<id>","media":{"contentType":"audio/x-mulaw","sampleRate":8000,"payload":"<b64>"}}
  outbound clear : {"event":"clearAudio","streamId":"<id>"}   <- barge-in
Modeled on gvox-voice-proxy/app/plivo_protocol.py.
"""

import asyncio
import array
import base64
import collections
import json
import logging
import math
import os
import re
import struct
import time
from urllib.parse import unquote

try:
    from starlette.websockets import WebSocketState
except ImportError:          # pragma: no cover - starlette always ships with FastAPI
    WebSocketState = None

# numpy makes the per-sample audio hot paths (mulaw<->PCM, energy, recording mix) ~50-100x
# faster, which stops event-loop stalls from breaking the 20ms playout pacing on small VMs.
# Every function keeps a pure-Python fallback so the bridge still works without it.
try:
    import numpy as _np
except ImportError:          # pragma: no cover
    _np = None

logger = logging.getLogger(__name__)

from gemini_live import _SILENT_SCHEDULING
# google-genai >= 2.x: record_outcome is SILENT (its result never forces a turn); on <2.x this is None and the blocking-path forced-turn suppression applies.
_RSVP_SILENT = _SILENT_SCHEDULING is not None

# Mutual-goodbye detection: after the agent's goodbye + end_call, a bare "bye/thanks/okay" lets the hangup proceed; a real follow-up still cancels it.
# Transcripts are usually romanised, so the common Hindi / Gujarati question words are listed alongside the English ones.
_QUESTION_RE = re.compile(
    r"[?]|\b(what|whats|when|where|who|whom|which|how|why|can i|could|would you|"
    r"is it|are|do you|does|will|actually|wait|hold on|one (thing|sec|second|"
    r"question|more)|but|sorry|hello|hi|"
    r"kya|kab|kahan|kaise|kitne|kyun|kaun|shu|kyare|kem|ketla|batao|bolo)\b", re.I)
_GOODBYE_RE = re.compile(
    r"\b(bye+|goodbye|good ?bye|tata|ta ta|thanks|thank you|thankyou|cheers|"
    r"that'?s all|that is all|nothing else|nothing|i'?m done|we'?re done|see you|"
    r"good ?night|great|perfect|okay bye|ok bye|done)\b", re.I)

# "Hold on / give me a minute" means stay on THIS call (not a sign-off or callback) — keeps the line open for a grace window (see _hold_until).
_HOLD_RE = re.compile(
    r"\b(hold on|hold please|please hold|hang on|bear with me|one moment|just a "
    r"(sec|second|minute|moment)|give me (a|one|two|a couple|a few)|one (sec|second|minute|moment)|"
    r"two (secs|seconds|minutes)|a (minute|moment|sec|second)|wait|"
    r"ruko|ruk jao|rukiye|ek (minute|min|second|sec|pal|kshan)|thambo|thodi war|jara ruko)\b", re.I)

# Used only during the pending-hangup grace window: a genuine question / new info re-opens the call; a bare "hello/okay/hmm" must NOT re-engage the model.
_REAL_FOLLOWUP_RE = re.compile(
    r"[?]|\b(what|whats|when|where|who|which|how|why|can i|could|would|is it|are you|do you|does|"
    r"will|register|registration|bring|time|venue|address|dress|kids?|child|children|wife|husband|"
    r"family|parents?|mother|father|sister|brother|change|cancel|question|but|actually|"
    r"laptop|claude|chatgpt|chrome|setup|install|account|link|website|"
    r"kya|kab|kahan|kaise|kitne|kyun|kaun|shu|kyare|kem|ketla|batao|bolo|bacche|bachche|patni|pati)\b", re.I)

# A guest naming a language after the goodbye ("मैं गुजराती में बात करता हूं", "Hindi mein bolo") is asking
# to KEEP talking, in that language — it is a statement, not a question, so _REAL_FOLLOWUP_RE missed it and
# the bridge hung up on them. Latin names use \b; native-script names are plain substrings, because \b is
# unreliable next to Indic vowel signs (they are not \w in Python's re).
_LANGUAGE_SWITCH_RE = re.compile(
    r"\b(english|angrezi|hindi|gujarati|gujrati|marathi|punjabi|bengali|bangla|tamil|telugu|kannada|malayalam)\b"
    r"|अंग्रेज|इंग्लिश|हिंदी|हिन्दी|गुजराती|मराठी|पंजाबी|बंगाली|बांग्ला|तमिल|तेलुगु|कन्नड|मलयालम"
    r"|ગુજરાતી|હિન્દી|હિંદી|અંગ્રેજી|ઇંગ્લિશ|ਪੰਜਾਬੀ|বাংলা|தமிழ்|తెలుగు|ಕನ್ನಡ|മലയാളം", re.I)


def _looks_like_language_switch(text: str) -> bool:
    return bool(_LANGUAGE_SWITCH_RE.search(text or ""))


# "Hello? hello?" repeated while the agent is talking = the member can't hear (a LINE problem) — never a sign-off, never a callback request.
_HELLO_WORDS = frozenset(("hello", "helo", "hallo", "halo", "hullo", "hulo", "hi", "hey"))
_HEAR_RE = re.compile(
    r"\b(are you there|(can|could) you hear|hear me|sun(o| rahe| rahi| sakte)|awaa?z|sambhal|sunai)\b", re.I)


# Native-script forms of the words above. Since the input-transcription language hints went
# in, a Hindi or Gujarati guest comes back in Devanagari / Gujarati script — "हेलो",
# "आवाज़ नहीं आ रही", "एक मिनट रुकिए", "कहाँ है" — and the romanised lists no longer match
# them: a Gujarati "I couldn't hear anything" was read as a bare acknowledgement and the call
# hung up. Plain substrings, no \b: Indic vowel signs are not \w, so \b is unreliable there.
_HELLO_NATIVE = frozenset(("हेलो", "हैलो", "हलो", "हेल्लो", "હેલો", "હલો", "હેલ્લો"))
_HEAR_NATIVE_RE = re.compile(
    r"सुनाई|सुन नहीं|सुन पा|आवाज़? नहीं|आवाज़? कट|आवाज़? टूट|संभळा|सम्भळा|"
    r"સંભળા|સાંભળ|અવાજ|આવાજ")
_HOLD_NATIVE_RE = re.compile(
    r"रुकिए|रुकिये|रुको|रुक जा|एक मिनट|एक मिनिट|एक सेकंड|ज़रा रुक|"
    r"ઊભા રહો|ઉભા રહો|એક મિનિટ|એક સેકન્ડ|થોડી વાર")
_FOLLOWUP_NATIVE_RE = re.compile(
    r"कब|कहाँ|कहां|क्या|कैसे|कितने|कितना|क्यों|कौन|बताइए|बताइये|बताओ|"
    r"ક્યારે|ક્યાં|શું|કેવી રીતે|કેટલા|કેટલું|કોણ|કહો")


def _native_tokens(text: str) -> list:
    return [t.strip(".,!?।॥ ") for t in (text or "").split() if t.strip(".,!?।॥ ")]


def _looks_like_hello(text: str) -> bool:
    """True for a short bare "hello? hello?" / "can you hear me?" (≤5 words) — the hello-storm signal."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    if words and len(words) <= 5:
        if all(w in _HELLO_WORDS or re.sub(r"(.)\1+", r"\1", w) in _HELLO_WORDS for w in words):
            return True
        if _HEAR_RE.search(" ".join(words)):
            return True
    tokens = _native_tokens(text)
    if tokens and len(tokens) <= 5 and all(t in _HELLO_NATIVE or t.lower() in _HELLO_WORDS for t in tokens):
        return True
    # "I can't hear you" in Hindi/Gujarati is often a whole sentence — allow more words than
    # the bare-hello check, it is the phrase that matters.
    return bool(tokens) and len(tokens) <= 14 and bool(_HEAR_NATIVE_RE.search(text or ""))


def _hello_word_count(text: str) -> int:
    """Hello-words in ONE transcript ("Hello. Hello. Hello." → 3): a single event can be a whole storm."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    latin = sum(1 for w in words if w in _HELLO_WORDS or re.sub(r"(.)\1+", r"\1", w) in _HELLO_WORDS)
    return latin + sum(1 for t in _native_tokens(text) if t in _HELLO_NATIVE)


def _is_hold(text: str) -> bool:
    return bool(_HOLD_RE.search(text or "") or _HOLD_NATIVE_RE.search(text or ""))


def _is_real_followup(text: str) -> bool:
    return bool(_REAL_FOLLOWUP_RE.search(text or "") or _FOLLOWUP_NATIVE_RE.search(text or ""))


# The caller transcript comes back in the script of the language they spoke, so the script of
# their FIRST reply is a strong hint at their language. Not proof — an English sentence has
# come back in Devanagari ("सॉरी, आई कुड नॉट हियर यू") — so this only ever PROMPTS the model,
# which hears the audio, to check; it never decides. Devanagari covers Hindi and Marathi.
_SCRIPT_LANGUAGES = (
    ("ऀ", "ॿ", "Hindi or Marathi"),
    ("ঀ", "৿", "Bengali"),
    ("਀", "੿", "Punjabi"),
    ("઀", "૿", "Gujarati"),
    ("஀", "௿", "Tamil"),
    ("ఀ", "౿", "Telugu"),
    ("ಀ", "೿", "Kannada"),
    ("ഀ", "ൿ", "Malayalam"),
)


# A first reply this short says nothing about language: "haan", "hello", "ji" are how most
# guests answer any call, and the transcriber often writes them in Devanagari even when the
# guest then speaks English. A live test (Shivi, 25 Sep 02:10) was pushed into Hindi by
# exactly that. Only a reply with several words can carry a language signal.
_MIN_WORDS_FOR_LANGUAGE = 3
# Greeting / acknowledgement words in native script that carry no language signal at all,
# whatever the word count — "हेलो हेलो हाँ" is still just a hello.
_NEUTRAL_NATIVE = frozenset((
    "हेलो", "हैलो", "हलो", "हेल्लो", "हाँ", "हां", "हा", "जी", "ओके", "ठीक", "है", "अच्छा",
    "હેલો", "હલો", "હા", "જી", "ઓકે", "ઠીક", "સારું",
))


def _script_language(text: str):
    """The language a transcript's script points to, or None when it is mostly Latin, or
    when it is too short / too generic to tell (see _MIN_WORDS_FOR_LANGUAGE)."""
    tokens = _native_tokens(text)
    if len([t for t in tokens if t not in _NEUTRAL_NATIVE]) < _MIN_WORDS_FOR_LANGUAGE:
        return None
    counts, latin = {}, 0
    for ch in text or "":
        if "a" <= ch.lower() <= "z":
            latin += 1
            continue
        for lo, hi, lang in _SCRIPT_LANGUAGES:
            if lo <= ch <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if not counts:
        return None
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n > latin else None


def _script_share(text: str, lang: str) -> tuple:
    """(characters in `lang`'s script, Latin letters) in `text`."""
    rng = next(((lo, hi) for lo, hi, name in _SCRIPT_LANGUAGES if name == lang), None)
    own = sum(1 for ch in text or "" if rng and rng[0] <= ch <= rng[1])
    latin = sum(1 for ch in text or "" if "a" <= ch.lower() <= "z")
    return own, latin


def _looks_like_goodbye(text: str) -> bool:
    """True only for a short caller sign-off with no real follow-up/question."""
    t = (text or "").strip().lower()
    if not t or _QUESTION_RE.search(t) or _FOLLOWUP_NATIVE_RE.search(t):
        return False
    if len(re.findall(r"[a-z']+", t)) > 7:          # too long to be a simple sign-off
        return False
    return bool(_GOODBYE_RE.search(t))


# Stricter than _looks_like_goodbye, for the ONE place the bridge ends a call on the caller's word alone
# (after record_outcome, when the agent has not ended it). "thank you", "great", "perfect", "done", "nothing"
# are what a guest says while LISTENING to the details — treating them as a sign-off cut guests off mid-call.
# Only an unmistakable farewell counts here; anything softer is left to the agent's own end_call and the
# post-outcome silence window.
_CLEAR_FAREWELL_RE = re.compile(
    r"\b(bye+|goodbye|good ?bye|tata|ta ta|good ?night|see you|that'?s all|that is all|nothing else|"
    r"okay bye|ok bye|chalo bye|alvida|aavjo|milte hain|shubh ratri)\b"
    r"|शुभ ?रात्रि|अलविदा|बाय|आवजो|આવજો|બાય", re.I)


def _looks_like_clear_farewell(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or _QUESTION_RE.search(t) or _FOLLOWUP_NATIVE_RE.search(t):
        return False
    if len(t.split()) > 7:
        return False
    return bool(_CLEAR_FAREWELL_RE.search(t))


# Within-turn repeat guard: stop feeding NEW audio once a known closing marker is voiced twice inside one turn.
# Deliberately marker-only: a generic "any 5-word run repeats" rule fired on ordinary long replies (the event
# name and date recur naturally) and on transcription re-sends, muting the agent mid-sentence.
_CLOSING_MARKERS = (
    "see you on the", "see you on thirty", "so glad you", "so glad to have you",
    "we'll miss you", "we will miss you",
    "drop all the details", "receive all the details", "details on the whatsapp",
    "details on your whatsapp", "on the whatsapp group",
    "anything else i can help", "look forward to seeing you", "see you tomorrow",
    # Real failure modes: paraphrased double-invite / double-apology in one turn.
    "count you in", "sorry about that", "calling on behalf of", "calling from eo gujarat",
)

# Agent turn that ended with a question / RSVP re-ask — give the caller more thinking time before "are you still there?"
_AGENT_QUESTION_RE = re.compile(
    r"[?]|\b(count you in|can i count|shall i put|would you be able|"
    r"are you (still )?there|can we count)\b", re.I)


def _has_closing_repeat(turn_text: str) -> bool:
    """True when a known closing marker occurs twice in ONE turn's transcript (a doubled closing)."""
    t = re.sub(r"[^a-z0-9 ]", " ", (turn_text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    return any(t.count(m) >= 2 for m in _CLOSING_MARKERS)


def _looks_like_agent_question(turn_text: str) -> bool:
    """True if the agent's last turn asked something (RSVP re-ask or any '?')."""
    t = (turn_text or "").strip()
    if not t:
        return False
    return bool(_AGENT_QUESTION_RE.search(t))


def _ends_with_question(turn_text: str) -> bool:
    """True when the agent's turn FINISHES on a question — it is waiting for an answer.
    "Anything else? … have a lovely evening!" finishes on the goodbye and is not one."""
    t = (turn_text or "").strip().rstrip(" \"'”’)")
    return t.endswith("?") or t.endswith("？")


# An agent turn that reads like a sign-off — the ONLY kind of post-RSVP turn that may arm the bridge-side hangup.
# Deliberately separate from _CLOSING_MARKERS (those are repeat-guard markers and include non-closings).
# "speak soon" / "talk soon" are deliberately NOT here: they are how the agent paraphrases the
# escalation promise ("someone will speak to you soon"), and treating that as a goodbye hung up on
# guests who had just asked for a human. Everything left is an unambiguous farewell.
_CLOSING_PHRASE_RE = re.compile(
    r"\b(see you|take care|all set|bye+|goodbye|good ?bye|look forward|"
    r"have a (lovely|great|good|wonderful|nice)|we('ll| will) miss you|thanks for letting me know|"
    r"milte hain|milenge|aavjo|dhanyavaad|dhanyawad|shukriya|khayal rakhna|dhyan rakhna)\b", re.I)

# A turn that PROMISES a follow-up is the opposite of a goodbye — the guest asked for a person and
# is waiting. This must beat the closing check, or the escalation line ends the call it was meant
# to keep open.
_ESCALATION_RE = re.compile(
    r"\b(will notify|i'll notify|will inform|i'll inform|will let (the|them) \w+ know|"
    r"reach out to you|reach out to them|get back to you|someone will (call|contact|speak|reach)|"
    r"will pass (this|that|it) on|team will (call|contact|reach|get))\b", re.I)


def _looks_like_closing(turn_text: str) -> bool:
    """True when the agent's COMPLETED turn is a sign-off. A turn that ends on a question is waiting
    for an answer ("Are you all set to join us?", "Anything you'd like me to repeat?") — never a closing,
    even if a closing word appears inside it. Nor is a turn promising someone will follow up."""
    t = (turn_text or "").strip().rstrip(" \"'”’)")
    if not t or t.endswith("?"):
        return False
    if _ESCALATION_RE.search(t):
        return False                    # "someone will reach out shortly" — stay on the line
    return bool(_CLOSING_PHRASE_RE.search(t))

# Mulaw codec tables (ITU-T G.711)

# Mulaw -> Linear PCM16 decode table (256 entries)
_MULAW_DECODE = array.array("h")  # signed short
for _i in range(256):
    _v = ~_i
    _sign = _v & 0x80
    _exponent = (_v >> 4) & 0x07
    _mantissa = _v & 0x0F
    _sample = ((_mantissa << 3) + 0x84) << _exponent
    _sample -= 0x84
    if _sign:
        _sample = -_sample
    _MULAW_DECODE.append(max(-32768, min(32767, _sample)))

# Linear PCM16 -> Mulaw encode
_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635
_MULAW_EXP_TABLE = [0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
                     4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
                     5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
                     5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
                     6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
                     6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
                     6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
                     6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
                     7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7]


def _pcm16_to_mulaw_sample(sample: int) -> int:
    """Encode one PCM16 sample to mulaw byte."""
    sign = 0
    if sample < 0:
        sign = 0x80
        sample = -sample
    if sample > _MULAW_CLIP:
        sample = _MULAW_CLIP
    sample += _MULAW_BIAS
    exponent = _MULAW_EXP_TABLE[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


# Audio conversion functions

def mulaw_to_pcm16k(mulaw_bytes: bytes) -> bytes:
    """Convert mulaw 8kHz (Plivo) -> PCM 16-bit 16kHz (Gemini input)."""
    if not mulaw_bytes:
        return b""
    if _np is not None:
        s8 = _MULAW_DECODE_NP[_np.frombuffer(mulaw_bytes, dtype=_np.uint8)]
        # Upsample 8kHz -> 16kHz by linear interpolation (last sample duplicated)
        mid = (s8.astype(_np.int32) + _np.append(s8[1:], s8[-1]).astype(_np.int32)) >> 1
        out = _np.empty(s8.size * 2, dtype=_np.int16)
        out[0::2] = s8
        out[1::2] = mid.astype(_np.int16)
        return out.tobytes()
    samples_8k = [_MULAW_DECODE[b] for b in mulaw_bytes]
    # Upsample 8kHz -> 16kHz by linear interpolation
    samples_16k = []
    for i in range(len(samples_8k)):
        samples_16k.append(samples_8k[i])
        if i + 1 < len(samples_8k):
            samples_16k.append((samples_8k[i] + samples_8k[i + 1]) >> 1)
        else:
            samples_16k.append(samples_8k[i])
    return struct.pack(f"<{len(samples_16k)}h", *samples_16k)


# Squares of the decode table: frame energy is a pure table-lookup sum (runs on every inbound 20ms frame, so it must be cheap).
_MULAW_SQ = [int(v) * int(v) for v in _MULAW_DECODE]

# Full PCM16 -> mulaw lookup (indexed by unsigned 16-bit pattern) makes the hottest loop — every agent sample at 24kHz — one bytes-index; built once at import.
_PCM_TO_ULAW = bytes(
    _pcm16_to_mulaw_sample(_u - 65536 if _u >= 32768 else _u) for _u in range(65536)
)

# Vectorized copies of the lookup tables (built once at import; None without numpy).
if _np is not None:
    _MULAW_DECODE_NP = _np.array(_MULAW_DECODE, dtype=_np.int16)
    _MULAW_SQ_NP = _np.array(_MULAW_SQ, dtype=_np.int64)
    _PCM_TO_ULAW_NP = _np.frombuffer(_PCM_TO_ULAW, dtype=_np.uint8)


def _mulaw_frame_meansquare(mulaw_bytes: bytes) -> float:
    """Cheap energy of one inbound mulaw frame (mean of squared PCM16 samples).
    Table lookups only (vectorized when numpy is available) — used as a real-time
    voice-activity gate so silence/comfort-noise frames don't count as speech."""
    n = len(mulaw_bytes)
    if not n:
        return 0.0
    if _np is not None:
        return float(_MULAW_SQ_NP[_np.frombuffer(mulaw_bytes, dtype=_np.uint8)].mean())
    return sum(map(_MULAW_SQ.__getitem__, mulaw_bytes)) / n


def pcm24k_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert PCM 16-bit 24kHz (Gemini output) -> mulaw 8kHz (Plivo). Downsample 3:1 with a cheap
    3-tap average (a low-pass) instead of naive decimation, so frequencies above 4kHz don't alias
    into a metallic/robotic tone — this also makes the agent's LIVE voice clearer to the caller."""
    if _np is not None:
        s = _np.frombuffer(pcm_bytes[:len(pcm_bytes) & ~1], dtype=_np.int16)
        k = s.size // 3
        if k == 0:
            return b""
        avg = s[:k * 3].astype(_np.int32).reshape(k, 3).sum(axis=1) // 3
        return _PCM_TO_ULAW_NP[avg & 0xFFFF].tobytes()
    n_samples = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{n_samples}h", pcm_bytes[:n_samples * 2])
    lut = _PCM_TO_ULAW
    return bytes(lut[((samples[i] + samples[i + 1] + samples[i + 2]) // 3) & 0xFFFF]
                 for i in range(0, n_samples - 2, 3))


# 20ms of mulaw @ 8kHz = 160 bytes per frame
ULAW_FRAME_BYTES = 160
ULAW_FRAME_S = 0.020

# 20ms of digital silence as PCM16 @ 16kHz (320 samples × 2 bytes) — substituted (never dropped) for below-gate frames by the noise squelch.
_SILENCE_20MS_16K = bytes(640)


def _env_float(name, default):
    """One env-float parser for every tunable in this file."""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


# Soft connect melody looped over the 2-4s gap before Gemini's greeting; deliberately NOT a single repeating tone (reads like a countdown/alarm). Built once.
_HOLD_MUSIC_FRAMES = None


def _hold_music_frames():
    """One loop of the soft connect melody as a list of 20ms mulaw frames (built once, cached)."""
    global _HOLD_MUSIC_FRAMES
    if _HOLD_MUSIC_FRAMES is not None:
        return _HOLD_MUSIC_FRAMES
    rate = 8000

    def note(freq, dur, amp=0.24):
        # sine + soft 2nd harmonic; ~8ms attack then exponential decay so notes settle without clicking on/off.
        n = int(rate * dur)
        atk = max(1, int(rate * 0.008))
        out = []
        for i in range(n):
            env = (i / atk) if i < atk else math.exp(-3.2 * (i - atk) / n)
            s = math.sin(2 * math.pi * freq * i / rate) + 0.25 * math.sin(2 * math.pi * 2 * freq * i / rate)
            out.append(int(amp * env * 32767 * s / 1.25))
        return out

    def rest(dur):
        return [0] * int(rate * dur)

    C5, D5, E5, G5, A5 = 523.25, 587.33, 659.25, 783.99, 880.00   # warm mid-band, phone-safe
    b = 0.34
    phrase = [(E5, b), (G5, b), (A5, b), (G5, b), (E5, b), (D5, b), (C5, 2 * b)]
    pcm = []
    for f, d in phrase:
        pcm += note(f, d)
    pcm += rest(b)                                                # a small breath before the loop repeats
    if len(pcm) % ULAW_FRAME_BYTES:
        pcm += [0] * (ULAW_FRAME_BYTES - len(pcm) % ULAW_FRAME_BYTES)
    ulaw = bytes(_pcm16_to_mulaw_sample(s) for s in pcm)
    _HOLD_MUSIC_FRAMES = [ulaw[i:i + ULAW_FRAME_BYTES] for i in range(0, len(ulaw), ULAW_FRAME_BYTES)]
    return _HOLD_MUSIC_FRAMES


class PlivoMediaBridge:
    """Bridges a Plivo bidirectional Audio Stream WebSocket with a Gemini Live session."""

    def __init__(self, websocket, gemini_client, text_trigger, on_event=None,
                 resolve_identity=None, resolve_trigger=None, preopened=None,
                 listen_seconds=0):
        self.ws = websocket
        self.gemini = gemini_client
        self._preopened = preopened   # pre-warmed Gemini session handle (or None → cold connect)
        # Announce-then-listen agents (a reminder call) want a SHORTER post-outcome window
        # than a conversational one. This overrides EO_POST_RSVP_IDLE_SECONDS rather than
        # adding a second timer — two competing hangup paths is how call quality breaks.
        try:
            self.listen_seconds = float(listen_seconds or 0)
        except (TypeError, ValueError):
            self.listen_seconds = 0.0
        self.stream_id = None
        self.call_id = ""
        self.caller = ""
        self.generation = 0
        self.text_trigger = text_trigger
        self.on_event = on_event  # async callback for live transcript
        # (call_id, header_caller, header_name) -> (caller, first_name); personalises the greeting even when Plivo drops extraHeaders.
        self.resolve_identity = resolve_identity
        # call_id -> per-call opening trigger (inbound call-backs); '' keeps the defaults.
        self.resolve_trigger = resolve_trigger
        self._resolved_trigger = ""

        # Audio queue is BOUNDED (drop-oldest on overflow) so a stalled Gemini send can never buffer minutes of stale audio.
        self.audio_input_queue = asyncio.Queue(
            maxsize=max(10, int(_env_float("EO_AUDIO_INPUT_QUEUE_FRAMES", 150))))
        self.video_input_queue = asyncio.Queue()
        self.text_input_queue = asyncio.Queue()

        # Outbound (to Plivo) paced 20ms mulaw frames
        self._out_frames = asyncio.Queue()
        self._residual = bytearray()
        self._started = False
        self._call_end_emitted = False
        self._pending_hangup_task = None
        self._ending = False                     # hangup scheduled → drop any further agent audio (no re-greet)
        # Connect ringback fills the post-answer / pre-greeting gap; stopped at the agent's first audio.
        self._agent_audio_started = False
        self._connect_tone_task = None
        # Auto-hangup signals (so the call ends even if the agent never calls end_call):
        self._rsvp_recorded = False              # set True once record_outcome fires
        self._last_activity = time.monotonic()   # last time either party spoke / a turn ended
        self._turn_text = ""                     # accumulated agent transcript for the current turn
        self._suppress_turn = False              # drop the rest of this turn's audio (repeat detected)
        self._suppress_turn_at = 0.0             # when the repeat mute was armed (self-expires — see audio_output_callback)
        self._last_gemini_text_at = 0.0          # last agent output-transcription chunk (the turn is still streaming)
        # Interrupt confirmation + phantom recovery: recent VOICED inbound frames (energy VAD) decide whether a Gemini
        # "interrupted" is a real barge-in; a phantom keeps playback and asks the model to finish its sentence.
        self._voiced_ts = collections.deque(maxlen=64)   # monotonic ts of recent voiced 20ms frames
        self._interrupt_ignored_at = 0.0         # last phantom (ignored) Gemini interrupt
        self._resume_task = None                 # pending "finish your sentence" nudge after a phantom interrupt
        self._resume_count = 0                   # resume nudges sent this call (hard cap)
        self._mute_record_task = None            # delayed "say your closing" nudge after a silent record_outcome
        # "Hello? hello?" storm while the agent talks = the member can't hear (line trouble, never a callback).
        self._hello_ts = collections.deque(maxlen=8)     # recent bare-hello transcriptions
        self._hello_nudge_at = 0.0
        self._hello_nudge_count = 0
        # Stray forced-turn guard: blocking record_outcome forces one more model turn; when the agent already spoke, that turn is filler — drop its audio until the caller next speaks (keyed on VAD to dodge the turn_complete-vs-tool_call race).
        self._spoke_since_user = False           # agent emitted real audio since the caller last voiced
        self._suppress_post_record = False       # drop the forced post-record turn's audio
        self._did_suppress_audio = False         # a stray was actually dropped (gates the turn_complete clear)
        self._suppress_post_record_at = 0.0      # monotonic arm time (watchdog so the flag can never latch)
        # Real-time caller voice-activity leads the laggy transcription "user" events; starts at 0.0 so it reads "stale" until real speech.
        self._last_caller_audio = 0.0            # monotonic ts of the last VOICED inbound frame
        self._hold_until = 0.0                   # don't idle-hangup while now < this (caller asked to hold)
        self.first_name = ""                     # resolved member first name (for personalised nudges)
        self._hangup_done = False                # dialer.hangup_call already issued for this call
        self._last_user_event = 0.0              # monotonic ts of the last "user" transcription event
        self._last_agent_audio = 0.0             # monotonic ts of the last agent audio chunk
        self._turn_open = False                  # a model turn is being generated (first audio may lag)
        self._silence_nudged = False             # "are you still there?" asked for the current quiet spell
        self._silence_nudge_at = 0.0             # when the nudge was injected
        self._silence_nudge_count = 0            # total nudges this call (hard cap)
        self._silence_wrapup_at = 0.0            # when the wrap-up nudge was injected (0 = not yet)
        self._soft_end_at = 0.0                  # when a NON-muting hangup was scheduled (0 = none)
        self._greeting_sent_at = 0.0             # when the opening trigger was queued (0 = not yet)
        self._greeting_nudged = False            # the speak-NOW watchdog push was already sent
        self._reply_nudged = False               # missed-reply rescue sent for the current unanswered spell
        self._any_turn_complete = False          # a model turn fully completed at least once this call
        self._greeting_rescued = False           # once-per-call: opening re-sent after a pre-speech interrupt
        # After an RSVP re-ask / question, give the caller more thinking time before the silence nudge.
        self._last_agent_asked_question = False
        # Soft-hangup once after RSVP + closing turn if the agent never called end_call (stops bare "Hello" re-engage).
        self._post_rsvp_hangup_armed = False
        self._post_rsvp_closing_done = False
        # Wrap-up state: once the goodbye is given (end_call, a closing turn, or the caller's goodbye) only a REAL
        # question keeps the line open. A voice abort of the hangup is allowed EO_HANGUP_ABORT_MAX times per wrap-up
        # (a member who starts a question is saved), then a bare "hello?" / "ok" ends the call muted.
        self._wrapping_up = False
        self._hangup_aborts = 0
        self._abort_locked = False               # no further voice aborts (a bare hello/ack/goodbye decided the end)
        self._language_reopens = 0               # post-goodbye "switch to Gujarati" re-opens (capped)
        # The guest's FIRST reply decides the call's language (the prompt says so). When it came
        # back in a non-Latin script and the agent still answers in English, prompt it once.
        # Set when end_call arrives inside the current agent turn; checked at turn_complete so a
        # turn that ENDS ON A QUESTION ("…is there anything else?") keeps the line open.
        self._end_call_this_turn = False
        self._last_turn_ended_on_question = False   # cleared the moment the guest speaks
        self._first_reply_seen = False
        self._reply_language = None
        self._post_reply_agent_text = ""
        self._language_nudged = False
        self._goodbye_drained = False            # the goodbye audio has fully played out
        # Noise squelch (EO_NOISE_GATE, default OFF): below-gate frames are replaced by digital silence, NEVER dropped — the server VAD must hear the quiet to close a turn.
        self._gate_on = os.getenv("EO_NOISE_GATE", "false").strip().lower() in ("1", "true", "yes", "on")
        self._gate_thr = _env_float("EO_NOISE_GATE_RMS", 250) ** 2
        self._gate_thr_low = self._gate_thr / 4  # hysteresis: sustained speech stays open above this
        self._gate_hangover = _env_float("EO_NOISE_GATE_HANGOVER_S", 0.6)
        self._gate_voiced_at = 0.0               # last frame above the gate (hysteresis anchor)
        self._gate_frames = 0                    # total inbound frames (squelch-ratio log)
        self._gate_squelched = 0                 # frames replaced with silence
        try:
            self._vad_ms_threshold = float(os.getenv("EO_VAD_RMS_THRESHOLD", "500")) ** 2
        except ValueError:
            self._vad_ms_threshold = 500.0 ** 2
        # Call recording: mix caller + agent mulaw into one mono 8k PCM16 timeline, written to WAV at call end; guarded so a recording failure can never affect the live call.
        self._rec_on = os.getenv("EO_RECORD_CALLS", "true").strip().lower() not in ("0", "false", "no", "off")
        self._rec_t0 = None
        self._rec = array.array("h")             # mono 8kHz PCM16 mix (sample-indexed timeline)
        try:
            self._rec_max_samples = int(float(os.getenv("EO_RECORD_MAX_SECONDS", "420")) * 8000)
        except ValueError:
            self._rec_max_samples = 420 * 8000

    def _rec_add(self, mulaw_bytes):
        """Mix one ~20ms mulaw frame (either direction) into the recording timeline at its
        real-time offset. Guarded — never raises into the live audio path."""
        if not self._rec_on or not mulaw_bytes:
            return
        try:
            now = time.monotonic()
            if self._rec_t0 is None:
                self._rec_t0 = now
            start = int((now - self._rec_t0) * 8000)
            if start > self._rec_max_samples:
                return                            # cap runaway recordings
            buf = self._rec
            n = len(buf)
            if n < start:                         # silence gap since the last frame
                buf.frombytes(bytes(2 * (start - n)))   # append (start-n) zero int16 samples
                n = start
            if _np is not None:
                dec_np = _MULAW_DECODE_NP[_np.frombuffer(mulaw_bytes, dtype=_np.uint8)]
                ov = min(n - start, len(dec_np)) if start < n else 0
                if ov > 0:                        # overlap (barge-in) — AVERAGE (-6dB mix), no clip needed
                    # .astype copies immediately, so no numpy view keeps buf's buffer exported
                    # (a live export would make the frombytes append below raise BufferError).
                    old = _np.frombuffer(buf, dtype=_np.int16, count=ov,
                                         offset=start * 2).astype(_np.int32)
                    mixed = (old + dec_np[:ov].astype(_np.int32)) >> 1
                    buf[start:start + ov] = array.array("h", mixed.astype(_np.int16).tobytes())
                if ov < len(dec_np):
                    buf.frombytes(dec_np[ov:].tobytes())
                return
            dec = _MULAW_DECODE
            for i, b in enumerate(mulaw_bytes):
                s = dec[b]
                idx = start + i
                if idx < n:                       # overlap (barge-in) — AVERAGE (no clip) not raw sum
                    v = (buf[idx] + s) >> 1       # -6dB mix keeps both voices in-range, no distortion
                    buf[idx] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
                else:
                    buf.append(s)
        except Exception:
            pass

    def _write_recording(self):
        """Flush the mixed timeline to a mono/8kHz/16-bit WAV keyed by call_sid. Guarded."""
        if not self._rec_on or not self._rec or not self.call_id:
            return
        try:
            import wave
            import store
            path = store.recording_path(self.call_id)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(self._rec.tobytes())
            logger.info(f"Saved call recording: {path} ({len(self._rec) / 8000:.0f}s)")
        except Exception as e:
            logger.warning(f"Failed to write call recording: {e}")

    # Outbound (Gemini -> Plivo)

    async def _play_connect_tone(self):
        """Fill the gap between the caller answering and the agent's first words with a soft
        music-box melody, so they never hear dead air. Loops the phrase until the agent starts
        speaking or a safety cap elapses (in case Gemini never produces audio)."""
        try:
            cap_s = min(float(os.getenv("EO_CONNECT_TONE_MAX_S", "8")), 15.0)
        except ValueError:
            cap_s = 8.0
        frames = _hold_music_frames()
        started = time.monotonic()
        i = 0
        try:
            while not self._agent_audio_started and (time.monotonic() - started) < cap_s:
                if self.stream_id:
                    await self._out_frames.put(frames[i % len(frames)])
                i += 1
                await asyncio.sleep(ULAW_FRAME_S)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"connect-tone error: {e}")

    def _stop_connect_tone(self):
        """Agent audio is starting (or the call is ending): stop the ringback and drop any
        of its frames still queued, so the greeting plays immediately with no tail."""
        self._agent_audio_started = True
        if self._connect_tone_task and not self._connect_tone_task.done():
            self._connect_tone_task.cancel()
        try:
            while True:
                self._out_frames.get_nowait()
        except asyncio.QueueEmpty:
            pass

    async def audio_output_callback(self, data: bytes):
        """Gemini produced audio (24k PCM16). Convert to mulaw 8k and chunk into 20ms frames."""
        if not self.stream_id:
            return
        if not self._agent_audio_started:
            self._stop_connect_tone()            # first real agent audio → cut the connect ringback
            if self._greeting_sent_at > 0.0:
                logger.info(f"GREETING LATENCY: {time.monotonic() - self._greeting_sent_at:.2f}s "
                            f"from trigger to first agent audio")
        if self._ending:
            return                               # call is wrapping up — never play a re-greet / extra audio
        if self._suppress_turn:
            # a repeated closing was detected — drop the duplicate audio; self-expires so a missing turn_complete can never leave the agent mute
            if time.monotonic() - self._suppress_turn_at <= _env_float("EO_SUPPRESS_TURN_MAX_S", 4.0):
                return
            self._suppress_turn = False
        if self._suppress_post_record:
            # Forced tool-result turn after the agent already spoke is filler — drop it; if armed too long, fall through rather than stay muted.
            if time.monotonic() - self._suppress_post_record_at <= 4.0:
                self._did_suppress_audio = True
                return
            self._suppress_post_record = False
        now_a = time.monotonic()
        if not self._spoke_since_user:
            # First agent audio since the caller last voiced = start of the audible reply; log the turn latency.
            if self._last_caller_audio > 0.0:
                since_voice = now_a - self._last_caller_audio
                since_text = (now_a - self._last_user_event) if self._last_user_event > 0.0 else -1.0
                logger.info(f"TURN LATENCY: first agent audio {since_voice:.2f}s after caller's last "
                            f"voiced frame ({since_text:.2f}s after their transcription)")
        self._last_agent_audio = now_a
        # _silence_nudged is deliberately NOT reset here — the nudge's own audio would clear it; only voiced caller audio clears it (see handle_plivo_messages).
        self._reply_nudged = False               # the agent IS replying — re-arm the missed-reply rescue
        self._spoke_since_user = True            # a genuine agent frame is going out this "since-caller" window
        try:
            self._residual.extend(pcm24k_to_mulaw(data))
            while len(self._residual) >= ULAW_FRAME_BYTES:
                frame = bytes(self._residual[:ULAW_FRAME_BYTES])
                del self._residual[:ULAW_FRAME_BYTES]
                await self._out_frames.put(frame)
        except Exception as e:
            logger.error(f"Error queuing audio for Plivo: {e}")

    def _ws_connected(self) -> bool:
        """Best-effort: is the Plivo WebSocket still open in both directions?"""
        if WebSocketState is None:
            return True
        try:
            return (self.ws.client_state == WebSocketState.CONNECTED
                    and self.ws.application_state == WebSocketState.CONNECTED)
        except Exception:
            return True

    async def _outbound_sender(self):
        """Send queued mulaw frames to Plivo, paced at 20ms for smooth playout.

        This task is the ONLY consumer of _out_frames, so it must survive transient send
        errors (a single failure here used to silently mute the agent for the rest of the
        call). A transient error is logged and skipped; a closed socket — or a burst of
        consecutive failures — ends the task, which ends the bridge (run() waits on us)."""
        # Jitter pre-buffer: at the start of each audio burst (after an idle gap), let a few
        # frames accumulate before playout begins, so a hiccup in Gemini's streaming never
        # becomes an audible mid-sentence gap. Barge-in still flushes instantly (clearAudio).
        prebuf_frames = max(0, int(_env_float("EO_PREBUFFER_MS", 160) / (ULAW_FRAME_S * 1000)))
        next_t = None
        failures = 0
        try:
            while True:
                if self._out_frames.empty():
                    wait0 = time.monotonic()
                    frame = await self._out_frames.get()
                    # Only a REAL idle gap (>60ms) starts a new burst — the connect tone's
                    # 20ms-paced frames and normal steady streaming never trigger this.
                    if prebuf_frames > 1 and time.monotonic() - wait0 > 0.06:
                        deadline = time.monotonic() + prebuf_frames * ULAW_FRAME_S
                        while (self._out_frames.qsize() + 1 < prebuf_frames
                               and time.monotonic() < deadline):
                            await asyncio.sleep(0.01)
                        next_t = None              # resync pacing after the idle gap
                else:
                    frame = await self._out_frames.get()
                if not self.stream_id:
                    continue
                try:
                    payload = base64.b64encode(frame).decode("ascii")
                    await self.ws.send_json({
                        "event": "playAudio",
                        "streamId": self.stream_id,
                        "media": {
                            "contentType": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "payload": payload,
                        },
                    })
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failures += 1
                    if not self._ws_connected():
                        logger.info(f"Plivo socket closed while sending audio ({e}); ending bridge")
                        break
                    if failures >= 25:             # ~0.5s of continuous failures on an "open" socket
                        logger.error(f"Plivo outbound sender: {failures} consecutive send failures "
                                     f"({e}); ending bridge")
                        break
                    logger.warning(f"Plivo outbound send failed (transient, #{failures}): {e}")
                    # keep the frame cadence on failures too — otherwise a burst burns the 25-failure budget in microseconds instead of ~0.5s
                    await asyncio.sleep(ULAW_FRAME_S)
                    continue
                self._rec_add(frame)               # record what the caller heard (agent side)
                now = time.monotonic()
                self._last_agent_audio = now       # PLAYOUT time (paced) — silence timers key on this
                next_t = (next_t or now) + ULAW_FRAME_S
                delay = next_t - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.1:
                    next_t = time.monotonic()  # fell behind, resync
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Plivo outbound sender error: {e}")

    def _drain_outbound(self):
        self._residual.clear()
        n = 0
        try:
            while True:
                self._out_frames.get_nowait()
                n += 1
        except asyncio.QueueEmpty:
            pass
        return n

    async def _flush_playout(self):
        """Drop queued frames AND tell Plivo to clear already-buffered playout (barge-in / repeat cut)."""
        dropped = self._drain_outbound()
        if dropped:
            logger.info(f"Barge-in: cleared {dropped} queued frames (agent audio flushed)")
        if not self.stream_id:
            return
        try:
            await self.ws.send_json({"event": "clearAudio", "streamId": self.stream_id})
        except Exception:
            pass

    async def audio_interrupt_callback(self):
        """Barge-in: drop queued agent audio + tell Plivo to flush its playout.

        Phantom-interrupt gate: Gemini's server VAD can fire `interrupted` off line echo of the
        agent's own voice, a click or a cough. Honour it only if OUR energy VAD saw SUSTAINED
        voiced caller audio — at least EO_INTERRUPT_MIN_VOICED_MS inside the last
        EO_INTERRUPT_CONFIRM_WINDOW_S; a single loud frame is not a barge-in. Otherwise keep
        playback — but Gemini has still stopped generating mid-sentence, so ask it to finish
        (_resume_after_phantom) instead of leaving the member in silence."""
        if not self.stream_id:
            return
        confirm_s = _env_float("EO_INTERRUPT_CONFIRM_WINDOW_S", 0.8)
        min_ms = _env_float("EO_INTERRUPT_MIN_VOICED_MS", 120)
        if confirm_s > 0:
            voiced_ms = self._voiced_ms_within(confirm_s)
            if voiced_ms < min_ms:
                logger.info(f"Ignoring Gemini interrupt: {voiced_ms:.0f}ms voiced caller audio in the last "
                            f"{confirm_s:.1f}s (echo/noise phantom) — keeping playback")
                self._interrupt_ignored_at = time.monotonic()
                self._schedule_resume_after_phantom()
                return
        await self._flush_playout()

    def _voiced_ms_within(self, window_s: float, now: float | None = None) -> float:
        """Milliseconds of VOICED inbound frames (energy VAD) seen in the last `window_s` seconds."""
        now = time.monotonic() if now is None else now
        cutoff = now - window_s
        return 20.0 * sum(1 for ts in self._voiced_ts if ts >= cutoff)

    def _schedule_resume_after_phantom(self):
        """A phantom interrupt kept our playback but Gemini still stopped mid-sentence: once the
        dust settles (and only if the member really didn't speak) ask it to finish the unsaid part."""
        if self._resume_task and not self._resume_task.done():
            return
        if self._wrapping_up:
            return                                   # after the goodbye a "resume" would replay the closing
        if self._resume_count >= int(_env_float("EO_PHANTOM_RESUME_MAX", 2)):
            return
        was_streaming = (self._turn_open or not self._out_frames.empty()
                         or (time.monotonic() - self._last_agent_audio) < 1.0)
        if not was_streaming:
            return
        self._resume_task = asyncio.create_task(self._resume_after_phantom(self._interrupt_ignored_at))

    def _cancel_resume(self):
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
        self._resume_task = None

    async def _resume_after_phantom(self, ignored_at: float):
        try:
            await asyncio.sleep(_env_float("EO_PHANTOM_RESUME_DELAY_S", 1.2))
        except asyncio.CancelledError:
            return
        now = time.monotonic()
        max_n = int(_env_float("EO_PHANTOM_RESUME_MAX", 2))
        if (self._ending or self._wrapping_up
                or (self._pending_hangup_task and not self._pending_hangup_task.done())
                or self._last_user_event >= ignored_at                       # the member did say something
                or self._voiced_ms_within(now - ignored_at, now) >= _env_float("EO_INTERRUPT_MIN_VOICED_MS", 120)
                or self._resume_count >= max_n):
            return
        self._resume_count += 1
        logger.info(f"Phantom interrupt cut the turn; asking the agent to resume ({self._resume_count}/{max_n})")
        await self.text_input_queue.put(
            "[Line noise cut you off mid-sentence — the member did NOT speak. Continue exactly from "
            "where you stopped and finish only the unsaid part in one short breath. Do not restart, "
            "do not apologise, do not ask if they are there.]")

    async def _maybe_rescue_greeting(self):
        """Once per call: if an interrupt killed the opening before ANY evidence of a live
        caller (no transcription event, no voiced frame), re-send the opening. Zero-evidence
        only — a genuine barge-in, hold, or wrap-up must never re-greet."""
        if (self._greeting_rescued or self._any_turn_complete or self._ending or self._wrapping_up
                or (self._pending_hangup_task and not self._pending_hangup_task.done())
                or self._greeting_sent_at <= 0.0
                or self._last_user_event > 0.0
                or self._last_caller_audio > 0.0):
            return
        self._greeting_rescued = True
        self._greeting_sent_at = time.monotonic()
        logger.info("Greeting interrupted by noise before any caller speech; re-sending opening (once)")
        await self.text_input_queue.put(
            "[Line noise cut off your opening before the member heard it. "
            "Say your opening line again now — just once, warmly.]")

    # Inbound (Plivo -> Gemini)

    @staticmethod
    def _extract_header(data: dict, start: dict, name: str) -> str:
        name = name.lower()
        raw = (data.get("extra_headers") or data.get("extraHeaders")
               or start.get("extra_headers") or start.get("extraHeaders"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if str(k).strip().lower() == name:
                    return unquote(str(v))
            return ""
        if isinstance(raw, str) and raw:
            for pair in raw.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k.strip().lower() == name:
                        return unquote(v.strip())
        return ""

    async def handle_plivo_messages(self):
        """Receive messages from the Plivo Audio Stream WebSocket."""
        try:
            while True:
                message = await self.ws.receive_text()
                data = json.loads(message)
                event = str(data.get("event") or data.get("type") or "").lower()

                if event == "start":
                    start = data.get("start") if isinstance(data.get("start"), dict) else data
                    self.stream_id = str(start.get("streamId") or start.get("stream_id")
                                         or data.get("streamId") or "")
                    self.call_id = str(start.get("callId") or start.get("call_id") or "")
                    self.caller = self._extract_header(data, start, "x-caller")
                    header_name = self._extract_header(data, start, "x-caller-name")
                    gen_raw = self._extract_header(data, start, "x-callback-gen")
                    try:
                        self.generation = int(gen_raw) if gen_raw else 0
                    except ValueError:
                        self.generation = 0
                    # Resolve identity BEFORE emitting call_start (which may pop the pending-call metadata the resolver relies on).
                    first_name = ""
                    if self.resolve_identity:
                        try:
                            caller, first_name = self.resolve_identity(
                                self.call_id, self.caller, header_name)
                            self.caller = caller or self.caller
                        except Exception as e:
                            logger.warning(f"resolve_identity failed: {e}")
                    self.first_name = first_name or ""
                    # Same pre-emit constraint for the per-call opening trigger (inbound call-backs).
                    if self.resolve_trigger:
                        try:
                            self._resolved_trigger = self.resolve_trigger(self.call_id) or ""
                        except Exception as e:
                            logger.warning(f"resolve_trigger failed: {e}")
                    logger.info(f"Plivo stream started: stream_id={self.stream_id}, "
                                f"call={self.call_id}, caller={self.caller}, "
                                f"named={'yes' if first_name else 'no'}, gen={self.generation}")
                    if not self._started:
                        self._started = True
                        await self._emit({"type": "call_start", "call_sid": self.call_id or "",
                                          "caller": self.caller or "",
                                          "generation": self.generation})
                        # Soft connect ringback (once per call) so the caller isn't met with silence.
                        if not self._agent_audio_started:
                            self._connect_tone_task = asyncio.create_task(self._play_connect_tone())
                        # Opening trigger stays INSIDE the once-only guard: a duplicate Plivo `start` must NOT re-send it, or the agent re-reads its whole opening mid-call.
                        if self._resolved_trigger:
                            # Per-call trigger (inbound call-back: "thanks for calling back…") replaces both defaults.
                            trigger = self._resolved_trigger
                        elif first_name:
                            trigger = (f"[The guest has just answered. Their first name is {first_name}. "
                                       f"Begin THE OPENING: your first turn is EXACTLY "
                                       f'"Hello! Am I speaking to {first_name}?" — say ONLY that, then STOP '
                                       f"and wait. Do NOT give your message or introduce yourself until "
                                       f"you know who answered. Use the name naturally, never overuse it.]")
                        else:
                            trigger = self.text_trigger
                        await self.text_input_queue.put(trigger)
                        self._greeting_sent_at = time.monotonic()
                    else:
                        logger.info("Duplicate Plivo 'start' ignored (greeting already sent) — "
                                    "not re-triggering the opening")

                elif event == "media":
                    media = data.get("media") or {}
                    payload = media.get("payload")
                    if payload:
                        mulaw_bytes = base64.b64decode(payload)
                        # Real-time VAD: stamp caller activity on VOICED frames (long before transcription) so idle/hangup guards never fire mid-speech; energy-gated since Plivo streams ~20ms frames non-stop.
                        track = str(media.get("track") or "inbound").lower()
                        if track == "inbound":
                            self._rec_add(mulaw_bytes)   # record the caller side (all frames)
                            frame_ms = _mulaw_frame_meansquare(mulaw_bytes)
                            now_m = time.monotonic()
                            if frame_ms >= self._vad_ms_threshold:
                                self._last_caller_audio = now_m
                                self._last_activity = now_m
                                self._voiced_ts.append(now_m)   # sustained-voice evidence for the interrupt gate
                                # Caller is speaking now → the agent's next audio is a genuine reply, not forced-turn filler; clear the stray guard and the "agent spoke" signal.
                                self._spoke_since_user = False
                                self._suppress_post_record = False
                                self._did_suppress_audio = False
                                self._silence_nudged = False
                                self._silence_wrapup_at = 0.0
                            if self._gate_on:
                                # SUBSTITUTE silence for below-gate frames (Gemini's VAD needs to HEAR the quiet); hysteresis + hangover so onsets/tails and inter-word gaps are never clipped.
                                if frame_ms >= self._gate_thr or (
                                        frame_ms >= self._gate_thr_low
                                        and (now_m - self._gate_voiced_at) <= self._gate_hangover):
                                    self._gate_voiced_at = now_m
                                self._gate_frames += 1
                                if (now_m - self._gate_voiced_at) <= self._gate_hangover:
                                    self._put_audio(mulaw_to_pcm16k(mulaw_bytes))
                                else:
                                    self._gate_squelched += 1
                                    self._put_audio(_SILENCE_20MS_16K)
                            else:
                                self._put_audio(mulaw_to_pcm16k(mulaw_bytes))
                        else:
                            self._put_audio(mulaw_to_pcm16k(mulaw_bytes))

                elif event == "dtmf":
                    pass

                elif event == "stop":
                    logger.info("Plivo stream stopped")
                    break

        except Exception as e:
            # WebSocket close 1000 is a normal caller hangup, not an error — log it quietly.
            code = e.args[0] if getattr(e, "args", None) else None
            if code == 1000:
                logger.info("Plivo stream closed by caller (hangup)")
            else:
                logger.error(f"Plivo receive error: {e}")

    def _put_audio(self, chunk: bytes):
        """Enqueue one PCM chunk for Gemini, dropping the OLDEST frame when full — if the
        Gemini send ever stalls, staying live matters more than replaying stale audio."""
        q = self.audio_input_queue
        while True:
            try:
                q.put_nowait(chunk)
                return
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass

    async def _emit(self, event):
        """Send event to live transcript watchers."""
        if self.on_event:
            try:
                await self.on_event(event)
            except Exception:
                pass

    async def _drain_then_hangup(self):
        """Hang up gently: wait until the paced outbound buffer (the goodbye) has
        been STABLY empty (so trailing audio is fully sent, never cut mid-word),
        then give Plivo's own playout a gentle beat to finish before hanging up."""
        import dialer
        try:
            # hard-capped at 1.0s so a stale .env can't drag out the hangup
            grace = min(float(os.getenv("CALL_HANGUP_GRACE_SECONDS", "1.0")), 1.0)
        except ValueError:
            grace = 1.0
        # If the agent's farewell is still being generated (first audio can lag 1-3s), give it a bounded head start — otherwise a fast grace window hangs up between "bye" and the reply.
        try:
            farewell_wait = min(float(os.getenv("EO_FAREWELL_WAIT_SECONDS", "4")), 8.0)
        except ValueError:
            farewell_wait = 4.0
        waited = 0.0
        # Only when a reply is actually expected: the caller HAS spoken and the mute isn't armed (armed → no audio can ever arrive).
        while (waited < farewell_wait and not self._ending
               and self._last_user_event > 0.0
               and self._out_frames.empty() and not self._residual
               and self._last_agent_audio <= self._last_user_event):
            await asyncio.sleep(0.05)
            waited += 0.05
        stable = 0
        for _ in range(600):                       # up to ~12s
            if self._out_frames.empty() and not self._residual:
                stable += 1
                if stable >= 10:                   # ~0.2s of continuous silence sent
                    break
            else:
                stable = 0                         # more audio arrived; keep waiting
            await asyncio.sleep(0.02)
        await asyncio.sleep(grace)                 # let Plivo finish playing + a natural pause
        if self._wrapping_up:
            self._goodbye_drained = True           # the goodbye has fully played out
        # Last-instant save: if the caller is voicing right now (transcription lagging), abort and keep the line up.
        if self._try_voice_abort("within abort window"):
            return
        if self.call_id:
            self._hangup_done = True
            await dialer.hangup_call(self.call_id)

    def _caller_voiced_recently(self) -> bool:
        """True if the caller produced VOICE within the abort window — used to cancel a
        pending hangup so we never cut someone off who's just started talking. Keyed on
        inbound-only audio, so the agent's own goodbye can never trigger it."""
        try:
            window = float(os.getenv("EO_HANGUP_ABORT_WINDOW_SECONDS", "1.2"))
        except ValueError:
            window = 1.2
        return (time.monotonic() - self._last_caller_audio) <= window

    async def _max_duration_guard(self):
        """Safety net: hang up a call that runs longer than CALL_MAX_SECONDS."""
        try:
            max_s = int(os.getenv("CALL_MAX_SECONDS", "420"))
        except ValueError:
            max_s = 420
        try:
            await asyncio.sleep(max_s)
            logger.warning(f"Call {self.call_id} exceeded {max_s}s; hanging up")
            import dialer
            if self.call_id:
                await dialer.hangup_call(self.call_id)
        except asyncio.CancelledError:
            pass

    async def _idle_hangup_guard(self):
        """Keyword-free auto-hangup: end the call when the line goes quiet — quickly once
        the RSVP is recorded (task done), and after a longer window for a dead/abandoned
        call. This is what guarantees the call ends even if the agent never calls end_call.
        The actual hangup goes through _schedule_end (idempotent + grace-cancellable).

        Silence check (EO_SILENCE_CHECK, default on): before the RSVP is settled, a quiet
        spell first gets ONE warm "are you still there?" nudge (after X s of mutual
        silence), then a wrap-up nudge + hangup (after Y more s of caller silence)."""
        def _cfg(name, default):
            try:
                return float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default
        # The agent's own listen window wins over the server-wide default: a reminder
        # agent announces and hangs up shortly after; a logistics agent stays for a chat.
        post_rsvp = self.listen_seconds or _cfg("EO_POST_RSVP_IDLE_SECONDS", 12.0)
        dead_air = _cfg("EO_IDLE_HANGUP_SECONDS", 25.0)
        nudge_on = os.getenv("EO_SILENCE_CHECK", "true").strip().lower() not in ("0", "false", "no", "off")
        nudge_x = _cfg("EO_SILENCE_PROMPT_SECONDS", 6.0)
        nudge_after_q = _cfg("EO_SILENCE_AFTER_QUESTION_SECONDS", 12.0)
        nudge_y = _cfg("EO_SILENCE_HANGUP_SECONDS", 10.0)
        nudge_max = int(_cfg("EO_SILENCE_NUDGE_MAX", 2))
        nudge_cooldown = _cfg("EO_SILENCE_NUDGE_COOLDOWN_S", 15.0)
        greet_nudge_s = _cfg("EO_GREETING_NUDGE_SECONDS", 4.0)
        reply_rescue_s = _cfg("EO_UNANSWERED_REPLY_SECONDS", 2.5)    # caller finished, no reply yet
        deaf_rescue_s = _cfg("EO_DEAF_RESCUE_SECONDS", 6.0)           # caller keeps voicing, agent mute
        try:
            while True:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                # Greeting watchdog: Gemini occasionally stalls 5-7s on the opening line; one firm push after ~4s almost always unsticks it.
                if (not self._agent_audio_started and not self._greeting_nudged
                        and self._greeting_sent_at > 0.0
                        and now - self._greeting_sent_at >= greet_nudge_s):
                    self._greeting_nudged = True
                    logger.info(f"Greeting not spoken after {now - self._greeting_sent_at:.1f}s; "
                                f"pushing the agent to speak")
                    await self.text_input_queue.put(
                        "[Speak your opening line NOW — the member is waiting on a silent line.]")
                    continue
                if self._pending_hangup_task and not self._pending_hangup_task.done():
                    continue                       # already ending
                if now < self._hold_until:
                    continue                       # caller asked to hold — keep the line open
                # MUTUAL silence: activity events (text, turns, caller voice) AND agent PLAYOUT — a long turn's text
                # ends seconds before its audio does, so every idle timer keys on the later of the two.
                quiet_for = now - max(self._last_activity, self._last_agent_audio, self._last_caller_audio)
                # turn_complete may never come (text-triggered greeting / unregistered caller turn) — self-expire the turn after 3s of agent silence with a drained queue, or the nudge ladder stays muzzled.
                agent_quiet = (self._out_frames.empty() and not self._residual
                               and (not self._turn_open
                                    or now - self._last_agent_audio >= 3.0))
                if self._rsvp_recorded and agent_quiet and quiet_for >= post_rsvp:
                    logger.info(f"Quiet {quiet_for:.0f}s after RSVP (mutual silence, agent drained); scheduling hangup")
                    # after a voice-aborted goodbye with no transcript ever arriving: end muted, never re-greet
                    self._schedule_end(mute=self._wrapping_up)
                    continue
                # Never nudge while the model is still streaming this turn's text (its audio may simply be lagging
                # or it was told to finish a cut sentence) — the ladder is for MUTUAL silence, not a slow turn.
                if ((self._turn_open and now - self._last_gemini_text_at < 3.0)
                        or (self._resume_task and not self._resume_task.done())):
                    continue
                if nudge_on and not self._rsvp_recorded and not self._wrapping_up and self._agent_audio_started \
                        and not self._ending and agent_quiet:
                    # Missed-reply rescue: Gemini sometimes never registers a caller reply as a turn (a short
                    # "yeah, hello" at LOW start sensitivity). Two paths, both requiring the caller to have spoken
                    # AFTER the agent's last audio: FAST — the caller finished `reply_rescue_s` ago and nothing came
                    # back; DEAF — the caller keeps voicing ("hello? hello?") while the agent has been mute for
                    # `deaf_rescue_s` (keyed on the AGENT's silence so the repeats can't reset it).
                    agent_silent = now - self._last_agent_audio
                    caller_silent = now - self._last_caller_audio
                    # The FAST path needs the caller to have paused. The DEAF path must not (a guest
                    # going "hello? hello?" into a mute agent never pauses), so it keys only on the
                    # agent's silence — and the nudge text itself forbids re-delivering anything,
                    # because on a live test it fired mid-sentence and the whole schedule was
                    # spoken twice.
                    if (not self._reply_nudged
                            and self._last_caller_audio > self._last_agent_audio
                            and ((caller_silent >= reply_rescue_s and agent_silent >= reply_rescue_s)
                                 or agent_silent >= deaf_rescue_s)):
                        self._reply_nudged = True
                        logger.info(f"Agent silent {agent_silent:.1f}s since its last audio, caller quiet "
                                    f"{caller_silent:.1f}s; prompting it to reply")
                        await self.text_input_queue.put(
                            "[The member just said something and is waiting. If you caught it, "
                            "reply to THAT in one short line — never re-deliver the schedule or anything "
                            "you already said; if you did not catch it, politely ask them to repeat. "
                            "ONE short line only — never re-deliver something you already said; "
                            "if a question of yours is still unanswered, just re-ask it briefly.]")
                        continue
                    # After an RSVP re-ask, give the caller thinking time before "are you still there?"
                    need_quiet = nudge_after_q if self._last_agent_asked_question else nudge_x
                    if (not self._silence_nudged and quiet_for >= need_quiet
                            and self._silence_nudge_count < nudge_max
                            and (self._silence_nudge_at == 0.0
                                 or now - self._silence_nudge_at >= nudge_cooldown)):
                        self._silence_nudged = True
                        self._silence_nudge_at = now
                        self._silence_nudge_count += 1
                        who = f"'{self.first_name}, are you still there? I can't hear you.'" \
                            if self.first_name else "'Hello — are you still there? I can't hear you.'"
                        logger.info(f"Quiet for {quiet_for:.0f}s; injecting are-you-still-there nudge "
                                    f"({self._silence_nudge_count}/{nudge_max}"
                                    f"{'; after-question' if self._last_agent_asked_question else ''})")
                        await self.text_input_queue.put(
                            f"[The line has gone quiet — warmly ask ONCE, {who} Then wait silently.]")
                        continue
                    if (self._silence_nudged and not self._silence_wrapup_at
                            and self._last_caller_audio < self._silence_nudge_at
                            and now - self._silence_nudge_at >= nudge_y):
                        self._silence_wrapup_at = now
                        logger.info("Still silent after the nudge; asking the agent to wrap up")
                        await self.text_input_queue.put(
                            "[Still no reply — the line seems dead: no voice heard from the member since your "
                            "check-in. If no outcome is recorded yet, record \"callback\" with the note "
                            "\"no reply after silence check\". Then give ONE short warm goodbye and call end_call.]")
                        continue
                    if (self._silence_wrapup_at
                            and now - self._silence_wrapup_at >= 8.0):
                        # A guest speaking a language we transcribe badly looks exactly like
                        # a dead line: Gemini returns little or no text, so the ladder climbs
                        # while they are in fact still talking. VAD is language-blind, so
                        # trust it over the transcript and stay on the line.
                        if now - self._last_caller_audio < 8.0:
                            logger.info("Wrap-up pending but the caller is still voicing; "
                                        "cancelling the hangup and asking the agent to re-engage")
                            self._silence_nudged = False
                            self._silence_wrapup_at = 0.0
                            await self.text_input_queue.put(
                                "[The member IS speaking but you may not have understood them. Do NOT "
                                "end the call. Say ONE short line in simple words asking them to repeat, "
                                "and if they seem to be using another language, switch to it.]")
                            continue
                        logger.info("Wrap-up nudge got no end_call; scheduling hangup")
                        self._schedule_end(mute=False)
                        continue
                if agent_quiet and quiet_for >= dead_air:
                    logger.info(f"Quiet {quiet_for:.0f}s (dead air, mutual silence); scheduling hangup")
                    self._schedule_end(mute=self._wrapping_up)
        except asyncio.CancelledError:
            pass

    async def _on_rsvp_recorded(self, result):
        """record_outcome fired: arm the post-closing soft hangup and keep the closing
        mute-proof."""
        self._rsvp_recorded = True
        self._post_rsvp_hangup_armed = True   # soft-end after the closing turn if no end_call
        if _RSVP_SILENT:
            # Silent RSVP can't double the closing; only risk is a MUTE record — nudge it to speak once, after a
            # short delay so the nudge can never interrupt a closing that is just starting to stream.
            if not self._spoke_since_user:
                if self._mute_record_task and not self._mute_record_task.done():
                    self._mute_record_task.cancel()
                self._mute_record_task = asyncio.create_task(self._nudge_if_still_mute())
        elif self._spoke_since_user:
            # Blocking fallback (<2.x): the result forces one more turn — the agent already spoke, so drop that filler turn's audio.
            self._suppress_post_record = True
            self._did_suppress_audio = False
            self._suppress_post_record_at = time.monotonic()

    async def _nudge_if_still_mute(self):
        """record_outcome landed before any spoken closing: if the agent is STILL mute after a beat, ask
        for the one closing. Tool calls often arrive a moment before the same turn's audio, so an
        immediate nudge could cut into a closing that was about to play."""
        delay = _env_float("EO_MUTE_RECORD_NUDGE_DELAY_S", 1.5)
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._spoke_since_user or self._ending or self._wrapping_up:
            return                                   # the closing started on its own — nothing to rescue
        await self.text_input_queue.put(
            "[Recorded. You have NOT said anything to the member since their last answer — reply to it now "
            "(if the call is at its end, that is your ONE short closing), then stop.]")

    # Wrap-up: the goodbye has been given. From here only a REAL question keeps the line open; a voice
    # abort of the hangup is allowed EO_HANGUP_ABORT_MAX times, then a bare hello/ack ends the call muted.

    def _voice_abort_allowed(self) -> bool:
        """Outside a wrap-up a voice abort is always allowed (idle / dead-air soft ends keep today's behaviour)."""
        if not self._wrapping_up:
            return True
        return not self._abort_locked and self._hangup_aborts < int(_env_float("EO_HANGUP_ABORT_MAX", 1))

    def _try_voice_abort(self, where: str) -> bool:
        """Mid-speech save at a hangup checkpoint: abort if the caller is voicing right now AND the
        budget allows it. Returns True when the hangup was aborted."""
        if not self._caller_voiced_recently():
            return False
        max_n = int(_env_float("EO_HANGUP_ABORT_MAX", 1))
        if self._voice_abort_allowed():
            self._hangup_aborts += 1
            self._pending_hangup_task = None
            self._soft_end_at = 0.0
            # After a drained goodbye stay MUTED until the transcript decides (a bare hello must never hear
            # "are you still there?"); before the goodbye has drained the farewell must stay audible.
            self._ending = bool(self._wrapping_up and self._goodbye_drained)
            logger.info(f"Caller voiced {where}; ABORTING hangup (mid-speech save"
                        + (f" {self._hangup_aborts}/{max_n}; wrapping up — next transcript decides)"
                           if self._wrapping_up else ")"))
            return True
        logger.info(f"Caller voiced {where} but the abort budget is spent ({self._hangup_aborts}/{max_n}"
                    f"{', locked' if self._abort_locked else ''}); hanging up anyway")
        return False

    def _enter_wrapping_up(self, reason: str):
        if not self._wrapping_up:
            self._wrapping_up = True
            logger.info(f"Wrapping up ({reason})")

    def _lock_abort_budget(self, reason: str):
        if not self._abort_locked:
            self._abort_locked = True
            logger.info(f"Abort budget locked ({reason}); the hangup can no longer be voice-aborted")

    def _clear_wrapping_up(self, reason: str):
        """A real follow-up re-opened the conversation: fresh wrap-up cycle, fresh abort budget, and the
        bridge-side closing hangup is re-armed so a SECOND closing still ends the call without end_call."""
        self._wrapping_up = False
        self._hangup_aborts = 0
        self._abort_locked = False
        self._goodbye_drained = False
        self._ending = False
        self._soft_end_at = 0.0
        self._post_rsvp_closing_done = False
        self._post_rsvp_hangup_armed = self._rsvp_recorded
        logger.info(f"Wrap-up cleared ({reason}); call continues")

    def _keep_open_after_question(self):
        """The agent called end_call in a turn that FINISHED on a question ("…Great Park,
        ma'am — is there anything else?"). It is waiting for an answer, so the call must not
        end under the guest: cancel the hangup and start a fresh wrap-up cycle. The next
        closing turn, or the post-outcome silence window, still ends the call."""
        if self._pending_hangup_task and not self._pending_hangup_task.done():
            self._pending_hangup_task.cancel()
        self._pending_hangup_task = None
        if self._wrapping_up:
            self._clear_wrapping_up("agent ended its turn on a question")
        self._ending = False
        self._soft_end_at = 0.0
        logger.info("end_call ignored: the agent's turn ended on a question — waiting for the answer")

    async def _reopen_for_language(self, text: str):
        """The guest asked to go on in another language after the goodbye: cancel the hangup, start a fresh
        wrap-up cycle, and tell the agent the call is not over — it has just said goodbye and would otherwise
        treat the conversation as finished. Capped (EO_LANGUAGE_REOPEN_MAX, default 2) so a transcript that
        keeps naming a language cannot hold a line open forever."""
        max_reopen = int(_env_float("EO_LANGUAGE_REOPEN_MAX", 2))
        if self._language_reopens >= max_reopen:
            logger.info(f"Language switch after goodbye ({text!r}) but the reopen cap is spent "
                        f"({self._language_reopens}/{max_reopen}); letting the end stand")
            if self._wrapping_up and not (self._pending_hangup_task and not self._pending_hangup_task.done()):
                self._lock_abort_budget("language reopen cap spent")
                self._schedule_end(mute=True)
            return
        self._language_reopens += 1
        if self._pending_hangup_task and not self._pending_hangup_task.done():
            self._pending_hangup_task.cancel()
        self._pending_hangup_task = None
        if self._wrapping_up:
            self._clear_wrapping_up("guest asked to switch language")
        self._ending = False
        self._soft_end_at = 0.0
        logger.info(f"Caller asked to switch language after the goodbye ({text!r}); keeping the call open "
                    f"({self._language_reopens}/{max_reopen})")
        await self.text_input_queue.put(
            "[The guest has just asked to continue in another language. The call is NOT over: do not say "
            "goodbye and do not call end_call. Switch NOW to the language they asked for and stay in it. In "
            "ONE short turn, say you are happy to continue in that language and ask whether they would like "
            "you to go over the details again. Then stop and listen.]")

    def _maybe_end_after_closing_turn(self) -> bool:
        """turn_complete after record_outcome with no end_call: schedule the muted hangup ONLY if the turn
        that just finished reads like a closing. A checklist step, an answer or a question keeps the arm
        and waits for a later closing turn (the reminder flow records the RSVP before its checklist).
        Returns True when it scheduled."""
        if not self._post_rsvp_hangup_armed or self._post_rsvp_closing_done:
            return False
        if self._pending_hangup_task and not self._pending_hangup_task.done():
            return False
        if not self._spoke_since_user:
            return False                                   # tool-only / silent turn — nothing was said
        tail = self._turn_text.strip()[-80:]
        if not _looks_like_closing(self._turn_text):
            logger.info(f"RSVP recorded; turn ended without a closing phrase — staying armed (tail={tail!r})")
            return False
        self._post_rsvp_closing_done = True
        self._post_rsvp_hangup_armed = False
        self._enter_wrapping_up("bridge closing-turn hangup")
        logger.info(f"RSVP closing turn done (tail={tail!r}); scheduling muted hangup (agent did not call end_call)")
        self._schedule_end(mute=True)
        return True

    def _schedule_end(self, mute: bool = True):
        """Schedule the hangup after a grace window (see _grace_then_hangup).

        mute=True (agent-initiated end_call): also drop any FURTHER agent audio — after
        end_call the only thing the model could still produce is forced-turn filler.
        mute=False (caller said goodbye / idle guard): the agent has NOT spoken its
        farewell yet — let that reply play; _drain_then_hangup waits for it. The mute is
        then armed on the next turn_complete (see _gemini_loop) so a re-greet after the
        goodbye still can't play."""
        if mute:
            self._ending = True                  # call is wrapping up — mute any further agent audio
        if self._pending_hangup_task and not self._pending_hangup_task.done():
            return
        if not mute:
            # remember WHEN the soft end was scheduled: the turn_complete-armed mute in
            # _gemini_loop only arms once agent audio has PLAYED after this moment (i.e.
            # the farewell actually went out), so a tool-only or stale turn can't arm it.
            self._soft_end_at = time.monotonic()
        self._pending_hangup_task = asyncio.create_task(self._grace_then_hangup())

    async def _grace_then_hangup(self):
        """After end_call, wait a short window so the member can jump back in. If
        they speak, _gemini_loop cancels this task and the call continues; if they
        stay silent, drain the goodbye audio and hang up."""
        try:
            # hard-capped at 2.0s so a stale .env can't drag out the "listen for resume" window
            grace = min(float(os.getenv("CALL_END_GRACE_SECONDS", "2")), 2.0)
        except ValueError:
            grace = 2.0
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:
            return                          # caller resumed — do NOT hang up
        # Caller voiced during the grace window (transcription still catching up) → abort here too; _drain_then_hangup re-checks as the authoritative gate.
        if self._try_voice_abort("during end grace"):
            return
        await asyncio.shield(self._drain_then_hangup())

    async def _on_agent_text(self, text: str):
        """Agent output transcription streamed in ("gemini" event): the turn is open, accumulate its
        text and run the within-turn repeat guard (marker-only; drops NEW audio, keeps queued playout)."""
        now = time.monotonic()
        self._turn_open = True           # a model turn is streaming (audio may lag the text)
        self._last_gemini_text_at = now
        self._turn_text += " " + (text or "")
        if self._reply_language and not self._language_nudged:
            self._post_reply_agent_text += " " + (text or "")
            await self._maybe_language_nudge()
        if not self._suppress_turn and _has_closing_repeat(self._turn_text):
            self._suppress_turn = True
            self._suppress_turn_at = now
            flush = os.getenv("EO_REPEAT_GUARD_FLUSH", "false").strip().lower() in ("1", "true", "yes", "on")
            logger.info("Repeat guard: closing marker repeated mid-turn; dropping the rest of this turn's audio"
                        + (" and flushing queued playout" if flush else " (queued playout kept)"))
            if flush:
                await self._flush_playout()

    async def _maybe_hello_storm(self, text: str):
        """"Hello? hello? hello?" while the agent is talking means the member can't hear it. Tell the
        model so (once, capped) — otherwise it reads the pile of interruptions as a dead line and
        records a callback."""
        if not _looks_like_hello(text):
            return
        now = time.monotonic()
        speaking = (self._turn_open or not self._out_frames.empty()
                    or (now - self._last_agent_audio) < 4.0)
        if (not speaking or not self._agent_audio_started or self._ending or self._wrapping_up
                or (self._pending_hangup_task and not self._pending_hangup_task.done())):
            return
        window = _env_float("EO_HELLO_STORM_WINDOW_S", 15.0)
        max_n = int(_env_float("EO_HELLO_STORM_MAX", 2))
        storm_count = int(_env_float("EO_HELLO_STORM_COUNT", 3))
        # one transcript can carry the whole storm ("Hello. Hello. Hello.") — count the hello WORDS
        self._hello_ts.extend([now] * max(1, min(_hello_word_count(text), storm_count)))
        recent = sum(1 for ts in self._hello_ts if now - ts <= window)
        if (recent < storm_count
                or self._hello_nudge_count >= max_n
                or (self._hello_nudge_at
                    and now - self._hello_nudge_at < _env_float("EO_HELLO_STORM_COOLDOWN_S", 30.0))):
            return
        self._hello_ts.clear()
        self._hello_nudge_at = now
        self._hello_nudge_count += 1
        self._reply_nudged = True                    # don't stack the missed-reply rescue on top of this
        logger.info(f"Hello storm: {recent} hello words in {window:.0f}s while the agent was speaking; "
                    f"nudging once ({self._hello_nudge_count}/{max_n})")
        await self.text_input_queue.put(
            "[The member has said 'hello?' several times while you were talking — they may not be hearing "
            "you. Stop what you were saying. Say ONCE, slowly: 'Yes, I'm here — can you hear me now?' Then "
            "WAIT silently for their answer. This is a LINE problem, not a request: do NOT offer or record a "
            "callback and do NOT end the call. When they reply, continue from where you left off in shorter "
            "sentences.]")

    async def _maybe_language_nudge(self):
        """The guest's first reply came back in a non-Latin script (e.g. "हां, बोलो") and the
        agent is now answering: if it has switched, stand down; if it is plainly still in
        English, prompt it ONCE. The prompt tells the model to follow the first reply, and a
        tester still got the whole schedule in English — this makes that switch dependable.
        Worded as a check, not an order: an English sentence has come back in Devanagari
        before, and only the model hears the audio."""
        lang = self._reply_language
        own, latin = _script_share(self._post_reply_agent_text, lang)
        if own >= 3:
            self._reply_language = None          # it is already speaking that language
            return
        if latin < 20:
            return                               # too little said yet to tell
        self._language_nudged = True
        logger.info(f"Guest's first reply looked like {lang} but the agent is answering in English; "
                    f"prompting it to switch")
        await self.text_input_queue.put(
            f"[The guest's first reply looked like {lang}. If that is what they are speaking, switch "
            f"to it NOW and stay in it for the whole call — carry on from where you are, do not start "
            f"over or repeat what you already said. If they were actually speaking English, ignore "
            f"this and stay in English.]")

    async def _on_caller_text(self, text: str):
        """A caller transcription ("user" event): line-trouble and end-of-call decisions."""
        text = text or ""
        if text.strip():
            self._last_turn_ended_on_question = False    # they have answered
        if not self._first_reply_seen and self._agent_audio_started and text.strip():
            self._first_reply_seen = True
            self._reply_language = _script_language(text)
            self._post_reply_agent_text = ""
        self._cancel_resume()                        # the member spoke — no "finish your sentence" needed
        await self._maybe_hello_storm(text)
        # Caller asked to hold: keep the line open, cancel any pending hangup — deterministic, not a sign-off.
        if _is_hold(text):
            try:
                hold = float(os.getenv("EO_HOLD_GRACE_SECONDS", "30"))
            except ValueError:
                hold = 30.0
            self._hold_until = time.monotonic() + hold
            if self._pending_hangup_task and not self._pending_hangup_task.done():
                self._pending_hangup_task.cancel()
                self._pending_hangup_task = None
            if self._wrapping_up:
                self._clear_wrapping_up("caller asked to hold")
            self._ending = False           # staying on the line — allow agent audio again
            self._soft_end_at = 0.0
            logger.info(f"Caller asked to hold; staying on the line for {hold:.0f}s")
            return
        max_n = int(_env_float("EO_HANGUP_ABORT_MAX", 1))
        # A request to carry on in another language outranks every end-of-call rule below: the guest has
        # just told us they want to keep talking. Checked first so it wins over the bare-ack/lock paths.
        if (self._wrapping_up or (self._pending_hangup_task and not self._pending_hangup_task.done())) \
                and _looks_like_language_switch(text):
            await self._reopen_for_language(text)
            return
        # Decide what a caller utterance means around the end of the call.
        if self._pending_hangup_task and not self._pending_hangup_task.done():
            # Hello-shaped text is checked BEFORE the follow-up regex so "Hello? Hello?" (its "?") can't re-open the call.
            if _looks_like_hello(text):
                logger.info(f"Bare hello / can-you-hear-me after goodbye ({text!r}); letting the hangup proceed")
                if self._wrapping_up:
                    self._lock_abort_budget("bare hello after goodbye")
            elif _looks_like_goodbye(text):
                logger.info("Caller said goodbye; letting the hangup proceed")
                if self._wrapping_up:
                    self._lock_abort_budget("mutual goodbye")
            elif _is_real_followup(text):
                # Post-goodbye, ONLY a genuine question re-opens the call.
                self._pending_hangup_task.cancel()
                self._pending_hangup_task = None
                if self._wrapping_up:
                    self._clear_wrapping_up("real follow-up")
                self._ending = False
                self._soft_end_at = 0.0
                logger.info(f"Caller asked a real follow-up ({text!r}); cancelling hangup")
            else:
                logger.info(f"Bare ack after goodbye ({text!r}); letting the hangup proceed")
                if self._wrapping_up:
                    self._lock_abort_budget("bare ack after goodbye")
            return
        if self._wrapping_up:
            # The goodbye was given and its hangup was voice-aborted (nothing pending now) — THIS transcript decides.
            if (not _looks_like_hello(text) and not _looks_like_goodbye(text)
                    and _is_real_followup(text)):
                logger.info(f"Post-goodbye real follow-up after a voice abort ({text!r}); keeping the call open")
                self._clear_wrapping_up("post-abort real follow-up")
                return
            kind = ("hello" if _looks_like_hello(text)
                    else "goodbye" if _looks_like_goodbye(text) else "non-question")
            self._lock_abort_budget(f"post-abort {kind}")
            logger.info(f"Post-goodbye {kind} after a voice abort ({text!r}); re-scheduling MUTED hangup "
                        f"(aborts {self._hangup_aborts}/{max_n}, no further voice aborts)")
            if self._goodbye_drained:
                await self._flush_playout()          # drop a queued "are you still there?" — the goodbye has played
            self._schedule_end(mute=True)
            return
        if self._rsvp_recorded and _looks_like_clear_farewell(text):
            # Caller signed off after the RSVP but the agent never called end_call — end it ourselves. mute=False so
            # the agent's farewell reply still plays; the mute arms on that turn's turn_complete. Before an RSVP exists
            # the model's own end_call decides: a garbled (non-English) transcript must never be able to hang up.
            self._enter_wrapping_up("caller goodbye after RSVP")
            logger.info("Caller said goodbye; scheduling hangup (agent hadn't ended)")
            self._schedule_end(mute=False)

    async def _gemini_loop(self):
        """Drive the Gemini Live session. On end_call it schedules a hangup after a
        grace window (cancelled if the caller keeps talking) rather than cutting
        immediately. When the caller hangs up first, run() cancels this task;
        CancelledError then propagates into start_session's async generator, whose
        finally closes the live session immediately (so the AI is never 'on hold')."""
        try:
            async for event in self.gemini.start_session(
                audio_input_queue=self.audio_input_queue,
                video_input_queue=self.video_input_queue,
                text_input_queue=self.text_input_queue,
                audio_output_callback=self.audio_output_callback,
                audio_interrupt_callback=self.audio_interrupt_callback,
                preopened=self._preopened,
            ):
                if event:
                    await self._emit(event)
                    etype = event.get("type")
                    if etype == "error":
                        logger.error(f"Gemini error during Plivo call: {event}")
                        break
                    # Feed the idle-hangup guard: mark the task done + stamp any activity.
                    if etype == "tool_call" and event.get("name") == "record_outcome":
                        await self._on_rsvp_recorded(event.get("result"))
                    # Stamp activity on caller AND agent speech so the idle timer only counts TRUE mutual silence.
                    if etype in ("user", "interrupted", "turn_complete", "gemini"):
                        self._last_activity = time.monotonic()
                    if etype == "user":
                        self._last_user_event = time.monotonic()
                    # Agent transcript → turn tracking + the within-turn repeat guard (see _on_agent_text).
                    if etype == "gemini":
                        await self._on_agent_text(event.get("text") or "")
                    elif etype in ("turn_complete", "interrupted"):
                        if etype == "turn_complete":
                            self._cancel_resume()        # the turn finished on its own — no resume needed
                            self._last_agent_asked_question = _looks_like_agent_question(self._turn_text)
                            self._last_turn_ended_on_question = _ends_with_question(self._turn_text)
                            if self._end_call_this_turn and self._last_turn_ended_on_question:
                                self._keep_open_after_question()
                            # After RSVP, a turn that READS LIKE A CLOSING ends the call muted so a bare "Hello"
                            # cannot re-engage; a checklist step / answer / question stays armed and waits.
                            self._maybe_end_after_closing_turn()
                        self._turn_open = False
                        self._turn_text = ""
                        self._end_call_this_turn = False
                        self._suppress_turn = False
                        self._suppress_turn_at = 0.0
                        # Arm the mute only after agent audio PLAYED past the soft-end schedule — a tool-only or stale turn's turn_complete must not mute the real farewell.
                        if (etype == "turn_complete" and not self._ending
                                and self._pending_hangup_task and not self._pending_hangup_task.done()
                                and self._soft_end_at > 0.0
                                and self._last_agent_audio >= self._soft_end_at):
                            self._ending = True
                        # Clear the stray guard once the turn we ACTUALLY muted ends (_did_suppress_audio gates it); barge-in always clears.
                        if etype == "interrupted" or self._did_suppress_audio:
                            self._suppress_post_record = False
                            self._did_suppress_audio = False
                        # After the suppress flags reset (so the re-greet can't be muted by them):
                        if etype == "turn_complete":
                            self._any_turn_complete = True
                        else:
                            await self._maybe_rescue_greeting()
                    if etype == "end_call":
                        self._post_rsvp_hangup_armed = False
                        self._post_rsvp_closing_done = True
                        # Gemini interleaves tool calls with audio: end_call often lands while the
                        # closing is still streaming (or before its first chunk). A muted hangup
                        # drops every chunk after that instant and cuts the closing mid-sentence
                        # ("...just a quick reminder—" *click*). If the farewell is in flight or
                        # not started, schedule a SOFT end instead: audio keeps playing, the mute
                        # arms on turn_complete once that audio has gone out (see below), and
                        # _drain_then_hangup waits for the buffer to empty before hanging up.
                        farewell_pending = (self._turn_open
                                            or not self._out_frames.empty()
                                            or bool(self._residual)
                                            or not self._spoke_since_user
                                            or (time.monotonic() - self._last_agent_audio) < 1.0)
                        logger.info("Agent requested end_call; will hang up after grace window"
                                    + (" (farewell still in flight — soft end, audio plays out)"
                                       if farewell_pending else ""))
                        self._enter_wrapping_up("end_call")
                        self._schedule_end(mute=not farewell_pending)
                        self._end_call_this_turn = True
                        # end_call as a separate tool-only turn, right after a turn that asked a
                        # question the guest has not answered yet: the same wait applies.
                        if not self._turn_open and self._last_turn_ended_on_question:
                            self._keep_open_after_question()
                        continue           # stay live during the grace window
                    # Caller transcript → hello-storm, hold, and end-of-call decisions (see _on_caller_text).
                    if etype == "user":
                        await self._on_caller_text(event.get("text") or "")
        except asyncio.CancelledError:
            raise                          # caller hung up: let the generator finally close the session
        except Exception as e:
            logger.error(f"Gemini session error: {e}")

    async def run(self):
        """Run the bridge: Plivo <-> Gemini.

        Race the Gemini loop against the Plivo receive loop (and the max-duration
        guard). Whichever finishes first — caller hangup, agent end_call, or the
        guard — the finally cancels the rest, so the Gemini session never lingers
        billing after the caller leaves.
        """
        gemini_task = asyncio.create_task(self._gemini_loop())
        plivo_task = asyncio.create_task(self.handle_plivo_messages())
        sender_task = asyncio.create_task(self._outbound_sender())
        guard_task = asyncio.create_task(self._max_duration_guard())
        idle_task = asyncio.create_task(self._idle_hangup_guard())

        try:
            await asyncio.wait(
                {gemini_task, plivo_task, sender_task, guard_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            tasks = [gemini_task, plivo_task, sender_task, guard_task, idle_task]
            if self._pending_hangup_task:
                tasks.append(self._pending_hangup_task)
            if self._connect_tone_task:
                tasks.append(self._connect_tone_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Plivo answers with keepCallAlive=true — if our side died the carrier call can stay up with the member hearing silence; best-effort idempotent hangup.
            if self.call_id and not self._hangup_done:
                self._hangup_done = True
                try:
                    import dialer
                    await dialer.hangup_call(self.call_id)
                except Exception:
                    pass
            self._write_recording()            # audio tasks stopped — flush the mixed WAV
            if self._gate_on and self._gate_frames:
                logger.info(f"Noise squelch: {self._gate_squelched}/{self._gate_frames} inbound frames "
                            f"replaced with silence ({100.0 * self._gate_squelched / self._gate_frames:.0f}%)")
            if self._started and not self._call_end_emitted:
                self._call_end_emitted = True
                await self._emit({"type": "call_end"})
            logger.info("Plivo-Gemini bridge closed")
