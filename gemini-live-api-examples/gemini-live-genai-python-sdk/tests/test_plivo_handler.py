"""plivo_handler.py — codec tables, goodbye/mute separation, sender resilience, squelch."""

import asyncio
import base64
import json
import os
import struct
import time

import pytest

import plivo_handler as ph
from plivo_handler import (PlivoMediaBridge, _has_closing_repeat, _looks_like_agent_question,
                           _looks_like_goodbye,
                           _MULAW_DECODE, _MULAW_SQ, _PCM_TO_ULAW, _SILENCE_20MS_16K,
                           _mulaw_frame_meansquare, _pcm16_to_mulaw_sample, pcm24k_to_mulaw)

try:
    from starlette.websockets import WebSocketState
except ImportError:
    WebSocketState = None


class FakeWS:
    def __init__(self, fail_times=0, connected=True, incoming=None):
        self.sent = []
        self.fail_times = fail_times
        self._incoming = list(incoming or [])
        if WebSocketState is not None:
            state = WebSocketState.CONNECTED if connected else WebSocketState.DISCONNECTED
            self.client_state = state
            self.application_state = state

    async def send_json(self, payload):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.sent.append(payload)

    async def receive_text(self):
        if not self._incoming:
            raise Exception((1000,))
        return self._incoming.pop(0)


def _bridge(ws=None, **env):
    return PlivoMediaBridge(ws or FakeWS(), gemini_client=None, text_trigger="[go]")


# Codec tables

def test_pcm_to_ulaw_table_matches_reference_encoder():
    for s in (-32768, -32635, -10000, -1, 0, 1, 42, 8000, 32635, 32767):
        assert _PCM_TO_ULAW[s & 0xFFFF] == _pcm16_to_mulaw_sample(s)


def test_mulaw_square_table_matches_decode_table():
    for b in range(256):
        assert _MULAW_SQ[b] == int(_MULAW_DECODE[b]) ** 2


