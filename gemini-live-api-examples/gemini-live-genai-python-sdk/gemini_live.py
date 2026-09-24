import asyncio
import inspect
import logging
import os
import traceback

logger = logging.getLogger(__name__)
from google import genai
from google.genai import types

from agent_tools import END_CALL_DECLARATION

# NON_BLOCKING + SILENT function calling (the record_outcome double-reply fix) exists only in google-genai >= 2.x; feature-detect and fall back to a blocking tool result on older SDKs.
try:
    _SILENT_SCHEDULING = types.FunctionResponseScheduling.SILENT
except AttributeError:
    _SILENT_SCHEDULING = None
    logger.warning("DOUBLE-REPLY FIX DEGRADED: installed google-genai lacks "
                   "NON_BLOCKING/SILENT (SDK < 2.x). record_outcome falls back to the "
                   "prompt+tool-result mitigation. Upgrade to google-genai>=2.10 for "
                   "the protocol-level fix.")
else:
    logger.info("google-genai async function calling ACTIVE: record_outcome is NON_BLOCKING + SILENT "
                "(no forced turn → no doubled closing); the bridge nudges the agent to speak if it "
                "records without speaking first (mute-proof).")


# 7x is multi-agent: the system prompt is no longer a module constant. Each call renders its agent's
# template against its wedding/event/guest rows (see prompt_render.render_prompt) and passes the result
# into GeminiLive(system_instruction=...). This module knows nothing about any particular script.

# Spoken fallback used only when a caller constructs GeminiLive without a rendered instruction (a cold
# connect whose context lookup failed). Deliberately generic: it must never claim to be any client.
FALLBACK_SYSTEM_INSTRUCTION = (
    "You are a warm, professional hospitality caller on an Indian phone line. Speak natural, unhurried "
    "Indian English, one short idea per turn, then stop and listen. You have not been given the details "
    "of this call, so do NOT invent an event, a name, a time or a venue. Apologise briefly for the "
    "confusion, say someone from the team will call back shortly, then call end_call."
)


_DEFAULT_TRANSCRIBE_HINTS = "en-IN,hi-IN,gu-IN,mr-IN,pa-IN,bn-IN,ta-IN,te-IN,kn-IN,ml-IN"


# Set once the Live API has rejected our transcription language settings. Every later
# session then uses the plain config, which is known to work.
_transcribe_lang_disabled = False


def _input_transcription_config():
    """How the CALLER's words are transcribed.

    This is the text copy of what the guest said: call logs, the language stats, and the
    bridge's text heuristics. It does NOT change what the model understands — a
    native-audio model hears the audio itself.

    language_hints biases the transcript toward the languages this platform serves.
    language_auto, language_hints and language_codes are ONE oneof on the server
    ('language_config'): setting two of them makes the Live API refuse the session with
    1007, which failed every call. The SDK does not check this, so only one may ever be
    set here. (language_codes is also Vertex-only.) An empty EO_TRANSCRIBE_LANGUAGE_HINTS
    means the plain config."""
    if _transcribe_lang_disabled:
        return types.AudioTranscriptionConfig()
    hints = [h.strip() for h in
             (os.getenv("EO_TRANSCRIBE_LANGUAGE_HINTS", _DEFAULT_TRANSCRIBE_HINTS) or "").split(",")
             if h.strip()]
    if not hints:
        return types.AudioTranscriptionConfig()
    try:
        return types.AudioTranscriptionConfig(
            language_hints=types.LanguageHints(language_codes=hints))
    except Exception as e:                  # older SDK: the field is absent
        logger.warning(f"Live API transcription language hints unsupported ({e}); "
                       f"falling back to default transcription")
        return types.AudioTranscriptionConfig()


