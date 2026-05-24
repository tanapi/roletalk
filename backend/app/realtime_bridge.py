from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
import wave
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import websockets
from fastapi import WebSocket
from websockets.exceptions import ConnectionClosed

from .main_types import InterviewSession
from .prompts import discussion_conclusion_prompt, discussion_interruption_prompt
from .voice import VoiceProvider
from .webrtc_live import (
    GEMINI_REST_URL,
    GEMINI_WS_URL,
    _api_key,
    _audio_sample_rate,
    _extract_generate_content_text,
    _input_language_code,
    _normalize_transcript,
    _output_pitch_factor,
    _realtime_input_config,
)


SCORING_SEGMENT_SAMPLE_RATE = 48000
SCORING_SEGMENT_SAMPLE_WIDTH = 2
SCORING_SEGMENT_MAX_SECONDS = float(os.getenv("SCORING_SEGMENT_MAX_SECONDS", "90"))
SCORING_SEGMENT_SILENT_CLOSE_SECONDS = float(os.getenv("SCORING_SEGMENT_SILENT_CLOSE_SECONDS", "4.0"))
SCORING_ASSISTANT_CLOSE_GRACE_SECONDS = float(os.getenv("SCORING_ASSISTANT_CLOSE_GRACE_SECONDS", "0.8"))
SCORING_MAX_SEGMENTS = int(os.getenv("SCORING_MAX_SEGMENTS", "40"))
SCORING_TRANSCRIPTION_TIMEOUT_SEC = float(os.getenv("SCORING_TRANSCRIPTION_TIMEOUT_SEC", "30"))
SCORING_DEFAULT_TRANSCRIPTION_MODEL = "gemini-2.5-flash"
SCORING_PROMPT_TEXT = (
    "次の音声を日本語で正確に文字起こししてください。"
    "聞き取れない箇所は推測で補わず、自然な句読点を付け、"
    "話者ラベル、説明、引用符、注釈は付けないでください。"
    "文字起こし本文のみを返してください。"
)


SFU_BUFFER_CHUNK_SIZE = 16 * 1024
INPUT_RING_SECONDS = 35
INPUT_SAMPLE_RATE = 48000
INPUT_SAMPLE_WIDTH = 2
INPUT_RING_MAX_BYTES = INPUT_RING_SECONDS * INPUT_SAMPLE_RATE * INPUT_SAMPLE_WIDTH
MAX_TRACE_EVENTS = 1200
MAX_DIAGNOSTIC_CAPTURES = 8