def test_pcm24k_to_mulaw_lookup_equivalent_to_per_sample_encode():
    samples = [0, 100, -100, 8000, -8000, 32767, -32768, 5, 6, 7]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    expected = bytes(_pcm16_to_mulaw_sample((samples[i] + samples[i+1] + samples[i+2]) // 3)
                     for i in range(0, len(samples) - 2, 3))
    assert pcm24k_to_mulaw(pcm) == expected


def test_meansquare_zero_for_silence_high_for_speech():
    silence = bytes([_pcm16_to_mulaw_sample(0)]) * 160
    loud = bytes([_pcm16_to_mulaw_sample(8000)]) * 160
    assert _mulaw_frame_meansquare(silence) < 10
    assert _mulaw_frame_meansquare(loud) > 250_000


# Goodbye heuristics

def test_looks_like_goodbye():
    assert _looks_like_goodbye("okay bye") is True
    assert _looks_like_goodbye("thank you") is True
    assert _looks_like_goodbye("bye, but what time is it?") is False       # real follow-up
    assert _looks_like_goodbye("thanks a lot for calling me today about this event") is False  # too long
    assert _looks_like_goodbye("") is False


_LIVE_DOUBLE_CLOSING = (
    "Oh wonderful so glad to have you there! You'll receive all the details "
    "on your WhatsApp shortly. See you on thirty first Oh lovely! So glad "
    "you'll be there. You'll receive all the details on your WhatsApp shortly. "
    "See you on thirty first!"
)


def test_has_closing_repeat_detects_doubled_closing():
    assert _has_closing_repeat("see you on the tenth! ... see you on the tenth!") is True
    assert _has_closing_repeat("lovely, see you on the tenth then") is False
    # Real failure: paraphrased double-invite with "count you in" / "sorry about that" twice
    doubled = (
        "Oh, sorry about that! Let me try again. We're hosting an evening with "
        "Raghav Chadha. Can we count you in for that? Oh, sorry about that! Yes, "
        "I was just calling from EO Gujarat. Can we count you in?"
    )
    assert _has_closing_repeat(doubled) is True
    # a closing MARKER voiced twice inside one turn
    assert _has_closing_repeat("can we count you in now can we count you in please") is True
    # Live failure: two closings glued — "see you on thirty" / "so glad to have you"
    assert _has_closing_repeat(_LIVE_DOUBLE_CLOSING) is True


def test_has_closing_repeat_ignores_ordinary_long_replies():
    """A long legitimate answer naturally repeats 5-word runs (event name, date). That is NOT a
    doubled closing — the old generic rule muted mid-sentence and caused the dead-air complaints."""
    long_reply = (
        "It's our AI First Mindset Workshop with Raj Goodman, on Friday the eleventh of September "
        "from four in the afternoon at the DoubleTree by Hilton. Kids twelve and above are very welcome. "
        "So, for the AI First Mindset Workshop with Raj Goodman on the eleventh of September, "
        "shall I put you down as coming?"
    )
    assert _has_closing_repeat(long_reply) is False


def test_looks_like_agent_question():
    assert _looks_like_agent_question("But could we still count you and your spouse in?") is True
    assert _looks_like_agent_question("Would you be able to make it?") is True
    assert _looks_like_agent_question("Oh wonderful, so glad you'll be there!") is False
    assert _looks_like_agent_question("") is False


def test_repeat_guard_drops_rest_of_turn_but_keeps_queued_playout(monkeypatch):
    """A doubled closing mutes the REST of the turn's new audio; what is already queued / playing is
    never flushed (that cut sentences mid-word). EO_REPEAT_GUARD_FLUSH=true restores the old flush."""
    async def run(flush):
        if flush:
            monkeypatch.setenv("EO_REPEAT_GUARD_FLUSH", "true")
        else:
            monkeypatch.delenv("EO_REPEAT_GUARD_FLUSH", raising=False)
        ws = FakeWS()
        b = _bridge(ws)
        b.stream_id = "s1"
        b._agent_audio_started = True
        for _ in range(5):                                   # a closing already queued for playout
            await b._out_frames.put(b"\xff" * 160)
        await b._on_agent_text(_LIVE_DOUBLE_CLOSING)          # the doubled closing streams in
        assert b._suppress_turn is True and b._turn_open is True
        await b.audio_output_callback(b"\x00\x10" * 240)     # NEW audio while suppressed → dropped
        assert not b._residual
        cleared = any(p.get("event") == "clearAudio" for p in ws.sent)
        return b._out_frames.qsize(), cleared

    assert asyncio.run(run(False)) == (5, False)     # queued playout untouched, no clearAudio
    assert asyncio.run(run(True)) == (0, True)       # opt-in flush behaves like before


def test_suppress_turn_expires_when_no_turn_boundary_arrives(monkeypatch):
    monkeypatch.setenv("EO_SUPPRESS_TURN_MAX_S", "0.05")

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._suppress_turn = True
        b._suppress_turn_at = time.monotonic() - 1.0        # armed long ago, turn_complete never came
        await b.audio_output_callback(b"\x00\x10" * 240)
        return b._suppress_turn, (not b._out_frames.empty() or bool(b._residual))
    assert asyncio.run(run()) == (False, True)          # mute lifted, audio flows again


def test_post_rsvp_closing_schedules_muted_hangup():
    """After record_outcome + a spoken CLOSING turn_complete, hang up muted so bare Hello can't re-engage."""
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        b._post_rsvp_hangup_armed = True
        b._spoke_since_user = True
        b._last_agent_audio = time.monotonic()
        b._turn_text = "Perfect, Pratik — you're all set. See you tomorrow at four, take care!"
        assert b._maybe_end_after_closing_turn() is True
        assert b._ending is True and b._wrapping_up is True
        assert b._pending_hangup_task is not None
        b._pending_hangup_task.cancel()
        return True
    assert asyncio.run(run()) is True


def test_looks_like_closing():
    assert ph._looks_like_closing("Perfect, Pratik — you're all set. See you tomorrow at four, take care!")
    assert ph._looks_like_closing("No problem — the setup steps are on the WhatsApp group. See you tomorrow!")
    assert ph._looks_like_closing("Oh wonderful, so glad you'll be there! See you on the eleventh!")
    # turns that WAIT for an answer are never closings, even with a closing word inside
    assert not ph._looks_like_closing("Are you all set to join us?")
    assert not ph._looks_like_closing("Wonderful! Since it's a hands-on session there's a short laptop setup — may I run through it quickly?")
    assert not ph._looks_like_closing("That's all — it's on the WhatsApp group too. Anything you'd like me to repeat?")
    assert not ph._looks_like_closing("")


def test_post_rsvp_non_closing_turn_stays_armed():
    """The reminder flow records the RSVP early and THEN runs the checklist: a turn that ends on a
    question must not be mistaken for the closing (that scheduled a muted hangup mid-call)."""
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        b._post_rsvp_hangup_armed = True
        b._spoke_since_user = True
        b._turn_text = "Wonderful! Since it's a hands-on session there's a short laptop setup — may I run through it quickly?"
        assert b._maybe_end_after_closing_turn() is False
        assert b._post_rsvp_hangup_armed is True and b._pending_hangup_task is None and b._ending is False
        b._turn_text = "Perfect, you're all set then! See you tomorrow at four, take care!"
        assert b._maybe_end_after_closing_turn() is True
        b._pending_hangup_task.cancel()
        return True
    assert asyncio.run(run()) is True


def test_settled_rsvp_arms_hangup_and_nudges_a_mute_record(monkeypatch):
    """The "say your closing" rescue waits a beat: it fires only if the agent is STILL mute, so it can
    never interrupt a closing that was already starting to stream when record_outcome landed."""
    monkeypatch.setenv("EO_MUTE_RECORD_NUDGE_DELAY_S", "0.05")
    monkeypatch.setattr(ph, "_RSVP_SILENT", True)

    async def run(agent_speaks_meanwhile):
        b = _bridge()
        b._spoke_since_user = False
        await b._on_rsvp_recorded({"outcome_status": "wrong_number"})
        assert b._post_rsvp_hangup_armed is True
        if agent_speaks_meanwhile:
            b._spoke_since_user = True                 # the closing started on its own
        await asyncio.sleep(0.15)
        return not b.text_input_queue.empty()        # "say your closing now" nudge
    assert asyncio.run(run(False)) is True
    assert asyncio.run(run(True)) is False


# Goodbye playback: scheduling a hangup must not mute the farewell

def test_schedule_end_soft_lets_farewell_audio_through():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._schedule_end(mute=False)
        assert b._ending is False                     # farewell may still play
        await b.audio_output_callback(b"\x00\x10" * 240)   # 24kHz pcm chunk
        got = not b._out_frames.empty() or bool(b._residual)
        b._pending_hangup_task.cancel()
        return got
    assert asyncio.run(run()) is True


def test_schedule_end_muted_drops_further_audio():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._schedule_end(mute=True)
        assert b._ending is True
        await b.audio_output_callback(b"\x00\x10" * 240)
        got = b._out_frames.empty() and not b._residual
        b._pending_hangup_task.cancel()
        return got
    assert asyncio.run(run()) is True


# Outbound sender resilience

def test_sender_survives_transient_send_failures():
    async def run():
        ws = FakeWS(fail_times=3, connected=True)
        b = _bridge(ws)
        b.stream_id = "s1"
        for _ in range(5):
            b._out_frames.put_nowait(b"\x00" * 160)
        task = asyncio.create_task(b._outbound_sender())
        await asyncio.sleep(0.3)
        alive = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # 3 transient failures skipped, remaining 2 frames actually sent
        return alive, len(ws.sent)
    alive, sent = asyncio.run(run())
    assert alive is True
    assert sent == 2


@pytest.mark.skipif(WebSocketState is None, reason="starlette not installed")
def test_sender_exits_when_socket_is_closed():
    async def run():
        ws = FakeWS(fail_times=99, connected=False)
        b = _bridge(ws)
        b.stream_id = "s1"
        b._out_frames.put_nowait(b"\x00" * 160)
        task = asyncio.create_task(b._outbound_sender())
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            return False
        return True
    assert asyncio.run(run()) is True


# Noise squelch: substitutes silence, same frame cadence, never drops

def _media_msg(mulaw: bytes) -> str:
    return json.dumps({"event": "media",
                       "media": {"payload": base64.b64encode(mulaw).decode(), "track": "inbound"}})


def test_squelch_substitutes_silence_and_preserves_frame_count(monkeypatch):
    monkeypatch.setenv("EO_NOISE_GATE", "true")
    quiet = bytes([_pcm16_to_mulaw_sample(0)]) * 160
    loud = bytes([_pcm16_to_mulaw_sample(8000)]) * 160

    async def run():
        ws = FakeWS(incoming=[_media_msg(quiet), _media_msg(quiet), _media_msg(loud)])
        b = _bridge(ws)
        b._rec_on = False
        await b.handle_plivo_messages()
        frames = []
        while not b.audio_input_queue.empty():
            frames.append(b.audio_input_queue.get_nowait())
        return frames

    frames = asyncio.run(run())
    assert len(frames) == 3                          # cadence preserved — nothing dropped
    assert frames[0] == _SILENCE_20MS_16K            # below gate, no recent voice → silence
    assert frames[1] == _SILENCE_20MS_16K
    assert frames[2] != _SILENCE_20MS_16K            # voiced frame passes unmodified


def test_gate_off_forwards_everything_verbatim(monkeypatch):
    monkeypatch.setenv("EO_NOISE_GATE", "false")
    quiet = bytes([_pcm16_to_mulaw_sample(0)]) * 160

    async def run():
        ws = FakeWS(incoming=[_media_msg(quiet)])
        b = _bridge(ws)
        b._rec_on = False
        await b.handle_plivo_messages()
        return b.audio_input_queue.get_nowait()

    frame = asyncio.run(run())
    assert len(frame) == 640
    # decoded silence upsampled is all-zero PCM, but it went through the codec path
    assert frame == ph.mulaw_to_pcm16k(quiet)


# Silence check: ask once, then escalate — never loop the question

def test_silence_nudge_fires_once_then_escalates(monkeypatch):
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_SILENCE_PROMPT_SECONDS", "0.2")
    monkeypatch.setenv("EO_SILENCE_HANGUP_SECONDS", "0.5")

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b.first_name = "Pratik"
        t = time.monotonic()
        b._last_agent_audio = t - 10          # agent finished long ago, caller silent since
        b._last_caller_audio = t - 10
        b._last_activity = t - 10
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.1)              # 1st guard tick → "are you still there?"
        await b.audio_output_callback(b"\x00\x10" * 240)   # the nudge is spoken aloud...
        b._drain_outbound()                                # ...and finishes playing
        await asyncio.sleep(2.2)              # must ESCALATE now, not re-ask
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return msgs

    msgs = asyncio.run(run())
    still_there = [m for m in msgs if "still there" in m]
    wrapups = [m for m in msgs if "seems dead" in m]
    assert len(still_there) == 1, f"nudge must fire exactly once, got {msgs}"
    assert len(wrapups) == 1, f"expected one wrap-up escalation, got {msgs}"
    assert "Pratik" in still_there[0]