def _note_setup_rejection(exc) -> bool:
    """If the Live API refused the session over our transcription settings, turn them
    off for the rest of this process and return True.

    The SDK builds configs the server then rejects (see _input_transcription_config), and
    a pre-send check cannot catch that. So the first refusal switches every later session
    to the plain config: on a phone call the pre-warm fails, and the cold connect that
    follows it succeeds; in the browser test the retry does."""
    global _transcribe_lang_disabled
    if "input_audio_transcription" not in str(exc) or _transcribe_lang_disabled:
        return False
    _transcribe_lang_disabled = True
    logger.error("Live API rejected the input-transcription language settings (%s). "
                 "Using plain transcription for every session from now on; fix "
                 "EO_TRANSCRIBE_LANGUAGE_HINTS and restart.", str(exc)[:160])
    return True


class _PreopenedSession:
    """A Live session whose connect handshake already happened (see GeminiLive.open_connection).
    Quacks like the async context manager start_session expects: __aenter__ hands back the
    already-open session; __aexit__ closes the underlying connection."""
    def __init__(self, ctx, session):
        self._ctx = ctx
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return await self._ctx.__aexit__(*exc)


class GeminiLive:
    """
    Handles the interaction with the Gemini Live API.
    """
    def __init__(self, api_key, model, input_sample_rate, tools=None, tool_mapping=None,
                 system_instruction=None, voice_name=None, speech_language_code=None):
        """
        Initializes the GeminiLive client.

        Args:
            api_key (str): The Gemini API Key.
            model (str): The model name to use.
            input_sample_rate (int): The sample rate for audio input.
            tools (list, optional): Tool config, already built for this call's agent
                (see agent_tools.build_tools). Falls back to a bare end_call so a
                context-less cold connect can still hang up cleanly.
            tool_mapping (dict, optional): Mapping of tool names to functions.
            system_instruction (str, optional): The rendered prompt for THIS call
                (see prompt_render.render_prompt). Falls back to
                FALLBACK_SYSTEM_INSTRUCTION, which claims no client identity.
            voice_name (str, optional): Per-agent voice; falls back to EO_VOICE_NAME.
            speech_language_code (str, optional): Per-agent BCP-47 accent; falls back
                to EO_SPEECH_LANGUAGE_CODE. Fixed for the session — cannot switch mid-call.
        """
        self.api_key = api_key
        self.model = model
        self.input_sample_rate = input_sample_rate
        self.client = genai.Client(api_key=api_key)
        self.tools = tools or [{"function_declarations": [END_CALL_DECLARATION]}]
        self.tool_mapping = tool_mapping or {}
        self.system_instruction = system_instruction or FALLBACK_SYSTEM_INSTRUCTION
        self.voice_name = (voice_name or "").strip()
        self.speech_language_code = (speech_language_code or "").strip()

    def _plan_tool_result(self, func_name, result):
        """Post-process a tool result before it goes back to Gemini.
        Returns (result, scheduling, end_requested).

        record_outcome is silent bookkeeping: it must never force a turn, or the agent
        speaks its closing twice (the double-reply bug). end_call ends the call."""
        if func_name == "record_outcome":
            return result, _SILENT_SCHEDULING, False
        if func_name == "end_call":
            return result, None, True
        return result, None, False

    def _build_config(self):
        """LiveConnectConfig from env — shared by start_session and open_connection.
        Server-side VAD knobs, env-tunable; silence_duration_ms is the biggest lever on perceived reply latency."""
        def _env_int(name, default):
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default
        vad_prefix_ms = _env_int("EO_VAD_PREFIX_MS", 250)
        vad_silence_ms = _env_int("EO_VAD_SILENCE_MS", 550)
        start_sens = (types.StartSensitivity.START_SENSITIVITY_HIGH
                      if os.getenv("EO_VAD_START_SENSITIVITY", "LOW").strip().upper() == "HIGH"
                      else types.StartSensitivity.START_SENSITIVITY_LOW)   # KEEP LOW: anti-echo on phone
        end_sens = (types.EndSensitivity.END_SENSITIVITY_LOW
                    if os.getenv("EO_VAD_END_SENSITIVITY", "HIGH").strip().upper() == "LOW"
                    else types.EndSensitivity.END_SENSITIVITY_HIGH)        # KEEP HIGH: snappy end-of-turn
        # Per-agent voice wins; EO_VOICE_NAME is the server-wide fallback so voices can be A/B'd
        # without a code deploy. Warm female default.
        voice_name = self.voice_name or (os.getenv("EO_VOICE_NAME", "Aoede") or "Aoede").strip() or "Aoede"
        # Voice language bias. en-IN = Indian-English accent; the native-audio model still understands and can
        # speak Hindi/Gujarati (see the agent's LANGUAGE prompt section). Fixed for the whole session — cannot
        # switch mid-call, which is why it lives on the agent row rather than being decided per turn.
        language_code = (self.speech_language_code
                         or (os.getenv("EO_SPEECH_LANGUAGE_CODE", "en-IN") or "en-IN").strip()
                         or "en-IN")
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                language_code=language_code,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
            system_instruction=types.Content(parts=[types.Part(text=self.system_instruction)]),
            # INPUT transcription must not inherit the output voice's language_code. With
            # it pinned to en-IN, a guest answering in Hindi was transcribed as garbled
            # English, so the agent could not respond and the silence ladder hung up on
            # them. language_auto detects per utterance; the hints bias it to the languages
            # this platform actually serves. The OUTPUT voice stays fixed — the Live API
            # cannot change speaking language mid-session.
            input_audio_transcription=_input_transcription_config(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=start_sens,
                    end_of_speech_sensitivity=end_sens,
                    prefix_padding_ms=vad_prefix_ms,    # committed speech required before start → ignore clicks/echo tails
                    silence_duration_ms=vad_silence_ms, # this much silence ends the turn → latency vs patience trade
                ),
                turn_coverage="TURN_INCLUDES_ONLY_ACTIVITY",
            ),
            tools=self.tools,
        )
        # Affective dialog (native-audio models): the model reads the caller's tone and answers with matching
        # expression instead of a flat read. Opt-in until verified on the test box — an unsupported model
        # rejects the session at connect time, which would kill every call.
        affective = os.getenv("EO_AFFECTIVE_DIALOG", "false").strip().lower() in ("1", "true", "yes", "on")
        if affective and "enable_affective_dialog" in getattr(types.LiveConnectConfig, "model_fields", {}):
            config.enable_affective_dialog = True
        elif affective:
            logger.warning("EO_AFFECTIVE_DIALOG=true but this google-genai has no enable_affective_dialog; ignored")
        logger.info(f"Voice={voice_name} language={language_code} "
                    f"prompt_chars={len(self.system_instruction)} "
                    f"affective={'on' if getattr(config, 'enable_affective_dialog', None) else 'off'}; "
                    f"VAD config: prefix={vad_prefix_ms}ms "
                    f"silence={vad_silence_ms}ms "
                    f"start={'HIGH' if start_sens == types.StartSensitivity.START_SENSITIVITY_HIGH else 'LOW'} "
                    f"end={'LOW' if end_sens == types.EndSensitivity.END_SENSITIVITY_LOW else 'HIGH'}")
        if start_sens == types.StartSensitivity.START_SENSITIVITY_HIGH:
            logger.warning(
                "EO_VAD_START_SENSITIVITY=HIGH: on phone audio this makes line echo of the "
                "agent's own voice trigger FALSE barge-ins (mid-word audio cuts heard as "
                "'voice breaking' + repeated lines). Set it to LOW unless you know why.")
        if vad_silence_ms < 500:
            logger.warning(
                f"EO_VAD_SILENCE_MS={vad_silence_ms} is aggressive: caller turns get cut at "
                "short mid-sentence pauses, so the agent replies to half a sentence. "
                "550-650ms is the recommended range for phone calls.")
        return config

    async def open_connection(self):
        """Pre-open a Live session (the ~1s network handshake) BEFORE the media stream
        arrives, so the handshake overlaps the telephony setup instead of adding to the
        caller's dead air. Pass the returned handle to start_session(preopened=...); if
        it's never adopted, the owner must close it via handle.__aexit__(None, None, None)."""
        config = self._build_config()
        logger.info(f"Pre-connecting Gemini Live (model={self.model})")
        ctx = self.client.aio.live.connect(model=self.model, config=config)
        try:
            session = await ctx.__aenter__()
        except Exception as e:
            _note_setup_rejection(e)       # the cold connect that follows then succeeds
            raise
        logger.info("Gemini Live session pre-opened")
        return _PreopenedSession(ctx, session)

    async def start_session(self, audio_input_queue, video_input_queue, text_input_queue, audio_output_callback, audio_interrupt_callback=None, preopened=None):
        if preopened is not None:
            cm = preopened
            logger.info("Adopting pre-warmed Gemini Live session (connect handshake already done)")
        else:
            cm = self.client.aio.live.connect(model=self.model, config=self._build_config())
            logger.info(f"Connecting to Gemini Live with model={self.model}")
        try:
          async with cm as session:
            logger.info("Gemini Live session opened successfully")

            async def send_audio():
                try:
                    while True:
                        chunk = await audio_input_queue.get()
                        await session.send_realtime_input(
                            audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={self.input_sample_rate}")
                        )
                except asyncio.CancelledError:
                    logger.debug("send_audio task cancelled")
                except Exception as e:
                    logger.error(f"send_audio error: {e}\n{traceback.format_exc()}")

            async def send_video():
                try:
                    while True:
                        chunk = await video_input_queue.get()
                        logger.info(f"Sending video frame to Gemini: {len(chunk)} bytes")
                        await session.send_realtime_input(
                            video=types.Blob(data=chunk, mime_type="image/jpeg")
                        )
                except asyncio.CancelledError:
                    logger.debug("send_video task cancelled")
                except Exception as e:
                    logger.error(f"send_video error: {e}\n{traceback.format_exc()}")

            async def send_text():
                try:
                    while True:
                        text = await text_input_queue.get()
                        logger.info(f"Sending text to Gemini: {text}")
                        await session.send_realtime_input(text=text)
                except asyncio.CancelledError:
                    logger.debug("send_text task cancelled")
                except Exception as e:
                    logger.error(f"send_text error: {e}\n{traceback.format_exc()}")

            event_queue = asyncio.Queue()

            async def receive_loop():
                try:
                    while True:
                        async for response in session.receive():
                            logger.debug(f"Received response from Gemini: {response}")

                            # Real token usage for cost tracking (split by modality).
                            if response.usage_metadata:
                                um = response.usage_metadata
                                await event_queue.put({
                                    "type": "usage",
                                    "total": um.total_token_count or 0,
                                    "thoughts": um.thoughts_token_count or 0,
                                    "prompt_by_modality": [
                                        (str(d.modality), d.token_count or 0)
                                        for d in (um.prompt_tokens_details or [])
                                    ],
                                    "response_by_modality": [
                                        (str(d.modality), d.token_count or 0)
                                        for d in (um.response_tokens_details or [])
                                    ],
                                })

                            if response.go_away:
                                logger.warning(f"Received GoAway from Gemini: {response.go_away}")
                                await event_queue.put({"type": "go_away"})
                                return
                            if response.session_resumption_update:
                                logger.debug(f"Session resumption update: {response.session_resumption_update}")

                            server_content = response.server_content
                            tool_call = response.tool_call

                            if server_content:
                                if server_content.model_turn:
                                    for part in server_content.model_turn.parts:
                                        if part.inline_data:
                                            if inspect.iscoroutinefunction(audio_output_callback):
                                                await audio_output_callback(part.inline_data.data)
                                            else:
                                                audio_output_callback(part.inline_data.data)

                                if server_content.input_transcription and server_content.input_transcription.text:
                                    await event_queue.put({"type": "user", "text": server_content.input_transcription.text})

                                if server_content.output_transcription and server_content.output_transcription.text:
                                    await event_queue.put({"type": "gemini", "text": server_content.output_transcription.text})

                                if server_content.turn_complete:
                                    await event_queue.put({"type": "turn_complete"})

                                if server_content.interrupted:
                                    if audio_interrupt_callback:
                                        if inspect.iscoroutinefunction(audio_interrupt_callback):
                                            await audio_interrupt_callback()
                                        else:
                                            audio_interrupt_callback()
                                    await event_queue.put({"type": "interrupted"})

                            if tool_call:
                                function_responses = []
                                end_requested = False
                                for fc in tool_call.function_calls:
                                    func_name = fc.name
                                    args = fc.args or {}
                                    if func_name == "end_call" and func_name not in self.tool_mapping:
                                        end_requested = True

                                    if func_name in self.tool_mapping:
                                        try:
                                            tool_func = self.tool_mapping[func_name]
                                            if inspect.iscoroutinefunction(tool_func):
                                                result = await tool_func(**args)
                                            else:
                                                loop = asyncio.get_running_loop()
                                                result = await loop.run_in_executor(None, lambda: tool_func(**args))
                                        except Exception as e:
                                            result = f"Error: {e}"

                                        # record_outcome's result is SILENT (when supported) so it never continues a turn (the doubled-closing fix); end_call and the <2.x fallback stay blocking.
                                        result, scheduling, ends = self._plan_tool_result(func_name, result)
                                        end_requested = end_requested or ends
                                        fr_kwargs = {"name": func_name, "id": fc.id, "response": {"result": result}}
                                        if scheduling is not None:
                                            fr_kwargs["scheduling"] = scheduling
                                        try:
                                            function_responses.append(types.FunctionResponse(**fr_kwargs))
                                        except (TypeError, ValueError) as e:
                                            logger.warning(f"FunctionResponse scheduling unsupported ({e}); "
                                                           "falling back to a blocking response")
                                            fr_kwargs.pop("scheduling", None)
                                            function_responses.append(types.FunctionResponse(**fr_kwargs))
                                        await event_queue.put({"type": "tool_call", "name": func_name, "args": args, "result": result})

                                if function_responses:
                                    await session.send_tool_response(function_responses=function_responses)
                                # Signal the caller to hang up only after the goodbye audio has been emitted.
                                if end_requested:
                                    await event_queue.put({"type": "end_call"})

                        # session.receive() iterator ended (e.g. after turn_complete) — re-enter to keep listening
                        logger.debug("Gemini receive iterator completed, re-entering receive loop")

                except asyncio.CancelledError:
                    logger.debug("receive_loop task cancelled")
                except Exception as e:
                    logger.error(f"receive_loop error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                    await event_queue.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
                finally:
                    logger.info("receive_loop exiting")
                    await event_queue.put(None)

            send_audio_task = asyncio.create_task(send_audio())
            send_video_task = asyncio.create_task(send_video())
            send_text_task = asyncio.create_task(send_text())
            receive_task = asyncio.create_task(receive_loop())

            try:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    if isinstance(event, dict) and event.get("type") == "error":
                        # Yield the error event instead of raising so the caller can handle it.
                        yield event
                        break
                    yield event
            finally:
                logger.info("Cleaning up Gemini Live session tasks")
                send_audio_task.cancel()
                send_video_task.cancel()
                send_text_task.cancel()
                receive_task.cancel()
        except Exception as e:
            _note_setup_rejection(e)       # so the caller's retry connects with a plain config
            logger.error(f"Gemini Live session error: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise
        finally:
            logger.info("Gemini Live session closed")
