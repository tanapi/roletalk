from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Difficulty = Literal["easy", "normal", "hard"]
SessionMode = Literal["interview", "discussion"]
VoiceProviderName = Literal["gemini-live"]


class Interviewer(BaseModel):
    name: str
    personality: str
    speakingStyle: str
    imageKey: str


class InterviewSession(BaseModel):
    interviewId: str
    mode: SessionMode = "interview"
    jobRole: str
    difficulty: Difficulty
    voiceProvider: VoiceProviderName
    createdAt: str
    durationSec: int = 900
    candidateLastName: str
    candidateLastNameKana: str
    interviewer: Interviewer
    scenario: dict[str, Any]
    evaluationRubric: dict[str, float]
    hiddenCriteria: list[str]
    messages: list[dict[str, str]]
    interactionMetrics: dict[str, Any] = Field(default_factory=dict)
    currentPhaseIndex: int = 0
    maxTurns: int = 6