# Greeting watchdog: one firm push when Gemini stalls on the opening line

def test_greeting_watchdog_pushes_exactly_once(monkeypatch):
    monkeypatch.setenv("EO_GREETING_NUDGE_SECONDS", "0.5")

    async def run():
        b = _bridge()
        b._greeting_sent_at = time.monotonic() - 5    # trigger sent, still no agent audio
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(2.3)                      # two+ guard ticks
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return msgs

    msgs = asyncio.run(run())
    assert sum("Speak your opening line" in m for m in msgs) == 1


def test_greeting_watchdog_never_fires_after_audio_started(monkeypatch):
    monkeypatch.setenv("EO_GREETING_NUDGE_SECONDS", "0.5")

    async def run():
        b = _bridge()
        b._greeting_sent_at = time.monotonic() - 5
        b._agent_audio_started = True                 # greeting already played
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "Speak your opening line" in m]

    assert asyncio.run(run()) == []


def test_greeting_rescue_fires_once_for_noise_killed_opening():
    async def run():
        b = _bridge()
        b._greeting_sent_at = time.monotonic() - 1    # opening queued, zero caller evidence yet
        await b._maybe_rescue_greeting()
        await b._maybe_rescue_greeting()              # a second interrupt: once-per-call guard holds
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return msgs

    msgs = asyncio.run(run())
    assert sum("Say your opening line again" in m for m in msgs) == 1


def test_greeting_rescue_stays_quiet_for_real_speech_or_after_a_turn():
    async def run():
        results = []
        for field, value in (("_last_user_event", time.monotonic()),
                             ("_last_caller_audio", time.monotonic()),
                             ("_any_turn_complete", True)):
            b = _bridge()
            b._greeting_sent_at = time.monotonic() - 1
            setattr(b, field, value)
            await b._maybe_rescue_greeting()
            results.append(b.text_input_queue.empty())
        return results

    assert asyncio.run(run()) == [True, True, True]


def test_missed_reply_rescue_fires_when_caller_speech_goes_unanswered(monkeypatch):
    """Caller spoke AFTER the agent's last audio and got nothing for 4s → prompt the
    agent once. The still-there ladder can't cover this (it measures silence, and a
    talking caller keeps resetting it)."""
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_UNANSWERED_REPLY_SECONDS", "0.3")

    async def run():
        b = _bridge()
        b._agent_audio_started = True
        t = time.monotonic()
        b._last_agent_audio = t - 10              # greeting ended long ago
        b._last_caller_audio = t - 5              # caller replied 5s ago...
        b._last_activity = t - 5                  # ...and nothing since
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(2.3)                  # two+ ticks: must fire exactly once
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "said something" in m]

    assert len(asyncio.run(run())) == 1


def test_missed_reply_rescue_fires_while_caller_keeps_talking(monkeypatch):
    """THE shivi case: caller repeats 'hello hello' every 2s (fresh voiced frames) while
    the agent stays mute. The DEAF path keys on the AGENT's silence — the caller's
    repeats must not keep resetting it (the fast path is deliberately out of reach here)."""
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_UNANSWERED_REPLY_SECONDS", "5")
    monkeypatch.setenv("EO_DEAF_RESCUE_SECONDS", "0.3")

    async def run():
        b = _bridge()
        b._agent_audio_started = True
        t = time.monotonic()
        b._last_agent_audio = t - 10              # agent mute for 10s...
        b._last_caller_audio = t - 1              # ...caller spoke just 1s ago (still trying)
        b._last_activity = t - 1
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "said something" in m]

    assert len(asyncio.run(run())) == 1


