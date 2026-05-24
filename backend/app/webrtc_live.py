from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from fractions import Fraction
from urllib.parse import quote

import numpy as np
import websockets
import aioice.ice
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame
from av.audio.resampler import AudioResampler
from websockets.exceptions import ConnectionClosed

from .main_types import InterviewSession
from .voice import VoiceProvider


GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_REST_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_AIOICE_PATCHED = False
_NEXT_RTP_PORT = 0
_ADVERTISED_ICE_HOST = ""
_AUDIO_RATE_RE = re.compile(r"(?:^|;)rate=(\d+)(?:;|$)")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")


def _api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()


def _rtp_port_range() -> tuple[int, int]:
    return (
        int(os.getenv("WEBRTC_UDP_PORT_MIN", "50000")),
        int(os.getenv("WEBRTC_UDP_PORT_MAX", "50050")),
    )


def _next_rtp_port() -> int:
    global _NEXT_RTP_PORT
    min_port, max_port = _rtp_port_range()
    if _NEXT_RTP_PORT < min_port or _NEXT_RTP_PORT > max_port:
        _NEXT_RTP_PORT = min_port
    port = _NEXT_RTP_PORT
    _NEXT_RTP_PORT += 1
    if _NEXT_RTP_PORT > max_port:
        _NEXT_RTP_PORT = min_port
    return port


def patch_aioice_port_range() -> None:
    global _AIOICE_PATCHED
    if _AIOICE_PATCHED:
        return
    _AIOICE_PATCHED = True

    async def get_component_candidates(self, component: int, addresses: list[str], timeout: int = 5) -> list:
        advertised_host = _ADVERTISED_ICE_HOST or os.getenv("WEBRTC_ICE_HOST", "").strip()
        if advertised_host.lower() == "auto":
            advertised_host = ""
        candidates = []
        loop = asyncio.get_event_loop()
        host_protocols = []
        for address in addresses:
            transport = None
            protocol = None
            for _ in range(max(_rtp_port_range()[1] - _rtp_port_range()[0] + 1, 1)):
                try:
                    transport, protocol = await loop.create_datagram_endpoint(
                        lambda: aioice.ice.StunProtocol(self),
                        local_addr=(address, _next_rtp_port()),
                    )
                    sock = transport.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(
                            aioice.ice.socket.SOL_SOCKET,
                            aioice.ice.socket.SO_RCVBUF,
                            aioice.ice.turn.UDP_SOCKET_BUFFER_SIZE,
                        )
                    break
                except OSError:
                    transport = None
                    protocol = None
                    continue
            if transport is None or protocol is None:
                continue

            host_protocols.append(protocol)
            candidate_address = protocol.transport.get_extra_info("sockname")
            protocol.local_candidate = aioice.ice.Candidate(
                foundation=aioice.ice.candidate_foundation("host", "udp", candidate_address[0]),
                component=component,
                transport="udp",
                priority=aioice.ice.candidate_priority(component, "host"),
                host=advertised_host or candidate_address[0],
                port=candidate_address[1],
                type="host",
            )
            if self._transport_policy == aioice.ice.TransportPolicy.ALL:
                candidates.append(protocol.local_candidate)
        self._protocols += host_protocols

        tasks = []
        if self.stun_server:
            for protocol in host_protocols:
                if aioice.ice.ipaddress.ip_address(protocol.local_candidate.host).version == 4:
                    tasks.append(
                        asyncio.create_task(
                            aioice.ice.server_reflexive_candidate(protocol, self.stun_server)
                        )
                    )
        if self.turn_server:
            tasks.append(
                asyncio.create_task(
                    aioice.ice.relayed_candidate(
                        component=component,
                        protocol_factory=lambda: aioice.ice.StunProtocol(self),
                        turn_server=self.turn_server,
                        turn_username=self.turn_username,
                        turn_password=self.turn_password,
                        turn_ssl=self.turn_ssl,
                        turn_transport=self.turn_transport,
                    )
                )
            )

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in done:
                if task.exception() is None:
                    candidate, protocol = task.result()
                    candidates.append(candidate)
                    if protocol is not None:
                        self._protocols.append(protocol)
            for task in pending:
                task.cancel()
        return candidates

    aioice.ice.Connection.get_component_candidates = get_component_candidates


def _audio_sample_rate(mime_type: str | None, default: int) -> int:
    if not mime_type:
        return default
    match = _AUDIO_RATE_RE.search(mime_type)
    if not match:
        return default
    try:
        sample_rate = int(match.group(1))
    except ValueError:
        return default
    if sample_rate <= 0:
        return default
    return sample_rate