class CloudflareRealtimeBridgeSession:
    def __init__(
        self,
        provider: VoiceProvider,
        session: InterviewSession,
        logger,
        input_adapter_refresh: Callable[[], Any] | None = None,
    ) -> None:
        self.provider = provider
        self.session = session
        self.logger = logger
        self.input_adapter_refresh = input_adapter_refresh
        self.gemini_send_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.output_audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.events: list[dict[str, Any]] = []
        self.event_offset = 0
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.gemini_ready: asyncio.Event | None = None
        self.output_ready = asyncio.Event()
        self.suppress_input_audio = False
        self.pitch_factor = _output_pitch_factor()
        self.pending_input_transcripts: list[str] = []
        self.turn_output_audio_chunks = 0
        self.turn_output_transcripts = 0
        self.turn_output_started = False
        self.turn_audio_started = False
        self.assistant_output_active = False
        self.assistant_generation_complete = False
        self.pending_discussion_conclusion = False
        self.skip_current_turn_for_discussion_conclusion = False
        self.buffered_input_after_generation: deque[bytes] = deque()
        self.buffered_input_after_generation_bytes = 0
        self.buffered_input_after_generation_frames = 0
        self.audio_stats = {
            "frames": 0,
            "samples": 0,
            "zeroSamples": 0,
            "silentFrames": 0,
            "rmsSum": 0,
            "rmsMax": 0,
            "peak": 0,
            "lastBytesPerFrame": 0,
        }
        self.last_input_audio_at = 0.0
        self.last_input_transcript_at = 0.0
        self.last_turn_complete_at = 0.0
        self.last_gemini_message_at = 0.0
        self.last_gemini_server_summary: dict[str, Any] = {}
        self.last_stall_event_at = 0.0
        self.last_playback_interrupt_at = 0.0
        self.last_input_adapter_refresh_at = 0.0
        self.input_frames_at_last_adapter_refresh = 0
        self.audio_stream_end_sent_for_turn = False
        self.silence_padding_active_for_turn = False
        self.last_silence_padding_at = 0.0
        self.input_transcripts_at_last_turn = 0
        self.sent_messages_at_last_turn = 0
        self.input_audio_frames_at_last_turn = 0
        self.silence_padding_frames_at_last_turn = 0
        self.trace_started_at = time.monotonic()
        self.trace_events: list[dict[str, Any]] = []
        self.input_audio_ring: deque[bytes] = deque()
        self.input_audio_ring_bytes = 0
        self.diagnostic_captures: dict[str, dict[str, Any]] = {}
        self.diagnostic_capture_order: list[str] = []
        self.counters = {
            "input_audio_frames": 0,
            "input_audio_bytes": 0,
            "suppressed_input_audio_frames": 0,
            "buffered_input_audio_frames": 0,
            "flushed_buffered_input_audio_frames": 0,
            "dropped_input_audio_frames": 0,
            "text_turns": 0,
            "metrics": 0,
            "gemini_messages": 0,
            "gemini_sent_messages": 0,
            "gemini_audio_chunks": 0,
            "gemini_audio_bytes": 0,
            "output_audio_packets": 0,
            "input_transcripts": 0,
            "output_transcripts": 0,
            "turn_complete": 0,
            "gemini_setup_sent": 0,
            "gemini_setup_complete": 0,
            "gemini_ready_timeouts": 0,
            "gemini_receiver_errors": 0,
            "gemini_response_fallbacks": 0,
            "gemini_audio_stream_end_sent": 0,
            "gemini_silence_padding_frames": 0,
            "cloudflare_input_adapter_refreshes": 0,
            "output_preroll_packets": 0,
            "output_tail_packets": 0,
            "playback_interrupts": 0,
            "dropped_output_audio_packets": 0,
        }
        self.gemini_state: dict[str, Any] = {
            "connected": False,
            "setupSent": False,
            "setupComplete": False,
            "closed": False,
            "closeCode": None,
            "closeReason": "",
            "lastMessageType": "",
            "lastError": "",
        }
        self._last_stats_at = time.monotonic()
        # Scoring-only post-audio transcription pipeline (separate from realtime input/output).
        self._scoring_segment_buffer: bytearray = bytearray()
        self._scoring_segment_start_at: float | None = None
        self._scoring_segment_last_voiced_at: float | None = None
        self._scoring_segment_voiced_frames: int = 0
        self._scoring_segment_realtime_chunks: list[str] = []
        self._scoring_next_segment_id: int = 0
        self._scoring_segments: list[dict[str, Any]] = []
        self._scoring_segment_tasks: set[asyncio.Task] = set()
        self._scoring_pending_close_at: float | None = None
        self._scoring_pending_close_reason: str | None = None
        self._scoring_silence_rms_threshold = int(
            os.getenv("VOICE_BRIDGE_SILENCE_RMS_THRESHOLD", "80")
        )
        self._final_transcript_errors: list[dict[str, Any]] = []
        self._trace("session_created", {"model": self.provider.status().model})

    def ensure_started(self) -> None:
        if self.started:
            return
        self.started = True
        self._trace("gemini_task_starting", {"model": self.provider.status().model})
        self._emit_event({"type": "gemini_task_starting", "model": self.provider.status().model})
        task = asyncio.create_task(self._run_gemini())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        watchdog = asyncio.create_task(self._input_idle_watchdog())
        self.tasks.add(watchdog)
        watchdog.add_done_callback(self.tasks.discard)

    def handle_control_payload(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        if event_type == "text":
            self.ensure_started()
            text = str(payload.get("text", ""))
            self._queue_control_text_turn(text, "client_text_turn")
        elif event_type == "discussion_conclusion":
            self.ensure_started()
            self.pending_discussion_conclusion = True
            self.skip_current_turn_for_discussion_conclusion = (
                self.turn_output_started or self.turn_output_audio_chunks > 0 or self.turn_output_transcripts > 0
            )
            self._queue_control_text_turn(self._discussion_conclusion_prompt(), "discussion_conclusion_turn")
        elif event_type == "discussion_interruption":
            self.ensure_started()
            self._queue_control_text_turn(self._discussion_interruption_prompt(), "discussion_interruption_turn")
        elif event_type == "metrics":
            self.counters["metrics"] += 1
            metrics = payload.get("metrics")
            if isinstance(metrics, dict):
                self._record_interaction_metrics(metrics)
            self.logger.info("cloudflare_bridge_metrics interview_id=%s metrics=%s", self.session.interviewId, metrics)
        elif event_type == "audio_stream_end":
            self._send_gemini_audio_stream_end("control")
        elif event_type == "interrupt":
            dropped_packets = self._clear_output_audio_queue()
            self.assistant_output_active = False
            self._emit_event({"type": "interrupted", "source": "control", "droppedOutputPackets": dropped_packets})
            self.logger.info("cloudflare_bridge_interrupt interview_id=%s", self.session.interviewId)

    def _queue_control_text_turn(self, text: str, trace_type: str) -> None:
        dropped_audio = self._drop_pending_realtime_audio()
        dropped_output = self._clear_output_audio_queue()
        self.assistant_output_active = False
        self.suppress_input_audio = False
        self.counters["text_turns"] += 1
        self._trace(
            trace_type,
            {"chars": len(text), "droppedAudio": dropped_audio, "droppedOutput": dropped_output},
        )
        self.logger.info(
            "cloudflare_bridge_text_turn interview_id=%s type=%s text_len=%s dropped_audio=%s dropped_output=%s",
            self.session.interviewId,
            trace_type,
            len(text),
            dropped_audio,
            dropped_output,
        )
        self.gemini_send_queue.put_nowait(
            {
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": text}]}],
                    "turnComplete": True,
                }
            }
        )

    def _discussion_conclusion_prompt(self) -> str:
        return discussion_conclusion_prompt()

    def _discussion_interruption_prompt(self) -> str:
        return discussion_interruption_prompt()

    def _emit_discussion_conclusion_complete(self) -> None:
        fallback_text = "ここまでの議論を確認しました。これでディスカッションを終了します"
        last_assistant = next(
            (
                str(message.get("content", ""))
                for message in reversed(self.session.messages)
                if message.get("role") == "assistant"
            ),
            "",
        )
        transcript_missing_final_phrase = "これでディスカッションを終了します" not in last_assistant
        if transcript_missing_final_phrase:
            self._trace("discussion_conclusion_transcript_missing", {"fallbackChars": len(fallback_text)})
        self._emit_event(
            {
                "type": "discussion_conclusion_complete",
                "fallbackTranscript": transcript_missing_final_phrase,
                "turnAudioChunks": self.turn_output_audio_chunks,
                "outputTranscripts": self.turn_output_transcripts,
            }
        )

    def enable_output(self) -> None:
        self.output_ready.set()

    async def handle_input_websocket(self, websocket: WebSocket) -> None:
        self.ensure_started()
        try:
            while True:
                message = await websocket.receive()
                data = message.get("bytes")
                if data:
                    pcm_payload = _decode_sfu_packet(data)
                    if pcm_payload is None:
                        pcm_payload = data
                    gemini_pcm = _pcm_stereo_48k_to_mono_48k(pcm_payload)
                    self.counters["input_audio_frames"] += 1
                    self.counters["input_audio_bytes"] += len(gemini_pcm)
                    self._record_input_audio_stats(gemini_pcm)
                    self._remember_input_audio(gemini_pcm)
                    self._maybe_interrupt_playback_for_user_audio(gemini_pcm)
                    self._accumulate_scoring_audio(gemini_pcm)
                    self._enqueue_gemini_audio(gemini_pcm)
                    self._maybe_emit_input_stall()
                    self._maybe_log_stats("audio_in")
                if message.get("type") == "websocket.disconnect":
                    break
        finally:
            self.logger.info("cloudflare_bridge_input_done interview_id=%s counters=%s", self.session.interviewId, self.counters.copy())

    async def handle_output_websocket(self, websocket: WebSocket) -> None:
        self.ensure_started()
        try:
            await self.output_ready.wait()
            while True:
                audio = await self.output_audio_queue.get()
                if audio is None:
                    return
                self.counters["output_audio_packets"] += 1
                await websocket.send_bytes(audio)
        finally:
            self.logger.info("cloudflare_bridge_output_done interview_id=%s counters=%s", self.session.interviewId, self.counters.copy())

    async def close(self) -> None:
        self.flush_pending_scoring_segment()
        self.store_audio_summary()
        self.gemini_send_queue.put_nowait(None)
        self.output_audio_queue.put_nowait(None)
        for task in list(self.tasks):
            task.cancel()

    def get_events_since(self, cursor: int) -> tuple[int, list[dict[str, Any]]]:
        next_cursor = self.event_offset + len(self.events)
        safe_cursor = max(self.event_offset, min(cursor, next_cursor))
        start_index = safe_cursor - self.event_offset
        return next_cursor, self.events[start_index:]

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "bridge": True,
            "started": self.started,
            "mode": "gemini",
            "counters": self.counters.copy(),
            "geminiState": self.gemini_state.copy(),
            "queues": {
                "gemini": self.gemini_send_queue.qsize(),
                "output": self.output_audio_queue.qsize(),
            },
            "ring": {
                "bytes": self.input_audio_ring_bytes,
                "secondsApprox": round(self.input_audio_ring_bytes / (INPUT_SAMPLE_RATE * INPUT_SAMPLE_WIDTH), 2),
            },
            "captures": [
                {
                    "id": capture_id,
                    "createdAt": capture["createdAt"],
                    "reason": capture["reason"],
                    "bytes": len(capture["wav"]),
                    "seconds": capture["seconds"],
                    "endpoint": f"/api/interviews/{self.session.interviewId}/voice/diagnostics/{capture_id}.wav",
                }
                for capture_id, capture in self.diagnostic_captures.items()
            ],
            "trace": self.trace_events,
        }

    def get_diagnostic_wav(self, capture_id: str) -> bytes | None:
        capture = self.diagnostic_captures.get(capture_id)
        if not capture:
            return None
        wav = capture.get("wav")
        return wav if isinstance(wav, bytes) else None

    def _emit_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)
        if len(self.events) > 5000:
            drop_count = len(self.events) - 5000
            self.event_offset += drop_count
            self.events = self.events[drop_count:]

    def _trace(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        now = time.monotonic()
        event = {
            "atSec": round(now - self.trace_started_at, 3),
            "type": event_type,
            "turnComplete": self.counters.get("turn_complete", 0),
            "inFrames": self.counters.get("input_audio_frames", 0),
            "sent": self.counters.get("gemini_sent_messages", 0),
            "inText": self.counters.get("input_transcripts", 0),
            "outText": self.counters.get("output_transcripts", 0),
            "outChunks": self.counters.get("gemini_audio_chunks", 0),
        }
        if details:
            event.update(details)
        self.trace_events.append(event)
        if len(self.trace_events) > MAX_TRACE_EVENTS:
            self.trace_events = self.trace_events[-MAX_TRACE_EVENTS:]

    def record_timeline_event(self, event_type: str, **details: Any) -> None:
        now = time.monotonic()
        at_sec = round(now - self.trace_started_at, 3)
        event: dict[str, Any] = {"type": event_type, "atSec": at_sec}
        text = details.pop("text", None)
        if text is not None:
            text_str = str(text).strip()
            event["chars"] = len(text_str)
            excerpt = text_str if len(text_str) <= 60 else text_str[:60] + "…"
            event["textExcerpt"] = excerpt
        for key, value in details.items():
            if value is None:
                continue
            event[key] = value
        timeline = self.session.interactionMetrics.setdefault("timelineEvents", [])
        timeline.append(event)
        if len(timeline) > 1000:
            del timeline[: len(timeline) - 1000]

    def _update_audio_bucket(self, at_sec: float, rms: int, peak: int, is_silent: bool) -> None:
        bucket_start = int(at_sec)
        buckets = self.session.interactionMetrics.setdefault("audioBuckets", [])
        if buckets and buckets[-1].get("startSec") == bucket_start:
            bucket = buckets[-1]
        else:
            bucket = {
                "startSec": bucket_start,
                "endSec": bucket_start + 1,
                "frames": 0,
                "voicedFrames": 0,
                "silentFrames": 0,
                "rmsAvg": 0.0,
                "rmsMax": 0,
                "peak": 0,
            }
            buckets.append(bucket)
            if len(buckets) > 600:
                del buckets[: len(buckets) - 600]
        bucket["frames"] += 1
        if is_silent:
            bucket["silentFrames"] += 1
        else:
            bucket["voicedFrames"] += 1
        n = bucket["frames"]
        bucket["rmsAvg"] = round(bucket["rmsAvg"] + (rms - bucket["rmsAvg"]) / n, 2)
        bucket["rmsMax"] = max(bucket["rmsMax"], int(rms))
        bucket["peak"] = max(bucket["peak"], int(peak))

    def _drop_pending_realtime_audio(self) -> int:
        kept: list[dict[str, Any] | None] = []
        dropped = 0
        while True:
            try:
                item = self.gemini_send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, dict) and "realtimeInput" in item:
                dropped += 1
                continue
            kept.append(item)
        for item in kept:
            self.gemini_send_queue.put_nowait(item)
        return dropped

    async def _run_gemini(self) -> None:
        key = _api_key()
        if not key:
            self.logger.error("cloudflare_bridge_gemini_key_missing interview_id=%s", self.session.interviewId)
            return

        url = f"{GEMINI_WS_URL}?key={quote(key)}"
        self.logger.info("cloudflare_bridge_gemini_connecting interview_id=%s model=%s", self.session.interviewId, self.provider.status().model)
        self._emit_event({"type": "gemini_connecting", "model": self.provider.status().model})
        try:
            self.gemini_ready = asyncio.Event()
            async with websockets.connect(url, max_size=16 * 1024 * 1024) as gemini_ws:
                self.gemini_state["connected"] = True
                self._trace("gemini_connected")
                self._emit_event({"type": "gemini_connected"})
                setup_payload = self._setup_message()
                await gemini_ws.send(json.dumps(setup_payload))
                self.counters["gemini_setup_sent"] += 1
                self.gemini_state["setupSent"] = True
                self._trace(
                    "gemini_setup_sent",
                    {
                        "responseModalities": ",".join(setup_payload["setup"]["generationConfig"].get("responseModalities", [])),
                        "realtimeInputConfig": setup_payload["setup"].get("realtimeInputConfig", {}),
                    },
                )
                self._emit_event(
                    {
                        "type": "gemini_setup_sent",
                        "responseModalities": ",".join(setup_payload["setup"]["generationConfig"].get("responseModalities", [])),
                        "hasSystemInstruction": bool(setup_payload["setup"].get("systemInstruction")),
                    }
                )
                sender = asyncio.create_task(self._gemini_sender(gemini_ws))
                receiver = asyncio.create_task(self._gemini_receiver(gemini_ws))
                self.tasks.update({sender, receiver})
                done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    if not task.cancelled():
                        task.result()
        except ConnectionClosed as exc:
            self.gemini_state["closed"] = True
            self.gemini_state["closeCode"] = exc.code
            self.gemini_state["closeReason"] = exc.reason
            self._emit_event({"type": "gemini_closed", "code": exc.code, "reason": exc.reason})
            self.logger.info("cloudflare_bridge_gemini_closed interview_id=%s code=%s reason=%s", self.session.interviewId, exc.code, exc.reason)
        except Exception as exc:
            self.gemini_state["lastError"] = str(exc)
            self._emit_event({"type": "gemini_failed", "message": str(exc)})
            self.logger.exception("cloudflare_bridge_gemini_failed interview_id=%s", self.session.interviewId)
        finally:
            self.output_audio_queue.put_nowait(None)

    async def _gemini_sender(self, gemini_ws) -> None:
        while True:
            payload = await self.gemini_send_queue.get()
            if payload is None:
                return
            ready = self.gemini_ready
            if ready is not None:
                try:
                    await asyncio.wait_for(ready.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self.counters["gemini_ready_timeouts"] += 1
                    self.logger.warning("cloudflare_bridge_gemini_ready_timeout interview_id=%s", self.session.interviewId)
                    self._emit_event({"type": "gemini_setup_timeout"})
            self.counters["gemini_sent_messages"] += 1
            realtime_input = payload.get("realtimeInput")
            is_audio_frame = isinstance(realtime_input, dict) and "audio" in realtime_input
            if not is_audio_frame:
                self._trace("gemini_client_message_sent", {"keys": ",".join(payload.keys())})
                self._emit_event({"type": "gemini_client_message_sent", "keys": ",".join(payload.keys())})
            await gemini_ws.send(json.dumps(payload))

    async def _gemini_receiver(self, gemini_ws) -> None:
        try:
            async for raw_message in gemini_ws:
                self.counters["gemini_messages"] += 1
                response = json.loads(raw_message)
                message_type = next((key for key in ("setupComplete", "serverContent", "toolCall", "toolCallCancellation", "usageMetadata", "goAway", "sessionResumptionUpdate") if key in response), "unknown")
                self.gemini_state["lastMessageType"] = message_type
                self.last_gemini_message_at = time.monotonic()
                summary = self._summarize_gemini_response(response, message_type)
                self.last_gemini_server_summary = summary
                if message_type != "sessionResumptionUpdate":
                    self._trace("gemini_message", summary)
                self._maybe_emit_gemini_summary(summary)
                if message_type != "sessionResumptionUpdate" and (self.counters["gemini_messages"] <= 3 or message_type != "serverContent"):
                    self._emit_event({"type": "gemini_message", "messageType": message_type})
                if "setupComplete" in response:
                    self.counters["gemini_setup_complete"] += 1
                    self.gemini_state["setupComplete"] = True
                    self._trace("gemini_setup_complete")
                    ready = self.gemini_ready
                    if ready is not None:
                        ready.set()
                    self._emit_event({"type": "gemini_setup_complete"})
                    self.logger.info("cloudflare_bridge_gemini_ready interview_id=%s", self.session.interviewId)

                server_content = response.get("serverContent", {})

                if "inputTranscription" in server_content:
                    text = _normalize_transcript(server_content["inputTranscription"].get("text", ""))
                    if text:
                        self.counters["input_transcripts"] += 1
                        self.last_input_transcript_at = time.monotonic()
                        self._trace("input_transcript", {"chars": len(text)})
                        self.pending_input_transcripts.append(text)
                        self._append_session_message("user", text)
                        self._record_scoring_segment_realtime_text(text)
                        self._emit_event({"type": "input_transcript", "text": text})
                        self.record_timeline_event("user_transcript", role="user", text=text)

                if "outputTranscription" in server_content:
                    text = _normalize_transcript(server_content["outputTranscription"].get("text", ""))
                    if text:
                        self.counters["output_transcripts"] += 1
                        self.turn_output_transcripts += 1
                        self._trace("output_transcript", {"chars": len(text)})
                        if not self.turn_output_started:
                            self._mark_assistant_response_started("transcript")
                        self._append_session_message("assistant", text)
                        self._emit_event({"type": "output_transcript", "text": text})
                        self.record_timeline_event("assistant_transcript", role="assistant", text=text)

                model_turn = server_content.get("modelTurn", {})
                for part in model_turn.get("parts", []):
                    inline_data = part.get("inlineData")
                    if not inline_data:
                        continue
                    pcm_data = base64.b64decode(inline_data.get("data", ""))
                    sample_rate = _audio_sample_rate(inline_data.get("mimeType"), 24000)
                    processed_audio = _pcm_mono_to_stereo_48k(pcm_data, sample_rate)
                    self.counters["gemini_audio_chunks"] += 1
                    self.turn_output_audio_chunks += 1
                    if not self.turn_audio_started:
                        self.turn_audio_started = True
                        self._trace("assistant_audio_started", {"chunk": self.counters["gemini_audio_chunks"]})
                        self._emit_event({"type": "assistant_audio_started", "chunks": self.counters["gemini_audio_chunks"]})
                    if not self.turn_output_started:
                        self._mark_assistant_response_started("audio")
                    self.counters["gemini_audio_bytes"] += len(pcm_data)
                    if self.turn_output_audio_chunks == 1:
                        self._enqueue_output_silence("preroll")
                    self._enqueue_output_pcm(processed_audio)
                    self._maybe_log_stats("audio_out")

                if server_content.get("generationComplete"):
                    self._trace(
                        "generation_complete",
                        {
                            "releasedInputGate": True,
                            "bufferingUntilTurnComplete": False,
                            "outputQueue": self.output_audio_queue.qsize(),
                            "turnAudioChunks": self.turn_output_audio_chunks,
                        },
                    )
                    self._emit_event(
                        {
                            "type": "assistant_generation_complete",
                            "outputQueue": self.output_audio_queue.qsize(),
                            "turnAudioChunks": self.turn_output_audio_chunks,
                            "bufferingUntilTurnComplete": False,
                        }
                    )

                if server_content.get("turnComplete"):
                    self.counters["turn_complete"] += 1
                    self._trace(
                        "turn_complete",
                        {
                            "turnAudioChunks": self.turn_output_audio_chunks,
                            "outputAudioPackets": self.counters["output_audio_packets"],
                        },
                    )
                    self.last_turn_complete_at = time.monotonic()
                    self.input_transcripts_at_last_turn = self.counters["input_transcripts"]
                    self.sent_messages_at_last_turn = self.counters["gemini_sent_messages"]
                    self.input_audio_frames_at_last_turn = self.counters["input_audio_frames"]
                    self.silence_padding_frames_at_last_turn = self.counters["gemini_silence_padding_frames"]
                    self.audio_stream_end_sent_for_turn = False
                    self.silence_padding_active_for_turn = False
                    self.last_silence_padding_at = 0.0
                    self.suppress_input_audio = False
                    if self._scoring_pending_close_at is not None:
                        self._close_open_scoring_segment(self._scoring_pending_close_reason or "assistant_response_started")
                    if self.turn_output_audio_chunks:
                        self._enqueue_output_silence("tail")
                    self._emit_event(
                        {
                            "type": "assistant_audio_finished",
                            "turnAudioChunks": self.turn_output_audio_chunks,
                            "outputAudioPackets": self.counters["output_audio_packets"],
                            "flushedBufferedInputFrames": 0,
                        }
                    )
                    conclusion_pending_after_turn = self.pending_discussion_conclusion
                    if self.pending_discussion_conclusion and self.skip_current_turn_for_discussion_conclusion:
                        self._trace(
                            "discussion_conclusion_skipped_interrupted_turn",
                            {
                                "turnAudioChunks": self.turn_output_audio_chunks,
                                "turnOutputTranscripts": self.turn_output_transcripts,
                            },
                        )
                        self.skip_current_turn_for_discussion_conclusion = False
                    elif self.pending_discussion_conclusion and self.turn_output_audio_chunks:
                        self._emit_discussion_conclusion_complete()
                        conclusion_pending_after_turn = False
                    self._emit_event({"type": "turn_complete"})
                    self.record_timeline_event("assistant_turn_complete", role="assistant")
                    if self.pending_input_transcripts and self.turn_output_audio_chunks == 0 and self.turn_output_transcripts == 0:
                        fallback_text = "".join(self.pending_input_transcripts).strip()
                        if fallback_text:
                            self.counters["gemini_response_fallbacks"] += 1
                            self._emit_event({"type": "gemini_response_fallback", "chars": len(fallback_text)})
                            self.gemini_send_queue.put_nowait(
                                {
                                    "clientContent": {
                                        "turns": [{"role": "user", "parts": [{"text": fallback_text}]}],
                                        "turnComplete": True,
                                    }
                                }
                            )
                    self.pending_input_transcripts = []
                    self.turn_output_audio_chunks = 0
                    self.turn_output_transcripts = 0
                    self.turn_output_started = False
                    self.turn_audio_started = False
                    self.assistant_output_active = False
                    self.pending_discussion_conclusion = conclusion_pending_after_turn
        except Exception as exc:
            self.counters["gemini_receiver_errors"] += 1
            self.gemini_state["lastError"] = str(exc)
            self._emit_event({"type": "gemini_receiver_failed", "message": str(exc)})
            raise

    async def _input_idle_watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.5)
                self._maybe_send_input_idle_silence_padding()
                await self._maybe_refresh_input_adapter()
        except asyncio.CancelledError:
            return

    def _setup_message(self) -> dict[str, Any]:
        generation_config: dict[str, Any] = {"responseModalities": ["AUDIO"]}
        speech_config: dict[str, Any] = {}
        voice_name = self.provider.voice_name(self.session)
        if voice_name:
            speech_config["voiceConfig"] = {"prebuiltVoiceConfig": {"voiceName": voice_name}}
        language_code = _input_language_code()
        if language_code:
            speech_config["languageCode"] = language_code
        if speech_config:
            generation_config["speechConfig"] = speech_config
        model_id = self.provider.status().model
        self.logger.info(
            "cloudflare_bridge_gemini_setup interview_id=%s model=%s voice=%s language=%s",
            self.session.interviewId,
            model_id,
            voice_name or "(none)",
            language_code or "(none)",
        )
        self._emit_event(
            {
                "type": "gemini_setup_config",
                "model": model_id,
                "voice": voice_name or "",
                "language": language_code or "",
            }
        )
        return {
            "setup": {
                "model": f"models/{model_id}",
                "generationConfig": generation_config,
                "systemInstruction": {"parts": [{"text": self.provider.system_instruction(self.session)}]},
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                "realtimeInputConfig": _realtime_input_config(),
            }
        }

    def _record_interaction_metrics(self, metrics: dict[str, Any]) -> None:
        event = metrics.get("event")
        if event == "ai_interruption":
            self.session.interactionMetrics["aiInterruptionCount"] = self.session.interactionMetrics.get("aiInterruptionCount", 0) + 1
        elif event == "post_interruption_continued":
            self.session.interactionMetrics["postInterruptionContinuedCount"] = (
                self.session.interactionMetrics.get("postInterruptionContinuedCount", 0) + 1
            )
        elif event == "post_interruption_stalled":
            self.session.interactionMetrics["postInterruptionStalledCount"] = (
                self.session.interactionMetrics.get("postInterruptionStalledCount", 0) + 1
            )

    def _append_session_message(self, role: str, text: str) -> None:
        trimmed = text.strip()
        if not trimmed:
            return
        if self.session.messages and self.session.messages[-1].get("role") == role:
            self.session.messages[-1]["content"] = f"{self.session.messages[-1].get('content', '')}{trimmed}"
            return
        self.session.messages.append({"role": role, "content": trimmed})

    def store_audio_summary(self) -> None:
        frames = int(self.audio_stats["frames"])
        if frames <= 0:
            return
        snapshot = self._audio_stats_snapshot()
        observed_duration_sec = round(frames * 0.02, 2)
        user_text = "".join(
            str(message.get("content", ""))
            for message in self.session.messages
            if message.get("role") == "user"
        )
        fillers = [marker for marker in ("えー", "えっと", "あの", "その", "まあ") if marker in user_text]
        audio_payload: dict[str, Any] = {
            "available": True,
            "observedDurationSec": observed_duration_sec,
            "rmsAverage": snapshot["rmsAvg"],
            "fillerCount": len(fillers),
            "fillers": fillers,
        }
        existing = self.session.interactionMetrics.get("audio") or {}
        existing.update(audio_payload)
        # Drop session-wide silence fields so detailedAnalysis no longer uses them.
        existing.pop("silenceSec", None)
        existing.pop("durationSec", None)
        existing.pop("silenceRatio", None)
        self.session.interactionMetrics["audio"] = existing
        self._trace("audio_summary_stored", audio_payload.copy())

    def _enqueue_gemini_audio(self, pcm_data: bytes) -> None:
        self.gemini_send_queue.put_nowait(
            {
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(pcm_data).decode("ascii"),
                        "mimeType": "audio/pcm;rate=48000",
                    }
                }
            }
        )

    def _enqueue_output_pcm(self, pcm_data: bytes, counter_name: str | None = None) -> int:
        packets = 0
        for offset in range(0, len(pcm_data), SFU_BUFFER_CHUNK_SIZE):
            chunk = pcm_data[offset : offset + SFU_BUFFER_CHUNK_SIZE]
            if not chunk:
                continue
            self.output_audio_queue.put_nowait(_encode_sfu_packet(chunk))
            packets += 1
        if counter_name and packets:
            self.counters[counter_name] += packets
        return packets

    def _enqueue_output_silence(self, kind: str) -> None:
        if kind == "preroll":
            seconds = float(os.getenv("GEMINI_OUTPUT_PREROLL_SECONDS", "0.25"))
            counter_name = "output_preroll_packets"
        elif kind == "tail":
            seconds = float(os.getenv("GEMINI_OUTPUT_TAIL_SECONDS", "0.7"))
            counter_name = "output_tail_packets"
        else:
            return
        frames = max(0, round(INPUT_SAMPLE_RATE * seconds))
        if frames <= 0:
            return
        self._enqueue_output_pcm(b"\x00" * frames * 4, counter_name)

    def _enqueue_gemini_silence(self, frame_count: int) -> None:
        frame_count = max(0, frame_count)
        if frame_count <= 0:
            return
        silence = b"\x00" * int(INPUT_SAMPLE_RATE * 0.02 * INPUT_SAMPLE_WIDTH)
        for _ in range(frame_count):
            self._enqueue_gemini_audio(silence)
            self.counters["gemini_silence_padding_frames"] += 1

    def _maybe_log_stats(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_stats_at < 5:
            return
        self._last_stats_at = now
        audio_snapshot = self._audio_stats_snapshot()
        seconds_since_last_turn = round(now - self.last_turn_complete_at, 2) if self.last_turn_complete_at else None
        seconds_since_last_gemini_message = round(now - self.last_gemini_message_at, 2) if self.last_gemini_message_at else None
        self.logger.info("cloudflare_bridge_stats interview_id=%s reason=%s counters=%s", self.session.interviewId, reason, self.counters.copy())
        self._emit_event(
            {
                "type": "bridge_stats",
                "reason": reason,
                "inputAudioFrames": self.counters["input_audio_frames"],
                "geminiSentMessages": self.counters["gemini_sent_messages"],
                "geminiAudioChunks": self.counters["gemini_audio_chunks"],
                "outputAudioPackets": self.counters["output_audio_packets"],
                "inputTranscripts": self.counters["input_transcripts"],
                "outputTranscripts": self.counters["output_transcripts"],
                "silencePaddingFrames": self.counters["gemini_silence_padding_frames"],
                "outputPrerollPackets": self.counters["output_preroll_packets"],
                "outputTailPackets": self.counters["output_tail_packets"],
                "playbackInterrupts": self.counters["playback_interrupts"],
                "droppedOutputAudioPackets": self.counters["dropped_output_audio_packets"],
                "suppressedInputAudioFrames": self.counters["suppressed_input_audio_frames"],
                "bufferedInputAudioFrames": self.counters["buffered_input_audio_frames"],
                "flushedBufferedInputAudioFrames": self.counters["flushed_buffered_input_audio_frames"],
                "rmsAvg": audio_snapshot["rmsAvg"],
                "rmsMax": audio_snapshot["rmsMax"],
                "peak": audio_snapshot["peak"],
                "zeroRatio": audio_snapshot["zeroRatio"],
                "bytesPerFrame": audio_snapshot["bytesPerFrame"],
                "geminiQueue": self.gemini_send_queue.qsize(),
                "outputQueue": self.output_audio_queue.qsize(),
                "secondsSinceLastTurn": seconds_since_last_turn,
                "secondsSinceLastGeminiMessage": seconds_since_last_gemini_message,
                "lastGeminiMessageType": self.gemini_state["lastMessageType"],
                "lastGeminiServerSummary": self.last_gemini_server_summary,
            }
        )
        self._maybe_emit_input_stall(now)

    def _record_input_audio_stats(self, pcm_data: bytes) -> None:
        now = time.monotonic()
        self.last_input_audio_at = now
        stats = _pcm_stats(pcm_data)
        silence_rms_threshold = int(os.getenv("VOICE_BRIDGE_SILENCE_RMS_THRESHOLD", "80"))
        is_silent = stats["rms"] <= silence_rms_threshold
        self.audio_stats["frames"] += 1
        self.audio_stats["samples"] += stats["samples"]
        self.audio_stats["zeroSamples"] += stats["zeroSamples"]
        if is_silent:
            self.audio_stats["silentFrames"] += 1
        self.audio_stats["rmsSum"] += stats["rms"]
        self.audio_stats["rmsMax"] = max(self.audio_stats["rmsMax"], stats["rms"])
        self.audio_stats["peak"] = max(self.audio_stats["peak"], stats["peak"])
        self.audio_stats["lastBytesPerFrame"] = len(pcm_data)
        self._update_audio_bucket(now - self.trace_started_at, stats["rms"], stats["peak"], is_silent)

    def _maybe_interrupt_playback_for_user_audio(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        if self.output_audio_queue.qsize() <= 0:
            return
        now = time.monotonic()
        cooldown = float(os.getenv("PLAYBACK_INTERRUPT_COOLDOWN_SECONDS", "1.5"))
        if now - self.last_playback_interrupt_at < cooldown:
            return
        stats = _pcm_stats(pcm_data)
        threshold = int(os.getenv("PLAYBACK_INTERRUPT_RMS_THRESHOLD", "250"))
        if stats["rms"] < threshold:
            return
        dropped_packets = self._clear_output_audio_queue()
        if dropped_packets <= 0:
            return
        self.last_playback_interrupt_at = now
        self.counters["playback_interrupts"] += 1
        self.assistant_output_active = False
        event = {
            "type": "interrupted",
            "source": "playback_user_audio",
            "droppedOutputPackets": dropped_packets,
            "rms": stats["rms"],
            "peak": stats["peak"],
            "outputQueue": self.output_audio_queue.qsize(),
        }
        self._trace("playback_interrupted_by_user_audio", event.copy())
        self._emit_event(event)

    def _clear_output_audio_queue(self) -> int:
        dropped = 0
        kept: list[bytes | None] = []
        while True:
            try:
                item = self.output_audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                kept.append(item)
                continue
            dropped += 1
        for item in kept:
            self.output_audio_queue.put_nowait(item)
        self.counters["dropped_output_audio_packets"] += dropped
        return dropped

    def _accumulate_scoring_audio(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        # Skip while assistant is actively generating output to avoid overlap-heavy chunks.
        if self.assistant_output_active and self._scoring_segment_start_at is None:
            return
        stats = _pcm_stats(pcm_data)
        is_voiced = stats["rms"] > self._scoring_silence_rms_threshold
        now = time.monotonic()
        if self._scoring_segment_start_at is None:
            if not is_voiced:
                return
            self._scoring_segment_start_at = now
            self._scoring_segment_last_voiced_at = now
            self._scoring_segment_voiced_frames = 1
            self._scoring_segment_buffer = bytearray(pcm_data)
            return
        self._scoring_segment_buffer.extend(pcm_data)
        if is_voiced:
            self._scoring_segment_last_voiced_at = now
            self._scoring_segment_voiced_frames += 1
        duration = now - self._scoring_segment_start_at
        if self._scoring_pending_close_at is not None and now >= self._scoring_pending_close_at:
            self._close_open_scoring_segment(self._scoring_pending_close_reason or "assistant_response_started")
            return
        if duration >= SCORING_SEGMENT_MAX_SECONDS:
            self._close_open_scoring_segment("max_duration")
            return
        if (
            self._scoring_segment_last_voiced_at is not None
            and self._scoring_segment_voiced_frames > 0
            and (now - self._scoring_segment_last_voiced_at) >= SCORING_SEGMENT_SILENT_CLOSE_SECONDS
        ):
            self._close_open_scoring_segment("silence")

    def _close_open_scoring_segment(self, reason: str) -> None:
        if self._scoring_segment_start_at is None:
            return
        if self._scoring_segment_voiced_frames <= 0 or not self._scoring_segment_buffer:
            self._reset_scoring_segment_state()
            return
        pcm_bytes = bytes(self._scoring_segment_buffer)
        start_at = round(self._scoring_segment_start_at - self.trace_started_at, 3)
        end_at = round(time.monotonic() - self.trace_started_at, 3)
        realtime_text = "".join(self._scoring_segment_realtime_chunks).strip()
        segment_id = self._scoring_next_segment_id
        self._scoring_next_segment_id += 1
        segment: dict[str, Any] = {
            "id": segment_id,
            "startAtSec": start_at,
            "endAtSec": end_at,
            "pcm": pcm_bytes,
            "realtimeText": realtime_text,
            "text": "",
            "status": "pending",
            "closedReason": reason,
        }
        self._scoring_segments.append(segment)
        if len(self._scoring_segments) > SCORING_MAX_SEGMENTS:
            dropped = self._scoring_segments[: len(self._scoring_segments) - SCORING_MAX_SEGMENTS]
            self._scoring_segments = self._scoring_segments[-SCORING_MAX_SEGMENTS:]
            for stale in dropped:
                stale.pop("pcm", None)
                if stale.get("status") in {"pending", "started"}:
                    stale["status"] = "dropped"
        self._reset_scoring_segment_state()
        self._emit_event(
            {
                "type": "final_transcript.segment_created",
                "segmentId": segment_id,
                "startAtSec": start_at,
                "endAtSec": end_at,
                "reason": reason,
                "bytes": len(pcm_bytes),
            }
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(self._transcribe_segment(segment))
            self._scoring_segment_tasks.add(task)
            task.add_done_callback(self._scoring_segment_tasks.discard)

    def _reset_scoring_segment_state(self) -> None:
        self._scoring_segment_buffer = bytearray()
        self._scoring_segment_start_at = None
        self._scoring_segment_last_voiced_at = None
        self._scoring_segment_voiced_frames = 0
        self._scoring_segment_realtime_chunks = []
        self._scoring_pending_close_at = None
        self._scoring_pending_close_reason = None

    def _mark_assistant_response_started(self, source: str) -> None:
        self.turn_output_started = True
        self.assistant_output_active = True
        self._schedule_scoring_segment_close("assistant_response_started")
        self._emit_event({"type": "assistant_response_started", "source": source})
        self.record_timeline_event("assistant_response_started", role="assistant", source=source)

    def _schedule_scoring_segment_close(self, reason: str) -> None:
        if self._scoring_segment_start_at is None:
            return
        now = time.monotonic()
        self._scoring_pending_close_at = now + max(0.0, SCORING_ASSISTANT_CLOSE_GRACE_SECONDS)
        self._scoring_pending_close_reason = reason
        self._emit_event(
            {
                "type": "final_transcript.segment_close_scheduled",
                "reason": reason,
                "graceSec": SCORING_ASSISTANT_CLOSE_GRACE_SECONDS,
            }
        )

    def flush_pending_scoring_segment(self) -> None:
        self._close_open_scoring_segment("flush")

    def _record_scoring_segment_realtime_text(self, text: str) -> None:
        if not text or self._scoring_segment_start_at is None:
            return
        self._scoring_segment_realtime_chunks.append(text)

    async def _transcribe_segment(self, segment: dict[str, Any]) -> None:
        segment["status"] = "started"
        segment_id = segment.get("id")
        pcm = segment.pop("pcm", b"")
        self._emit_event({"type": "final_transcript.started", "segmentId": segment_id, "bytes": len(pcm)})
        try:
            text = await asyncio.to_thread(self._transcribe_pcm_sync, pcm)
            segment["text"] = text or ""
            segment["status"] = "completed" if text else "empty"
            self._emit_event(
                {
                    "type": "final_transcript.completed",
                    "segmentId": segment_id,
                    "chars": len(segment["text"]),
                }
            )
        except Exception as exc:  # noqa: BLE001
            segment["status"] = "failed"
            message = str(exc)[:240]
            segment["error"] = message
            self._record_final_transcript_error(int(segment_id) if segment_id is not None else -1, message)
            self._emit_event(
                {
                    "type": "final_transcript.failed",
                    "segmentId": segment_id,
                    "message": message,
                }
            )

    def _transcribe_pcm_sync(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        key = _api_key()
        if not key:
            raise RuntimeError("api_key_missing")
        model = os.getenv("GEMINI_TRANSCRIPTION_MODEL", "").strip() or SCORING_DEFAULT_TRANSCRIPTION_MODEL
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(SCORING_SEGMENT_SAMPLE_WIDTH)
            wav_file.setframerate(SCORING_SEGMENT_SAMPLE_RATE)
            wav_file.writeframes(pcm)
        wav_bytes = wav_buffer.getvalue()
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": SCORING_PROMPT_TEXT},
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": base64.b64encode(wav_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 2048,
            },
        }
        request = urllib.request.Request(
            GEMINI_REST_URL.format(model=quote(model, safe="")),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=SCORING_TRANSCRIPTION_TIMEOUT_SEC) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"http_{exc.code}:{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"network:{exc.reason}") from exc
        return (_extract_generate_content_text(body) or "").strip()

    def _record_final_transcript_error(self, segment_id: int, message: str) -> None:
        self._final_transcript_errors.append({"segmentId": segment_id, "message": message[:240]})
        if len(self._final_transcript_errors) > 50:
            self._final_transcript_errors = self._final_transcript_errors[-50:]

    async def wait_for_pending_transcriptions(self, timeout: float) -> int:
        pending = [t for t in self._scoring_segment_tasks if not t.done()]
        if not pending:
            return 0
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
            return 0
        except asyncio.TimeoutError:
            still_pending = sum(1 for t in pending if not t.done())
            self._emit_event(
                {
                    "type": "final_transcript.finish_wait_timeout",
                    "pending": still_pending,
                    "timeoutSec": timeout,
                }
            )
            return still_pending

    def store_final_transcript(self) -> None:
        segments_summary: list[dict[str, Any]] = []
        pending_count = 0
        error_count = 0
        completed_count = 0
        for segment in self._scoring_segments:
            status = segment.get("status", "pending")
            text = (segment.get("text") or "").strip()
            entry = {
                "id": segment.get("id"),
                "startAtSec": segment.get("startAtSec"),
                "endAtSec": segment.get("endAtSec"),
                "text": text,
                "status": status,
                "closedReason": segment.get("closedReason"),
            }
            if segment.get("error"):
                entry["error"] = segment["error"]
            segments_summary.append(entry)
            if status == "completed" and text:
                completed_count += 1
            elif status == "failed":
                error_count += 1
            elif status in {"pending", "started"}:
                pending_count += 1
        user_turns = self._merged_scoring_user_turns(segments_summary)
        if not self._scoring_segments:
            source = "realtime_fallback"
        elif pending_count == 0 and error_count == 0 and completed_count > 0:
            source = "post_audio_transcription"
        elif completed_count > 0:
            source = "mixed"
        else:
            source = "realtime_fallback"
        user_text = "\n".join(user_turns)
        final_transcript = {
            "available": bool(user_turns),
            "source": source,
            "userText": user_text,
            "userTurns": user_turns,
            "segments": segments_summary,
            "segmentCount": len(segments_summary),
            "completedCount": completed_count,
            "pendingCount": pending_count,
            "errorCount": error_count,
        }
        if self._final_transcript_errors:
            final_transcript["errors"] = list(self._final_transcript_errors)
        self.session.interactionMetrics["finalTranscript"] = final_transcript
        self._trace(
            "final_transcript_stored",
            {
                "source": source,
                "segments": len(segments_summary),
                "pending": pending_count,
                "errors": error_count,
                "chars": len(user_text),
            },
        )

    def _merged_scoring_user_turns(self, segments: list[dict[str, Any]]) -> list[str]:
        turns: list[str] = []
        current: list[str] = []
        previous: dict[str, Any] | None = None
        for segment in segments:
            if segment.get("status") != "completed":
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            gap: float | None = None
            if previous and previous.get("endAtSec") is not None and segment.get("startAtSec") is not None:
                gap = float(segment["startAtSec"]) - float(previous["endAtSec"])
            should_merge = False
            if previous:
                previous_reason = str(previous.get("closedReason") or "")
                current_reason = str(segment.get("closedReason") or "")
                should_merge = previous_reason == "max_duration"
                should_merge = should_merge or current_reason == "max_duration"
                should_merge = should_merge or (gap is not None and gap <= 0.8)
            if current and not should_merge:
                turns.append("".join(current).strip())
                current = []
            current.append(text)
            previous = segment
        if current:
            turns.append("".join(current).strip())
        return [turn for turn in turns if turn]

    def _remember_input_audio(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        self.input_audio_ring.append(pcm_data)
        self.input_audio_ring_bytes += len(pcm_data)
        while self.input_audio_ring_bytes > INPUT_RING_MAX_BYTES and self.input_audio_ring:
            removed = self.input_audio_ring.popleft()
            self.input_audio_ring_bytes -= len(removed)

    def _buffer_input_after_generation(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        max_buffer_bytes = int(os.getenv("VOICE_BRIDGE_POST_GENERATION_BUFFER_SECONDS", "20")) * INPUT_SAMPLE_RATE * INPUT_SAMPLE_WIDTH
        self.buffered_input_after_generation.append(pcm_data)
        self.buffered_input_after_generation_bytes += len(pcm_data)
        self.buffered_input_after_generation_frames += 1
        self.counters["buffered_input_audio_frames"] += 1
        while self.buffered_input_after_generation_bytes > max_buffer_bytes and self.buffered_input_after_generation:
            removed = self.buffered_input_after_generation.popleft()
            self.buffered_input_after_generation_bytes -= len(removed)
            self.buffered_input_after_generation_frames = max(0, self.buffered_input_after_generation_frames - 1)

    def _flush_buffered_input_after_generation(self) -> int:
        flushed = 0
        while self.buffered_input_after_generation:
            pcm_data = self.buffered_input_after_generation.popleft()
            self.buffered_input_after_generation_bytes -= len(pcm_data)
            self._enqueue_gemini_audio(pcm_data)
            flushed += 1
        self.buffered_input_after_generation_frames = 0
        self.counters["flushed_buffered_input_audio_frames"] += flushed
        if flushed:
            self.input_audio_frames_at_last_turn = max(0, self.counters["input_audio_frames"] - flushed)
            self.sent_messages_at_last_turn = max(0, self.counters["gemini_sent_messages"] - flushed)
            self._trace(
                "buffered_input_flushed",
                {
                    "frames": flushed,
                    "geminiQueue": self.gemini_send_queue.qsize(),
                },
            )
            self._emit_event(
                {
                    "type": "buffered_input_flushed",
                    "frames": flushed,
                    "geminiQueue": self.gemini_send_queue.qsize(),
                }
            )
        return flushed

    def _capture_input_wav(self, reason: str, details: dict[str, Any]) -> dict[str, Any] | None:
        if not self.input_audio_ring:
            return None
        pcm = b"".join(self.input_audio_ring)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(INPUT_SAMPLE_WIDTH)
            wav.setframerate(INPUT_SAMPLE_RATE)
            wav.writeframes(pcm)
        capture_id = f"{int(time.time())}-{len(self.diagnostic_capture_order) + 1}"
        wav_bytes = output.getvalue()
        capture = {
            "id": capture_id,
            "createdAt": int(time.time()),
            "reason": reason,
            "seconds": round(len(pcm) / (INPUT_SAMPLE_RATE * INPUT_SAMPLE_WIDTH), 2),
            "details": details,
            "wav": wav_bytes,
        }
        self.diagnostic_captures[capture_id] = capture
        self.diagnostic_capture_order.append(capture_id)
        while len(self.diagnostic_capture_order) > MAX_DIAGNOSTIC_CAPTURES:
            old_id = self.diagnostic_capture_order.pop(0)
            self.diagnostic_captures.pop(old_id, None)
        self._trace(
            "diagnostic_wav_captured",
            {
                "captureId": capture_id,
                "reason": reason,
                "seconds": capture["seconds"],
                "bytes": len(wav_bytes),
            },
        )
        return {
            "id": capture_id,
            "seconds": capture["seconds"],
            "bytes": len(wav_bytes),
            "endpoint": f"/api/interviews/{self.session.interviewId}/voice/diagnostics/{capture_id}.wav",
        }

    def _audio_stats_snapshot(self) -> dict[str, Any]:
        frames = int(self.audio_stats["frames"])
        samples = int(self.audio_stats["samples"])
        return {
            "rmsAvg": round(self.audio_stats["rmsSum"] / frames) if frames else 0,
            "rmsMax": int(self.audio_stats["rmsMax"]),
            "peak": int(self.audio_stats["peak"]),
            "zeroRatio": round(self.audio_stats["zeroSamples"] / samples, 4) if samples else 0,
            "bytesPerFrame": int(self.audio_stats["lastBytesPerFrame"]),
        }

    def _summarize_gemini_response(self, response: dict[str, Any], message_type: str) -> dict[str, Any]:
        server_content = response.get("serverContent")
        if not isinstance(server_content, dict):
            return {
                "messageType": message_type,
                "hasUsageMetadata": "usageMetadata" in response,
                "hasGoAway": "goAway" in response,
                "hasSessionResumptionUpdate": "sessionResumptionUpdate" in response,
            }
        model_turn = server_content.get("modelTurn")
        parts = model_turn.get("parts", []) if isinstance(model_turn, dict) else []
        audio_parts = 0
        text_parts = 0
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "inlineData" in part:
                audio_parts += 1
            if "text" in part:
                text_parts += 1
        return {
            "messageType": message_type,
            "hasInputTranscription": "inputTranscription" in server_content,
            "hasOutputTranscription": "outputTranscription" in server_content,
            "modelParts": len(parts),
            "audioParts": audio_parts,
            "textParts": text_parts,
            "turnComplete": bool(server_content.get("turnComplete")),
            "interrupted": bool(server_content.get("interrupted")),
            "generationComplete": bool(server_content.get("generationComplete")),
            "hasUsageMetadata": "usageMetadata" in response,
            "hasGoAway": "goAway" in response,
            "hasSessionResumptionUpdate": "sessionResumptionUpdate" in response,
        }

    def _maybe_emit_gemini_summary(self, summary: dict[str, Any]) -> None:
        message_type = summary.get("messageType")
        if message_type == "sessionResumptionUpdate":
            return
        should_emit = message_type != "serverContent"
        should_emit = should_emit or bool(summary.get("hasInputTranscription"))
        should_emit = should_emit or bool(summary.get("turnComplete"))
        should_emit = should_emit or bool(summary.get("interrupted"))
        should_emit = should_emit or bool(summary.get("generationComplete"))
        should_emit = should_emit or bool(summary.get("hasGoAway"))
        if should_emit:
            self._emit_event({"type": "gemini_server_summary", **summary})

    def _maybe_emit_input_stall(self, now: float | None = None) -> None:
        now = now or time.monotonic()
        turn_state = self._input_turn_state(now)
        if not turn_state:
            return
        if turn_state["secondsSinceLastTurn"] < 8:
            return
        if turn_state["framesSinceTurn"] < 50 or turn_state["sentSinceTurn"] < 50:
            return
        if now - self.last_stall_event_at < 8:
            return
        self.last_stall_event_at = now
        sent_audio_stream_end = False
        audio_snapshot = self._audio_stats_snapshot()
        capture = self._capture_input_wav(
            "gemini_input_stalled",
            {
                "framesSinceTurn": turn_state["framesSinceTurn"],
                "sentSinceTurn": turn_state["sentSinceTurn"],
                "secondsSinceLastTurn": turn_state["secondsSinceLastTurn"],
                **audio_snapshot,
            },
        )
        self._trace(
            "gemini_input_stalled",
            {
                "framesSinceTurn": turn_state["framesSinceTurn"],
                "sentSinceTurn": turn_state["sentSinceTurn"],
                "secondsSinceLastTurn": turn_state["secondsSinceLastTurn"],
                "audioStreamEndSent": sent_audio_stream_end,
                "captureId": capture["id"] if capture else None,
                **audio_snapshot,
            },
        )
        self._emit_event(
            {
                "type": "gemini_input_stalled",
                "framesSinceTurn": turn_state["framesSinceTurn"],
                "sentSinceTurn": turn_state["sentSinceTurn"],
                "secondsSinceLastTurn": turn_state["secondsSinceLastTurn"],
                "secondsSinceLastGeminiMessage": round(now - self.last_gemini_message_at, 2) if self.last_gemini_message_at else None,
                "lastGeminiMessageType": self.gemini_state["lastMessageType"],
                "lastGeminiServerSummary": self.last_gemini_server_summary,
                "audioStreamEndSent": sent_audio_stream_end,
                "captureId": capture["id"] if capture else None,
                "captureEndpoint": capture["endpoint"] if capture else None,
                "captureSeconds": capture["seconds"] if capture else None,
                **audio_snapshot,
            }
        )

    def _maybe_send_input_idle_silence_padding(self, now: float | None = None) -> None:
        now = now or time.monotonic()
        enabled = os.getenv("GEMINI_INPUT_IDLE_SILENCE_PADDING_ENABLED", "1").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return
        turn_state = self._input_turn_state(now)
        if not turn_state:
            return
        if self.audio_stream_end_sent_for_turn:
            return
        if turn_state["framesSinceTurn"] < 50 or turn_state["sentSinceTurn"] < 50:
            return
        if turn_state["secondsSinceLastInputAudio"] is None:
            return
        idle_seconds = float(os.getenv("GEMINI_INPUT_IDLE_SILENCE_PADDING_SECONDS", "2.8"))
        if turn_state["secondsSinceLastInputAudio"] < idle_seconds:
            return
        max_padding_seconds = float(os.getenv("GEMINI_INPUT_IDLE_MAX_SILENCE_PADDING_SECONDS", "4.0"))
        padded_frames = self.counters["gemini_silence_padding_frames"] - self.silence_padding_frames_at_last_turn
        padded_seconds = padded_frames * 0.02
        if self.silence_padding_active_for_turn and padded_seconds >= max_padding_seconds:
            return
        if now - self.last_silence_padding_at < 0.18:
            return
        frame_count = int(os.getenv("GEMINI_INPUT_IDLE_SILENCE_PADDING_FRAMES", "10"))
        self._enqueue_gemini_silence(frame_count)
        self.silence_padding_active_for_turn = True
        self.last_silence_padding_at = now
        event = {
            "type": "gemini_silence_padding_sent",
            "reason": "input_idle",
            "frames": frame_count,
            "totalFrames": self.counters["gemini_silence_padding_frames"],
            "geminiQueue": self.gemini_send_queue.qsize(),
            **turn_state,
        }
        self._trace("gemini_silence_padding_sent", event.copy())
        self._emit_event(event)

    async def _maybe_refresh_input_adapter(self, now: float | None = None) -> None:
        if self.input_adapter_refresh is None:
            return
        now = now or time.monotonic()
        if not self.last_input_audio_at:
            return
        idle_seconds = float(os.getenv("CLOUDFLARE_INPUT_ADAPTER_IDLE_RESTART_SECONDS", "2.5"))
        if now - self.last_input_audio_at < idle_seconds:
            return
        refresh_interval = float(os.getenv("CLOUDFLARE_INPUT_ADAPTER_RESTART_INTERVAL_SECONDS", "10.0"))
        if now - self.last_input_adapter_refresh_at < refresh_interval:
            return
        current_frames = self.counters["input_audio_frames"]
        if current_frames == self.input_frames_at_last_adapter_refresh:
            return
        self.last_input_adapter_refresh_at = now
        self.input_frames_at_last_adapter_refresh = current_frames
        self.counters["cloudflare_input_adapter_refreshes"] += 1
        self._emit_event(
            {
                "type": "cloudflare_input_adapter_refresh_requested",
                "inputAudioFrames": current_frames,
                "secondsSinceLastInputAudio": round(now - self.last_input_audio_at, 2),
            }
        )
        try:
            result = await asyncio.to_thread(self.input_adapter_refresh)
            self._trace(
                "cloudflare_input_adapter_refreshed",
                {
                    "inputAudioFrames": current_frames,
                    "result": result,
                },
            )
            self._emit_event(
                {
                    "type": "cloudflare_input_adapter_refreshed",
                    "inputAudioFrames": current_frames,
                    "result": result,
                }
            )
        except Exception as exc:
            self.input_frames_at_last_adapter_refresh = -1
            self._trace("cloudflare_input_adapter_refresh_failed", {"message": str(exc)})
            self._emit_event({"type": "cloudflare_input_adapter_refresh_failed", "message": str(exc)})

    def _input_turn_state(self, now: float) -> dict[str, Any] | None:
        if not self.last_turn_complete_at:
            return None
        if self.counters["input_transcripts"] > self.input_transcripts_at_last_turn:
            return None
        frames_since_turn = self.counters["input_audio_frames"] - self.input_audio_frames_at_last_turn
        sent_since_turn = self.counters["gemini_sent_messages"] - self.sent_messages_at_last_turn
        if frames_since_turn <= 0 or sent_since_turn <= 0:
            return None
        return {
            "framesSinceTurn": frames_since_turn,
            "sentSinceTurn": sent_since_turn,
            "secondsSinceLastTurn": round(now - self.last_turn_complete_at, 2),
            "secondsSinceLastInputAudio": round(now - self.last_input_audio_at, 2) if self.last_input_audio_at else None,
            "secondsSinceLastGeminiMessage": round(now - self.last_gemini_message_at, 2) if self.last_gemini_message_at else None,
            "lastGeminiMessageType": self.gemini_state["lastMessageType"],
        }

    def _send_gemini_audio_stream_end(self, reason: str, details: dict[str, Any] | None = None) -> bool:
        if self.audio_stream_end_sent_for_turn:
            return False
        self.audio_stream_end_sent_for_turn = True
        self.counters["gemini_audio_stream_end_sent"] += 1
        self.gemini_send_queue.put_nowait({"realtimeInput": {"audioStreamEnd": True}})
        event = {
            "type": "gemini_audio_stream_end_sent",
            "reason": reason,
            "geminiQueue": self.gemini_send_queue.qsize(),
        }
        if details:
            event.update(details)
        self._trace("gemini_audio_stream_end_sent", event.copy())
        self._emit_event(event)
        return True


def _pcm_mono_to_stereo_48k(pcm_data: bytes, sample_rate: int) -> bytes:
    if not pcm_data:
        return b""
    if len(pcm_data) % 2:
        pcm_data = pcm_data[:-1]
    samples = [int.from_bytes(pcm_data[i : i + 2], "little", signed=True) for i in range(0, len(pcm_data), 2)]
    if sample_rate == 24000:
        resampled: list[int] = []
        for index, sample in enumerate(samples[:-1]):
            resampled.append(sample)
            resampled.append(round((sample + samples[index + 1]) / 2))
        if samples:
            resampled.extend([samples[-1], samples[-1]])
        samples = resampled
    elif sample_rate != 48000:
        # The SFU PCM adapter expects 48 kHz stereo. Preserve non-24k/48k input
        # duration approximately with nearest-neighbor resampling.
        target_len = max(1, round(len(samples) * 48000 / sample_rate))
        samples = [samples[min(len(samples) - 1, round(i * sample_rate / 48000))] for i in range(target_len)]

    stereo = bytearray(len(samples) * 4)
    offset = 0
    for sample in samples:
        encoded = int(sample).to_bytes(2, "little", signed=True)
        stereo[offset : offset + 2] = encoded
        stereo[offset + 2 : offset + 4] = encoded
        offset += 4
    return bytes(stereo)


def _pcm_stereo_48k_to_mono_48k(pcm_data: bytes) -> bytes:
    if not pcm_data:
        return b""
    if len(pcm_data) % 4:
        pcm_data = pcm_data[: len(pcm_data) - (len(pcm_data) % 4)]
    mono = bytearray(len(pcm_data) // 2)
    out = 0
    frame_count = len(pcm_data) // 4
    for frame_index in range(frame_count):
        offset = frame_index * 4
        left = int.from_bytes(pcm_data[offset : offset + 2], "little", signed=True)
        right = int.from_bytes(pcm_data[offset + 2 : offset + 4], "little", signed=True)
        sample = round((left + right) / 2)
        mono[out : out + 2] = int(sample).to_bytes(2, "little", signed=True)
        out += 2
    return bytes(mono[:out])


def _pcm_stats(pcm_data: bytes) -> dict[str, int]:
    if not pcm_data:
        return {"rms": 0, "peak": 0, "zeroSamples": 0, "samples": 0}
    if len(pcm_data) % 2:
        pcm_data = pcm_data[:-1]
    if not pcm_data:
        return {"rms": 0, "peak": 0, "zeroSamples": 0, "samples": 0}
    total = 0
    count = 0
    peak = 0
    zero_samples = 0
    for offset in range(0, len(pcm_data), 2):
        sample = int.from_bytes(pcm_data[offset : offset + 2], "little", signed=True)
        absolute = abs(sample)
        total += sample * sample
        peak = max(peak, absolute)
        if sample == 0:
            zero_samples += 1
        count += 1
    if count == 0:
        return {"rms": 0, "peak": 0, "zeroSamples": 0, "samples": 0}
    return {"rms": round(math.sqrt(total / count)), "peak": peak, "zeroSamples": zero_samples, "samples": count}


def _encode_sfu_packet(payload: bytes) -> bytes:
    if not payload:
        return b""
    return bytes([0x2A]) + _encode_varint(len(payload)) + payload


def _decode_sfu_packet(packet: bytes) -> bytes | None:
    index = 0
    while index < len(packet):
        tag = packet[index]
        index += 1
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            _, index = _decode_varint(packet, index)
            continue
        if wire_type != 2:
            return None
        length, index = _decode_varint(packet, index)
        if index + length > len(packet):
            return None
        value = packet[index : index + length]
        index += length
        if field_number == 5:
            return value
    return None


def _encode_varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _decode_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 35:
            break
    raise ValueError("Invalid varint")