def test_missed_reply_nudge_never_asks_for_a_redelivery(monkeypatch):
    """Live test, 25 Sep 02:06: the deaf rescue fired mid-conversation and the model, told to
    "reply NOW", read the entire schedule out a second time. Whatever triggers it, the nudge
    must ask for one line about what the guest just said and forbid repeating the script."""
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_DEAF_RESCUE_SECONDS", "0.3")

    async def run():
        b = _bridge()
        b._agent_audio_started = True
        t = time.monotonic()
        b._last_agent_audio = t - 10
        b._last_caller_audio = t - 1
        b._last_activity = t - 1
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "said something" in m]

    (msg,) = asyncio.run(run())
    assert "never re-deliver the schedule" in msg
    assert "reply NOW" not in msg


def test_missed_reply_rescue_stays_quiet_when_agent_already_replied(monkeypatch):
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_UNANSWERED_REPLY_SECONDS", "0.3")

    async def run():
        b = _bridge()
        b._agent_audio_started = True
        t = time.monotonic()
        b._last_caller_audio = t - 5
        b._last_agent_audio = t - 2               # agent DID reply after the caller
        b._last_activity = t - 2
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "said something" in m]

    assert asyncio.run(run()) == []


def test_silence_nudge_fires_even_when_turn_open_flag_is_stuck(monkeypatch):
    """Gemini may never send turn_complete for a text-triggered greeting when the
    caller's speech doesn't register as a turn — the stuck _turn_open flag must not
    muzzle the 'are you still there?' ladder."""
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_SILENCE_PROMPT_SECONDS", "0.2")

    async def run():
        b = _bridge()
        b._agent_audio_started = True
        b._turn_open = True                       # stuck: turn_complete never arrived
        t = time.monotonic()
        b._last_agent_audio = t - 10              # ...but no agent audio for 10s
        b._last_caller_audio = t - 10
        b._last_activity = t - 10
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "still there" in m]

    assert len(asyncio.run(run())) == 1


# Silence nudge: cooldown + hard cap survive noise-blip flag resets

def test_silence_nudge_respects_cooldown_after_noise_reset(monkeypatch):
    monkeypatch.setenv("EO_SILENCE_CHECK", "true")
    monkeypatch.setenv("EO_SILENCE_PROMPT_SECONDS", "0.2")
    monkeypatch.setenv("EO_SILENCE_NUDGE_COOLDOWN_S", "60")

    async def run():
        b = _bridge()
        b._agent_audio_started = True
        t = time.monotonic()
        b._last_agent_audio = t - 10
        b._last_caller_audio = t - 10
        b._last_activity = t - 10
        # a nudge fired 1s ago; then a noise blip reset the flag
        b._silence_nudged = False
        b._silence_nudge_at = t - 1
        b._silence_nudge_count = 1
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "still there" in m]

    assert asyncio.run(run()) == []                   # cooldown blocks the re-ask


# Bounded input queue: drop-oldest, never blocks

def test_put_audio_drops_oldest_on_overflow():
    async def run():
        b = _bridge()
        b.audio_input_queue = asyncio.Queue(maxsize=3)
        for i in range(5):
            b._put_audio(bytes([i]) * 4)
        out = []
        while not b.audio_input_queue.empty():
            out.append(b.audio_input_queue.get_nowait())
        return out
    out = asyncio.run(run())
    assert len(out) == 3
    assert out[0][0] == 2 and out[-1][0] == 4        # oldest (0,1) dropped


# Interrupt gate: a single loud frame (click / echo) is a phantom; a sustained burst is a real barge-in

def _voiced_frames(n):
    loud = bytes([_pcm16_to_mulaw_sample(20000)]) * 160
    return [_media_msg(loud)] * n


def test_interrupt_needs_sustained_voice_not_a_single_frame(monkeypatch):
    monkeypatch.setenv("EO_INTERRUPT_CONFIRM_WINDOW_S", "0.8")
    monkeypatch.setenv("EO_INTERRUPT_MIN_VOICED_MS", "120")

    async def run(n_frames):
        ws = FakeWS(incoming=_voiced_frames(n_frames))
        b = _bridge(ws)
        b.stream_id = "s1"
        b._rec_on = False
        b._agent_audio_started = True
        b._turn_open = True
        await b.handle_plivo_messages()                  # feeds the voiced frames through the energy VAD
        for _ in range(5):
            await b._out_frames.put(b"\xff" * 160)
        await b.audio_interrupt_callback()
        cleared = any(p.get("event") == "clearAudio" for p in ws.sent)
        if b._resume_task:
            b._resume_task.cancel()
        return b._out_frames.qsize(), cleared

    assert asyncio.run(run(1)) == (5, False)     # one 20 ms blip → phantom, playback kept
    assert asyncio.run(run(10)) == (0, True)     # 200 ms of voice → real barge-in, flushed


def test_phantom_interrupt_asks_the_agent_to_resume_unless_the_caller_spoke(monkeypatch):
    monkeypatch.setenv("EO_PHANTOM_RESUME_DELAY_S", "0.05")

    async def run(caller_speaks, phantoms=1):
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._turn_open = True                                  # the agent was mid-sentence
        b._last_agent_audio = time.monotonic()
        for _ in range(phantoms):
            await b.audio_interrupt_callback()               # no voiced caller audio at all → phantom
            if caller_speaks:
                b._last_user_event = time.monotonic()        # the member actually said something
            await asyncio.sleep(0.15)
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "cut you off" in m]

    assert len(asyncio.run(run(False))) == 1
    assert asyncio.run(run(True)) == []
    assert len(asyncio.run(run(False, phantoms=4))) == 2        # hard cap per call


def test_silence_nudge_waits_while_agent_text_is_still_streaming(monkeypatch):
    monkeypatch.setenv("EO_SILENCE_PROMPT_SECONDS", "0.2")

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        t = time.monotonic()
        b._last_agent_audio = t - 10
        b._last_caller_audio = t - 10
        b._last_activity = t - 10
        b._turn_open = True
        b._last_gemini_text_at = t          # transcript still arriving for this turn (audio may lag)
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return msgs
    assert asyncio.run(run()) == []


# Hello storm: "hello? hello? hello?" over the agent = a line problem, never a callback

def test_looks_like_hello_and_hindi_line_words():
    assert ph._looks_like_hello("Hello?") and ph._looks_like_hello("hello hello") and ph._looks_like_hello("Helloooo")
    assert ph._looks_like_hello("can you hear me?") and ph._looks_like_hello("awaaz nahi aa rahi")
    assert not ph._looks_like_hello("hello, what time is it?")
    assert not ph._looks_like_hello("yes")
    assert ph._HOLD_RE.search("ek minute ruko") and ph._HOLD_RE.search("hold on")
    assert ph._QUESTION_RE.search("kitne baje hai") and ph._REAL_FOLLOWUP_RE.search("bacche aa sakte hai")