def _is_wrong_language_input_transcript(text: str) -> bool:
    chars = [char for char in text if char.isalpha()]
    if len(chars) < 3:
        return False

    kana_chars = len(_KANA_RE.findall(text))
    japanese_chars = len(_JAPANESE_RE.findall(text))
    non_japanese_script_chars = sum(
        len(pattern.findall(text))
        for pattern in (_HANGUL_RE, _CYRILLIC_RE, _THAI_RE, _ARABIC_RE, _DEVANAGARI_RE)
    )
    latin_chars = len(_LATIN_RE.findall(text))

    if non_japanese_script_chars >= 3 and non_japanese_script_chars > japanese_chars:
        return True
    if latin_chars >= 8 and kana_chars == 0 and japanese_chars == 0:
        return True
    return False


def _translation_model() -> str:
    return os.getenv("GEMINI_TRANSLATION_MODEL", "gemini-2.5-flash").strip()


def _output_pitch_factor() -> float:
    try:
        factor = float(os.getenv("GEMINI_OUTPUT_PITCH_FACTOR", "0.92"))
    except ValueError:
        return 0.92
    return min(max(factor, 0.75), 1.25)


def _input_language_code() -> str:
    return os.getenv("GEMINI_LIVE_LANGUAGE", "ja-JP").strip()


# 日本語（仮名・漢字・全角・和文約物）に隣接する空白だけを除去する。
# Gemini の文字起こしが形態素ごとに空白を挟むため。英単語間の空白は保持。
_JP_CHAR = (
    r"　-〿々〆぀-ヿㇰ-ㇿ"
    r"㐀-䶿一-鿿＀-￯"
)
_SPACE_AFTER_JP_RE = re.compile(rf"(?<=[{_JP_CHAR}])\s+")
_SPACE_BEFORE_JP_RE = re.compile(rf"\s+(?=[{_JP_CHAR}])")
_MULTI_SPACE_RE = re.compile(r"[ \t　]{2,}")


def _normalize_transcript(text: str) -> str:
    if not text or os.getenv("GEMINI_TRANSCRIPT_NORMALIZE", "1").strip() == "0":
        return text
    text = _SPACE_AFTER_JP_RE.sub("", text)
    text = _SPACE_BEFORE_JP_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()


def _vad_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(0, value)


def _realtime_input_config() -> dict:
    activity_detection: dict = {
        "disabled": False,
        "silenceDurationMs": _vad_int("GEMINI_VAD_SILENCE_MS", 350),
        "prefixPaddingMs": _vad_int("GEMINI_VAD_PREFIX_PADDING_MS", 100),
    }
    start_sensitivity = os.getenv("GEMINI_VAD_START_SENSITIVITY", "START_SENSITIVITY_HIGH").strip()
    end_sensitivity = os.getenv("GEMINI_VAD_END_SENSITIVITY", "END_SENSITIVITY_HIGH").strip()
    if start_sensitivity:
        activity_detection["startOfSpeechSensitivity"] = start_sensitivity
    if end_sensitivity:
        activity_detection["endOfSpeechSensitivity"] = end_sensitivity
    turn_coverage = os.getenv("GEMINI_VAD_TURN_COVERAGE", "TURN_INCLUDES_ONLY_ACTIVITY").strip()
    activity_handling = os.getenv("GEMINI_ACTIVITY_HANDLING", "NO_INTERRUPTION").strip()
    config = {
        "automaticActivityDetection": activity_detection,
        "turnCoverage": turn_coverage or "TURN_INCLUDES_ONLY_ACTIVITY",
    }
    if activity_handling:
        config["activityHandling"] = activity_handling
    return config


def _extract_generate_content_text(response: dict) -> str:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content", {})
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        return ""
    return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()


