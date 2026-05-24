from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .main_types import InterviewSession
from .prompts import discussion_system_instruction


VoiceProviderName = Literal["gemini-live"]
DEFAULT_DISCUSSION_COLLABORATIVE_VOICE = "Leda"
DEFAULT_DISCUSSION_LOGICAL_VOICE = "Charon"


@dataclass(frozen=True)
class VoiceProviderStatus:
    name: VoiceProviderName
    label: str
    model: str
    enabled: bool
    configured: bool
    realtime: bool
    notes: list[str]


class VoiceProvider:
    name: VoiceProviderName
    label: str
    model: str
    realtime: bool = False
    default_voice_name: str | None = None

    def status(self) -> VoiceProviderStatus:
        raise NotImplementedError

    def voice_name(self, session: InterviewSession | None = None) -> str | None:
        if session and session.mode == "discussion":
            persona_key = str(session.scenario.get("discussionPersonaKey") or "")
            if persona_key == "collaborative":
                collaborative_voice = os.getenv("DISCUSSION_COLLABORATIVE_VOICE", "").strip()
                return collaborative_voice or DEFAULT_DISCUSSION_COLLABORATIVE_VOICE
            if persona_key == "logical":
                logical_voice = os.getenv("DISCUSSION_LOGICAL_VOICE", "").strip()
                return logical_voice or DEFAULT_DISCUSSION_LOGICAL_VOICE
        return self.default_voice_name

    def system_instruction(self, session: InterviewSession) -> str:
        if session.mode == "discussion":
            return discussion_system_instruction(session)

        return os.getenv("VOICE_FALLBACK_SYSTEM_INSTRUCTION", "You are a voice assistant.")

class GeminiLiveVoiceProvider(VoiceProvider):
    name = "gemini-live"
    label = "Gemini 3.1 Flash Live Preview"
    model = "gemini-3.1-flash-live-preview"
    realtime = True
    default_voice_name = "Charon"

    def status(self) -> VoiceProviderStatus:
        api_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
        credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        uses_vertex = bool(project and credentials and project != "your-gcp-project-id")
        uses_developer_api = bool(api_key)
        configured = uses_developer_api or uses_vertex
        notes = [
            "Gemini Live APIをFastAPI側で中継します。",
            "日本語面接用にlanguage hintとsystem instructionを渡す前提です。",
            f"音声は voice_name={self.voice_name()} を指定します。",
            "音声入力はPCM16 16kHz、音声出力はPCM16 24kHzとして扱います。",
        ]
        if not configured:
            notes.append("有効化にはGEMINI_API_KEY、またはVertex AIの認証情報が必要です。")
        elif uses_developer_api:
            notes.append("Google AI StudioのAPIキーでGemini Developer APIを使用します。")
        else:
            notes.append(f"Vertex AI location: {location}")
        return VoiceProviderStatus(
            name=self.name,
            label=self.label,
            model=os.getenv("GEMINI_LIVE_MODEL", self.model),
            enabled=True,
            configured=configured,
            realtime=True,
            notes=notes,
        )


PROVIDERS: dict[VoiceProviderName, VoiceProvider] = {
    "gemini-live": GeminiLiveVoiceProvider(),
}


def get_default_voice_provider_name() -> VoiceProviderName:
    return "gemini-live"


def get_voice_provider(name: str | None) -> VoiceProvider:
    if name in PROVIDERS:
        return PROVIDERS[name]  # type: ignore[index]
    return PROVIDERS[get_default_voice_provider_name()]