def test_hello_storm_nudges_once_and_never_ends_the_call(monkeypatch):
    monkeypatch.setenv("EO_HELLO_STORM_COUNT", "3")

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._turn_open = True                       # the agent is talking over them
        b._last_agent_audio = time.monotonic()
        for text in ("Hello?", "hello hello", "Hello?", "hello"):
            await b._on_caller_text(text)
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "can you hear me" in m.lower()], b._pending_hangup_task
    nudges, pending = asyncio.run(run())
    assert len(nudges) == 1 and pending is None


def test_caller_goodbye_only_ends_the_call_once_an_rsvp_exists():
    async def run(recorded):
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = recorded
        await b._on_caller_text("okay bye")
        pending = b._pending_hangup_task is not None
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        return pending
    assert asyncio.run(run(False)) is False   # pre-RSVP the model's own end_call decides (a garbled transcript can't hang up)
    assert asyncio.run(run(True)) is True


# Wrap-up: once the goodbye is given, a bare "hello?" can't resurrect the call

def _fast_hangup_env(monkeypatch):
    monkeypatch.setenv("CALL_END_GRACE_SECONDS", "0.05")
    monkeypatch.setenv("CALL_HANGUP_GRACE_SECONDS", "0.05")
    monkeypatch.setenv("EO_FAREWELL_WAIT_SECONDS", "0")


def _stub_hangup(monkeypatch):
    import dialer
    calls = []

    async def fake(call_id, provider=None):
        calls.append(call_id)
        return {"ok": True}
    monkeypatch.setattr(dialer, "hangup_call", fake)
    return calls


def test_voice_abort_is_capped_while_wrapping_up(monkeypatch):
    """A member who starts talking right after the goodbye is saved ONCE; the next voice-abort is refused
    and the hangup goes through (the incident: hello → abort → 'are you still there?' → closing repeated)."""
    _fast_hangup_env(monkeypatch)
    hangups = _stub_hangup(monkeypatch)

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b.call_id = "call-1"
        b._agent_audio_started = True
        b._wrapping_up = True
        b._last_caller_audio = time.monotonic()      # voicing right now → saved once
        b._schedule_end(mute=True)
        await b._pending_hangup_task
        first = (b._hangup_aborts, list(hangups), b._pending_hangup_task)
        b._last_caller_audio = time.monotonic()      # still voicing, but the budget is spent
        b._schedule_end(mute=True)
        await b._pending_hangup_task
        return first, list(hangups)
    (aborts, calls_after_first, pending), calls_after_second = asyncio.run(run())
    assert aborts == 1 and calls_after_first == [] and pending is None
    assert calls_after_second == ["call-1"]


def test_voice_abort_unlimited_outside_a_wrap_up(monkeypatch):
    _fast_hangup_env(monkeypatch)
    hangups = _stub_hangup(monkeypatch)

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b.call_id = "call-2"
        b._hangup_aborts = 5                         # no wrap-up → the budget is irrelevant
        b._last_caller_audio = time.monotonic()
        b._schedule_end(mute=False)
        await b._pending_hangup_task
        return list(hangups), b._ending
    assert asyncio.run(run()) == ([], False)


@pytest.mark.parametrize("text", ["Hello. Hello. Hello.", "Hello? Hello?", "can you hear me?", "okay", "thanks bye"])
def test_post_goodbye_bare_hello_reschedules_muted_hangup(text):
    async def run():
        ws = FakeWS()
        b = _bridge(ws)
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        b._wrapping_up = True                        # goodbye given, hangup was voice-aborted (nothing pending)
        b._goodbye_drained = True
        b._hangup_aborts = 1
        await b._on_caller_text(text)
        pending = b._pending_hangup_task is not None and not b._pending_hangup_task.done()
        cleared = any(p.get("event") == "clearAudio" for p in ws.sent)
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        return pending, b._ending, b._abort_locked, cleared
    assert asyncio.run(run()) == (True, True, True, True)


def test_post_goodbye_real_question_keeps_call_open():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        b._wrapping_up = True
        b._goodbye_drained = True
        b._hangup_aborts = 1
        b._ending = True
        await b._on_caller_text("what time does it start?")
        return (b._pending_hangup_task, b._wrapping_up, b._ending, b._hangup_aborts, b._post_rsvp_hangup_armed)
    assert asyncio.run(run()) == (None, False, False, 0, True)


def test_pending_triage_treats_hello_question_mark_as_bare():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        b._wrapping_up = True
        b._schedule_end(mute=False)                  # soft end pending (farewell in flight)
        task = b._pending_hangup_task
        await b._on_caller_text("Hello? Hello?")     # the "?" must NOT count as a real follow-up
        kept = b._pending_hangup_task is task and not task.cancelled()
        locked = b._abort_locked
        await b._on_caller_text("but what about the link?")
        reopened = b._pending_hangup_task is None and b._wrapping_up is False
        task.cancel()
        return kept, locked, reopened
    assert asyncio.run(run()) == (True, True, True)


def test_post_rsvp_idle_waits_for_agent_playout(monkeypatch):
    """A 15-second checklist turn: text events end long before the audio finishes playing. The
    post-RSVP idle must measure MUTUAL silence with the outbound queue drained."""
    monkeypatch.setenv("EO_POST_RSVP_IDLE_SECONDS", "0.2")

    async def run(playing):
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        t = time.monotonic()
        b._last_activity = t - 10                    # text events ended long ago...
        b._last_caller_audio = t - 10
        if playing:
            b._last_agent_audio = t                  # ...but the checklist is still playing out
            await b._out_frames.put(b"\xff" * 160)
        else:
            b._last_agent_audio = t - 10
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pending = b._pending_hangup_task is not None
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        return pending
    assert asyncio.run(run(True)) is False
    assert asyncio.run(run(False)) is True


def test_hello_words_counted_per_event(monkeypatch):
    monkeypatch.setenv("EO_HELLO_STORM_COUNT", "3")
    assert ph._hello_word_count("Hello. Hello. Hello.") == 3
    assert ph._hello_word_count("can you hear me") == 0

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._turn_open = True
        b._last_agent_audio = time.monotonic()
        await b._on_caller_text("Hello. Hello. Hello.")     # ONE event, three hellos
        msgs = []
        while not b.text_input_queue.empty():
            msgs.append(b.text_input_queue.get_nowait())
        return [m for m in msgs if "can you hear me" in m.lower()]
    assert len(asyncio.run(run())) == 1