def _translate_to_japanese_sync(text: str) -> str:
    key = _api_key()
    model = _translation_model()
    if not key or not model:
        return ""

    payload = {
        "systemInstruction": {
            "parts": [
                {
                    "text": (
                        "You translate speech transcripts into natural Japanese. "
                        "Return only the Japanese translation. Do not add explanations."
                    )
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "次の音声文字起こしを日本語に翻訳してください。"
                            "固有名詞や短い相槌は自然な日本語にしてください。\n\n"
                            f"{text}"
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 128,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_REST_URL.format(model=quote(model, safe="")),
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response_body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return ""
    return _extract_generate_content_text(response_body)


async def _translate_to_japanese(text: str) -> str:
    return await asyncio.to_thread(_translate_to_japanese_sync, text)


class GeminiAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._timestamp = 0
        self._default_input_sample_rate = 24000
        self._output_sample_rate = 48000
        self._frame_samples = 960
        self._frame_duration = self._frame_samples / self._output_sample_rate
        self._next_frame_at: float | None = None
        self._pcm_buffer = np.empty(0, dtype=np.int16)
        self._queued_output_samples = 0
        self._resamplers: dict[int, AudioResampler] = {}
        self.audio_queue: asyncio.Queue[tuple[bytes, int] | None] = asyncio.Queue()
        self.pitch_factor = _output_pitch_factor()

    def enqueue_audio(self, pcm_data: bytes, input_sample_rate: int) -> None:
        samples = len(pcm_data) // 2
        if samples <= 0:
            return
        sample_rate = input_sample_rate or self._default_input_sample_rate
        adjusted_sample_rate = max(1, int(round(sample_rate * self.pitch_factor)))
        self.audio_queue.put_nowait((pcm_data, adjusted_sample_rate))

    def mark_turn_end(self) -> None:
        self.audio_queue.put_nowait((b"", -1))

    async def recv(self) -> AudioFrame:
        while self._pcm_buffer.size < self._frame_samples:
            audio_item = await self.audio_queue.get()
            if audio_item is None:
                raise asyncio.CancelledError
            pcm_data, input_sample_rate = audio_item
            if input_sample_rate < 0:
                if self._pcm_buffer.size:
                    padding = self._frame_samples - self._pcm_buffer.size
                    self._pcm_buffer = np.pad(self._pcm_buffer, (0, padding))
                    break
                continue
            samples = len(pcm_data) // 2
            if samples <= 0:
                continue
            ndarr = np.frombuffer(pcm_data, dtype=np.int16).reshape(1, samples)
            input_frame = AudioFrame.from_ndarray(ndarr, format="s16", layout="mono")
            input_frame.sample_rate = input_sample_rate or self._default_input_sample_rate
            resampler = self._resamplers.get(input_frame.sample_rate)
            if resampler is None:
                resampler = AudioResampler(format="s16", layout="mono", rate=self._output_sample_rate)
                self._resamplers[input_frame.sample_rate] = resampler
            for resampled in resampler.resample(input_frame):
                resampled_samples = resampled.to_ndarray().reshape(-1)
                self._queued_output_samples += resampled_samples.size
                self._pcm_buffer = np.concatenate((self._pcm_buffer, resampled_samples))

        frame_samples = self._pcm_buffer[: self._frame_samples]
        self._pcm_buffer = self._pcm_buffer[self._frame_samples :]
        frame = AudioFrame.from_ndarray(frame_samples.reshape(1, self._frame_samples), format="s16", layout="mono")
        frame.sample_rate = self._output_sample_rate
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, self._output_sample_rate)
        self._timestamp += frame.samples

        now = time.monotonic()
        if self._next_frame_at is None or self._next_frame_at < now - self._frame_duration:
            self._next_frame_at = now
        delay = self._next_frame_at - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_frame_at += self._frame_duration
        self._queued_output_samples = max(0, self._queued_output_samples - frame.samples)
        return frame

    def clear_buffered_audio(self) -> None:
        self._pcm_buffer = np.empty(0, dtype=np.int16)
        self._queued_output_samples = 0
        self._next_frame_at = None
        while True:
            try:
                self.audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def wait_until_played(self, *, extra_delay: float = 0.45, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queued_output_samples <= 0 and self._pcm_buffer.size == 0 and self.audio_queue.empty():
                await asyncio.sleep(extra_delay)
                return
            await asyncio.sleep(0.05)


class WebRTCGeminiSession:
    def __init__(self, provider: VoiceProvider, session: InterviewSession, logger, ice_host: str = "") -> None:
        patch_aioice_port_range()
        self.provider = provider
        self.session = session
        self.logger = logger
        self.ice_host = ice_host
        self.pc = RTCPeerConnection()
        self.gemini_track = GeminiAudioTrack()
        self.pc.addTrack(self.gemini_track)
        self.data_channel = None
        self.pending_data_messages: list[dict] = []
        self.gemini_send_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.tasks: set[asyncio.Task] = set()
        self.counters = {
            "browser_audio_frames": 0,
            "browser_audio_bytes": 0,
            "browser_text_turns": 0,
            "browser_metrics": 0,
            "gemini_messages": 0,
            "gemini_audio_chunks": 0,
            "gemini_audio_bytes": 0,
            "input_transcripts": 0,
            "output_transcripts": 0,
            "turn_complete": 0,
        }
        self._last_stats_at = time.monotonic()
        self._wire_peer_connection()

    def _wire_peer_connection(self) -> None:
        @self.pc.on("datachannel")
        def on_datachannel(channel):
            self.data_channel = channel
            self.logger.info("webrtc_datachannel_opened interview_id=%s label=%s", self.session.interviewId, channel.label)

            @channel.on("open")
            def on_open() -> None:
                self._flush_pending_data_messages()

            @channel.on("message")
            def on_message(message) -> None:
                if isinstance(message, bytes):
                    return
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    self.logger.info("webrtc_unknown_data_message interview_id=%s bytes=%s", self.session.interviewId, len(message))
                    return
                self._handle_control_payload(payload)

            self._flush_pending_data_messages()

        @self.pc.on("track")
        def on_track(track) -> None:
            if track.kind != "audio":
                return
            self.logger.info("webrtc_audio_track_received interview_id=%s", self.session.interviewId)
            task = asyncio.create_task(self._process_browser_audio(track))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            self.logger.info(
                "webrtc_connection_state interview_id=%s state=%s",
                self.session.interviewId,
                self.pc.connectionState,
            )
            if self.pc.connectionState in {"failed", "closed", "disconnected"}:
                await self.close()

    def _handle_control_payload(self, payload: dict) -> None:
        event_type = payload.get("type")
        if event_type == "text":
            text = str(payload.get("text", ""))
            self.counters["browser_text_turns"] += 1
            self.logger.info("webrtc_text_turn interview_id=%s text_len=%s", self.session.interviewId, len(text))
            self.gemini_send_queue.put_nowait(
                {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": text}]}],
                        "turnComplete": True,
                    }
                }
            )
        elif event_type == "metrics":
            self.counters["browser_metrics"] += 1
            metrics = payload.get("metrics")
            if isinstance(metrics, dict):
                self._record_interaction_metrics(metrics)
            self.logger.info("voice_metrics interview_id=%s metrics=%s", self.session.interviewId, metrics)
        elif event_type == "audio_stream_end":
            self.logger.info("webrtc_audio_stream_end interview_id=%s reason=%s", self.session.interviewId, payload.get("reason", ""))
            self.gemini_send_queue.put_nowait({"realtimeInput": {"audioStreamEnd": True}})
        elif event_type == "interrupt":
            self.logger.info("webrtc_interrupt interview_id=%s", self.session.interviewId)
        else:
            self.logger.info("webrtc_unknown_control_event interview_id=%s event_type=%s", self.session.interviewId, event_type)

    def _record_interaction_metrics(self, metrics: dict) -> None:
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

    async def _process_browser_audio(self, track) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=16000)
        try:
            while True:
                frame = await track.recv()
                for resampled in resampler.resample(frame):
                    pcm = np.ascontiguousarray(resampled.to_ndarray().reshape(-1), dtype=np.int16)
                    pcm_bytes = pcm.tobytes()
                    if not pcm_bytes:
                        continue
                    self.counters["browser_audio_frames"] += 1
                    self.counters["browser_audio_bytes"] += len(pcm_bytes)
                    self.gemini_send_queue.put_nowait(
                        {
                            "realtimeInput": {
                                "audio": {
                                    "data": base64.b64encode(pcm_bytes).decode("ascii"),
                                    "mimeType": "audio/pcm;rate=16000",
                                }
                            }
                        }
                    )
                    self._maybe_log_stats("audio_in")
        except Exception as exc:
            self.logger.info("webrtc_audio_track_done interview_id=%s reason=%s", self.session.interviewId, exc)

    def _send_data(self, payload: dict) -> None:
        if self.data_channel and self.data_channel.readyState == "open":
            self.data_channel.send(json.dumps(payload))
            return
        self.pending_data_messages.append(payload)

    def _flush_pending_data_messages(self) -> None:
        if not self.data_channel or self.data_channel.readyState != "open":
            return
        pending = self.pending_data_messages
        self.pending_data_messages = []
        for payload in pending:
            self.data_channel.send(json.dumps(payload))

    def _maybe_log_stats(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_stats_at < 5:
            return
        self._last_stats_at = now
        self.logger.info("webrtc_gemini_stats interview_id=%s reason=%s counters=%s", self.session.interviewId, reason, self.counters.copy())

    async def start_gemini(self) -> None:
        key = _api_key()
        if not key:
            self._send_data({"type": "error", "message": "GEMINI_API_KEY is not configured on the backend."})
            return

        url = f"{GEMINI_WS_URL}?key={quote(key)}"
        self.logger.info("webrtc_gemini_connecting interview_id=%s model=%s", self.session.interviewId, self.provider.status().model)
        try:
            async with websockets.connect(url, max_size=16 * 1024 * 1024) as gemini_ws:
                await gemini_ws.send(json.dumps(self._setup_message()))
                self._send_data({"type": "session.ready", "provider": self.provider.status().__dict__})
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
            self.logger.info("webrtc_gemini_closed interview_id=%s code=%s reason=%s", self.session.interviewId, exc.code, exc.reason)
            self._send_data({"type": "session.reconnect", "reason": f"gemini_closed:{exc.code}"})
        except Exception:
            self.logger.exception("webrtc_gemini_failed interview_id=%s", self.session.interviewId)
            self._send_data({"type": "error", "message": "Gemini Live connection failed."})
        finally:
            self.logger.info("webrtc_gemini_done interview_id=%s counters=%s", self.session.interviewId, self.counters.copy())

    async def _gemini_sender(self, gemini_ws) -> None:
        while True:
            payload = await self.gemini_send_queue.get()
            if payload is None:
                return
            await gemini_ws.send(json.dumps(payload))

    async def _gemini_receiver(self, gemini_ws) -> None:
        async for raw_message in gemini_ws:
            self.counters["gemini_messages"] += 1
            response = json.loads(raw_message)
            if "goAway" in response:
                self._send_data({"type": "session.reconnect", "reason": "goAway"})
                return

            server_content = response.get("serverContent", {})
            if server_content.get("interrupted"):
                self.gemini_track.clear_buffered_audio()
                self._send_data({"type": "interrupted"})

            if "inputTranscription" in server_content:
                text = server_content["inputTranscription"].get("text", "")
                if text:
                    if _is_wrong_language_input_transcript(text):
                        translated = await _translate_to_japanese(text)
                        if not translated:
                            self.logger.info(
                                "input_transcript_dropped_wrong_language interview_id=%s text_len=%s",
                                self.session.interviewId,
                                len(text),
                            )
                            continue
                        self.logger.info(
                            "input_transcript_translated interview_id=%s source_len=%s translated_len=%s",
                            self.session.interviewId,
                            len(text),
                            len(translated),
                        )
                        text = translated
                    text = _normalize_transcript(text)
                    if text:
                        self.counters["input_transcripts"] += 1
                        self.session.messages.append({"role": "user", "content": text})
                        self._send_data({"type": "input_transcript", "text": text})

            if "outputTranscription" in server_content:
                text = server_content["outputTranscription"].get("text", "")
                if text:
                    text = _normalize_transcript(text)
                    if text:
                        self.counters["output_transcripts"] += 1
                        self.session.messages.append({"role": "assistant", "content": text})
                        self._send_data({"type": "output_transcript", "text": text})

            model_turn = server_content.get("modelTurn", {})
            for part in model_turn.get("parts", []):
                inline_data = part.get("inlineData")
                if inline_data:
                    pcm_data = base64.b64decode(inline_data.get("data", ""))
                    mime_type = inline_data.get("mimeType")
                    sample_rate = _audio_sample_rate(mime_type, self.gemini_track._default_input_sample_rate)
                    self.counters["gemini_audio_chunks"] += 1
                    self.counters["gemini_audio_bytes"] += len(pcm_data)
                    self.gemini_track.enqueue_audio(pcm_data, sample_rate)
                    self._maybe_log_stats("audio_out")

            if server_content.get("turnComplete"):
                self.gemini_track.mark_turn_end()
                await self.gemini_track.wait_until_played()
                self.counters["turn_complete"] += 1
                self._send_data({"type": "turn_complete"})

            if "setupComplete" in response:
                self._send_data({"type": "setup_complete"})

    def _setup_message(self) -> dict:
        generation_config = {"responseModalities": ["AUDIO"]}
        speech_config: dict = {}
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
            "webrtc_gemini_setup interview_id=%s model=%s voice=%s language=%s",
            self.session.interviewId,
            model_id,
            voice_name or "(none)",
            language_code or "(none)",
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

    async def create_answer(self, offer_sdp: str, offer_type: str) -> dict[str, str]:
        global _ADVERTISED_ICE_HOST
        _ADVERTISED_ICE_HOST = self.ice_host
        offer = RTCSessionDescription(sdp=offer_sdp, type=offer_type)
        await self.pc.setRemoteDescription(offer)
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        task = asyncio.create_task(self.start_gemini())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {"sdp": self.pc.localDescription.sdp, "type": self.pc.localDescription.type}

    async def close(self) -> None:
        self.gemini_send_queue.put_nowait(None)
        self.gemini_track.audio_queue.put_nowait(None)
        for task in list(self.tasks):
            task.cancel()
        await self.pc.close()
