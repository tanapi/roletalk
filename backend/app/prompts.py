from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from .main_types import InterviewSession


class PromptConfigError(RuntimeError):
    pass


# Public UI metadata only. Actual model instructions are loaded from secrets.
DISCUSSION_PERSONAS: dict[str, dict[str, str]] = {
    "collaborative": {
        "label": "共創型の討論者",
        "personality": "相手の主張を受け止め、一緒に考えを深めるディスカッション相手",
        "speakingStyle": "共感を置いてから、別視点や確認をやわらかく返す",
    },
    "logical": {
        "label": "論理型の討論者",
        "personality": "相手の主張を受け止めながら、反対側の論点を丁寧に提示するディスカッション相手",
        "speakingStyle": "短く論点を整理し、根拠や例外条件を確認しながら議論を進める",
    },
}


PROMPT_CONFIG_ENV = "ROLETALK_PROMPTS_JSON"
PROMPT_CONFIG_FILE_ENV = "ROLETALK_PROMPTS_FILE"


def _default_prompt_paths() -> list[Path]:
    backend_dir = Path(__file__).resolve().parents[1]
    return [
        Path(os.getenv(PROMPT_CONFIG_FILE_ENV, "")).expanduser(),
        Path("/run/secrets/roletalk-prompts.json"),
        backend_dir / "prompts.local.json",
        Path.cwd() / "prompts.local.json",
    ]


@lru_cache(maxsize=1)
def _prompt_config() -> dict[str, Any]:
    inline_json = os.getenv(PROMPT_CONFIG_ENV, "").strip()
    if inline_json:
        return _parse_prompt_json(inline_json, PROMPT_CONFIG_ENV)

    for path in _default_prompt_paths():
        if not str(path):
            continue
        if path.is_file():
            return _parse_prompt_json(path.read_text(encoding="utf-8"), str(path))

    raise PromptConfigError(
        f"Prompt config is not configured. Set {PROMPT_CONFIG_ENV} or {PROMPT_CONFIG_FILE_ENV}."
    )


def _parse_prompt_json(raw: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromptConfigError(f"Invalid prompt JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise PromptConfigError(f"Prompt config in {source} must be a JSON object.")
    return value


def _required_text(key: str) -> str:
    value = _prompt_config().get(key)
    if not isinstance(value, str) or not value.strip():
        raise PromptConfigError(f"Prompt config key is missing or empty: {key}")
    return value


def _optional_personas() -> dict[str, dict[str, str]]:
    value = _prompt_config().get("personas")
    if not isinstance(value, dict):
        return DISCUSSION_PERSONAS
    personas: dict[str, dict[str, str]] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        personas[key] = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    return personas or DISCUSSION_PERSONAS


def _render(template: str, values: dict[str, Any]) -> str:
    safe_values = {key: str(value) for key, value in values.items()}
    return Template(template).safe_substitute(safe_values)


def discussion_personas() -> dict[str, dict[str, str]]:
    try:
        return _optional_personas()
    except PromptConfigError:
        return DISCUSSION_PERSONAS


def discussion_persona_instruction(persona_key: str) -> str:
    instructions = _prompt_config().get("personaInstructions")
    if isinstance(instructions, dict):
        value = instructions.get(persona_key) or instructions.get("collaborative")
        if isinstance(value, str) and value.strip():
            return value
    return _required_text(f"personaInstructions.{persona_key}")


def discussion_system_instruction(session: InterviewSession) -> str:
    topic = session.scenario.get("discussionTopic", "与えられたお題")
    persona_key = str(session.scenario.get("discussionPersonaKey") or "collaborative")
    persona_label = str(session.scenario.get("discussionPersonaLabel") or "共創型の討論者")
    criteria = "、".join(session.hiddenCriteria)
    return _render(
        _required_text("discussionSystemTemplate"),
        {
            "topic": topic,
            "persona_key": persona_key,
            "persona_label": persona_label,
            "persona_instruction": discussion_persona_instruction(persona_key),
            "candidate_last_name": session.candidateLastName,
            "candidate_last_name_kana": session.candidateLastNameKana,
            "criteria": criteria,
        },
    )


def discussion_conclusion_prompt() -> str:
    return _required_text("discussionConclusion")


def discussion_interruption_prompt() -> str:
    return _required_text("discussionInterruption")


def topic_generation_payload(
    *,
    difficulty_hint: str,
    count: int,
    current_hint_text: str,
    random_seed: int,
) -> dict[str, Any]:
    return {
        "systemInstruction": {
            "parts": [{"text": _required_text("topicGenerationSystem")}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": _render(
                            _required_text("topicGenerationUserTemplate"),
                            {
                                "difficulty_hint": difficulty_hint,
                                "count": count,
                                "current_hint_text": current_hint_text,
                                "random_seed": random_seed,
                            },
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": float(os.getenv("TOPIC_GENERATION_TEMPERATURE", "1.0")),
            "maxOutputTokens": int(os.getenv("TOPIC_GENERATION_MAX_OUTPUT_TOKENS", "160")),
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }


def feedback_summary_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "systemInstruction": {
            "parts": [{"text": _required_text("feedbackSummarySystem")}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": _render(
                            _required_text("feedbackSummaryUserTemplate"),
                            {"source_json": json.dumps(source, ensure_ascii=False)},
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": float(os.getenv("FEEDBACK_SUMMARY_TEMPERATURE", "0.35")),
            "maxOutputTokens": int(os.getenv("FEEDBACK_SUMMARY_MAX_OUTPUT_TOKENS", "520")),
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