def test_wrapping_up_silences_rescues(monkeypatch):
    monkeypatch.setenv("EO_SILENCE_PROMPT_SECONDS", "0.2")
    monkeypatch.setenv("EO_PHANTOM_RESUME_DELAY_S", "0")
    monkeypatch.setenv("EO_MUTE_RECORD_NUDGE_DELAY_S", "0")

    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._wrapping_up = True
        b._greeting_sent_at = time.monotonic() - 5
        await b._maybe_rescue_greeting()
        b._turn_open = True
        b._last_agent_audio = time.monotonic()
        await b.audio_interrupt_callback()          # phantom → would normally schedule a resume
        await b._nudge_if_still_mute()
        t = time.monotonic()
        b._last_agent_audio = t - 10
        b._last_caller_audio = t - 10
        b._last_activity = t - 10
        b._turn_open = False
        task = asyncio.create_task(b._idle_hangup_guard())
        await asyncio.sleep(1.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if b._resume_task:
            b._resume_task.cancel()
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        return b.text_input_queue.empty()
    assert asyncio.run(run()) is True


def test_closing_gate_through_gemini_loop():
    events = [
        {"type": "tool_call", "name": "record_outcome", "args": {},
         "result": {"outcome_status": "acknowledged"}},
        {"type": "gemini", "text": "Wonderful! There's a short laptop setup — may I run through it quickly?"},
        {"type": "turn_complete"},
        {"type": "gemini", "text": "Perfect, you're all set then! See you tomorrow at four, take care!"},
        {"type": "turn_complete"},
    ]

    class FakeGemini:
        async def start_session(self, **kw):
            for ev in events:
                yield ev

    async def run():
        b = _bridge()
        b.gemini = FakeGemini()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._spoke_since_user = True
        scheduled = []
        orig = b._schedule_end

        def spy(mute=True):
            scheduled.append(b._turn_text.strip()[-30:])
            orig(mute=mute)
        b._schedule_end = spy
        await b._gemini_loop()
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        if b._mute_record_task:
            b._mute_record_task.cancel()
        return scheduled, b._wrapping_up
    scheduled, wrapping = asyncio.run(run())
    assert wrapping is True and len(scheduled) == 1 and "take care!" in scheduled[0]


# Per-agent listen window (announce-then-listen reminder calls)

def test_listen_seconds_overrides_the_post_outcome_idle_window(monkeypatch):
    """A reminder agent announces and hangs up shortly after; a logistics agent stays for
    a conversation. This must OVERRIDE the existing window, never add a second timer —
    two competing hangup paths is how call quality breaks."""
    monkeypatch.setenv("EO_POST_RSVP_IDLE_SECONDS", "12.0")

    # The value the idle watchdog will actually use, resolved the same way the loop does.
    def effective(bridge):
        return bridge.listen_seconds or float(os.getenv("EO_POST_RSVP_IDLE_SECONDS", "12.0"))

    assert effective(PlivoMediaBridge(FakeWS(), gemini_client=None, text_trigger="[go]",
                                      listen_seconds=6)) == 6.0
    # 0 / None / absent all mean "use the server default", never "hang up instantly"
    assert effective(PlivoMediaBridge(FakeWS(), gemini_client=None, text_trigger="[go]",
                                      listen_seconds=0)) == 12.0
    assert effective(PlivoMediaBridge(FakeWS(), gemini_client=None, text_trigger="[go]",
                                      listen_seconds=None)) == 12.0
    assert effective(PlivoMediaBridge(FakeWS(), gemini_client=None,
                                      text_trigger="[go]")) == 12.0


def test_a_malformed_listen_seconds_falls_back_rather_than_crashing_the_call():
    b = PlivoMediaBridge(FakeWS(), gemini_client=None, text_trigger="[go]",
                         listen_seconds="not a number")
    assert b.listen_seconds == 0.0


# ---------------------------------------------- asking to switch language after the goodbye
# Live call 7cccd613: the agent said goodbye, the guest answered "मैं तुमने गुजराती में बात करता हूं"
# ("I'll talk in Gujarati"), and the bridge hung up — only a QUESTION could re-open the call, and this
# is a statement. Naming a language after the goodbye means the guest wants to keep talking.
def _wrapped_up_bridge(pending=False):
    b = _bridge()
    b.stream_id = "s1"
    b._agent_audio_started = True
    b._rsvp_recorded = True
    b._wrapping_up = True
    b._goodbye_drained = True
    b._hangup_aborts = 1
    b._ending = True
    if pending:
        b._schedule_end(mute=False)
    return b


def _queued(b):
    out = []
    while not b.text_input_queue.empty():
        out.append(b.text_input_queue.get_nowait())
    return out


@pytest.mark.parametrize("text", [
    "मैं तुमने गुजराती में बात करता हूं।",          # the exact transcript from the live call
    "Hindi mein bolo",
    "ગુજરાતીમાં વાત કરો",
    "can we talk in Marathi",
    "अंग्रेज़ी में बात कीजिए",
])
def test_a_language_switch_after_the_goodbye_keeps_the_call_open(text):
    async def run():
        b = _wrapped_up_bridge()
        await b._on_caller_text(text)
        nudges = _queued(b)
        return (b._pending_hangup_task, b._wrapping_up, b._ending,
                len(nudges) == 1 and "NOT over" in nudges[0])
    assert asyncio.run(run()) == (None, False, False, True)


def test_a_language_switch_cancels_a_pending_hangup():
    async def run():
        b = _wrapped_up_bridge(pending=True)
        task = b._pending_hangup_task
        await b._on_caller_text("Gujarati ma bolo")
        await asyncio.sleep(0)
        return task.cancelled(), b._pending_hangup_task, b._wrapping_up
    assert asyncio.run(run()) == (True, None, False)


def test_language_reopens_are_capped_so_a_call_cannot_be_held_open_forever():
    async def run():
        b = _wrapped_up_bridge()
        for _ in range(2):
            await b._on_caller_text("Hindi mein bolo")
            b._wrapping_up, b._goodbye_drained, b._ending = True, True, True   # the agent said goodbye again
        await b._on_caller_text("Hindi mein bolo")                             # third time: over the cap
        pending = b._pending_hangup_task is not None and not b._pending_hangup_task.done()
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        return b._language_reopens, pending, b._abort_locked
    assert asyncio.run(run()) == (2, True, True)


def test_naming_a_language_mid_call_changes_nothing():
    """Only the end-of-call rules are affected: mid-conversation, the model handles a switch itself."""
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        await b._on_caller_text("Hindi mein bolo")
        return b._pending_hangup_task, b._wrapping_up, _queued(b)
    assert asyncio.run(run()) == (None, False, [])


@pytest.mark.parametrize("text", ["okay", "thanks bye", "theek hai", "haan ji", "shukriya", "ok thank you"])
def test_ordinary_sign_offs_do_not_look_like_a_language_switch(text):
    from plivo_handler import _looks_like_language_switch
    assert _looks_like_language_switch(text) is False


# ------------------------------------------------ listening sounds are not a sign-off
# Client report: "if I just say 'okay' or 'barobar' to show I am listening, it suddenly hangs up.
# It says 'thank you' and cuts the call." After record_outcome the bridge used to end the call on
# any caller "thank you / great / perfect / done" — exactly what a guest says while listening.
@pytest.mark.parametrize("text", ["okay", "barobar", "thank you", "okay thank you", "great",
                                  "perfect", "done", "haan ji", "theek hai"])
def test_a_listening_sound_after_the_outcome_does_not_hang_up(text):
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        await b._on_caller_text(text)
        return b._pending_hangup_task, b._wrapping_up
    assert asyncio.run(run()) == (None, False)


@pytest.mark.parametrize("text", ["ok bye", "bye", "good night", "शुभरात्रि", "આવજો"])
def test_a_clear_farewell_after_the_outcome_still_ends_the_call(text):
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._rsvp_recorded = True
        await b._on_caller_text(text)
        pending = b._pending_hangup_task is not None
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        return pending, b._wrapping_up
    assert asyncio.run(run()) == (True, True)


# ------------------------------------ following the guest's language
# Apeksha answered "हां, बोलो" and got the schedule in English; the prompt below fixes that
# for a REAL Hindi sentence. But Shivi (25 Sep 02:10) answered with one word the
# transcriber wrote in Devanagari and was pushed into Hindi she never asked for — so a
# greeting-length reply must never count as a language signal.
def test_a_hindi_sentence_answered_in_english_gets_one_switch_prompt():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        await b._on_caller_text("हाँ जी बोलिए, क्या काम है आपको?")
        await b._on_agent_text("Great. Sir, we've arranged a nice high tea for you today at four")
        first = _queued(b)
        await b._on_agent_text(" in the evening. It's at Harvest, with refreshments and snacks.")
        await b._on_caller_text("हां")                      # later Hindi: never a second prompt
        return first, _queued(b)
    first, later = asyncio.run(run())
    assert len(first) == 1 and "Hindi or Marathi" in first[0]
    assert "If they were actually speaking English" in first[0]   # a check, not an order
    assert later == []


@pytest.mark.parametrize("first_reply", ["हाँ", "हैलो", "हेलो हेलो हाँ", "जी", "हां, बोलो। हां, बोलो।", "હા બોલો"])
def test_a_one_word_or_greeting_reply_never_triggers_the_prompt(first_reply):
    """"haan", "hello", "haan bolo" say nothing about which language the guest wants."""
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        await b._on_caller_text(first_reply)
        await b._on_agent_text("Great. First, there's a high tea today at four in the evening at Harvest, "
                               "with refreshments and light snacks for everyone.")
        return _queued(b)
    assert asyncio.run(run()) == []


def test_no_prompt_when_the_agent_already_switched():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        await b._on_caller_text("હા બોલો")
        await b._on_agent_text("સરસ! કાલે સાંજે ચાર વાગ્યે Harvest માં Hi-Tea છે, અને")
        await b._on_agent_text(" then the Sufi Night at seven in the evening at Great Park.")
        return _queued(b)
    assert asyncio.run(run()) == []


def test_no_prompt_for_an_english_first_reply():
    """Mansi's first reply was "Hello." — Latin — so nothing fires, even though a later
    English sentence of hers came back in Devanagari ("सॉरी, आई कुड नॉट हियर यू")."""
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        await b._on_caller_text("Hello.")
        await b._on_caller_text("सॉरी, आई कुड नॉट हियर यू। कैन यू रिपीट दिस अगेन?")
        await b._on_agent_text("Oh, sorry. I'm speaking from Ved and Riya's Hospitality Team.")
        return _queued(b)
    assert asyncio.run(run()) == []


def test_speech_before_the_greeting_is_not_the_first_reply():
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        await b._on_caller_text("हेलो")                      # picked up, agent not yet speaking
        return b._first_reply_seen
    assert asyncio.run(run()) is False


def test_the_script_ranges_are_the_real_unicode_blocks():
    """Written as literal characters (several are unassigned code points): pin them so a
    re-encode or an editor can never shift them silently."""
    from plivo_handler import _SCRIPT_LANGUAGES
    got = {lang: (ord(lo), ord(hi)) for lo, hi, lang in _SCRIPT_LANGUAGES}
    assert got == {"Hindi or Marathi": (0x0900, 0x097F), "Bengali": (0x0980, 0x09FF),
                   "Punjabi": (0x0A00, 0x0A7F), "Gujarati": (0x0A80, 0x0AFF),
                   "Tamil": (0x0B80, 0x0BFF), "Telugu": (0x0C00, 0x0C7F),
                   "Kannada": (0x0C80, 0x0CFF), "Malayalam": (0x0D00, 0x0D7F)}


# -------------------------------------------- native-script replies are understood
# Since the language hints, a Hindi/Gujarati guest's words arrive in their own script; the
# romanised keyword lists missed every one of these.
@pytest.mark.parametrize("text", [
    "हेलो हेलो",
    "आवाज़ नहीं आ रही है",
    "सुनाई नहीं दे रहा",
    "હેલો હેલો કરી કરી ના ઉડતી થઈ ગઈ એક વસ્તુ ન સંભળાઈ મને",
    "हेलो हेलो करी करी ना उडती थई गई एक वस्तु न संभळाई मने नी",   # Mansi, as transcribed
])
def test_cant_hear_you_is_recognised_in_native_script(text):
    from plivo_handler import _looks_like_hello
    assert _looks_like_hello(text) is True


@pytest.mark.parametrize("text", ["हां, बोलो", "ठीक है", "બરાબર", "आपसे मिलकर अच्छा लगा"])
def test_ordinary_replies_are_not_line_trouble(text):
    from plivo_handler import _looks_like_hello
    assert _looks_like_hello(text) is False


@pytest.mark.parametrize("text", ["एक मिनट रुकिए", "रुको ज़रा", "એક મિનિટ ઊભા રહો"])
def test_hold_is_recognised_in_native_script(text):
    async def run():
        b = _bridge()
        b.stream_id = "s1"
        b._agent_audio_started = True
        await b._on_caller_text(text)
        return b._hold_until > time.monotonic()
    assert asyncio.run(run()) is True


@pytest.mark.parametrize("text", ["Hi-Tea कहाँ है", "कब शुरू होगा", "સૂફી નાઈટ ક્યારે છે"])
def test_a_native_script_question_after_the_goodbye_keeps_the_call_open(text):
    async def run():
        b = _wrapped_up_bridge()
        await b._on_caller_text(text)
        return b._pending_hangup_task, b._wrapping_up
    assert asyncio.run(run()) == (None, False)


# ------------------------------------ ending on a question (Shivi's call, 25 Sep 01:58)
# The agent answered "when is the Sufi Night?" with "…at Great Park, ma'am, is there
# anything?" AND called end_call in that same turn; the note-taker's "Great Park" then
# locked the hangup and "Can you say Great Park?" was never heard.
def _run_loop(events):
    class FakeGemini:
        async def start_session(self, **kw):
            for ev in events:
                yield ev

    async def run():
        b = _bridge()
        b.gemini = FakeGemini()
        b.stream_id = "s1"
        b._agent_audio_started = True
        b._spoke_since_user = True
        await b._gemini_loop()
        pending = b._pending_hangup_task is not None and not b._pending_hangup_task.done()
        if b._pending_hangup_task:
            b._pending_hangup_task.cancel()
        if b._mute_record_task:
            b._mute_record_task.cancel()
        return pending, b._wrapping_up
    return asyncio.run(run())


def test_end_call_in_a_turn_that_ends_on_a_question_keeps_the_call_open():
    pending, wrapping = _run_loop([
        {"type": "tool_call", "name": "record_outcome", "args": {},
         "result": {"outcome_status": "acknowledged"}},
        {"type": "gemini", "text": "The Sufi Night is today at seven in the evening at Great Park, "
                                   "ma'am. Is there anything else I can help you with?"},
        {"type": "end_call"},
        {"type": "turn_complete"},
    ])
    assert (pending, wrapping) == (False, False)


def test_a_real_goodbye_after_the_question_still_ends_the_call():
    """"Anything else? … have a lovely evening!" finishes on the goodbye, not the question."""
    pending, wrapping = _run_loop([
        {"type": "tool_call", "name": "record_outcome", "args": {},
         "result": {"outcome_status": "acknowledged"}},
        {"type": "gemini", "text": "Is there anything else? No? Then have a lovely evening, goodbye!"},
        {"type": "end_call"},
        {"type": "turn_complete"},
    ])
    assert (pending, wrapping) == (True, True)


def test_a_tool_only_end_call_waits_for_the_answer_then_ends_after_it():
    """end_call can come as its own turn just after the question: wait for the answer; once
    the guest has answered "no, thanks", the next end_call does hang up."""
    waiting = _run_loop([
        {"type": "gemini", "text": "Do you have any questions about any of these?"},
        {"type": "turn_complete"},
        {"type": "end_call"},
    ])
    assert waiting == (False, False)
    answered = _run_loop([
        {"type": "gemini", "text": "Do you have any questions about any of these?"},
        {"type": "turn_complete"},
        {"type": "user", "text": "No, thank you."},
        {"type": "end_call"},
    ])
    assert answered == (True, True)


# ------------------------------------------- the agent reading its tool call aloud
# Live test, 25 Sep: "…We look forward to seeing you! Goodbye. call record outcome guest name
# Shivi note Guest has food allergies strictly no peanuts and is vegetarian Call answered by
# their assistant outcome status acknowledged end call" — every word of it spoken.
_NARRATED_CLOSING = (
    "Thank you so much for being part of the celebrations. We look forward to seeing you! "
    "Goodbye. call record outcome guest name Shivi note Guest has food allergies strictly no "
    "peanuts and is vegetarian Call answered by their assistant outcome status acknowledged end call"
)


def test_tool_narration_is_detected_and_ordinary_closings_are_not():
    from plivo_handler import _is_tool_narration
    assert _is_tool_narration(_NARRATED_CLOSING) is True
    assert _is_tool_narration("Goodbye. record_outcome outcome_status acknowledged end_call") is True
    # the argument labels beside an outcome value, even without a function name
    assert _is_tool_narration("okay, guest name Shivi, note allergies, status acknowledged") is True
    # real speech that happens to use these words in passing
    assert _is_tool_narration("Thank you for being part of the celebrations, goodbye!") is False
    assert _is_tool_narration("I'll make a note of that and someone will call you back.") is False
    assert _is_tool_narration("Please note the Sufi Night starts at seven at Great Park.") is False
    assert _is_tool_narration("") is False


def test_narrated_tool_call_mutes_the_rest_of_the_turn_but_keeps_the_goodbye():
    """Once the transcript turns into bookkeeping, NEW audio for the turn is dropped; the
    goodbye already queued for playout is never flushed."""
    async def run():
        ws = FakeWS()
        b = _bridge(ws)
        b.stream_id = "s1"
        b._agent_audio_started = True
        for _ in range(5):                                   # the goodbye, already queued
            await b._out_frames.put(b"\xff" * 160)
        await b._on_agent_text("Thank you so much for being part of the celebrations. Goodbye.")
        before = b._suppress_turn
        await b._on_agent_text(" call record outcome guest name Shivi note allergies outcome status acknowledged")
        after = b._suppress_turn
        await b.audio_output_callback(b"\x00\x10" * 240)     # narration audio → dropped
        cleared = any(p.get("event") == "clearAudio" for p in ws.sent)
        return before, after, b._out_frames.qsize(), bool(b._residual), cleared
    assert asyncio.run(run()) == (False, True, 5, False, False)
