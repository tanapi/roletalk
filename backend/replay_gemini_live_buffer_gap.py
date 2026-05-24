from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import wave
from urllib.parse import quote

import websockets


GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)


def api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()


def read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"Only 16-bit PCM WAV is supported; got sample width={width}")
    if channels == 1:
        return frames, rate
    if channels != 2:
        raise ValueError(f"Only mono or stereo WAV is supported; got channels={channels}")
    mono = bytearray(len(frames) // 2)
    out = 0
    for offset in range(0, len(frames) - 3, 4):
        left = int.from_bytes(frames[offset : offset + 2], "little", signed=True)
        right = int.from_bytes(frames[offset + 2 : offset + 4], "little", signed=True)
        sample = round((left + right) / 2)
        mono[out : out + 2] = int(sample).to_bytes(2, "little", signed=True)
        out += 2
    return bytes(mono[:out]), rate


async def send_audio(ws, pcm: bytes, rate: int, chunk_ms: int, realtime: bool) -> int:
    bytes_per_ms = rate * 2 / 1000
    chunk_bytes = max(2, int(bytes_per_ms * chunk_ms))
    chunk_bytes -= chunk_bytes % 2
    sent = 0
    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset : offset + chunk_bytes]
        await ws.send(
            json.dumps(
                {
                    "realtimeInput": {
                        "audio": {
                            "data": base64.b64encode(chunk).decode("ascii"),
                            "mimeType": f"audio/pcm;rate={rate}",
                        }
                    }
                }
            )
        )
        sent += 1
        if realtime:
            await asyncio.sleep(len(chunk) / 2 / rate)
    return sent


def summarize_response(response: dict, started: float) -> dict:
    elapsed = time.monotonic() - started
    message_type = next(
        (
            key
            for key in (
                "setupComplete",
                "serverContent",
                "toolCall",
                "toolCallCancellation",
                "usageMetadata",
                "goAway",
                "sessionResumptionUpdate",
            )
            if key in response
        ),
        "unknown",
    )
    server_content = response.get("serverContent", {})
    summary = {
        "t": round(elapsed, 2),
        "type": message_type,
        "inTranscript": "inputTranscription" in server_content,
        "outTranscript": "outputTranscription" in server_content,
        "turnComplete": bool(server_content.get("turnComplete")),
        "generationComplete": bool(server_content.get("generationComplete")),
        "interrupted": bool(server_content.get("interrupted")),
        "goAway": "goAway" in response,
    }
    if "inputTranscription" in server_content:
        summary["inputText"] = server_content["inputTranscription"].get("text", "")
    if "outputTranscription" in server_content:
        summary["outputText"] = server_content["outputTranscription"].get("text", "")
    return summary


async def wait_for(ws, started: float, predicate, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out after {timeout}s")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        response = json.loads(raw)
        summary = summarize_response(response, started)
        print(json.dumps(summary, ensure_ascii=False))
        if predicate(response, summary):
            return response


async def replay(args: argparse.Namespace) -> None:
    key = api_key()
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY")
    pcm, rate = read_wav(args.wav)
    model = args.model or os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
    setup = {
        "setup": {
            "model": f"models/{model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"languageCode": args.language},
            },
            "systemInstruction": {"parts": [{"text": args.instruction}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "disabled": False,
                    "silenceDurationMs": args.silence_ms,
                    "prefixPaddingMs": args.prefix_ms,
                    "startOfSpeechSensitivity": args.start_sensitivity,
                    "endOfSpeechSensitivity": args.end_sensitivity,
                },
                "turnCoverage": args.turn_coverage,
                "activityHandling": args.activity_handling,
            },
        }
    }
    url = f"{GEMINI_WS_URL}?key={quote(key)}"
    started = time.monotonic()
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps(setup))
        await wait_for(ws, started, lambda _r, s: s["type"] == "setupComplete", args.wait_sec)
        await ws.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": args.opening}]}],
                        "turnComplete": True,
                    }
                }
            )
        )
        await wait_for(ws, started, lambda _r, s: s["generationComplete"], args.wait_sec)
        await wait_for(ws, started, lambda _r, s: s["turnComplete"], args.wait_sec)
        sent = await send_audio(ws, pcm, rate, args.chunk_ms, args.realtime)
        print(json.dumps({"sentChunksAfterTurnComplete": sent}, ensure_ascii=False))
        await wait_for(ws, started, lambda _r, s: s["inTranscript"], args.wait_sec)
        await wait_for(ws, started, lambda _r, s: s["outTranscript"], args.wait_sec)
        await wait_for(ws, started, lambda _r, s: s["turnComplete"], args.wait_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a captured WAV after an assistant turn completes.")
    parser.add_argument("wav")
    parser.add_argument("--model", default="")
    parser.add_argument("--language", default=os.getenv("GEMINI_LIVE_LANGUAGE", "ja-JP"))
    parser.add_argument("--instruction", default="あなたは日本語で短く応答する会話相手です。")
    parser.add_argument("--opening", default="在宅勤務について、短く質問してください。")
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--wait-sec", type=float, default=30)
    parser.add_argument("--realtime", action="store_true")
    parser.add_argument("--silence-ms", type=int, default=350)
    parser.add_argument("--prefix-ms", type=int, default=100)
    parser.add_argument("--start-sensitivity", default="START_SENSITIVITY_HIGH")
    parser.add_argument("--end-sensitivity", default="END_SENSITIVITY_HIGH")
    parser.add_argument("--turn-coverage", default="TURN_INCLUDES_ONLY_ACTIVITY")
    parser.add_argument("--activity-handling", default="NO_INTERRUPTION")
    asyncio.run(replay(parser.parse_args()))


if __name__ == "__main__":
    main()
