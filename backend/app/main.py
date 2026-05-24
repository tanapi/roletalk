from __future__ import annotations

import logging
import json
import os
import random
import re
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .cloudflare_realtime import (
    CloudflareRealtimeClient,
    CloudflareRealtimeError,
    get_cloudflare_realtime_config,
)
from .main_types import Difficulty, InterviewSession, Interviewer, VoiceProviderName
from .prompts import discussion_personas, feedback_summary_payload, topic_generation_payload
from .realtime_bridge import CloudflareRealtimeBridgeSession
from .runtime_config import RUNTIME_CONFIG_HEADER, apply_runtime_config_header
from .voice import get_voice_provider
from .webrtc_live import GEMINI_REST_URL, _api_key, _extract_generate_content_text


app = FastAPI(title="RoleTalk")
logger = logging.getLogger("roletalk")


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timezone_name = os.getenv("BACKEND_LOG_TIMEZONE", "Asia/Tokyo")
        created_at = datetime.fromtimestamp(record.created, ZoneInfo(timezone_name))
        if datefmt:
            return created_at.strftime(datefmt)
        timestamp = created_at.strftime("%Y-%m-%d %H:%M:%S")
        return f"{timestamp},{int(record.msecs):03d}"


def configure_logging() -> None:
    log_path = Path(os.getenv("BACKEND_LOG_FILE", "/app/logs/app.log"))
    max_bytes = int(os.getenv("BACKEND_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    backup_count = int(os.getenv("BACKEND_LOG_BACKUP_COUNT", "3"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setFormatter(
            LocalTimeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(file_handler)


configure_logging()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def apply_runtime_config(request: Request, call_next):
    apply_runtime_config_header(request.headers.get(RUNTIME_CONFIG_HEADER))
    return await call_next(request)


class StartDiscussionRequest(BaseModel):
    candidateLastName: str = Field(min_length=1, max_length=20)
    candidateLastNameKana: str = Field(min_length=1, max_length=40)
    difficulty: Difficulty = "normal"
    discussionTopic: str | None = Field(default=None, max_length=80)
    discussionPersonaKey: str | None = Field(default=None, max_length=40)


class DiscussionTopicRequest(BaseModel):
    difficulty: Difficulty = "normal"


class VoiceControlRequest(BaseModel):
    type: str = Field(min_length=1, max_length=80)
    text: str | None = None
    metrics: dict[str, Any] | None = None


class RealtimeSessionSnapshot(BaseModel):
    mode: str = "discussion"
    durationSec: int = 180
    candidateLastName: str = ""
    candidateLastNameKana: str = ""
    difficulty: Difficulty = "normal"
    interviewer: Interviewer
    scenario: dict[str, Any]
    openingQuestion: str = ""


class RealtimeSessionRequest(BaseModel):
    sdp: str = Field(min_length=1)
    type: str = Field(min_length=1)
    micTrackName: str = Field(min_length=1, max_length=120)
    micMid: str = Field(min_length=1, max_length=20)
    sessionSnapshot: RealtimeSessionSnapshot | None = None


class RealtimeTracksRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    sdp: str = Field(min_length=1)
    type: str = Field(min_length=1)
    micTrackName: str = Field(min_length=1, max_length=120)
    micMid: str = Field(min_length=1, max_length=20)
    sessionSnapshot: RealtimeSessionSnapshot | None = None


class RealtimeAdaptersRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    micTrackName: str = Field(min_length=1, max_length=120)
    sessionSnapshot: RealtimeSessionSnapshot | None = None


class RealtimeRenegotiateRequest(BaseModel):
    sessionId: str = Field(min_length=1)
    sdp: str = Field(min_length=1)
    type: str = Field(min_length=1)


sessions: dict[str, InterviewSession] = {}
realtime_sessions: dict[str, dict[str, Any]] = {}
realtime_bridge_sessions: dict[str, CloudflareRealtimeBridgeSession] = {}


def voice_transport() -> str:
    return "cloudflare-realtime"


def delegate_bridge_requests_enabled() -> bool:
    return os.getenv("VOICE_BRIDGE_DELEGATE_REQUESTS", "").strip().lower() in {"1", "true", "yes", "on"}


def bridge_delegate_base_url() -> str:
    return os.getenv("VOICE_BRIDGE_PUBLIC_BASE_URL", "").strip().rstrip("/")


def request_bridge_delegate(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = bridge_delegate_base_url()
    if not base_url:
        raise HTTPException(status_code=400, detail="VOICE_BRIDGE_PUBLIC_BASE_URL is not configured")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Bridge delegate returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Bridge delegate request failed: {exc.reason}") from exc
    return json.loads(body) if body else {}


def restore_realtime_session(interview_id: str, snapshot: RealtimeSessionSnapshot | None) -> InterviewSession | None:
    session = sessions.get(interview_id)
    if session or not snapshot:
        return session
    mode = "discussion" if snapshot.mode == "discussion" else "interview"
    restored = InterviewSession(
        interviewId=interview_id,
        mode=mode,
        jobRole="ディスカッション" if mode == "discussion" else snapshot.scenario.get("jobRole", "面接"),
        difficulty=snapshot.difficulty,
        voiceProvider="gemini-live",
        createdAt=datetime.now(timezone.utc).isoformat(),
        durationSec=snapshot.durationSec,
        candidateLastName=snapshot.candidateLastName or "候補者",
        candidateLastNameKana=snapshot.candidateLastNameKana or "コウホシャ",
        interviewer=snapshot.interviewer,
        scenario=snapshot.scenario,
        evaluationRubric={
            "claimClarity": 0.24,
            "reasoning": 0.22,
            "listening": 0.18,
            "interruptionHandling": 0.18,
            "conciseness": 0.1,
            "tone": 0.08,
        },
        hiddenCriteria=["結論と理由を分けて話せる", "相手の論点を受けて返答できる", "時間内に要点をまとめられる"],
        messages=[{"role": "assistant", "content": snapshot.openingQuestion}] if snapshot.openingQuestion else [],
        maxTurns=4 if mode == "discussion" else 6,
    )
    sessions[interview_id] = restored
    logger.info("realtime_session_restored interview_id=%s mode=%s", interview_id, mode)
    return restored


def local_bridge_auto_create_enabled() -> bool:
    return os.getenv("VOICE_BRIDGE_AUTO_CREATE_DEBUG_SESSION", "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_local_debug_bridge(interview_id: str) -> CloudflareRealtimeBridgeSession | None:
    bridge = realtime_bridge_sessions.get(interview_id)
    if bridge:
        return bridge
    if not local_bridge_auto_create_enabled():
        return None

    session = sessions.get(interview_id)
    if not session:
        interviewer = Interviewer(
            name="",
            personality="議論の相手",
            speakingStyle="簡潔に問い返す",
            imageKey="default",
        )
        session = InterviewSession(
            interviewId=interview_id,
            mode="discussion",
            jobRole="ディスカッション",
            difficulty="normal",
            voiceProvider="gemini-live",
            createdAt=datetime.now(timezone.utc).isoformat(),
            durationSec=180,
            candidateLastName=os.getenv("VOICE_BRIDGE_DEBUG_CANDIDATE_LAST_NAME", "候補者"),
            candidateLastNameKana=os.getenv("VOICE_BRIDGE_DEBUG_CANDIDATE_LAST_NAME_KANA", "コウホシャ"),
            interviewer=interviewer,
            scenario={
                "targetCompanyType": "デバッグ",
                "seniority": "デバッグ",
                "discussionTopic": os.getenv("VOICE_BRIDGE_DEBUG_TOPIC", "この接続テストについて会話してください"),
                "interviewPhases": [],
                "mainConcerns": [],
            },
            evaluationRubric={},
            hiddenCriteria=[],
            messages=[],
            maxTurns=4,
        )
        sessions[interview_id] = session

    provider = get_voice_provider(session.voiceProvider)
    status = provider.status()
    if provider.name != "gemini-live" or not status.configured:
        logger.warning(
            "local_debug_bridge_provider_unavailable interview_id=%s provider=%s configured=%s",
            interview_id,
            status.name,
            status.configured,
        )
        return None
    bridge = CloudflareRealtimeBridgeSession(provider, session, logger)
    bridge.enable_output()
    realtime_bridge_sessions[interview_id] = bridge
    logger.info("local_debug_bridge_created interview_id=%s", interview_id)
    return bridge


def to_websocket_url(public_base_url: str, path: str) -> str:
    base_url = public_base_url.rstrip("/")
    if base_url.startswith("https://"):
        base_url = f"wss://{base_url.removeprefix('https://')}"
    elif base_url.startswith("http://"):
        base_url = f"ws://{base_url.removeprefix('http://')}"
    return f"{base_url}{path}"


def refresh_realtime_input_adapter(interview_id: str) -> dict[str, Any]:
    state = realtime_sessions.get(interview_id)
    if not state:
        raise RuntimeError("Realtime session state is missing")
    session_id = str(state.get("sessionId") or "")
    mic_track_name = str(state.get("micTrackName") or "")
    if not session_id or not mic_track_name:
        raise RuntimeError("Realtime sessionId or micTrackName is missing")

    config = get_cloudflare_realtime_config()
    if not config.configured:
        raise RuntimeError("Cloudflare Realtime is not configured")
    if not config.bridge_public_base_url:
        raise RuntimeError("VOICE_BRIDGE_PUBLIC_BASE_URL is required for Cloudflare Realtime adapters")

    client = CloudflareRealtimeClient(config)
    bridge_input_endpoint = to_websocket_url(config.bridge_public_base_url, f"/api/voice/bridge/{interview_id}/input")
    stream_adapter = client.create_websocket_adapters(
        {
            "tracks": [
                {
                    "location": "remote",
                    "sessionId": session_id,
                    "trackName": mic_track_name,
                    "endpoint": bridge_input_endpoint,
                    "outputCodec": "pcm",
                }
            ]
        }
    )
    new_input_adapter_ids = [
        str(track.get("adapterId"))
        for track in stream_adapter.get("tracks", [])
        if track.get("adapterId")
    ]
    old_input_adapter_ids = [str(adapter_id) for adapter_id in state.get("inputAdapterIds", []) if adapter_id]
    if old_input_adapter_ids:
        try:
            client.close_websocket_adapters(old_input_adapter_ids)
        except CloudflareRealtimeError:
            logger.exception(
                "cloudflare_realtime_input_adapter_close_failed interview_id=%s adapters=%s",
                interview_id,
                old_input_adapter_ids,
            )

    previous_adapter_ids = [str(adapter_id) for adapter_id in state.get("adapterIds", []) if adapter_id]
    retained_adapter_ids = [adapter_id for adapter_id in previous_adapter_ids if adapter_id not in set(old_input_adapter_ids)]
    realtime_sessions[interview_id] = {
        **state,
        "inputAdapterIds": new_input_adapter_ids,
        "adapterIds": [*retained_adapter_ids, *new_input_adapter_ids],
    }
    logger.info(
        "cloudflare_realtime_input_adapter_refreshed interview_id=%s session_id=%s mic_track=%s adapters=%s old_adapters=%s",
        interview_id,
        session_id,
        mic_track_name,
        new_input_adapter_ids,
        old_input_adapter_ids,
    )
    return {"adapterIds": new_input_adapter_ids}


CURATED_DISCUSSION_TOPICS = [
    "小中学生のスマホ利用に年齢制限を設けるべきか",
    "学校の制服は完全に自由化すべきか",
    "宿題は原則として廃止すべきか",
    "部活動は学校ではなく地域主体に移すべきか",
    "給食の食べ残しを減らすための指導は必要か",
    "SNSは実名登録を必須にすべきか",
    "AI生成画像には表示ラベルを義務づけるべきか",
    "動画の倍速視聴を前提に授業や研修を作るべきか",
    "レビュー投稿は購入者だけに限定すべきか",
    "炎上防止のため投稿前チェック機能を標準化すべきか",
    "週休3日制をもっと広げるべきか",
    "リモート勤務を希望者に認めるべきか",
    "会社の飲み会は業務時間内に行うべきか",
    "副業を原則自由にすべきか",
    "年功序列より成果主義を優先すべきか",
    "電車内での通話を一部車両で認めるべきか",
    "飲食店のキャッシュレス限定を認めるべきか",
    "観光地の混雑対策として入場料を上げるべきか",
    "ペットを飼う前に講習を義務づけるべきか",
    "公共施設の利用料をもっと受益者負担にすべきか",
    "人気商品の転売にはより強い規制をかけるべきか",
    "サブスク解約は契約と同じ簡単さにすべきか",
    "映画や漫画のネタバレ投稿には配慮ルールが必要か",
    "推し活の高額課金に上限を設けるべきか",
    "配達員や店員への評価制度は見直すべきか",
    "子どもの顔写真をSNSに投稿することを制限すべきか",
    "災害時のデマ拡散により厳しい罰則を設けるべきか",
    "飲食店で長時間席を使う勉強や作業を認めるべきか",
    "学校や職場で生成AIの利用を積極的に認めるべきか",
    "マッチングアプリで本人確認をさらに厳格化すべきか",
]

CURRENT_TOPIC_QUERIES = [
    "生成AI 議論 日本",
    "SNS 炎上 議論",
    "スマホ 子ども 制限",
    "学校 校則 見直し",
    "働き方 リモート 出社",
    "観光 マナー 問題",
    "物価 サブスク 消費者",
    "ネット 誹謗中傷 対策",
]


def _clean_discussion_topic(topic: str) -> str:
    topic = re.sub(r"^[\s「『\"'・*\-0-9０-９.)）、]+", "", topic).strip()
    topic = re.sub(r"[\s」』\"']+$", "", topic).strip()
    topic = topic.splitlines()[0].strip() if topic else ""
    topic = re.split(r"ため|ので|理由は|背景は", topic)[0].strip(" 、。")
    topic = re.sub(r"[。.!！]+$", "", topic).strip()
    return topic[:60]


def _normalize_topic_key(topic: str) -> str:
    return re.sub(r"[\s　「」『』、。,.!?！？]", "", topic)


def _is_binary_discussion_topic(topic: str) -> bool:
    normalized = _normalize_topic_key(topic)
    if len(normalized) < 8:
        return False
    if re.search(r"(どこまで|どの程度|どう|どのよう|何を|何が|何歳|なぜ|どちら|どっち|いつから|いつまで)", topic):
        return False
    binary_endings = (
        "すべきか",
        "必要か",
        "認めるべきか",
        "許容すべきか",
        "禁止すべきか",
        "制限すべきか",
        "義務づけるべきか",
        "廃止すべきか",
        "見直すべきか",
        "広げるべきか",
    )
    return normalized.endswith(binary_endings)


def _fallback_discussion_topics(rng: random.Random, count: int, exclude: set[str] | None = None) -> list[str]:
    exclude = exclude or set()
    topics = CURATED_DISCUSSION_TOPICS[:]
    rng.shuffle(topics)
    picked: list[str] = []
    seen = set(exclude)
    for topic in topics:
        key = _normalize_topic_key(topic)
        if key in seen:
            continue
        seen.add(key)
        picked.append(topic)
        if len(picked) >= count:
            break
    return picked


def _current_topic_hints(rng: random.Random, limit: int = 8) -> list[str]:
    feeds = [
        feed.strip()
        for feed in os.getenv("DISCUSSION_TOPIC_TREND_FEEDS", "").split(",")
        if feed.strip()
    ]
    if not feeds:
        queries = CURRENT_TOPIC_QUERIES[:]
        rng.shuffle(queries)
        feeds = [
            "https://news.google.com/rss/search?q="
            + quote(query + " when:14d", safe="")
            + "&hl=ja&gl=JP&ceid=JP:ja"
            for query in queries[:4]
        ]

    hints: list[str] = []
    seen: set[str] = set()
    for feed in feeds[:6]:
        try:
            with urllib.request.urlopen(feed, timeout=3) as response:
                root = ET.fromstring(response.read())
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
            logger.info("discussion_topic_feed_failed url=%s error=%s", feed, exc)
            continue
        for item in root.findall(".//item")[:6]:
            title = (item.findtext("title") or "").strip()
            title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
            title = re.sub(r"【[^】]+】", "", title).strip()
            key = _normalize_topic_key(title)
            if len(title) < 8 or key in seen:
                continue
            seen.add(key)
            hints.append(title[:80])
            if len(hints) >= limit:
                return hints
    return hints


def _extract_topic_lines(text: str) -> list[str]:
    topics: list[str] = []
    for line in text.splitlines():
        candidate = _clean_discussion_topic(line)
        if len(candidate) >= 8:
            topics.append(candidate)
    if not topics:
        candidate = _clean_discussion_topic(text)
        if len(candidate) >= 8:
            topics.append(candidate)
    return topics


def generate_discussion_topic(rng: random.Random, difficulty: Difficulty) -> str:
    return generate_discussion_topics(rng, difficulty, count=1)[0]


def generate_discussion_topics(rng: random.Random, difficulty: Difficulty, count: int = 3) -> list[str]:
    key = _api_key()
    model = os.getenv("GEMINI_TOPIC_MODEL", os.getenv("GEMINI_TRANSLATION_MODEL", "gemini-2.5-flash")).strip()
    fallback = _fallback_discussion_topics(rng, count)
    if not key or not model:
        return fallback

    difficulty_hint = {
        "easy": "中学生にも理解でき、身近で話しやすいテーマ",
        "normal": "ネットや実生活でよく議論になり、幅広い人が自分ごととして意見を持ちやすいテーマ",
        "hard": "賛否が分かれやすく、自由、利便性、公平性、安全性の衝突を議論できるテーマ",
    }.get(difficulty, "ネットや実生活でよく議論になり、幅広い人が自分ごととして意見を持ちやすいテーマ")
    current_hints = _current_topic_hints(rng)
    current_hint_text = "\n".join(f"- {hint}" for hint in current_hints) if current_hints else "なし"
    payload = topic_generation_payload(
        difficulty_hint=difficulty_hint,
        count=count,
        current_hint_text=current_hint_text,
        random_seed=rng.randrange(1_000_000),
    )
    request = urllib.request.Request(
        GEMINI_REST_URL.format(model=quote(model, safe="")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("discussion_topic_generation_failed model=%s error=%s", model, exc)
        return fallback

    seen: set[str] = set()
    topics: list[str] = []
    for topic in _extract_topic_lines(_extract_generate_content_text(body)):
        if not _is_binary_discussion_topic(topic):
            logger.info("discussion_topic_rejected_non_binary topic=%s", topic)
            continue
        key_topic = _normalize_topic_key(topic)
        if key_topic in seen:
            continue
        seen.add(key_topic)
        topics.append(topic)
        if len(topics) >= count:
            return topics
    topics.extend(_fallback_discussion_topics(rng, count - len(topics), seen))
    return topics[:count]


def create_discussion_session(
    difficulty: Difficulty,
    voice_provider: VoiceProviderName,
    candidate_last_name: str,
    candidate_last_name_kana: str,
    discussion_topic: str | None = None,
    discussion_persona_key: str | None = None,
) -> tuple[InterviewSession, str]:
    rng = random.Random()
    topic = _clean_discussion_topic(discussion_topic or "") or generate_discussion_topic(rng, difficulty)
    persona_key = (discussion_persona_key or os.getenv("DISCUSSION_DEFAULT_PERSONA", "collaborative")).strip().lower()
    personas = discussion_personas()
    persona = personas.get(persona_key) or personas["collaborative"]
    persona_key = "logical" if persona_key == "logical" and "logical" in personas else "collaborative"
    interviewer = Interviewer(
        name="",
        personality=persona["personality"],
        speakingStyle=persona["speakingStyle"],
        imageKey=persona_key,
    )
    scenario = {
        "targetCompanyType": "ディスカッション練習",
        "seniority": "3分トライアル",
        "discussionTopic": topic,
        "discussionPersonaKey": persona_key,
        "discussionPersonaLabel": persona["label"],
        "interviewPhases": ["意見提示", "反対意見", "論点深掘り", "まとめ"],
        "mainConcerns": [
            "自分の立場を明確にできるか",
            "反対意見に対して冷静に応答できるか",
            "根拠と具体例を短く示せるか",
        ],
    }
    hidden_criteria = [
        "結論と理由を分けて話せる",
        "相手の論点を受けて返答できる",
        "反論時も感情的にならずに話せる",
        "時間内に要点をまとめられる",
    ]
    opening_question = (
        f"{candidate_last_name}さん、今回のお題は「{topic}」です。"
        "このお題に賛成か反対か、理由を聞かせてください？"
    )
    session = InterviewSession(
        interviewId=str(uuid.uuid4()),
        mode="discussion",
        jobRole="ディスカッション",
        difficulty=difficulty,
        voiceProvider=voice_provider,
        createdAt=datetime.now(timezone.utc).isoformat(),
        durationSec=180,
        candidateLastName=candidate_last_name,
        candidateLastNameKana=candidate_last_name_kana,
        interviewer=interviewer,
        scenario=scenario,
        evaluationRubric={
            "claimClarity": 0.24,
            "reasoning": 0.22,
            "listening": 0.18,
            "interruptionHandling": 0.18,
            "conciseness": 0.1,
            "tone": 0.08,
        },
        hiddenCriteria=hidden_criteria,
        messages=[{"role": "assistant", "content": opening_question}],
        maxTurns=4,
    )
    return session, opening_question


def record_interaction_metrics(session: InterviewSession, metrics: dict[str, Any]) -> None:
    event = metrics.get("event")
    if event == "ai_interruption":
        session.interactionMetrics["aiInterruptionCount"] = session.interactionMetrics.get("aiInterruptionCount", 0) + 1
    elif event == "post_interruption_continued":
        session.interactionMetrics["postInterruptionContinuedCount"] = (
            session.interactionMetrics.get("postInterruptionContinuedCount", 0) + 1
        )
    elif event == "post_interruption_stalled":
        session.interactionMetrics["postInterruptionStalledCount"] = (
            session.interactionMetrics.get("postInterruptionStalledCount", 0) + 1
        )
    elif event == "local_playback_overlap":
        session.interactionMetrics["localPlaybackOverlapCount"] = (
            session.interactionMetrics.get("localPlaybackOverlapCount", 0) + 1
        )
    elif event == "audio_summary":
        audio_payload = {
            key: metrics.get(key)
            for key in ("durationSec", "silenceSec", "speechRateWpm", "rmsAverage", "pitchVariance", "fillerCount", "fillers")
            if metrics.get(key) is not None
        }
        if audio_payload:
            existing = session.interactionMetrics.get("audio") or {}
            existing.update(audio_payload)
            session.interactionMetrics["audio"] = existing


DISCUSSION_RUBRIC_WEIGHTS: dict[str, float] = {
    "stanceClarity": 0.16,
    "reasoning": 0.18,
    "evidenceAndExamples": 0.12,
    "counterargumentHandling": 0.16,
    "listeningAndRelevance": 0.14,
    "structureAndConciseness": 0.10,
    "delivery": 0.08,
    "turnTakingResilience": 0.06,
}

DISCUSSION_RUBRIC_LABELS: dict[str, str] = {
    "stanceClarity": "立場の明確さ",
    "reasoning": "理由づけ",
    "evidenceAndExamples": "具体例・現実性",
    "counterargumentHandling": "反対意見への応答",
    "listeningAndRelevance": "聞く力・論点追従",
    "structureAndConciseness": "構成・簡潔さ",
    "delivery": "音声面",
    "turnTakingResilience": "ターンテイク・詰まり耐性",
}

DISCUSSION_MIN_SCORABLE_USER_CHARS = 20

STANCE_AFFIRMATIVE_MARKERS: tuple[str, ...] = (
    "賛成", "同意", "支持します", "そう思います", "そのとおり", "同感", "賛同",
)
STANCE_NEGATIVE_MARKERS: tuple[str, ...] = (
    "反対", "違うと思", "そう思いません", "賛成できません", "支持できません",
    "同意できません", "認めるべきではない", "認めるべきでない",
)
STANCE_CONDITIONAL_MARKERS: tuple[str, ...] = (
    "条件付き", "条件次第", "場合による", "状況による", "前提による",
    "ケースバイケース", "一概には",
)
REASON_MARKERS: tuple[str, ...] = (
    "なぜなら", "理由は", "例えば", "具体的", "だから", "ため", "ので", "つまり", "から", "結果として",
)
EVIDENCE_MARKERS: tuple[str, ...] = (
    "例えば", "具体", "ケース", "場面", "利用者", "費用", "コスト", "制度",
    "法律", "リスク", "事例", "体験", "データ", "統計", "実例", "実際",
)
CONCESSION_MARKERS: tuple[str, ...] = (
    "確かに", "一方で", "ただし", "とはいえ", "もちろん", "なるほど", "その点",
    "おっしゃる", "仰る", "それは確かに", "わかります",
)
STRUCTURE_MARKERS: tuple[str, ...] = (
    "まず", "次に", "最後に", "結論", "したがって", "そのため", "ポイントは",
)
QUESTION_MARKERS: tuple[str, ...] = (
    "?", "？", "どう思", "どう考", "教えてください", "ありますか",
    "いかがですか", "聞かせて", "どうですか", "どうでしょう",
)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = text.replace("　", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def count_markers(text: str, markers: tuple[str, ...] | list[str]) -> int:
    if not text:
        return 0
    total = 0
    for marker in markers:
        if not marker:
            continue
        total += text.count(marker)
    return total


def detect_stance(user_answers: list[str]) -> str:
    early = " ".join(user_answers[:3])
    if not early.strip():
        return "不明"
    has_conditional = any(marker in early for marker in STANCE_CONDITIONAL_MARKERS)
    has_affirmative = any(marker in early for marker in STANCE_AFFIRMATIVE_MARKERS)
    has_negative = any(marker in early for marker in STANCE_NEGATIVE_MARKERS)
    if has_conditional and (has_affirmative or has_negative):
        return "条件付き"
    if has_affirmative and not has_negative:
        return "賛成"
    if has_negative and not has_affirmative:
        return "反対"
    if has_affirmative and has_negative:
        return "条件付き"
    if has_conditional:
        return "条件付き"
    return "不明"


def _question_response_pairs(session: InterviewSession) -> tuple[int, int]:
    messages = session.messages
    questions = 0
    responded = 0
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        is_question = any(marker in content for marker in QUESTION_MARKERS)
        if not is_question:
            continue
        questions += 1
        for follow in messages[index + 1 :]:
            if follow.get("role") == "user":
                if len((follow.get("content") or "").strip()) >= 5:
                    responded += 1
                break
            if follow.get("role") == "assistant":
                continue
    return questions, responded


def _score_level(score: float) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 55:
        return "fair"
    return "needs_work"


def _score_stance_clarity(user_answers: list[str], stance: str) -> dict[str, Any]:
    if not user_answers:
        return {
            "score": 30,
            "evidence": ["ユーザー発話が記録されていません。"],
            "advice": "最初の発話で立場を一文で表明することから始めましょう。",
        }
    first = user_answers[0]
    early = " ".join(user_answers[:2])
    if stance in ("賛成", "反対"):
        markers = STANCE_AFFIRMATIVE_MARKERS if stance == "賛成" else STANCE_NEGATIVE_MARKERS
        if any(marker in first for marker in markers):
            return {
                "score": 88,
                "evidence": [f"最初の発話で「{stance}」の立場を明示できていました。"],
                "advice": "結論ファーストの型を続けると安定します。",
            }
        return {
            "score": 70,
            "evidence": [f"立場は「{stance}」と読み取れますが、明示はやや遅れていました。"],
            "advice": "冒頭の一文で立場を言い切ると、議論に入りやすくなります。",
        }
    if stance == "条件付き":
        if any(marker in early for marker in STANCE_CONDITIONAL_MARKERS):
            return {
                "score": 72,
                "evidence": ["条件付きの立場を明示できていました。"],
                "advice": "「Aなら賛成、Bなら反対」のように、条件と結論を一文にまとめると伝わりやすくなります。",
            }
        return {
            "score": 58,
            "evidence": ["賛成寄り・反対寄りが混在し、立場が固まらないまま議論が進んだ可能性があります。"],
            "advice": "条件付きで答える場合も、最初の一文に立場を入れると聞き手が追いやすくなります。",
        }
    return {
        "score": 38,
        "evidence": ["前半の発話で賛成・反対の明示が見つかりませんでした。"],
        "advice": "冒頭で「賛成です」「反対です」と一文で立場を述べると、議論が一気に進めやすくなります。",
    }


def _score_reasoning(joined_user: str, reason_count: int) -> dict[str, Any]:
    if not joined_user.strip():
        return {
            "score": 30,
            "evidence": ["ユーザー発話が空のため、理由づけを評価できませんでした。"],
            "advice": "主張の後に「なぜなら」を一文添えるだけでも説得力が変わります。",
        }
    if reason_count >= 3:
        return {
            "score": 86,
            "evidence": [f"「なぜなら」「理由」「例えば」などの理由表現を{reason_count}回使えていました。"],
            "advice": "理由の数を増やすより、1つを深掘りすると説得力がさらに上がります。",
        }
    if reason_count == 2:
        return {
            "score": 74,
            "evidence": ["主張に対して理由を複数回添えられていました。"],
            "advice": "理由の後に「具体的には」を続けて、根拠を一段深掘りしてみましょう。",
        }
    if reason_count == 1:
        return {
            "score": 60,
            "evidence": ["理由づけは確認できましたが、回数は限定的でした。"],
            "advice": "主張を出すたびに、理由を一文添える型に揃えるとブレが減ります。",
        }
    return {
        "score": 38,
        "evidence": ["「なぜなら」「理由は」「例えば」などの理由表現が確認できませんでした。"],
        "advice": "主張のあとに「なぜなら〜」を一文添えるだけでも、議論の重みが大きく変わります。",
    }


def _score_evidence(joined_user: str, evidence_count: int) -> dict[str, Any]:
    has_digit = any(ch.isdigit() for ch in joined_user)
    if evidence_count >= 3 or (evidence_count >= 2 and has_digit):
        notes = ["具体例・場面・現実的な要素を複数回提示できていました。"]
        if has_digit:
            notes.append("数値表現を含む説明がありました。")
        return {
            "score": 84,
            "evidence": notes,
            "advice": "具体例を一つだけ詳しく描写すると、さらに説得力が増します。",
        }
    if evidence_count >= 1 or has_digit:
        notes: list[str] = []
        if has_digit:
            notes.append("数字を含む発話がありました。")
        if evidence_count >= 1:
            notes.append("具体的な状況や場面の言及がありました。")
        return {
            "score": 68,
            "evidence": notes or ["具体的な要素はあったものの量は限定的でした。"],
            "advice": "「実際に〜」「例えば〜」で1つ場面を描写すると、抽象論を超えやすくなります。",
        }
    return {
        "score": 40,
        "evidence": ["数字、事例、場面など、具体的な要素が確認できませんでした。"],
        "advice": "「例えば」「実際には」で、誰のどんな場面かを描写すると、現実感が伝わります。",
    }


def _score_counterargument(joined_user: str, concession_count: int) -> dict[str, Any]:
    if concession_count >= 2:
        return {
            "score": 84,
            "evidence": ["「確かに」「一方で」など、相手の論点を受け止める表現が複数回ありました。"],
            "advice": "受け止めたあとに自分の論点をもう一段深掘りすると、議論が立体的になります。",
        }
    if concession_count == 1:
        return {
            "score": 68,
            "evidence": ["相手の論点を一度受け止めてから返す場面がありました。"],
            "advice": "「確かに〜、ただし〜」の型を毎ターン意識すると、反論がやわらかくなります。",
        }
    return {
        "score": 42,
        "evidence": ["相手の意見を受け止める譲歩表現が確認できませんでした。"],
        "advice": "反論前に「確かにその点は」「一方で」を一言足すと、議論が攻撃的にならず議論が進みます。",
    }


def _score_listening(session: InterviewSession) -> dict[str, Any]:
    questions, responded = _question_response_pairs(session)
    if questions == 0:
        return {
            "score": 60,
            "evidence": ["相手からの明確な問いかけが少なく、評価対象が限定的でした。"],
            "advice": "問いを受けたら、まず一文で答えてから自分の論点を述べる型を意識しましょう。",
        }
    ratio = responded / questions
    if ratio >= 0.9:
        return {
            "score": 86,
            "evidence": [f"{questions}回の問いに対して{responded}回応答しており、論点を追えていました。"],
            "advice": "応答後に「その上で」「逆に」と論点を広げると、会話を主導しやすくなります。",
        }
    if ratio >= 0.6:
        return {
            "score": 70,
            "evidence": [f"{questions}回の問いに対して{responded}回応答できていました。"],
            "advice": "問いに対しては、まず一文で答えてから自分の論点を続ける型を意識すると安定します。",
        }
    return {
        "score": 48,
        "evidence": [f"{questions}回の問いのうち、応答が確認できたのは{responded}回でした。"],
        "advice": "問いを受けたら、最初の一文は問いに対する直接の答えに使うと、論点逸脱が減ります。",
    }


def _score_structure(user_answers: list[str], joined_user: str) -> dict[str, Any]:
    turn_count = len(user_answers)
    if turn_count == 0:
        return {
            "score": 30,
            "evidence": ["ユーザー発話が空のため、構成を評価できませんでした。"],
            "advice": "まずは結論を一文で出すところから始めましょう。",
        }
    char_count = sum(len(text) for text in user_answers)
    avg_turn = char_count / turn_count if turn_count else 0
    structure_count = count_markers(joined_user, STRUCTURE_MARKERS)
    evidence: list[str] = []
    if avg_turn < 25:
        score = 50
        evidence.append(f"1発話の平均が約{int(avg_turn)}文字と短く、結論だけで止まりやすい傾向でした。")
        advice = "結論の後に理由を一文添えると、議論として成立しやすくなります。"
    elif avg_turn > 240:
        score = 55
        evidence.append(f"1発話の平均が約{int(avg_turn)}文字と長く、論点が広がりやすい傾向でした。")
        advice = "発話を結論+理由+補足の3文以内にまとめると、聞き手が論点を追いやすくなります。"
    else:
        score = 74
        evidence.append(f"1発話の平均は約{int(avg_turn)}文字で、議論に適した長さでした。")
        advice = "「結論→理由→補足」の3文構造を意識すると、さらに安定します。"
    if structure_count >= 2:
        score = min(score + 10, 90)
        evidence.append("「まず」「結論」「したがって」など構成を示す語が複数回ありました。")
    return {"score": score, "evidence": evidence, "advice": advice}


def _has_scorable_discussion_input(user_answers: list[str]) -> bool:
    return sum(len(text) for text in user_answers) >= DISCUSSION_MIN_SCORABLE_USER_CHARS


def _score_delivery(audio_stats: dict[str, Any], user_answers: list[str]) -> dict[str, Any]:
    if not _has_scorable_discussion_input(user_answers):
        return {
            "score": 45,
            "evidence": ["評価できるユーザー発話量が不足しているため、音声面は判定できませんでした。"],
            "advice": "採点前に、立場と理由を含む発話を少なくとも一度行ってください。",
        }
    if not audio_stats.get("available"):
        return {
            "score": 60,
            "evidence": ["音声メトリクスは今回取得できませんでした。"],
            "advice": "無音比率や話速など、音声面のメトリクスが取得できると、より精緻な分析が可能になります。",
        }
    observations = list(audio_stats.get("observations") or [])
    score = max(40, 90 - len(observations) * 10)
    return {
        "score": score,
        "evidence": observations or ["音声面で大きな崩れは見られませんでした。"],
        "advice": "結論前に半拍の間を置くと、抑揚と聞き取りやすさが両立します。",
    }


def _score_turn_taking(session: InterviewSession, audio_stats: dict[str, Any], user_answers: list[str]) -> dict[str, Any]:
    if not _has_scorable_discussion_input(user_answers):
        return {
            "score": 45,
            "evidence": ["評価できるユーザー発話量が不足しているため、ターンの受け渡しは判定できませんでした。"],
            "advice": "相手の問いに対して、まず一度は自分の立場と理由を話してから終了してください。",
        }
    interactions = session.interactionMetrics or {}
    interruption_count = int(interactions.get("aiInterruptionCount") or 0)
    continued = int(interactions.get("postInterruptionContinuedCount") or 0)
    stalled = int(interactions.get("postInterruptionStalledCount") or 0)
    long_thinking = int(audio_stats.get("longThinkingPauseCount") or 0)
    in_turn_pauses = int(audio_stats.get("inTurnLongSilenceCount") or 0)
    overlap_count = int(audio_stats.get("overlapCount") or 0)
    if overlap_count > 0:
        return {
            "score": 68,
            "evidence": [f"相手の発話中に話し始めた可能性がある場面が{overlap_count}回ありました。"],
            "advice": "相槌と回答を分け、相手の発話を止めたい時だけ短く入るとターンの意図が伝わりやすくなります。",
        }
    if interruption_count == 0:
        if in_turn_pauses > 0:
            return {
                "score": 58,
                "evidence": [f"回答中に長めの沈黙が{in_turn_pauses}回ありました。"],
                "advice": "考えが止まりそうな場面では、結論だけ先に置いてから補足を続けると会話の流れが保ちやすくなります。",
            }
    if long_thinking > 0:
        return {
            "score": 66,
            "evidence": [f"回答前に5秒以上考える場面が{long_thinking}回ありました。"],
            "advice": "考える時間が必要な時は「少し考えます」と一言置くと、沈黙も会話の一部として自然に扱えます。",
        }
        return {
            "score": 78,
            "evidence": ["割り込みや長い詰まりは目立たず、ターンの受け渡しは概ね安定していました。"],
            "advice": "今の受け答えのテンポを保ちつつ、長く説明する場面だけ要点を先に置くとさらに安定します。",
        }
    if continued >= interruption_count and stalled == 0:
        return {
            "score": 86,
            "evidence": [f"{interruption_count}回の割り込みに対し、すべて発話を継続できていました。"],
            "advice": "割り込み後の最初の一文を「ありがとうございます。一方で」のように型化するとさらに安定します。",
        }
    if continued > 0:
        return {
            "score": 66,
            "evidence": [f"割り込み{interruption_count}回中、継続できたのは{continued}回でした。"],
            "advice": "割り込み後は、深呼吸→相手の主旨を一言で言い直す→自分の論点に戻る、の3手で安定します。",
        }
    return {
        "score": 45,
        "evidence": [f"割り込み{interruption_count}回後に発話が止まる場面が{stalled}回ありました。"],
        "advice": "割り込みを止まりに変えないために、短く受け止めるテンプレを一つ用意しておきましょう。",
    }


def _compute_turn_taking_stats(
    timeline_events: list[dict[str, Any]],
    audio_buckets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    if not timeline_events:
        return [], 0
    events = sorted(timeline_events, key=lambda e: e.get("atSec") or 0)
    user_turns: list[dict[str, Any]] = []
    overlap_count = 0
    assistant_speaking = False
    overlap_recorded_in_segment = False
    last_assistant_turn_complete_at: float | None = None
    pending_user_turn: dict[str, Any] | None = None
    finish_at: float | None = None
    assistant_intervals: list[tuple[float, float]] = []
    assistant_interval_start: float | None = None

    def is_voiced_bucket(bucket: dict[str, Any]) -> bool:
        frames = int(bucket.get("frames") or 0)
        if frames <= 0:
            return False
        voiced = int(bucket.get("voicedFrames") or 0)
        voiced_ratio = voiced / frames
        rms_avg = float(bucket.get("rmsAvg") or 0)
        rms_max = float(bucket.get("rmsMax") or 0)
        peak = float(bucket.get("peak") or 0)
        return (
            voiced_ratio >= 0.25
            or (voiced >= 3 and rms_max >= 240)
            or (voiced >= 1 and rms_max >= 120)
            or rms_avg >= 140
            or rms_avg >= 60
            or peak >= 1800
            or peak >= 450
        )

    def bucket_start(bucket: dict[str, Any]) -> float:
        return float(bucket.get("startSec") or 0.0)

    def bucket_end(bucket: dict[str, Any]) -> float:
        start = bucket_start(bucket)
        return float(bucket.get("endSec") or start + 1.0)

    def voiced_buckets_between(after_sec: float, before_sec: float) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for bucket in audio_buckets:
            start_sec = bucket.get("startSec")
            if start_sec is None:
                continue
            start = bucket_start(bucket)
            end = bucket_end(bucket)
            if end < after_sec or start > before_sec:
                continue
            if is_voiced_bucket(bucket):
                candidates.append(bucket)
        return candidates

    def estimate_user_speech_start(after_sec: float, before_sec: float) -> float | None:
        candidates = voiced_buckets_between(after_sec, before_sec)
        if not candidates:
            return None
        first = min(candidates, key=bucket_start)
        return max(after_sec, bucket_start(first))

    def estimate_user_speech_end(start_sec: float, before_sec: float) -> float | None:
        candidates = voiced_buckets_between(start_sec, before_sec)
        if not candidates:
            return None
        last = max(candidates, key=bucket_end)
        return min(before_sec, bucket_end(last))

    def overlaps_assistant(start_sec: float, end_sec: float) -> bool:
        for assistant_start, assistant_end in assistant_intervals:
            if start_sec < assistant_end and end_sec > assistant_start:
                return True
        return False

    for event in events:
        event_type = event.get("type")
        at_sec = event.get("atSec")
        if at_sec is None:
            continue
        at_sec = float(at_sec)
        if event_type == "finish_requested":
            finish_at = at_sec
            continue
        if event_type == "assistant_response_started":
            assistant_speaking = True
            overlap_recorded_in_segment = False
            assistant_interval_start = at_sec if assistant_interval_start is None else min(assistant_interval_start, at_sec)
            if pending_user_turn is not None and pending_user_turn.get("endAt") is None:
                pending_user_turn["endAt"] = at_sec
                user_turns.append(pending_user_turn)
                pending_user_turn = None
        elif event_type == "assistant_transcript":
            assistant_speaking = True
            assistant_interval_start = at_sec if assistant_interval_start is None else min(assistant_interval_start, at_sec)
            if pending_user_turn is not None and pending_user_turn.get("endAt") is None:
                pending_user_turn["endAt"] = at_sec
                user_turns.append(pending_user_turn)
                pending_user_turn = None
        elif event_type == "assistant_turn_complete":
            assistant_speaking = False
            if assistant_interval_start is not None and at_sec > assistant_interval_start:
                assistant_intervals.append((assistant_interval_start, at_sec))
                assistant_interval_start = None
            last_assistant_turn_complete_at = at_sec
        elif event_type == "user_transcript":
            if pending_user_turn is None:
                response_latency: float | None = None
                start_at = at_sec
                latency_source = "transcript"
                if last_assistant_turn_complete_at is not None:
                    inferred_start_at = estimate_user_speech_start(last_assistant_turn_complete_at, at_sec)
                    if inferred_start_at is not None:
                        start_at = inferred_start_at
                        latency_source = "audio"
                    response_latency = max(0.0, start_at - last_assistant_turn_complete_at)
                pending_user_turn = {
                    "startAt": start_at,
                    "transcriptAt": at_sec,
                    "latencySource": latency_source,
                    "responseLatencySec": round(response_latency, 2) if response_latency is not None else None,
                }
            if assistant_speaking and not overlap_recorded_in_segment:
                pending_user_turn["overlapByTranscript"] = True
                overlap_recorded_in_segment = True

    if pending_user_turn is not None:
        if finish_at is not None:
            pending_user_turn["endAt"] = finish_at
        elif audio_buckets:
            pending_user_turn["endAt"] = audio_buckets[-1].get("endSec", pending_user_turn["startAt"])
        else:
            pending_user_turn["endAt"] = pending_user_turn["startAt"]
        user_turns.append(pending_user_turn)

    for index, turn in enumerate(user_turns):
        turn["index"] = index
        start = float(turn.get("startAt") or 0.0)
        end = float(turn.get("endAt") or start)
        inferred_end = estimate_user_speech_end(start, end)
        if inferred_end is not None:
            end = max(start, inferred_end)
        turn["startAt"] = round(start, 2)
        turn["endAt"] = round(end, 2)
        turn["userTurnDurationSec"] = round(max(0.0, end - start), 2)
        if overlaps_assistant(start, end):
            turn["overlapWithAssistant"] = True
        window_start = start
        window_end = end
        transcript_at = turn.get("transcriptAt")
        if window_end - window_start < 0.75 and transcript_at is not None:
            center = float(transcript_at)
            window_start = max(0.0, min(window_start, center - 0.75))
            window_end = max(window_end, center + 0.75)
        in_window = [
            bucket
            for bucket in audio_buckets
            if bucket.get("startSec") is not None
            and float(bucket.get("endSec") or bucket["startSec"]) >= window_start
            and float(bucket["startSec"]) <= window_end
        ]
        if in_window:
            frames = sum(int(bucket.get("frames") or 0) for bucket in in_window)
            voiced = sum(int(bucket.get("voicedFrames") or 0) for bucket in in_window)
            turn["speechActivityRatio"] = round(voiced / frames, 3) if frames else 0.0
            long_pauses = 0
            run_len = 0
            if end - start >= 4:
                for bucket in in_window:
                    silent = int(bucket.get("silentFrames") or 0)
                    total = int(bucket.get("frames") or 0)
                    if total > 0 and (silent / total) > 0.9:
                        run_len += 1
                    else:
                        if run_len >= 4:
                            long_pauses += 1
                        run_len = 0
                if run_len >= 4:
                    long_pauses += 1
            turn["longPauseCount"] = long_pauses

    overlap_count = sum(
        1
        for turn in user_turns
        if turn.get("overlapWithAssistant") or turn.get("overlapByTranscript")
    )

    return user_turns, overlap_count


UNCERTAINTY_MARKERS: tuple[str, ...] = (
    "たぶん", "多分", "おそらく", "かもしれ", "気がします", "と思います",
    "かな", "ちょっと", "なんか", "うーん", "えー", "えっと", "あの",
)
ASSERTIVE_MARKERS: tuple[str, ...] = (
    "絶対", "必ず", "当然", "明らか", "べきです", "べきだ", "必要です",
    "必要だ", "間違い", "ありえない", "強く", "絶対に",
)


def _signal_level(score: int) -> str:
    if score >= 75:
        return "high"
    if score >= 45:
        return "medium"
    return "low"


def _signal(
    key: str,
    label: str,
    score: int,
    evidence: list[str],
    advice: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "level": _signal_level(score),
        "score": max(0, min(100, int(score))),
        "evidence": evidence,
        "advice": advice,
    }


def _analyze_delivery_signals(
    session: InterviewSession,
    audio_stats: dict[str, Any],
    user_turns: list[dict[str, Any]],
    raw_audio: dict[str, Any],
    audio_buckets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    user_answers = get_scoring_user_turns(session)
    joined_user = "\n".join(user_answers)
    char_count = sum(len(text) for text in user_answers)
    turn_count = len(user_answers)
    avg_chars = char_count / turn_count if turn_count else 0
    durations = [
        float(turn.get("userTurnDurationSec") or 0)
        for turn in user_turns
        if float(turn.get("userTurnDurationSec") or 0) > 0
    ]
    total_duration = sum(durations)
    chars_per_sec = char_count / total_duration if total_duration > 0 and char_count else 0
    response_latency_avg = float(audio_stats.get("responseLatencyAvgSec") or 0)
    long_thinking = int(audio_stats.get("longThinkingPauseCount") or 0)
    in_turn_pauses = int(audio_stats.get("inTurnLongSilenceCount") or 0)
    filler_count = int(audio_stats.get("fillerCount") or 0)
    speech_ratios = [
        float(turn.get("speechActivityRatio") or 0)
        for turn in user_turns
        if turn.get("speechActivityRatio") is not None
    ]
    speech_activity_avg = sum(speech_ratios) / len(speech_ratios) if speech_ratios else 0
    rms_avg = float(raw_audio.get("rmsAverage") or audio_stats.get("rmsAverage") or 0)
    rms_max = max((float(bucket.get("rmsMax") or 0) for bucket in audio_buckets), default=0.0)
    peak_max = max((float(bucket.get("peak") or 0) for bucket in audio_buckets), default=0.0)
    uncertainty_count = count_markers(joined_user, UNCERTAINTY_MARKERS)
    assertive_count = count_markers(joined_user, ASSERTIVE_MARKERS)
    reason_count = count_markers(joined_user, REASON_MARKERS)

    signals: list[dict[str, Any]] = []

    if char_count < DISCUSSION_MIN_SCORABLE_USER_CHARS:
        return [
            _signal(
                "insufficient_speech",
                "話し方を判定する発話量が不足",
                45,
                [f"採点対象のユーザー発話は{char_count}文字でした。"],
                "話し方の傾向を見るには、立場と理由を含む発話を少なくとも一度行ってください。",
            )
        ]

    rushed_score = 0
    rushed_evidence: list[str] = []
    if chars_per_sec >= 8:
        rushed_score += 35
        rushed_evidence.append(f"文字起こし上の発話密度が約{chars_per_sec:.1f}文字/秒でした。")
    if speech_activity_avg >= 0.72:
        rushed_score += 25
        rushed_evidence.append("発話中の有音比率が高く、間が少ない傾向があります。")
    if filler_count >= 2:
        rushed_score += 15
        rushed_evidence.append(f"フィラーが{filler_count}回検出されています。")
    if rushed_score >= 35:
        signals.append(
            _signal(
                "rushed",
                "焦っているように聞こえる可能性",
                rushed_score,
                rushed_evidence,
                "結論を先に短く置き、理由の前に一拍置くと落ち着いた印象になります。",
            )
        )

    assertive_score = 0
    assertive_evidence: list[str] = []
    if rms_avg >= 900 or rms_max >= 6000 or peak_max >= 22000:
        assertive_score += 30
        assertive_evidence.append("声量またはピーク音量が比較的高めでした。")
    if assertive_count >= 2:
        assertive_score += 30
        assertive_evidence.append(f"断定的な表現が{assertive_count}回見られました。")
    if avg_chars <= 25 and assertive_count > 0:
        assertive_score += 15
        assertive_evidence.append("短い発話の中に強い断定表現が含まれています。")
    if assertive_score >= 35:
        signals.append(
            _signal(
                "strong_assertion",
                "強い主張に聞こえる可能性",
                assertive_score,
                assertive_evidence,
                "反対意見を受ける場面では「一方で」と添えると、強さを保ちながら柔らかく伝わります。",
            )
        )

    disengaged_score = 0
    disengaged_evidence: list[str] = []
    if turn_count >= 2 and avg_chars <= 18:
        disengaged_score += 35
        disengaged_evidence.append(f"平均発話文字数が{avg_chars:.0f}文字と短めでした。")
    if rms_avg > 0 and rms_avg <= 180:
        disengaged_score += 25
        disengaged_evidence.append("声量が全体的に小さめでした。")
    if reason_count == 0 and turn_count >= 2:
        disengaged_score += 20
        disengaged_evidence.append("理由を示す表現がほとんど見られませんでした。")
    if disengaged_score >= 35:
        signals.append(
            _signal(
                "low_engagement",
                "関心が薄そうに聞こえる可能性",
                disengaged_score,
                disengaged_evidence,
                "短くてもよいので「理由は一つあります」と足すと、参加姿勢が伝わりやすくなります。",
            )
        )

    confidence_score = 0
    confidence_evidence: list[str] = []
    if response_latency_avg >= 3:
        confidence_score += 25
        confidence_evidence.append(f"回答開始までの平均が約{response_latency_avg:.1f}秒でした。")
    if long_thinking > 0 or in_turn_pauses > 0:
        confidence_score += 25
        confidence_evidence.append(
            f"回答前後の長めの沈黙が合計{long_thinking + in_turn_pauses}回ありました。"
        )
    if uncertainty_count >= 3:
        confidence_score += 25
        confidence_evidence.append(f"曖昧・保留の表現が{uncertainty_count}回見られました。")
    if filler_count >= 2:
        confidence_score += 15
        confidence_evidence.append(f"フィラーが{filler_count}回検出されています。")
    if confidence_score >= 35:
        signals.append(
            _signal(
                "low_confidence",
                "自信がなさそうに聞こえる可能性",
                confidence_score,
                confidence_evidence,
                "迷う場面では「結論から言うと」と先に置き、補足で迷いを説明すると安定します。",
            )
        )

    if not signals:
        signals.append(
            _signal(
                "stable",
                "大きなネガティブ兆候は少なめ",
                20,
                ["話速、沈黙、声量、表現のいずれも大きく崩れていません。"],
                "今の安定感を保ちつつ、理由と具体例を一つずつ足すと説得力が増します。",
            )
        )
    return signals


def _extract_audio_stats(session: InterviewSession) -> dict[str, Any]:
    metrics = session.interactionMetrics or {}
    raw_audio = metrics.get("audio") or {}
    timeline_events = metrics.get("timelineEvents") or []
    audio_buckets = metrics.get("audioBuckets") or []

    if not raw_audio and not timeline_events and not audio_buckets:
        return {"available": False, "observations": []}

    user_turns, overlap_count = _compute_turn_taking_stats(timeline_events, audio_buckets)
    local_playback_overlap_count = int(metrics.get("localPlaybackOverlapCount") or 0)
    overlap_count += local_playback_overlap_count

    response_latencies = [
        float(turn["responseLatencySec"])
        for turn in user_turns
        if turn.get("responseLatencySec") is not None
    ]
    response_latency_avg = (
        sum(response_latencies) / len(response_latencies) if response_latencies else None
    )
    response_latency_max = max(response_latencies) if response_latencies else None
    long_thinking_pause_count = sum(1 for latency in response_latencies if latency >= 5)
    in_turn_long_silence_count = sum(int(turn.get("longPauseCount") or 0) for turn in user_turns)

    observations: list[str] = []
    if response_latency_avg is not None and response_latency_avg >= 6:
        observations.append(
            f"AIの問いかけ後、回答開始まで平均約{response_latency_avg:.1f}秒でした。"
            "考える間は自然ですが、最初に「少し考えます」と添えると会話が安定します。"
        )
    if long_thinking_pause_count > 0:
        observations.append(f"回答前に5秒以上考える場面が{long_thinking_pause_count}回ありました。")
    if in_turn_long_silence_count > 0:
        observations.append(
            f"回答中に長めの沈黙が{in_turn_long_silence_count}回ありました。"
            "結論だけ先に置くと聞き手が追いやすくなります。"
        )
    if overlap_count > 0:
        observations.append(f"相手の発話中に話し始めた可能性がある場面が{overlap_count}回ありました。")
    if not observations:
        observations.append("ターンテイキングは大きく崩れていません。")

    filler_count = int(raw_audio.get("fillerCount") or len(raw_audio.get("fillers") or []))

    stats: dict[str, Any] = {
        "available": bool(raw_audio.get("available")) or bool(user_turns) or bool(audio_buckets),
        "observations": observations,
    }
    if raw_audio.get("observedDurationSec") is not None:
        stats["observedDurationSec"] = raw_audio["observedDurationSec"]
    if raw_audio.get("rmsAverage") is not None:
        stats["rmsAverage"] = raw_audio["rmsAverage"]
    if raw_audio:
        stats["fillerCount"] = filler_count
    if response_latency_avg is not None:
        stats["responseLatencyAvgSec"] = round(response_latency_avg, 2)
    if response_latency_max is not None:
        stats["responseLatencyMaxSec"] = round(response_latency_max, 2)
    if user_turns:
        stats["longThinkingPauseCount"] = long_thinking_pause_count
        stats["inTurnLongSilenceCount"] = in_turn_long_silence_count
        stats["overlapCount"] = overlap_count
        stats["turnTakingStats"] = {
            "userTurns": [
                {
                    key: value
                    for key, value in turn.items()
                    if key in {
                        "index",
                        "responseLatencySec",
                        "latencySource",
                        "startAt",
                        "endAt",
                        "transcriptAt",
                        "userTurnDurationSec",
                        "longPauseCount",
                        "speechActivityRatio",
                        "overlapWithAssistant",
                    }
                    and value is not None
                }
                for turn in user_turns
            ]
        }
    stats["emotionSignals"] = _analyze_delivery_signals(
        session,
        stats,
        user_turns,
        raw_audio,
        audio_buckets,
    )
    return stats


def _build_discussion_timeline(session: InterviewSession, audio_stats: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    audio_user_turns = (((audio_stats or {}).get("turnTakingStats") or {}).get("userTurns") or [])
    user_audio_index = 0
    for message in session.messages:
        role = message.get("role")
        if role not in {"assistant", "user"}:
            continue
        content = (message.get("content") or "").strip()
        if not content:
            continue
        excerpt = content if len(content) <= 80 else content[:80] + "…"
        entry: dict[str, Any] = {"role": role, "excerpt": excerpt}
        if role == "user":
            if user_audio_index < len(audio_user_turns):
                timing = audio_user_turns[user_audio_index]
                user_audio_index += 1
                if timing.get("startAt") is not None:
                    entry["audioStartSec"] = timing["startAt"]
                if timing.get("endAt") is not None:
                    entry["audioEndSec"] = timing["endAt"]
                if timing.get("userTurnDurationSec") is not None:
                    entry["audioDurationSec"] = timing["userTurnDurationSec"]
                if timing.get("responseLatencySec") is not None:
                    entry["responseLatencySec"] = timing["responseLatencySec"]
                if timing.get("speechActivityRatio") is not None:
                    entry["speechActivityRatio"] = timing["speechActivityRatio"]
                if timing.get("longPauseCount") is not None:
                    entry["longPauseCount"] = timing["longPauseCount"]
            tags: list[str] = []
            if any(marker in content for marker in STANCE_AFFIRMATIVE_MARKERS):
                tags.append("賛成表明")
            if any(marker in content for marker in STANCE_NEGATIVE_MARKERS):
                tags.append("反対表明")
            if any(marker in content for marker in REASON_MARKERS):
                tags.append("理由")
            if any(marker in content for marker in CONCESSION_MARKERS):
                tags.append("譲歩")
            if any(marker in content for marker in EVIDENCE_MARKERS):
                tags.append("具体")
            if tags:
                entry["analysisNote"] = "/".join(tags)
        timeline.append(entry)
    return timeline


def get_scoring_user_turns(session: InterviewSession) -> list[str]:
    metrics = session.interactionMetrics or {}
    final_transcript = metrics.get("finalTranscript") or {}
    if final_transcript.get("available"):
        turns = final_transcript.get("userTurns")
        if isinstance(turns, list):
            cleaned = [normalize_text(text) for text in turns if isinstance(text, str)]
            cleaned = [text for text in cleaned if text]
            if cleaned:
                return cleaned
        user_text = final_transcript.get("userText")
        if isinstance(user_text, str) and user_text.strip():
            return [normalize_text(user_text)]
    fallback = [
        normalize_text(m.get("content"))
        for m in session.messages
        if m.get("role") == "user"
    ]
    return [text for text in fallback if text]


def summarize_transcript_stats(session: InterviewSession) -> dict[str, Any]:
    user_answers = get_scoring_user_turns(session)
    joined_user = "\n".join(user_answers)
    char_count = sum(len(text) for text in user_answers)
    avg_turn = char_count / len(user_answers) if user_answers else 0
    _, responded = _question_response_pairs(session)
    return {
        "userTurnCount": len(user_answers),
        "userCharCount": char_count,
        "averageUserTurnChars": int(round(avg_turn)),
        "stanceDetected": detect_stance(user_answers),
        "reasonMarkerCount": count_markers(joined_user, REASON_MARKERS),
        "exampleMarkerCount": count_markers(joined_user, EVIDENCE_MARKERS),
        "concessionMarkerCount": count_markers(joined_user, CONCESSION_MARKERS),
        "questionResponseCount": responded,
    }


def score_discussion(session: InterviewSession) -> dict[str, Any]:
    user_answers = get_scoring_user_turns(session)
    joined_user = "\n".join(user_answers)
    stance = detect_stance(user_answers)
    reason_count = count_markers(joined_user, REASON_MARKERS)
    evidence_count = count_markers(joined_user, EVIDENCE_MARKERS)
    concession_count = count_markers(joined_user, CONCESSION_MARKERS)
    audio_stats = _extract_audio_stats(session)

    scorers: list[tuple[str, dict[str, Any]]] = [
        ("stanceClarity", _score_stance_clarity(user_answers, stance)),
        ("reasoning", _score_reasoning(joined_user, reason_count)),
        ("evidenceAndExamples", _score_evidence(joined_user, evidence_count)),
        ("counterargumentHandling", _score_counterargument(joined_user, concession_count)),
        ("listeningAndRelevance", _score_listening(session)),
        ("structureAndConciseness", _score_structure(user_answers, joined_user)),
        ("delivery", _score_delivery(audio_stats, user_answers)),
        ("turnTakingResilience", _score_turn_taking(session, audio_stats, user_answers)),
    ]
    rubric_scores: list[dict[str, Any]] = []
    for key, result in scorers:
        raw_score = max(0, min(100, int(round(result.get("score", 0)))))
        rubric_scores.append(
            {
                "key": key,
                "label": DISCUSSION_RUBRIC_LABELS[key],
                "score": raw_score,
                "weight": DISCUSSION_RUBRIC_WEIGHTS[key],
                "level": _score_level(raw_score),
                "evidence": result.get("evidence", []),
                "advice": result.get("advice", ""),
            }
        )

    transcript_stats = summarize_transcript_stats(session)
    timeline = _build_discussion_timeline(session, audio_stats)
    sorted_items = sorted(rubric_scores, key=lambda item: item["score"], reverse=True)
    top = sorted_items[0] if sorted_items else None
    weakest = sorted_items[-1] if sorted_items else None
    if top and weakest and top["key"] != weakest["key"]:
        overall_comment = (
            f"全体として「{top['label']}」が比較的安定しており、「{weakest['label']}」が伸びしろです。"
            f"立場の検出は「{transcript_stats['stanceDetected']}」、"
            f"理由表現{transcript_stats['reasonMarkerCount']}回、"
            f"具体表現{transcript_stats['exampleMarkerCount']}回、"
            f"譲歩表現{transcript_stats['concessionMarkerCount']}回でした。"
        )
    else:
        overall_comment = (
            "発話量が限定的だったため、項目ごとの差は小さい結果になりました。"
            "次回は結論と理由を一文ずつ添えるところから整えていきましょう。"
        )

    final_transcript = (session.interactionMetrics or {}).get("finalTranscript") or {}
    transcript_source = final_transcript.get("source") if final_transcript else None
    if not transcript_source:
        transcript_source = "realtime_fallback"
    transcript_quality = {
        "segmentCount": int(final_transcript.get("segmentCount") or 0),
        "completedCount": int(final_transcript.get("completedCount") or 0),
        "pendingCount": int(final_transcript.get("pendingCount") or 0),
        "errorCount": int(final_transcript.get("errorCount") or 0),
    }

    return {
        "mode": "discussion",
        "overallComment": overall_comment,
        "rubricScores": rubric_scores,
        "transcriptStats": transcript_stats,
        "audioStats": audio_stats,
        "timeline": timeline,
        "transcriptSource": transcript_source,
        "transcriptQuality": transcript_quality,
    }


def _clip_text(text: Any, limit: int) -> str:
    value = normalize_text(str(text or ""))
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _compact_discussion_messages(session: InterviewSession, max_items: int = 14) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for message in session.messages[-max_items:]:
        role = message.get("role")
        content = normalize_text(message.get("content"))
        if role not in ("assistant", "user") or not content:
            continue
        messages.append({"role": role, "content": _clip_text(content, 240)})
    return messages


def _compact_discussion_timeline(analysis: dict[str, Any], max_items: int = 10) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for entry in list(analysis.get("timeline") or [])[-max_items:]:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {
            "role": entry.get("role"),
            "excerpt": _clip_text(entry.get("excerpt"), 180),
        }
        for key in ("analysisNote", "responseLatencySec", "speechActivityRatio", "longPauseCount"):
            if entry.get(key) is not None:
                item[key] = entry.get(key)
        compact.append(item)
    return compact


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_feedback_list(value: Any, fallback: list[str], limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned = [_clip_text(item, 160) for item in value if isinstance(item, str) and item.strip()]
    return cleaned[:limit] or fallback


def build_personalized_discussion_summary(
    session: InterviewSession,
    analysis: dict[str, Any],
    overall_score: int,
    decision: str,
    fallback_strengths: list[str],
    fallback_improvements: list[str],
    fallback_recommendation: str,
) -> dict[str, Any] | None:
    if os.getenv("GEMINI_FEEDBACK_SUMMARY_ENABLED", "true").lower() in ("0", "false", "off"):
        return None
    key = _api_key()
    if not key:
        return None

    transcript_stats = analysis.get("transcriptStats") or {}
    user_char_count = int(transcript_stats.get("userCharCount") or 0)
    if user_char_count < DISCUSSION_MIN_SCORABLE_USER_CHARS:
        return None

    model = os.getenv("GEMINI_FEEDBACK_MODEL", "gemini-2.5-flash")
    timeout = float(os.getenv("GEMINI_FEEDBACK_TIMEOUT_SECONDS", "8"))
    rubric_summary = [
        {
            "key": item.get("key"),
            "label": item.get("label"),
            "score": item.get("score"),
            "level": item.get("level"),
            "evidence": [_clip_text(text, 120) for text in list(item.get("evidence") or [])[:2]],
            "advice": _clip_text(item.get("advice"), 140),
        }
        for item in list(analysis.get("rubricScores") or [])
        if isinstance(item, dict)
    ]
    audio_stats = analysis.get("audioStats") or {}
    audio_summary = {
        key_name: audio_stats.get(key_name)
        for key_name in (
            "available",
            "longThinkingPauseCount",
            "inTurnLongSilenceCount",
            "overlapCount",
            "observations",
            "emotionalCue",
        )
        if audio_stats.get(key_name) is not None
    }
    topic = (session.scenario or {}).get("discussionTopic") or ""
    source = {
        "topic": _clip_text(topic, 120),
        "overallScore": overall_score,
        "decision": decision,
        "transcriptStats": transcript_stats,
        "audioStats": audio_summary,
        "rubricScores": rubric_summary,
        "messages": _compact_discussion_messages(session),
        "timeline": _compact_discussion_timeline(analysis),
        "fallback": {
            "strengths": fallback_strengths,
            "improvements": fallback_improvements,
            "recommendation": fallback_recommendation,
        },
    }
    payload = feedback_summary_payload(source)
    request = urllib.request.Request(
        GEMINI_REST_URL.format(model=quote(model, safe="")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("discussion_feedback_summary_failed model=%s error=%s", model, exc)
        return None

    parsed = _extract_json_object(_extract_generate_content_text(body))
    if not parsed:
        logger.warning("discussion_feedback_summary_invalid_json model=%s", model)
        return None

    overall_comment = _clip_text(parsed.get("overallComment"), 180)
    recommendation = _clip_text(parsed.get("recommendation"), 180)
    result = {
        "overallComment": overall_comment or analysis.get("overallComment"),
        "strengths": _clean_feedback_list(parsed.get("strengths"), fallback_strengths, limit=2),
        "improvements": _clean_feedback_list(parsed.get("improvements"), fallback_improvements, limit=2),
        "recommendation": recommendation or fallback_recommendation,
    }
    if not result["overallComment"]:
        return None
    return result


def build_discussion_feedback(session: InterviewSession) -> dict[str, Any]:
    analysis = score_discussion(session)
    rubric_scores: list[dict[str, Any]] = analysis["rubricScores"]
    weighted_total = sum(item["score"] * item["weight"] for item in rubric_scores)
    overall_score = int(round(weighted_total))
    transcript_stats = analysis.get("transcriptStats") or {}
    user_char_count = int(transcript_stats.get("userCharCount") or 0)

    sorted_items = sorted(rubric_scores, key=lambda item: item["score"], reverse=True)
    strengths: list[str] = []
    if user_char_count < DISCUSSION_MIN_SCORABLE_USER_CHARS:
        strengths.append("評価に必要なユーザー発話量がまだ不足しています。")
    else:
        for item in sorted_items[:2]:
            if item["score"] >= 70:
                head_evidence = item["evidence"][0] if item["evidence"] else "良好"
                strengths.append(f"{item['label']}: {head_evidence}")
        if not strengths:
            strengths.append("短い時間の中で、議論に参加する基本姿勢は確認できました。")

    improvements: list[str] = []
    if user_char_count < DISCUSSION_MIN_SCORABLE_USER_CHARS:
        improvements.append("採点前に、賛成・反対などの立場と、その理由を一文ずつ話してください。")
    else:
        for item in list(reversed(sorted_items))[:2]:
            if item["score"] < 70:
                improvements.append(f"{item['label']}: {item['advice']}")
        if not improvements:
            improvements.append("次回はさらに具体例の質を上げ、相手の論点を一段深掘りすると安定します。")

    if user_char_count < DISCUSSION_MIN_SCORABLE_USER_CHARS:
        overall_score = min(overall_score, 45)
        decision = "ディスカッション評価不可・発話量不足"
    elif overall_score >= 80:
        decision = "ディスカッション良好"
    elif overall_score >= 65:
        decision = "ディスカッション概ね良好・改善余地あり"
    else:
        decision = "ディスカッション要改善"

    recommendation = (
        "まずは相手の問いに対して、立場と理由を一文ずつ話してから採点に進んでください。"
        if user_char_count < DISCUSSION_MIN_SCORABLE_USER_CHARS
        else "次回は、結論、理由、具体例、相手の反論への返しをそれぞれ一文で準備すると、3分でも議論が締まります。"
    )
    personalized = build_personalized_discussion_summary(
        session,
        analysis,
        overall_score,
        decision,
        strengths,
        improvements,
        recommendation,
    )
    if personalized:
        analysis["overallComment"] = personalized["overallComment"]
        analysis["personalizedSummary"] = {"available": True, "model": os.getenv("GEMINI_FEEDBACK_MODEL", "gemini-2.5-flash")}
        strengths = personalized["strengths"]
        improvements = personalized["improvements"]
        recommendation = personalized["recommendation"]
    else:
        analysis["personalizedSummary"] = {"available": False}

    return {
        "overallScore": overall_score,
        "decision": decision,
        "strengths": strengths,
        "improvements": improvements,
        "rubric": session.evaluationRubric,
        "recommendation": recommendation,
        "detailedAnalysis": analysis,
    }


def build_feedback(session: InterviewSession) -> dict[str, Any]:
    user_answers = [m["content"] for m in session.messages if m["role"] == "user"]
    joined = "\n".join(user_answers)
    strengths: list[str] = []
    improvements: list[str] = []

    if session.mode == "discussion":
        return build_discussion_feedback(session)

    if any(char.isdigit() for char in joined):
        strengths.append("数字を使った説明があり、成果や規模感が伝わりやすい回答でした。")
    else:
        improvements.append("成果、規模、期間、人数などを数字で補足すると説得力が上がります。")

    if any(word in joined for word in ["課題", "原因", "改善", "解決", "工夫"]):
        strengths.append("課題と対応策を説明しようとする姿勢が見えました。")
    else:
        improvements.append("経験談では、課題、打ち手、結果の順に整理すると評価しやすくなります。")

    if len(joined) > 600:
        improvements.append("回答が長くなりやすい傾向があります。最初に結論を置き、その後に具体例を足してください。")
    elif len(joined) < 220:
        improvements.append("回答量がやや少なめです。背景、本人の役割、結果を一文ずつ追加すると深みが出ます。")

    if not strengths:
        strengths.append("質問に対して回答を返せており、面接の基本的な流れは成立しています。")

    recommendation = (
        f"{session.jobRole}の面接では、{session.scenario['mainConcerns'][0]}が特に見られています。"
        "次回はSTAR形式で、状況、課題、行動、結果をそれぞれ短く入れてください。"
    )
    return {
        "overallScore": 72 if improvements else 84,
        "decision": "要改善だが通過可能性あり" if improvements else "一次面接通過水準",
        "strengths": strengths,
        "improvements": improvements,
        "rubric": session.evaluationRubric,
        "recommendation": recommendation,
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/voice/transport")
def get_voice_transport() -> dict[str, Any]:
    realtime_config = get_cloudflare_realtime_config()
    transport = voice_transport()
    return {
        "transport": "cloudflare-realtime",
        "cloudflareRealtimeConfigured": realtime_config.configured,
        "bridgeConfigured": bool(realtime_config.bridge_public_base_url),
        "stunServers": ["stun:stun.cloudflare.com:3478"],
    }


@app.on_event("startup")
def log_voice_provider_on_startup() -> None:
    provider = get_voice_provider(None)
    status = provider.status()
    realtime_config = get_cloudflare_realtime_config()
    logger.info(
        "voice_provider_startup name=%s model=%s configured=%s realtime=%s transport=%s cf_realtime_configured=%s bridge_configured=%s notes=%s",
        status.name,
        status.model,
        status.configured,
        status.realtime,
        voice_transport(),
        realtime_config.configured,
        bool(realtime_config.bridge_public_base_url),
        " | ".join(status.notes),
    )


@app.on_event("shutdown")
async def close_realtime_sessions() -> None:
    config = get_cloudflare_realtime_config()
    if config.configured:
        client = CloudflareRealtimeClient(config)
        for state in list(realtime_sessions.values()):
            adapter_ids = [adapter_id for adapter_id in state.get("adapterIds", []) if isinstance(adapter_id, str)]
            if adapter_ids:
                try:
                    client.close_websocket_adapters(adapter_ids)
                except CloudflareRealtimeError as exc:
                    logger.warning("cloudflare_realtime_adapter_close_failed error=%s", exc)
    realtime_sessions.clear()


@app.post("/api/discussions/topics")
def discussion_topics(payload: DiscussionTopicRequest) -> dict[str, Any]:
    rng = random.Random()
    topics = generate_discussion_topics(rng, payload.difficulty, count=3)
    return {"topics": topics}


@app.post("/api/discussions/start")
def start_discussion(payload: StartDiscussionRequest) -> dict[str, Any]:
    provider = get_voice_provider(None)
    session, opening_question = create_discussion_session(
        payload.difficulty,
        provider.name,
        payload.candidateLastName.strip(),
        payload.candidateLastNameKana.strip(),
        payload.discussionTopic,
        payload.discussionPersonaKey,
    )
    sessions[session.interviewId] = session
    status = provider.status()
    logger.info(
        "discussion_started interview_id=%s topic=%s voice_provider=%s model=%s configured=%s",
        session.interviewId,
        session.scenario["discussionTopic"],
        status.name,
        status.model,
        status.configured,
    )
    return {
        "interviewId": session.interviewId,
        "mode": session.mode,
        "durationSec": session.durationSec,
        "interviewer": session.interviewer,
        "scenario": session.scenario,
        "openingQuestion": opening_question,
    }


@app.get("/api/interviews/{interview_id}")
def get_interview(interview_id: str) -> InterviewSession:
    session = sessions.get(interview_id)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")
    return session


@app.post("/api/interviews/{interview_id}/voice/control")
async def voice_control(interview_id: str, payload: VoiceControlRequest) -> dict[str, Any]:
    session = sessions.get(interview_id)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")
    if voice_transport() == "cloudflare-realtime" and delegate_bridge_requests_enabled():
        return request_bridge_delegate(
            f"/api/voice/bridge/{interview_id}/control",
            method="POST",
            payload=payload.model_dump(),
        )
    if voice_transport() == "cloudflare-realtime":
        bridge = realtime_bridge_sessions.get(interview_id)
        if bridge:
            bridge.handle_control_payload(payload.model_dump())
    if payload.type == "metrics" and payload.metrics:
        record_interaction_metrics(session, payload.metrics)
    logger.info(
        "voice_control_received interview_id=%s type=%s text_len=%s metrics=%s",
        interview_id,
        payload.type,
        len(payload.text or ""),
        bool(payload.metrics),
    )
    return {"status": "queued"}


@app.get("/api/interviews/{interview_id}/voice/events")
def get_voice_events(interview_id: str, cursor: int = 0) -> dict[str, Any]:
    if interview_id not in sessions:
        return {"cursor": cursor, "events": []}
    if voice_transport() == "cloudflare-realtime" and delegate_bridge_requests_enabled():
        return request_bridge_delegate(f"/api/voice/bridge/{interview_id}/events?cursor={cursor}")
    bridge = realtime_bridge_sessions.get(interview_id)
    if not bridge:
        return {"cursor": cursor, "events": []}
    next_cursor, events = bridge.get_events_since(cursor)
    return {"cursor": next_cursor, "events": events}


@app.get("/api/interviews/{interview_id}/voice/debug")
def get_voice_debug(interview_id: str) -> dict[str, Any]:
    if interview_id not in sessions:
        return {"bridge": False, "session": False}
    if voice_transport() == "cloudflare-realtime" and delegate_bridge_requests_enabled():
        return request_bridge_delegate(f"/api/voice/bridge/{interview_id}/debug")
    bridge = realtime_bridge_sessions.get(interview_id)
    if not bridge:
        return {"bridge": False}
    return {
        "bridge": True,
        "started": bridge.started,
        "events": len(bridge.events),
        "tasks": len(bridge.tasks),
        "geminiQueue": bridge.gemini_send_queue.qsize(),
        "outputQueue": bridge.output_audio_queue.qsize(),
        "mode": bridge.mode,
        "geminiState": bridge.gemini_state,
        "counters": bridge.counters,
    }


@app.get("/api/interviews/{interview_id}/voice/diagnostics")
def get_voice_diagnostics(interview_id: str) -> dict[str, Any]:
    if interview_id not in sessions:
        raise HTTPException(status_code=404, detail="Interview not found")
    bridge = realtime_bridge_sessions.get(interview_id)
    if not bridge:
        return {"bridge": False}
    return bridge.get_diagnostics()


@app.get("/api/interviews/{interview_id}/voice/diagnostics/{capture_id}.wav")
def get_voice_diagnostic_wav(interview_id: str, capture_id: str) -> Response:
    if interview_id not in sessions:
        raise HTTPException(status_code=404, detail="Interview not found")
    bridge = realtime_bridge_sessions.get(interview_id)
    if not bridge:
        raise HTTPException(status_code=404, detail="Bridge not found")
    wav = bridge.get_diagnostic_wav(capture_id)
    if wav is None:
        raise HTTPException(status_code=404, detail="Diagnostic capture not found")
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{interview_id}-{capture_id}.wav"'},
    )


@app.post("/api/interviews/{interview_id}/finish")
async def finish_interview(interview_id: str) -> dict[str, Any]:
    session = sessions.get(interview_id)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")
    bridge = realtime_bridge_sessions.get(interview_id)
    if bridge:
        bridge.record_timeline_event("finish_requested")
        bridge.flush_pending_scoring_segment()
        try:
            wait_timeout = float(os.getenv("SCORING_TRANSCRIPTION_WAIT_SEC", "4.0"))
        except ValueError:
            wait_timeout = 4.0
        await bridge.wait_for_pending_transcriptions(wait_timeout)
        bridge.store_audio_summary()
        bridge.store_final_transcript()
    return build_feedback(session)


@app.post("/api/interviews/{interview_id}/realtime/session")
def create_realtime_session(interview_id: str, payload: RealtimeSessionRequest) -> dict[str, Any]:
    session = restore_realtime_session(interview_id, payload.sessionSnapshot)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")

    config = get_cloudflare_realtime_config()
    if not config.configured:
        raise HTTPException(status_code=400, detail="Cloudflare Realtime is not configured")

    client = CloudflareRealtimeClient(config)
    try:
        realtime_session = client.create_session(correlation_id=interview_id)
        session_id = str(realtime_session["sessionId"])
        realtime_sessions[interview_id] = {
            "sessionId": session_id,
            "micTrackName": payload.micTrackName,
            "adapterIds": [],
        }
    except (CloudflareRealtimeError, KeyError) as exc:
        logger.exception("cloudflare_realtime_session_failed interview_id=%s", interview_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "cloudflare_realtime_session_created interview_id=%s session_id=%s mic_track=%s",
        interview_id,
        session_id,
        payload.micTrackName,
    )
    return {
        "transport": "cloudflare-realtime",
        "sessionId": session_id,
        "sessionDescription": realtime_session.get("sessionDescription"),
    }


@app.post("/api/interviews/{interview_id}/realtime/tracks")
def create_realtime_tracks(interview_id: str, payload: RealtimeTracksRequest) -> dict[str, Any]:
    session = restore_realtime_session(interview_id, payload.sessionSnapshot)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")

    config = get_cloudflare_realtime_config()
    if not config.configured:
        raise HTTPException(status_code=400, detail="Cloudflare Realtime is not configured")

    provider = get_voice_provider(session.voiceProvider)
    status = provider.status()
    if provider.name != "gemini-live" or not status.configured:
        raise HTTPException(status_code=400, detail="Gemini Live provider is not configured")
    if not config.bridge_public_base_url:
        raise HTTPException(status_code=400, detail="VOICE_BRIDGE_PUBLIC_BASE_URL is required for Cloudflare Realtime adapters")

    previous_bridge = realtime_bridge_sessions.pop(interview_id, None)
    if previous_bridge:
        try:
            import anyio

            anyio.from_thread.run(previous_bridge.close)
        except RuntimeError:
            pass
    realtime_bridge_sessions[interview_id] = CloudflareRealtimeBridgeSession(
        provider,
        session,
        logger,
        input_adapter_refresh=lambda: refresh_realtime_input_adapter(interview_id),
    )

    client = CloudflareRealtimeClient(config)
    try:
        session_id = payload.sessionId
        tracks_response = client.add_tracks(
            session_id,
            {
                "autoDiscover": True,
                "sessionDescription": {"sdp": payload.sdp, "type": payload.type},
            },
        )
        published_track = (tracks_response.get("tracks") or [{}])[0]
        published_track_name = str(published_track.get("trackName") or payload.micTrackName)
        state = realtime_sessions.get(interview_id, {})
        realtime_sessions[interview_id] = {
            **state,
            "sessionId": session_id,
            "micTrackName": published_track_name,
            "adapterIds": state.get("adapterIds", []),
        }
    except (CloudflareRealtimeError, KeyError) as exc:
        logger.exception("cloudflare_realtime_tracks_failed interview_id=%s", interview_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "cloudflare_realtime_tracks_created interview_id=%s session_id=%s mic_track=%s",
        interview_id,
        session_id,
        published_track_name,
    )
    return {
        "transport": "cloudflare-realtime",
        "sessionId": session_id,
        "micTrackName": published_track_name,
        "tracks": tracks_response.get("tracks", []),
        "sessionDescription": tracks_response.get("sessionDescription"),
        "requiresImmediateRenegotiation": tracks_response.get("requiresImmediateRenegotiation", False),
    }


@app.post("/api/interviews/{interview_id}/realtime/adapters")
def create_realtime_adapters(interview_id: str, payload: RealtimeAdaptersRequest) -> dict[str, Any]:
    session = restore_realtime_session(interview_id, payload.sessionSnapshot)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")

    config = get_cloudflare_realtime_config()
    if not config.configured:
        raise HTTPException(status_code=400, detail="Cloudflare Realtime is not configured")

    provider = get_voice_provider(session.voiceProvider)
    status = provider.status()
    if provider.name != "gemini-live" or not status.configured:
        raise HTTPException(status_code=400, detail="Gemini Live provider is not configured")
    if not config.bridge_public_base_url:
        raise HTTPException(status_code=400, detail="VOICE_BRIDGE_PUBLIC_BASE_URL is required for Cloudflare Realtime adapters")

    if interview_id not in realtime_bridge_sessions:
        realtime_bridge_sessions[interview_id] = CloudflareRealtimeBridgeSession(
            provider,
            session,
            logger,
            input_adapter_refresh=lambda: refresh_realtime_input_adapter(interview_id),
        )
    else:
        realtime_bridge_sessions[interview_id].input_adapter_refresh = lambda: refresh_realtime_input_adapter(interview_id)

    client = CloudflareRealtimeClient(config)
    try:
        session_id = payload.sessionId
        bridge_input_endpoint = to_websocket_url(config.bridge_public_base_url, f"/api/voice/bridge/{interview_id}/input")
        bridge_output_endpoint = to_websocket_url(config.bridge_public_base_url, f"/api/voice/bridge/{interview_id}/output")
        ai_track_name = f"ai-audio-{interview_id}"
        stream_adapter = client.create_websocket_adapters(
            {
                "tracks": [
                    {
                        "location": "remote",
                        "sessionId": session_id,
                        "trackName": payload.micTrackName,
                        "endpoint": bridge_input_endpoint,
                        "outputCodec": "pcm",
                    }
                ]
            }
        )
        ingest_adapter = client.create_websocket_adapters(
            {
                "tracks": [
                    {
                        "location": "local",
                        "trackName": ai_track_name,
                        "endpoint": bridge_output_endpoint,
                        "inputCodec": "pcm",
                        "mode": "buffer",
                    }
                ]
            }
        )
        ai_track = (ingest_adapter.get("tracks") or [{}])[0]
        ai_session_id = str(ai_track.get("sessionId", ""))
        remote_track_response: dict[str, Any] = {}
        if ai_session_id:
            remote_track_response = client.add_tracks(
                session_id,
                {
                    "tracks": [
                        {
                            "location": "remote",
                            "sessionId": ai_session_id,
                            "trackName": ai_track_name,
                            "kind": "audio",
                        }
                    ]
                },
            )
        realtime_bridge_sessions[interview_id].enable_output()
        adapter_ids = [
            str(track.get("adapterId"))
            for track in [*stream_adapter.get("tracks", []), *ingest_adapter.get("tracks", [])]
            if track.get("adapterId")
        ]
        input_adapter_ids = [
            str(track.get("adapterId"))
            for track in stream_adapter.get("tracks", [])
            if track.get("adapterId")
        ]
        output_adapter_ids = [
            str(track.get("adapterId"))
            for track in ingest_adapter.get("tracks", [])
            if track.get("adapterId")
        ]
        realtime_sessions[interview_id] = {
            "sessionId": session_id,
            "micTrackName": payload.micTrackName,
            "aiTrackName": ai_track_name,
            "aiSessionId": ai_session_id,
            "adapterIds": adapter_ids,
            "inputAdapterIds": input_adapter_ids,
            "outputAdapterIds": output_adapter_ids,
        }
    except (CloudflareRealtimeError, KeyError) as exc:
        logger.exception("cloudflare_realtime_adapters_failed interview_id=%s", interview_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info(
        "cloudflare_realtime_adapters_created interview_id=%s session_id=%s mic_track=%s adapters=%s",
        interview_id,
        session_id,
        payload.micTrackName,
        adapter_ids,
    )
    return {
        "transport": "cloudflare-realtime",
        "sessionId": session_id,
        "remoteSessionDescription": remote_track_response.get("sessionDescription"),
        "remoteRequiresImmediateRenegotiation": remote_track_response.get("requiresImmediateRenegotiation", False),
        "remoteTracks": remote_track_response.get("tracks", []),
        "adapters": [*stream_adapter.get("tracks", []), *ingest_adapter.get("tracks", [])],
    }


@app.put("/api/interviews/{interview_id}/realtime/renegotiate")
def renegotiate_realtime_session(interview_id: str, payload: RealtimeRenegotiateRequest) -> dict[str, Any]:
    if interview_id not in sessions:
        raise HTTPException(status_code=404, detail="Interview not found")
    config = get_cloudflare_realtime_config()
    if not config.configured:
        raise HTTPException(status_code=400, detail="Cloudflare Realtime is not configured")
    try:
        return CloudflareRealtimeClient(config).renegotiate(
            payload.sessionId,
            {"sessionDescription": {"sdp": payload.sdp, "type": "answer"}},
        )
    except CloudflareRealtimeError as exc:
        logger.exception("cloudflare_realtime_renegotiate_failed interview_id=%s", interview_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/interviews/{interview_id}/realtime/close")
async def close_realtime_session(interview_id: str) -> dict[str, Any]:
    state = realtime_sessions.pop(interview_id, None)
    bridge = realtime_bridge_sessions.pop(interview_id, None)
    if bridge:
        await bridge.close()
    if not state:
        return {"status": "ok"}
    config = get_cloudflare_realtime_config()
    if config.configured:
        adapter_ids = [adapter_id for adapter_id in state.get("adapterIds", []) if isinstance(adapter_id, str)]
        if adapter_ids:
            try:
                CloudflareRealtimeClient(config).close_websocket_adapters(adapter_ids)
            except CloudflareRealtimeError as exc:
                logger.warning("cloudflare_realtime_adapter_close_failed interview_id=%s error=%s", interview_id, exc)
    return {"status": "ok"}


@app.websocket("/api/voice/bridge/{interview_id}/input")
async def cloudflare_bridge_input(websocket: WebSocket, interview_id: str) -> None:
    apply_runtime_config_header(websocket.headers.get(RUNTIME_CONFIG_HEADER))
    await websocket.accept()
    logger.info("cloudflare_bridge_input_connected interview_id=%s", interview_id)
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        await websocket.close(code=1011)
        return
    try:
        await bridge.handle_input_websocket(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("cloudflare_bridge_input_closed interview_id=%s", interview_id)


@app.websocket("/api/voice/bridge/{interview_id}/output")
async def cloudflare_bridge_output(websocket: WebSocket, interview_id: str) -> None:
    apply_runtime_config_header(websocket.headers.get(RUNTIME_CONFIG_HEADER))
    await websocket.accept()
    logger.info("cloudflare_bridge_output_connected interview_id=%s", interview_id)
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        await websocket.close(code=1011)
        return
    try:
        await bridge.handle_output_websocket(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("cloudflare_bridge_output_closed interview_id=%s", interview_id)


@app.post("/api/voice/bridge/{interview_id}/control")
async def local_bridge_control(interview_id: str, payload: VoiceControlRequest) -> dict[str, Any]:
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        raise HTTPException(status_code=404, detail="Bridge not found")
    bridge.handle_control_payload(payload.model_dump())
    return {"status": "queued"}


@app.get("/api/voice/bridge/{interview_id}/events")
def local_bridge_events(interview_id: str, cursor: int = 0) -> dict[str, Any]:
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        return {"cursor": cursor, "events": []}
    next_cursor, events = bridge.get_events_since(cursor)
    return {"cursor": next_cursor, "events": events}


@app.get("/api/voice/bridge/{interview_id}/debug")
def local_bridge_debug(interview_id: str) -> dict[str, Any]:
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        return {"bridge": False}
    return {
        "bridge": True,
        "started": bridge.started,
        "events": len(bridge.events),
        "tasks": len(bridge.tasks),
        "geminiQueue": bridge.gemini_send_queue.qsize(),
        "outputQueue": bridge.output_audio_queue.qsize(),
        "mode": bridge.mode,
        "geminiState": bridge.gemini_state,
        "counters": bridge.counters,
    }


@app.get("/api/voice/bridge/{interview_id}/diagnostics")
def local_bridge_diagnostics(interview_id: str) -> dict[str, Any]:
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        return {"bridge": False}
    return bridge.get_diagnostics()


@app.get("/api/voice/bridge/{interview_id}/diagnostics/{capture_id}.wav")
def local_bridge_diagnostic_wav(interview_id: str, capture_id: str) -> Response:
    bridge = ensure_local_debug_bridge(interview_id)
    if not bridge:
        raise HTTPException(status_code=404, detail="Bridge not found")
    wav = bridge.get_diagnostic_wav(capture_id)
    if wav is None:
        raise HTTPException(status_code=404, detail="Diagnostic capture not found")
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{interview_id}-{capture_id}.wav"'},
    )


@app.get("/api/interviews/{interview_id}/voice")
def get_interview_voice(interview_id: str) -> dict[str, Any]:
    session = sessions.get(interview_id)
    if not session:
        raise HTTPException(status_code=404, detail="Interview not found")
    provider = get_voice_provider(session.voiceProvider)
    status = provider.status()
    logger.info(
        "voice_config_requested interview_id=%s voice_provider=%s model=%s configured=%s",
        interview_id,
        status.name,
        status.model,
        status.configured,
    )
    return {
        "provider": status,
        "systemInstruction": provider.system_instruction(session),
    }
