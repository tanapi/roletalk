<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

type Difficulty = 'easy' | 'normal' | 'hard'
type SessionMode = 'discussion'
type DiscussionPersonaKey = 'collaborative' | 'logical'

type ChatMessage = {
  role: 'assistant' | 'user'
  content: string
}

type Interviewer = {
  name: string
  personality: string
  speakingStyle: string
  imageKey: string
}

type RubricLevel = 'excellent' | 'good' | 'fair' | 'needs_work'

type RubricScore = {
  key: string
  label: string
  score: number
  weight: number
  level: RubricLevel
  evidence: string[]
  advice: string
}

type TranscriptStats = {
  userTurnCount: number
  userCharCount: number
  averageUserTurnChars: number
  stanceDetected: string
  reasonMarkerCount: number
  exampleMarkerCount: number
  concessionMarkerCount: number
  questionResponseCount: number
}

type UserTurnStat = {
  index: number
  startAt?: number
  endAt?: number
  transcriptAt?: number
  responseLatencySec?: number
  userTurnDurationSec?: number
  longPauseCount?: number
  speechActivityRatio?: number
}

type TurnTakingStats = {
  userTurns: UserTurnStat[]
}

type EmotionSignalLevel = 'high' | 'medium' | 'low'

type EmotionSignal = {
  key: string
  label: string
  level: EmotionSignalLevel
  score: number
  evidence: string[]
  advice: string
}

type AudioStats = {
  available: boolean
  observedDurationSec?: number
  rmsAverage?: number
  fillerCount?: number
  responseLatencyAvgSec?: number
  responseLatencyMaxSec?: number
  longThinkingPauseCount?: number
  inTurnLongSilenceCount?: number
  overlapCount?: number
  turnTakingStats?: TurnTakingStats
  emotionSignals?: EmotionSignal[]
  observations: string[]
}

type TimelineEntry = {
  role: 'assistant' | 'user'
  excerpt: string
  analysisNote?: string
  audioStartSec?: number
  audioEndSec?: number
  audioDurationSec?: number
  responseLatencySec?: number
  speechActivityRatio?: number
  longPauseCount?: number
}

type TranscriptSource = 'post_audio_transcription' | 'realtime_fallback' | 'mixed'

type TranscriptQuality = {
  segmentCount: number
  completedCount: number
  pendingCount: number
  errorCount: number
}

type DetailedAnalysis = {
  mode: 'discussion'
  overallComment: string
  rubricScores: RubricScore[]
  transcriptStats: TranscriptStats
  audioStats: AudioStats
  timeline: TimelineEntry[]
  transcriptSource?: TranscriptSource
  transcriptQuality?: TranscriptQuality
}

type Feedback = {
  overallScore: number
  decision: string
  strengths: string[]
  improvements: string[]
  recommendation: string
  rubric: Record<string, number>
  detailedAnalysis?: DetailedAnalysis
}

type Scenario = {
  targetCompanyType: string
  seniority: string
  discussionTopic?: string
  discussionPersonaKey?: string
  discussionPersonaLabel?: string
  interviewPhases: string[]
  mainConcerns: string[]
}

type MouthBounds = {
  imageWidth: number
  imageHeight: number
  mouth: {
    left: number
    top: number
    right: number
    bottom: number
  }
}

type AudioDevice = {
  deviceId: string
  label: string
  groupId: string
}

type DebugEvent = {
  id: number
  text: string
}

type VoiceTransportInfo = {
  transport: 'cloudflare-realtime'
  cloudflareRealtimeConfigured: boolean
  bridgeConfigured: boolean
  stunServers: string[]
}

type WakeLockSentinelLike = {
  released: boolean
  release: () => Promise<void>
  addEventListener: (type: 'release', listener: () => void) => void
}

type WakeLockNavigator = Navigator & {
  wakeLock?: {
    request: (type: 'screen') => Promise<WakeLockSentinelLike>
  }
}

function resolveApiBase() {
  const configured = String(import.meta.env.VITE_API_BASE_URL ?? '').trim()
  const pageHost = window.location.hostname
  if (configured) {
    try {
      const configuredUrl = new URL(configured)
      const configuredIsLocalhost = ['localhost', '127.0.0.1', '::1'].includes(configuredUrl.hostname)
      const pageIsLocalhost = ['localhost', '127.0.0.1', '::1'].includes(pageHost)
      if (!configuredIsLocalhost || pageIsLocalhost) {
        return configuredUrl.origin
      }
    } catch {
      return configured
    }
  }
  return ''
}

const apiBase = resolveApiBase()
const storedCandidateLastName = localStorage.getItem('roletalk.candidateLastName') ?? ''
const storedCandidateLastNameKana = localStorage.getItem('roletalk.candidateLastNameKana') ?? ''
const storedInputDeviceId = localStorage.getItem('roletalk.inputDeviceId') ?? ''
const storedOutputDeviceId = localStorage.getItem('roletalk.outputDeviceId') ?? ''
const selectedMode = ref<SessionMode | null>(null)
const activeMode = ref<SessionMode | null>(null)
const candidateLastName = ref(storedCandidateLastName)
const candidateLastNameKana = ref(storedCandidateLastNameKana)
const jobRole = ref('Webバックエンドエンジニア')
const difficulty = ref<Difficulty>('normal')
const selectedDiscussionPersonaKey = ref<DiscussionPersonaKey>('collaborative')
const inputDeviceId = ref(storedInputDeviceId)
const outputDeviceId = ref(storedOutputDeviceId)
const inputDevices = ref<AudioDevice[]>([])
const outputDevices = ref<AudioDevice[]>([])
const isDeviceDialogOpen = ref(false)
const isLoadingDevices = ref(false)
const deviceError = ref('')
const isTopicDialogOpen = ref(false)
const isLoadingTopics = ref(false)
const topicOptions = ref<string[]>([])
const selectedDiscussionTopic = ref('')
const topicError = ref('')
const topicCountdownSec = ref(0)
const interviewId = ref('')
const interviewer = ref<Interviewer | null>(null)
const scenario = ref<Scenario | null>(null)
const messages = ref<ChatMessage[]>([])
const isLoading = ref(false)
const isScoringFeedback = ref(false)
const isSpeaking = ref(false)
const mouthFrame = ref<'closed' | 'a' | 'o'>('closed')
const interviewerAssetFolder = ref('logical')
const mouthBounds = ref<MouthBounds>({
  imageWidth: 1254,
  imageHeight: 1254,
  mouth: { left: 604, top: 550, right: 678, bottom: 616 },
})
let mouthAnimationTimer = 0
const isMicActive = ref(false)
const feedback = ref<Feedback | null>(null)
const audioInsight = ref<string[]>([])
const debugEvents = ref<DebugEvent[]>([])
const chatLog = ref<HTMLElement | null>(null)
const debugLogList = ref<HTMLElement | null>(null)
const feedbackSection = ref<HTMLElement | null>(null)
const aiAudio = ref<HTMLAudioElement | null>(null)
let timeLimitBellAudio: HTMLAudioElement | null = null
const responseStartedAt = ref<number | null>(null)
const lastQuestionAt = ref<number | null>(null)
const pendingOpeningQuestion = ref('')
const awaitingOpeningTranscript = ref(false)
const openingSpeechActive = ref(false)
let openingTurnComplete = false
let openingOutputStarted = false
const durationSec = ref(900)
const elapsedSec = ref(0)
const discussionConclusionRequested = ref(false)
const discussionEnding = ref(false)
const conclusionVisualEnded = ref(false)
const finishWhenPlaybackEnds = ref(false)
const aiInterruptionCount = ref(0)
let openingQuestionSent = false
let sessionStartedAt = 0
let elapsedTimer = 0
let elapsedTimerStarted = false
let openingCompleteTimer = 0
let topicCountdownTimer = 0
let finalFinishTimer = 0
let conclusionAnswerWaitTimer = 0
let finalTurnComplete = false
let awaitingUserAnswerBeforeConclusion = false
let assistantQuestionPendingBeforeConclusion = false
let concludeAfterNextAssistantTurn = false
let conclusionPendingAfterUserAnswer = false
let suppressingAnswerResponseBeforeConclusion = false
let conclusionAfterAnswerTimer = 0
let timeLimitConclusionTimer = 0
let timeLimitConclusionScheduled = false
let assistantResponseStartedAt = 0
let assistantAudioFinishedAt = 0
let conclusionPlaybackReadyAt = 0
let userSpeechActive = false
let userSpeechSegmentStartedAt = 0
let lastUserSpeechAt = 0
let lastSpeechDetectedAt = 0
let interruptionSentThisSegment = false
let localPlaybackOverlapSentThisSegment = false
let pendingInterruptionAt = 0
let pendingInterruptionResolved = true
let lastAiInterruptionAt = 0
let mediaStream: MediaStream | null = null
let speechStarted = false
let speechStartTime = 0
let speechEndTime = 0
let silentFrames = 0
let totalFrames = 0
let peerConnection: RTCPeerConnection | null = null
let remoteStream: MediaStream | null = null
let shouldReconnectVoice = true
let reconnectTimer = 0
let reconnectAttempts = 0
let connectionWatchdogTimer = 0
let voiceEventsPollTimer = 0
let voiceEventsCursor = 0
let debugEventId = 0
let voiceSocketOpenedAt = 0
let sentAudioChunks = 0
let sentAudioSamples = 0
let suppressedAudioChunks = 0
let receivedAudioChunks = 0
let playedAudioChunks = 0
let workletChunks = 0
let lastVadMetricsAt = 0
let lastClientStatsAt = 0
const speechRmsThreshold = 0.01
const speechTailMs = 1000
const maxPreSpeechChunks = 15
let preSpeechChunks: Int16Array[] = []
let audioStreamOpen = false
let realtimeSessionId = ''
let micMonitorContext: AudioContext | null = null
let micMonitorSource: MediaStreamAudioSourceNode | null = null
let micMonitorAnalyser: AnalyserNode | null = null
let micMonitorTimer = 0
let playbackInterruptSpeechStartedAt = 0
let playbackInterruptedThisAssistantTurn = false
let remoteAudioTrackReady = false
let wakeLock: WakeLockSentinelLike | null = null
let wakeLockRequested = false


function buildMicConstraints(): MediaStreamConstraints {
  const selectedInputDeviceId = inputDeviceId.value
  return {
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
      ...(selectedInputDeviceId ? { deviceId: { exact: selectedInputDeviceId } } : {}),
    },
  }
}

function isIphoneLikeAudioDevice(device: AudioDevice) {
  return /iphone|continuity|連係|連携/i.test(device.label)
}

function isBuiltInMacInput(device: AudioDevice) {
  return /macbook|built.?in|internal|内蔵/i.test(device.label)
}

function hasKnownDeviceLabel(device: AudioDevice) {
  return !/^入力デバイス \d+$/.test(device.label)
}

function choosePreferredInputDevice(devices: AudioDevice[]) {
  const selectable = devices.filter((device) => device.deviceId && device.deviceId !== 'default' && hasKnownDeviceLabel(device))
  return (
    selectable.find(isBuiltInMacInput) ??
    selectable.find((device) => !isIphoneLikeAudioDevice(device)) ??
    null
  )
}

function normalizeDefaultDeviceLabel(label: string) {
  return label
    .replace(/^default\s*[-–—:]\s*/i, '')
    .replace(/^既定\s*[-–—:]\s*/i, '')
    .replace(/^デフォルト\s*[-–—:]\s*/i, '')
    .trim()
}

function labelAudioDevice(device: MediaDeviceInfo, sameKindDevices: MediaDeviceInfo[], fallback: string) {
  if (device.deviceId !== 'default') {
    return device.label || fallback
  }
  const explicitDefaultLabel = normalizeDefaultDeviceLabel(device.label)
  const matchingDevice = sameKindDevices.find(
    (candidate) => candidate.deviceId !== 'default' && candidate.groupId && candidate.groupId === device.groupId && candidate.label,
  )
  const actualLabel = explicitDefaultLabel || matchingDevice?.label || fallback
  return `${actualLabel}(既定)`
}

async function applyMicEnhancements(stream: MediaStream) {
  await Promise.all(
    stream.getAudioTracks().map(async (track) => {
      try {
        await track.applyConstraints({
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        })
      } catch (error) {
        debugLog('mic.constraints_apply_failed', {
          message: error instanceof Error ? error.message : String(error),
        })
      }
      const settings = track.getSettings()
      debugLog('mic.settings', {
        echoCancellation: settings.echoCancellation,
        noiseSuppression: settings.noiseSuppression,
        autoGainControl: settings.autoGainControl,
        channelCount: settings.channelCount,
        sampleRate: settings.sampleRate,
      })
    }),
  )
}

const lastAssistantMessage = computed(() => [...messages.value].reverse().find((m) => m.role === 'assistant')?.content ?? '')
const currentMode = computed(() => activeMode.value ?? selectedMode.value)
const hasCandidateName = computed(() => candidateLastName.value.trim().length > 0 && candidateLastNameKana.value.trim().length > 0)
const modeTitle = computed(() => 'AIディスカッション')
const finishButtonLabel = computed(() => 'ディスカッションを終了')
const isSessionRunning = computed(() => Boolean(interviewId.value && !feedback.value))
const callDisconnected = computed(
  () => currentMode.value === 'discussion' && (Boolean(feedback.value) || isScoringFeedback.value || conclusionVisualEnded.value),
)
const discussionPersonas: Array<{
  key: DiscussionPersonaKey
  label: string
  lead: string
  description: string
  imageSrc: string
}> = [
  {
    key: 'collaborative',
    label: '共創型の討論者',
    lead: '強く押し切らず、考えを深める相手',
    description: 'あなたの意見を受け止めながら、条件や別視点を穏やかに問いかけます。建設的に話したい方向けです。',
    imageSrc: '/interviewers/collaborative/person.png',
  },
  {
    key: 'logical',
    label: '論理型の討論者',
    lead: '反対側の論点を明確に返す相手',
    description: '根拠、例外、懸念を短く提示します。反論への対応力を強めたい方向けです。',
    imageSrc: '/interviewers/logical/person.png',
  },
]
const selectedDiscussionPersona = computed(
  () => discussionPersonas.find((persona) => persona.key === selectedDiscussionPersonaKey.value) ?? discussionPersonas[0],
)
const interviewerBasePath = computed(() => `/interviewers/${interviewerAssetFolder.value}`)
const interviewerImageSrc = computed(() => `${interviewerBasePath.value}/person.png`)
const mouthOverlaySrc = computed(() =>
  `${interviewerBasePath.value}/${mouthFrame.value === 'a' ? 'person_a.png' : 'person_o.png'}`,
)
const mouthOverlayStyle = computed(() => {
  const bounds = mouthBounds.value
  const width = Math.max(1, bounds.imageWidth)
  const height = Math.max(1, bounds.imageHeight)
  const mouth = bounds.mouth
  const top = (mouth.top / height) * 100
  const right = ((width - mouth.right) / width) * 100
  const bottom = ((height - mouth.bottom) / height) * 100
  const left = (mouth.left / width) * 100
  return {
    clipPath: `inset(${top}% ${right}% ${bottom}% ${left}%)`,
  }
})

function normalizeInterviewerAssetFolder(value: string | undefined) {
  return value === 'collaborative' ? 'collaborative' : 'logical'
}

async function loadInterviewerAssets(nextScenario: Scenario) {
  const folder = normalizeInterviewerAssetFolder(
    nextScenario.discussionPersonaKey ?? (nextScenario.discussionPersonaLabel ? 'collaborative' : undefined),
  )
  interviewerAssetFolder.value = folder
  try {
    const response = await fetch(`/interviewers/${folder}/mouth.json`, { cache: 'no-cache' })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const data = (await response.json()) as MouthBounds
    if (
      !Number.isFinite(data.imageWidth) ||
      !Number.isFinite(data.imageHeight) ||
      !data.mouth ||
      !Number.isFinite(data.mouth.left) ||
      !Number.isFinite(data.mouth.top) ||
      !Number.isFinite(data.mouth.right) ||
      !Number.isFinite(data.mouth.bottom)
    ) {
      throw new Error('invalid mouth metadata')
    }
    mouthBounds.value = data
  } catch (error) {
    debugLog('interviewer.mouth_metadata_failed', {
      folder,
      message: error instanceof Error ? error.message : String(error),
    })
    mouthBounds.value =
      folder === 'collaborative'
        ? { imageWidth: 1254, imageHeight: 1254, mouth: { left: 560, top: 558, right: 666, bottom: 630 } }
        : { imageWidth: 1254, imageHeight: 1254, mouth: { left: 604, top: 550, right: 678, bottom: 616 } }
  }
}

function startMouthAnimation() {
  if (mouthAnimationTimer) return
  mouthFrame.value = 'a'
  mouthAnimationTimer = window.setInterval(() => {
    mouthFrame.value = mouthFrame.value === 'a' ? 'o' : 'a'
  }, 160)
}

function stopMouthAnimation() {
  if (mouthAnimationTimer) {
    window.clearInterval(mouthAnimationTimer)
    mouthAnimationTimer = 0
  }
  mouthFrame.value = 'closed'
}

watch(isSpeaking, (value) => {
  if (value) {
    startMouthAnimation()
  } else {
    stopMouthAnimation()
  }
})
const remainingTimeLabel = computed(() => {
  const remaining = Math.max(durationSec.value - elapsedSec.value, 0)
  const minutes = Math.floor(remaining / 60)
  const seconds = remaining % 60
  return `${minutes}:${seconds.toString().padStart(2, '0')}`
})

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  let lastError: unknown
  const maxAttempts = path.includes('/voice/events') ? 1 : path.endsWith('/start') || path.includes('/realtime/') ? 6 : 3
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const response = await fetch(`${apiBase}${path}`, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
      })
      if (!response.ok) {
        const body = await response.text()
        throw new Error(`HTTP ${response.status}${body ? `: ${body}` : ''}`)
      }
      return response.json() as Promise<T>
    } catch (error) {
      lastError = error
      if (attempt === maxAttempts) break
      debugLog('api.retry', { path, attempt, message: error instanceof Error ? error.message : String(error) })
      await new Promise((resolve) => window.setTimeout(resolve, Math.min(attempt * 1000, 3000)))
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError))
}

async function loadVoiceTransportInfo(): Promise<VoiceTransportInfo> {
  const info = await requestJson<VoiceTransportInfo>('/api/voice/transport')
  debugLog('voice.transport', {
    transport: info.transport,
    cloudflareRealtimeConfigured: info.cloudflareRealtimeConfigured,
    bridgeConfigured: info.bridgeConfigured,
  })
  return info
}

function debugLog(event: string, details: Record<string, unknown> = {}) {
  const time = new Date().toLocaleTimeString('ja-JP', { hour12: false })
  const suffix = Object.entries(details)
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(' ')
  const line = suffix ? `${time} ${event} ${suffix}` : `${time} ${event}`
  debugEventId += 1
  debugEvents.value = [{ id: debugEventId, text: line }, ...debugEvents.value].slice(0, 2000)
  console.debug('[voice-debug]', event, details)
}

function logBrowserAudioEnvironment() {
  debugLog('browser.audio_env', {
    protocol: window.location.protocol,
    host: window.location.host,
    secure: window.isSecureContext,
    mediaDevices: Boolean(navigator.mediaDevices),
    getUserMedia: Boolean(navigator.mediaDevices?.getUserMedia),
    apiBase: apiBase || 'same-origin',
  })
}

function scrollChatToLatest() {
  void nextTick(() => {
    if (chatLog.value) {
      chatLog.value.scrollTop = chatLog.value.scrollHeight
    }
  })
}

function scrollDebugToLatest() {
  void nextTick(() => {
    if (debugLogList.value) {
      debugLogList.value.scrollTop = 0
    }
  })
}

watch(candidateLastName, (value) => {
  localStorage.setItem('roletalk.candidateLastName', value.trim())
})

watch(candidateLastNameKana, (value) => {
  localStorage.setItem('roletalk.candidateLastNameKana', value.trim())
})

watch(inputDeviceId, (value) => {
  localStorage.setItem('roletalk.inputDeviceId', value)
})

watch(outputDeviceId, (value) => {
  localStorage.setItem('roletalk.outputDeviceId', value)
})

watch(messages, scrollChatToLatest, { deep: true })

watch(debugEvents, scrollDebugToLatest)

watch(feedback, async (value) => {
  if (!value) return
  await nextTick()
  feedbackSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
})

function stopSpeakingAudio() {
  isSpeaking.value = false
}

function selectMode(mode: SessionMode) {
  if (!hasCandidateName.value) return
  selectedMode.value = mode
  activeMode.value = null
  durationSec.value = mode === 'discussion' ? 180 : 900
  elapsedSec.value = 0
  feedback.value = null
}

function returnToModeSelection() {
  if (isSessionRunning.value) return
  selectedMode.value = null
  activeMode.value = null
  interviewId.value = ''
  feedback.value = null
  messages.value = []
  audioInsight.value = []
}

function resetSessionState() {
  debugLog('session.reset')
  shouldReconnectVoice = false
  releaseWakeLock()
  closePeerConnection()
  feedback.value = null
  audioInsight.value = []
  messages.value = []
  interviewerAssetFolder.value = selectedDiscussionPersonaKey.value
  mouthBounds.value =
    selectedDiscussionPersonaKey.value === 'collaborative'
      ? { imageWidth: 1254, imageHeight: 1254, mouth: { left: 560, top: 558, right: 666, bottom: 630 } }
      : { imageWidth: 1254, imageHeight: 1254, mouth: { left: 604, top: 550, right: 678, bottom: 616 } }
  pendingOpeningQuestion.value = ''
  awaitingOpeningTranscript.value = false
  openingSpeechActive.value = false
  openingTurnComplete = false
  openingOutputStarted = false
  openingQuestionSent = false
  finalTurnComplete = false
  window.clearTimeout(finalFinishTimer)
  window.clearTimeout(conclusionAnswerWaitTimer)
  window.clearTimeout(conclusionAfterAnswerTimer)
  window.clearTimeout(timeLimitConclusionTimer)
  timeLimitConclusionScheduled = false
  awaitingUserAnswerBeforeConclusion = false
  assistantQuestionPendingBeforeConclusion = false
  concludeAfterNextAssistantTurn = false
  conclusionPendingAfterUserAnswer = false
  suppressingAnswerResponseBeforeConclusion = false
  assistantResponseStartedAt = 0
  discussionConclusionRequested.value = false
  discussionEnding.value = false
  conclusionVisualEnded.value = false
  finishWhenPlaybackEnds.value = false
  isScoringFeedback.value = false
  conclusionPlaybackReadyAt = 0
  aiInterruptionCount.value = 0
  userSpeechActive = false
  userSpeechSegmentStartedAt = 0
  lastUserSpeechAt = 0
  lastSpeechDetectedAt = 0
  interruptionSentThisSegment = false
  localPlaybackOverlapSentThisSegment = false
  assistantAudioFinishedAt = 0
  pendingInterruptionAt = 0
  pendingInterruptionResolved = true
  lastAiInterruptionAt = 0
  playbackInterruptSpeechStartedAt = 0
  playbackInterruptedThisAssistantTurn = false
  remoteAudioTrackReady = false
  reconnectAttempts = 0
  sentAudioChunks = 0
  sentAudioSamples = 0
  suppressedAudioChunks = 0
  receivedAudioChunks = 0
  playedAudioChunks = 0
  workletChunks = 0
  lastVadMetricsAt = 0
  lastClientStatsAt = 0
  elapsedSec.value = 0
  elapsedTimerStarted = false
  window.clearInterval(elapsedTimer)
  window.clearTimeout(openingCompleteTimer)
  window.clearTimeout(connectionWatchdogTimer)
  stopTopicCountdown()
  stopSpeakingAudio()
}

function startElapsedTimer() {
  window.clearInterval(elapsedTimer)
  sessionStartedAt = Date.now()
  elapsedTimerStarted = true
  elapsedSec.value = 0
  debugLog('timer.start', { durationSec: durationSec.value })
  elapsedTimer = window.setInterval(() => {
    if (!sessionStartedAt) return
    elapsedSec.value = Math.floor((Date.now() - sessionStartedAt) / 1000)
    if (activeMode.value === 'discussion' && elapsedSec.value >= durationSec.value) {
      handleDiscussionTimeLimit()
    }
  }, 1000)
}

async function openTopicDialog() {
  if (!hasCandidateName.value || isLoading.value) return
  isTopicDialogOpen.value = true
  selectedDiscussionTopic.value = ''
  topicError.value = ''
  await loadDiscussionTopics()
}

function closeTopicDialog() {
  if (isLoading.value) return
  stopTopicCountdown()
  isTopicDialogOpen.value = false
  topicError.value = ''
}

async function loadDiscussionTopics() {
  stopTopicCountdown()
  isLoadingTopics.value = true
  topicError.value = ''
  try {
    const data = await requestJson<{ topics: string[] }>('/api/discussions/topics', {
      method: 'POST',
      body: JSON.stringify({ difficulty: difficulty.value }),
    })
    topicOptions.value = data.topics.filter((topic) => topic.trim()).slice(0, 3)
    selectedDiscussionTopic.value = topicOptions.value[0] ?? ''
    debugLog('discussion.topics_loaded', { count: topicOptions.value.length })
    if (selectedDiscussionTopic.value) {
      startTopicCountdown()
    }
  } catch (error) {
    topicOptions.value = []
    selectedDiscussionTopic.value = ''
    topicError.value = error instanceof Error ? error.message : String(error)
    debugLog('discussion.topics_failed', { message: topicError.value })
  } finally {
    isLoadingTopics.value = false
  }
}

function startTopicCountdown() {
  stopTopicCountdown()
  topicCountdownSec.value = 10
  topicCountdownTimer = window.setInterval(() => {
    topicCountdownSec.value = Math.max(0, topicCountdownSec.value - 1)
    if (topicCountdownSec.value > 0) return
    stopTopicCountdown()
    if (!isTopicDialogOpen.value || isLoading.value || isLoadingTopics.value) return
    debugLog('discussion.topic_countdown_expired', { topic: selectedDiscussionTopic.value })
    void startSelectedDiscussionTopic()
  }, 1000)
}

function stopTopicCountdown() {
  window.clearInterval(topicCountdownTimer)
  topicCountdownTimer = 0
  topicCountdownSec.value = 0
}

async function selectDiscussionTopic(topic: string) {
  selectedDiscussionTopic.value = topic
  await startSelectedDiscussionTopic()
}

async function startSelectedDiscussionTopic() {
  if (!selectedDiscussionTopic.value) return
  stopTopicCountdown()
  isTopicDialogOpen.value = false
  selectedMode.value = 'discussion'
  await startSession(selectedDiscussionTopic.value)
}

async function startSession(discussionTopic = '') {
  selectedMode.value = 'discussion'
  isLoading.value = true
  resetSessionState()
  try {
    const candidate = {
      candidateLastName: candidateLastName.value.trim(),
      candidateLastNameKana: candidateLastNameKana.value.trim(),
    }
    const data = await requestJson<{
      interviewId: string
      mode: SessionMode
      durationSec: number
      interviewer: Interviewer
      scenario: Scenario
      openingQuestion: string
    }>('/api/discussions/start', {
      method: 'POST',
      body: JSON.stringify(
        { ...candidate, difficulty: difficulty.value, discussionTopic, discussionPersonaKey: selectedDiscussionPersonaKey.value },
      ),
    })
    debugLog('session.started', { id: data.interviewId, mode: data.mode, durationSec: data.durationSec })
    interviewId.value = data.interviewId
    activeMode.value = data.mode
    durationSec.value = data.durationSec
    interviewer.value = data.interviewer
    scenario.value = data.scenario
    void requestWakeLock()
    await loadInterviewerAssets(data.scenario)
    pendingOpeningQuestion.value = data.openingQuestion
    awaitingOpeningTranscript.value = true
    await connectVoiceSocket(data.interviewId)
  } catch (error) {
    debugLog('session.start_failed', { message: error instanceof Error ? error.message : String(error) })
    interviewId.value = ''
    activeMode.value = null
    pendingOpeningQuestion.value = ''
    awaitingOpeningTranscript.value = false
    closePeerConnection()
    throw error
  } finally {
    isLoading.value = false
  }
}

async function loadAudioDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return
  const devices = await navigator.mediaDevices.enumerateDevices()
  const audioInputs = devices.filter((device) => device.kind === 'audioinput')
  const audioOutputs = devices.filter((device) => device.kind === 'audiooutput')
  inputDevices.value = audioInputs
    .map((device, index) => ({
      deviceId: device.deviceId,
      groupId: device.groupId,
      label: labelAudioDevice(device, audioInputs, `入力デバイス ${index + 1}`),
    }))
  outputDevices.value = audioOutputs
    .map((device, index) => ({
      deviceId: device.deviceId,
      groupId: device.groupId,
      label: labelAudioDevice(device, audioOutputs, `出力デバイス ${index + 1}`),
    }))
  if (inputDeviceId.value && !inputDevices.value.some((device) => device.deviceId === inputDeviceId.value)) {
    inputDeviceId.value = ''
  }
  if (!inputDeviceId.value) {
    const preferred = choosePreferredInputDevice(inputDevices.value)
    if (preferred) {
      inputDeviceId.value = preferred.deviceId
      debugLog('mic.input_auto_selected', { label: preferred.label })
    }
  }
  if (outputDeviceId.value && !outputDevices.value.some((device) => device.deviceId === outputDeviceId.value)) {
    outputDeviceId.value = ''
  }
}

async function openDeviceDialog() {
  isDeviceDialogOpen.value = true
  isLoadingDevices.value = true
  deviceError.value = ''
  try {
    await loadAudioDevices()
  } catch {
    deviceError.value = 'デバイス一覧を取得できませんでした。ブラウザの権限設定を確認してください。'
  } finally {
    isLoadingDevices.value = false
  }
}

async function refreshAudioDevicesWithPermission() {
  logBrowserAudioEnvironment()
  if (!navigator.mediaDevices?.getUserMedia) {
    deviceError.value = 'このURLではマイクAPIを利用できません。HTTPSで開いて証明書を許可してください。'
    return
  }
  isLoadingDevices.value = true
  deviceError.value = ''
  try {
    const stream = await navigator.mediaDevices.getUserMedia(buildMicConstraints())
    await applyMicEnhancements(stream)
    stream.getTracks().forEach((track) => track.stop())
    await loadAudioDevices()
  } catch {
    deviceError.value = 'マイク権限が許可されていないため、デバイス名を取得できませんでした。'
  } finally {
    isLoadingDevices.value = false
  }
}

async function prepareInputDeviceForSession() {
  logBrowserAudioEnvironment()
  await loadAudioDevices()
  const selected = inputDevices.value.find((device) => device.deviceId === inputDeviceId.value)
  if (selected && selected.deviceId !== 'default' && isIphoneLikeAudioDevice(selected)) {
    inputDeviceId.value = ''
  }
  if (!inputDeviceId.value) {
    const preferred = choosePreferredInputDevice(inputDevices.value)
    if (preferred) {
      inputDeviceId.value = preferred.deviceId
      debugLog('mic.input_selected_for_session', { label: preferred.label })
    }
  }
  if (!inputDeviceId.value) {
    debugLog('mic.input_auto_fallback')
  }
  const finalSelected = inputDevices.value.find((device) => device.deviceId === inputDeviceId.value)
  debugLog('mic.input_ready', {
    selected: finalSelected?.label || 'auto',
    deviceId: inputDeviceId.value ? 'selected' : 'auto',
  })
}

function closeDeviceDialog() {
  isDeviceDialogOpen.value = false
}

function countSdpCandidates(sdp = '') {
  return sdp.split('\n').filter((line) => line.startsWith('a=candidate:')).length
}

async function waitForIceGatheringComplete(pc: RTCPeerConnection, timeoutMs = 5000) {
  if (pc.iceGatheringState === 'complete') return
  await new Promise<void>((resolve) => {
    const timeout = window.setTimeout(() => {
      pc.removeEventListener('icegatheringstatechange', onStateChange)
      debugLog('webrtc.ice_gathering_timeout', {
        state: pc.iceGatheringState,
        candidates: countSdpCandidates(pc.localDescription?.sdp),
      })
      resolve()
    }, timeoutMs)

    function onStateChange() {
      if (pc.iceGatheringState !== 'complete') return
      window.clearTimeout(timeout)
      pc.removeEventListener('icegatheringstatechange', onStateChange)
      resolve()
    }

    pc.addEventListener('icegatheringstatechange', onStateChange)
  })
}

function isPeerConnectionUsable(pc: RTCPeerConnection) {
  return pc.connectionState === 'connected' || ['connected', 'completed'].includes(pc.iceConnectionState)
}

async function waitForPeerConnectionConnected(pc: RTCPeerConnection, timeoutMs = 30000) {
  if (isPeerConnectionUsable(pc)) return
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pc.removeEventListener('connectionstatechange', onStateChange)
      pc.removeEventListener('iceconnectionstatechange', onStateChange)
      reject(new Error(`Cloudflare Realtime の接続が完了しませんでした: ${pc.connectionState}/${pc.iceConnectionState}`))
    }, timeoutMs)

    function onStateChange() {
      if (isPeerConnectionUsable(pc)) {
        window.clearTimeout(timeout)
        pc.removeEventListener('connectionstatechange', onStateChange)
        pc.removeEventListener('iceconnectionstatechange', onStateChange)
        resolve()
        return
      }
      if (['failed', 'closed'].includes(pc.connectionState) || ['failed', 'closed'].includes(pc.iceConnectionState)) {
        window.clearTimeout(timeout)
        pc.removeEventListener('connectionstatechange', onStateChange)
        pc.removeEventListener('iceconnectionstatechange', onStateChange)
        reject(new Error(`Cloudflare Realtime の接続に失敗しました: ${pc.connectionState}/${pc.iceConnectionState}`))
      }
    }

    pc.addEventListener('connectionstatechange', onStateChange)
    pc.addEventListener('iceconnectionstatechange', onStateChange)
  })
}

async function waitForRemoteAudioTrackReady(timeoutMs = 800) {
  if (remoteAudioTrackReady || remoteStream?.getAudioTracks().length) return
  await new Promise<void>((resolve) => {
    function onTrack(event: Event) {
      const track = (event as MediaStreamTrackEvent).track
      if (track?.kind !== 'audio') return
      window.clearTimeout(timeout)
      remoteStream?.removeEventListener('addtrack', onTrack)
      resolve()
    }

    const timeout = window.setTimeout(() => {
      remoteStream?.removeEventListener('addtrack', onTrack)
      resolve()
    }, timeoutMs)

    remoteStream?.addEventListener('addtrack', onTrack)
  })
}

async function connectVoiceSocket(id: string) {
  const transportInfo = await loadVoiceTransportInfo()
  let lastError: unknown = null
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      await connectCloudflareRealtime(id, transportInfo, attempt)
      return
    } catch (error) {
      lastError = error
      debugLog('cloudflare_realtime.connect_attempt_failed', {
        attempt,
        message: error instanceof Error ? error.message : String(error),
      })
      closePeerConnection()
      if (attempt < 2) {
        await new Promise((resolve) => window.setTimeout(resolve, 900))
      }
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError))
}

function stopVoiceEventsPolling() {
  window.clearTimeout(voiceEventsPollTimer)
  voiceEventsPollTimer = 0
  voiceEventsCursor = 0
}

function startVoiceEventsPolling(id: string) {
  stopVoiceEventsPolling()
  const poll = async () => {
    if (!interviewId.value || interviewId.value !== id) return
    try {
      const response = await requestJson<{ cursor: number; events: Array<{ type?: string; [key: string]: unknown }> }>(
        `/api/interviews/${id}/voice/events?cursor=${voiceEventsCursor}`,
      )
      voiceEventsCursor = response.cursor
      for (const event of response.events) {
        handleVoiceEvent(id, event)
      }
    } catch (error) {
      debugLog('cloudflare_realtime.events_failed', { message: error instanceof Error ? error.message : String(error) })
    }
    voiceEventsPollTimer = window.setTimeout(poll, 500)
  }
  voiceEventsPollTimer = window.setTimeout(poll, 300)
}

async function connectCloudflareRealtime(id: string, transportInfo: VoiceTransportInfo, attempt = 1) {
  closePeerConnection()
  shouldReconnectVoice = true
  voiceSocketOpenedAt = performance.now()
  debugLog('cloudflare_realtime.connecting', { id, attempt })

  await prepareInputDeviceForSession()
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('マイクAPIを利用できません。Cloudflare Realtime の疎通確認も HTTPS または localhost が必要です。')
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia(buildMicConstraints())
  } catch (error) {
    if (!inputDeviceId.value) throw error
    debugLog('mic.selected_input_failed', {
      message: error instanceof Error ? error.message : String(error),
    })
    inputDeviceId.value = ''
    mediaStream = await navigator.mediaDevices.getUserMedia(buildMicConstraints())
  }
  await applyMicEnhancements(mediaStream)
  startMicMonitor(mediaStream)
  await loadAudioDevices()
  isMicActive.value = true
  debugLog('mic.started', { transport: 'cloudflare-realtime' })

  const pc = new RTCPeerConnection({
    iceServers: transportInfo.stunServers.map((url) => ({ urls: url })),
  })
  peerConnection = pc
  remoteStream = new MediaStream()

  const micTrackName = `browser-mic-${id}`
  const audioTrack = mediaStream.getAudioTracks()[0]
  if (!audioTrack) throw new Error('マイクトラックを取得できませんでした。')
  const transceiver = pc.addTransceiver(audioTrack, { direction: 'sendrecv' })

  pc.ontrack = (event) => {
    debugLog('cloudflare_realtime.remote_track', { kind: event.track.kind, muted: event.track.muted })
    if (event.track.kind === 'audio') {
      remoteAudioTrackReady = true
    }
    event.track.onmute = () => {
      debugLog('cloudflare_realtime.remote_track_muted', { kind: event.track.kind })
    }
    event.track.onunmute = () => {
      debugLog('cloudflare_realtime.remote_track_unmuted', { kind: event.track.kind })
      void aiAudio.value?.play().then(
        () => debugLog('playback.playing', { source: 'cloudflare-realtime' }),
        (error: unknown) => debugLog('playback.play_failed', { message: error instanceof Error ? error.message : String(error) }),
      )
    }
    remoteStream?.addTrack(event.track)
    if (aiAudio.value && remoteStream) {
      aiAudio.value.muted = false
      aiAudio.value.volume = 1
      aiAudio.value.srcObject = remoteStream
      void aiAudio.value.play().then(
        () => debugLog('playback.playing', { source: 'cloudflare-realtime' }),
        (error: unknown) => debugLog('playback.play_failed', { message: error instanceof Error ? error.message : String(error) }),
      )
    }
  }

  pc.onconnectionstatechange = () => {
    debugLog('cloudflare_realtime.state', { state: pc.connectionState })
    if (pc.connectionState === 'connected') {
      reconnectAttempts = 0
      window.clearTimeout(connectionWatchdogTimer)
      return
    }
    if (['failed', 'disconnected', 'closed'].includes(pc.connectionState) && shouldReconnectVoice && interviewId.value === id) {
      scheduleVoiceReconnect(id)
    }
  }
  pc.oniceconnectionstatechange = () => {
    debugLog('cloudflare_realtime.ice_state', { state: pc.iceConnectionState })
  }
  pc.onicegatheringstatechange = () => {
    debugLog('cloudflare_realtime.ice_gathering', { state: pc.iceGatheringState })
  }

  const offer = await pc.createOffer()
  await pc.setLocalDescription(offer)
  await waitForIceGatheringComplete(pc, 8000)
  const micMid = transceiver.mid
  if (!micMid) throw new Error('Cloudflare Realtime に渡す mic mid を取得できませんでした。')
  debugLog('cloudflare_realtime.offer_ready', {
    mid: micMid,
    candidates: countSdpCandidates(pc.localDescription?.sdp),
  })
  debugLog('cloudflare_realtime.session_requesting', {
    candidates: countSdpCandidates(pc.localDescription?.sdp),
    sdpLength: pc.localDescription?.sdp?.length ?? 0,
  })
  const sessionSnapshot = {
    mode: activeMode.value ?? selectedMode.value ?? 'discussion',
    durationSec: durationSec.value,
    candidateLastName: candidateLastName.value.trim(),
    candidateLastNameKana: candidateLastNameKana.value.trim(),
    difficulty: difficulty.value,
    interviewer: interviewer.value,
    scenario: scenario.value,
    openingQuestion: pendingOpeningQuestion.value,
  }
  const response = await requestJson<{
    sessionId: string
    sessionDescription?: RTCSessionDescriptionInit
    requiresImmediateRenegotiation?: boolean
    remoteSessionDescription?: RTCSessionDescriptionInit
    remoteRequiresImmediateRenegotiation?: boolean
    tracks?: unknown[]
    remoteTracks?: unknown[]
    adapters?: unknown[]
  }>(`/api/interviews/${id}/realtime/session`, {
    method: 'POST',
    body: JSON.stringify({
      sdp: pc.localDescription?.sdp,
      type: offer.type,
      micTrackName,
      micMid,
      sessionSnapshot,
    }),
  })
  realtimeSessionId = response.sessionId
  debugLog('cloudflare_realtime.session_created', {
    sessionId: response.sessionId,
    hasSessionDescription: Boolean(response.sessionDescription),
  })
  let tracksSdp = pc.localDescription?.sdp
  let tracksType = offer.type
  if (response.sessionDescription) {
    await pc.setRemoteDescription(response.sessionDescription)

    const tracksOffer = await pc.createOffer()
    await pc.setLocalDescription(tracksOffer)
    await waitForIceGatheringComplete(pc, 8000)
    tracksSdp = pc.localDescription?.sdp
    tracksType = tracksOffer.type
  }
  debugLog('cloudflare_realtime.tracks_requesting', {
    sessionId: response.sessionId,
    type: tracksType,
    sdpLength: tracksSdp?.length ?? 0,
  })
  const tracksResponse = await requestJson<{
    sessionId: string
    micTrackName?: string
    sessionDescription?: RTCSessionDescriptionInit
    requiresImmediateRenegotiation?: boolean
    tracks?: unknown[]
  }>(`/api/interviews/${id}/realtime/tracks`, {
    method: 'POST',
    body: JSON.stringify({
      sessionId: response.sessionId,
      sdp: tracksSdp,
      type: tracksType,
      micTrackName,
      micMid,
      sessionSnapshot,
    }),
  })
  if (tracksResponse.sessionDescription) {
    await pc.setRemoteDescription(tracksResponse.sessionDescription)
  }
  const publishedMicTrackName = tracksResponse.micTrackName || micTrackName
  debugLog('cloudflare_realtime.answer_applied', {
    sessionId: response.sessionId,
    tracks: tracksResponse.tracks?.length ?? 0,
    micTrackName: publishedMicTrackName,
  })

  debugLog('cloudflare_realtime.waiting_connected', {
    sessionId: response.sessionId,
    state: pc.connectionState,
    iceState: pc.iceConnectionState,
  })
  await waitForPeerConnectionConnected(pc)
  debugLog('cloudflare_realtime.connected_for_adapters', {
    sessionId: response.sessionId,
    state: pc.connectionState,
    iceState: pc.iceConnectionState,
  })
  debugLog('cloudflare_realtime.adapters_requesting', {
    sessionId: response.sessionId,
    micTrackName: publishedMicTrackName,
  })
  const adaptersResponse = await requestJson<{
    sessionId: string
    remoteSessionDescription?: RTCSessionDescriptionInit
    remoteRequiresImmediateRenegotiation?: boolean
    remoteTracks?: unknown[]
    adapters?: unknown[]
  }>(`/api/interviews/${id}/realtime/adapters`, {
    method: 'POST',
    body: JSON.stringify({
      sessionId: response.sessionId,
      micTrackName: publishedMicTrackName,
      sessionSnapshot,
    }),
  })
  if (adaptersResponse.remoteSessionDescription) {
    await pc.setRemoteDescription(adaptersResponse.remoteSessionDescription)
    const answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    await requestJson(`/api/interviews/${id}/realtime/renegotiate`, {
      method: 'PUT',
      body: JSON.stringify({
        sessionId: response.sessionId,
        sdp: pc.localDescription?.sdp,
        type: answer.type,
      }),
    })
  }
  debugLog('cloudflare_realtime.adapters_created', {
    sessionId: response.sessionId,
    remoteTracks: adaptersResponse.remoteTracks?.length ?? 0,
    adapters: adaptersResponse.adapters?.length ?? 0,
  })
  await waitForRemoteAudioTrackReady()
  handleVoiceEvent(id, { type: 'setup_complete' })
  startVoiceEventsPolling(id)

  window.clearTimeout(connectionWatchdogTimer)
  connectionWatchdogTimer = window.setTimeout(() => {
    if (peerConnection !== pc) return
    if (pc.connectionState === 'connected') return
    debugLog('cloudflare_realtime.connect_timeout', {
      state: pc.connectionState,
      ice: pc.iceConnectionState,
      apiBase,
    })
    scheduleVoiceReconnect(id)
  }, 10000)
}

function handleVoiceEvent(id: string, payload: { type?: string; [key: string]: unknown }) {
  if (payload.type !== 'input_transcript' && payload.type !== 'output_transcript') {
    debugLog('voice.message', { type: payload.type })
  }
  if (payload.type === 'input_transcript') {
    const text = String(payload.text ?? '')
    upsertTranscript('user', text)
  } else if (payload.type === 'output_transcript') {
    if (suppressingAnswerResponseBeforeConclusion && !discussionConclusionRequested.value) {
      const text = String(payload.text ?? '')
      debugLog('discussion.pre_conclusion_response_transcript_suppressed', { chars: text.length })
      return
    }
    isSpeaking.value = true
    if (openingSpeechActive.value) {
      openingOutputStarted = true
    }
    window.clearTimeout(openingCompleteTimer)
    window.clearTimeout(finalFinishTimer)
    const text = String(payload.text ?? '')
    if (payload.fallback && activeMode.value === 'discussion') {
      debugLog('discussion.fallback_transcript_ignored', { chars: text.length })
      return
    }
    if (containsAssistantQuestion(text)) {
      assistantQuestionPendingBeforeConclusion = true
    }
    upsertTranscript('assistant', text)
  } else if (payload.type === 'turn_complete') {
    responseStartedAt.value = null
    if (!(discussionEnding.value && conclusionPlaybackReadyAt > performance.now())) {
      isSpeaking.value = false
    }
    lastQuestionAt.value = performance.now()
    if (openingSpeechActive.value) {
      openingTurnComplete = true
      completeOpeningSpeechIfReady()
      return
    }
    handleTurnComplete()
    finishAfterPlaybackIfReady()
  } else if (payload.type === 'discussion_conclusion_complete') {
    debugLog('discussion.conclusion_complete', {
      fallbackTranscript: payload.fallbackTranscript,
      turnAudioChunks: payload.turnAudioChunks,
    })
    markDiscussionConclusionComplete(Number(payload.turnAudioChunks ?? 0))
  } else if (payload.type === 'interrupted') {
    debugLog('voice.interrupted')
    isSpeaking.value = false
    finishWhenPlaybackEnds.value = false
  } else if (payload.type === 'session.reconnect') {
    debugLog('webrtc.reconnect_requested', { reason: payload.reason ?? 'unknown' })
    scheduleVoiceReconnect(id)
  } else if (payload.type === 'setup_complete') {
    sendOpeningQuestionToGemini()
  } else if (payload.type === 'assistant_response_started') {
    if (conclusionPendingAfterUserAnswer && !discussionConclusionRequested.value) {
      suppressingAnswerResponseBeforeConclusion = true
      isSpeaking.value = false
      if (aiAudio.value) {
        aiAudio.value.muted = true
        aiAudio.value.pause()
      }
      sendVoiceControl({ type: 'interrupt', reason: 'suppress_answer_response_before_conclusion' })
      debugLog('discussion.pre_conclusion_response_suppressed', { source: payload.source })
      return
    }
    playbackInterruptedThisAssistantTurn = false
    assistantResponseStartedAt = performance.now()
    assistantAudioFinishedAt = 0
    if (aiAudio.value) {
      aiAudio.value.muted = false
      aiAudio.value.volume = 1
      void aiAudio.value.play().catch(() => undefined)
    }
    debugLog('assistant.response_started', { source: payload.source })
  } else if (payload.type === 'assistant_audio_started') {
    if (!suppressingAnswerResponseBeforeConclusion) {
      isSpeaking.value = true
    }
    debugLog('assistant.audio_started', { chunks: payload.chunks })
  } else if (payload.type === 'assistant_audio_finished') {
    assistantAudioFinishedAt = performance.now()
    debugLog('assistant.audio_finished', {
      turnAudioChunks: payload.turnAudioChunks,
      outputAudioPackets: payload.outputAudioPackets,
      flushedBuffered: payload.flushedBufferedInputFrames,
    })
  } else if (payload.type === 'assistant_generation_complete') {
    debugLog('assistant.generation_complete', {
      outputQueue: payload.outputQueue,
      turnAudioChunks: payload.turnAudioChunks,
      buffering: payload.bufferingUntilTurnComplete,
    })
  } else if (payload.type === 'buffered_input_flushed') {
    debugLog('buffered.input_flushed', {
      frames: payload.frames,
      geminiQueue: payload.geminiQueue,
    })
  } else if (payload.type === 'bridge_stats') {
    debugLog('bridge.stats', {
      reason: payload.reason,
      inFrames: payload.inputAudioFrames,
      sent: payload.geminiSentMessages,
      outChunks: payload.geminiAudioChunks,
      outPackets: payload.outputAudioPackets,
      inText: payload.inputTranscripts,
      outText: payload.outputTranscripts,
      silence: payload.silencePaddingFrames,
      preroll: payload.outputPrerollPackets,
      tail: payload.outputTailPackets,
      suppressed: payload.suppressedInputAudioFrames,
      buffered: payload.bufferedInputAudioFrames,
      flushed: payload.flushedBufferedInputAudioFrames,
      rmsAvg: payload.rmsAvg,
      rmsMax: payload.rmsMax,
      peak: payload.peak,
      zeroRatio: payload.zeroRatio,
      bytesPerFrame: payload.bytesPerFrame,
      geminiQueue: payload.geminiQueue,
      outputQueue: payload.outputQueue,
      sinceTurn: payload.secondsSinceLastTurn,
      sinceGemini: payload.secondsSinceLastGeminiMessage,
      lastGemini: payload.lastGeminiMessageType,
    })
  } else if (payload.type === 'gemini_response_fallback') {
    debugLog('gemini.response_fallback', { chars: payload.chars })
  } else if (payload.type === 'gemini_setup_timeout') {
    debugLog('gemini.setup_timeout')
  } else if (payload.type === 'gemini_server_summary') {
    debugLog('gemini.server_summary', {
      messageType: payload.messageType,
      inTranscript: payload.hasInputTranscription,
      outTranscript: payload.hasOutputTranscription,
      modelParts: payload.modelParts,
      audioParts: payload.audioParts,
      turnComplete: payload.turnComplete,
      interrupted: payload.interrupted,
      generationComplete: payload.generationComplete,
      goAway: payload.hasGoAway,
    })
  } else if (payload.type === 'gemini_input_stalled') {
    debugLog('gemini.input_stalled', {
      framesSinceTurn: payload.framesSinceTurn,
      sentSinceTurn: payload.sentSinceTurn,
      sinceTurn: payload.secondsSinceLastTurn,
      sinceGemini: payload.secondsSinceLastGeminiMessage,
      lastGemini: payload.lastGeminiMessageType,
      audioStreamEndSent: payload.audioStreamEndSent,
      rmsAvg: payload.rmsAvg,
      rmsMax: payload.rmsMax,
      peak: payload.peak,
      zeroRatio: payload.zeroRatio,
      captureId: payload.captureId,
      captureSeconds: payload.captureSeconds,
    })
  } else if (payload.type === 'gemini_audio_stream_end_sent') {
    debugLog('gemini.audio_stream_end_sent', {
      reason: payload.reason,
      framesSinceTurn: payload.framesSinceTurn,
      sentSinceTurn: payload.sentSinceTurn,
      sinceTurn: payload.secondsSinceLastTurn,
      sinceInput: payload.secondsSinceLastInputAudio,
      sinceGemini: payload.secondsSinceLastGeminiMessage,
      queue: payload.geminiQueue,
    })
  } else if (payload.type === 'gemini_silence_padding_sent') {
    debugLog('gemini.silence_padding_sent', {
      reason: payload.reason,
      frames: payload.frames,
      totalFrames: payload.totalFrames,
      framesSinceTurn: payload.framesSinceTurn,
      sentSinceTurn: payload.sentSinceTurn,
      sinceTurn: payload.secondsSinceLastTurn,
      sinceInput: payload.secondsSinceLastInputAudio,
      sinceGemini: payload.secondsSinceLastGeminiMessage,
      queue: payload.geminiQueue,
    })
  } else if (payload.type === 'cloudflare_input_adapter_refresh_requested') {
    debugLog('cloudflare_realtime.input_adapter_refresh_requested', {
      inFrames: payload.inputAudioFrames,
      sinceInput: payload.secondsSinceLastInputAudio,
    })
  } else if (payload.type === 'cloudflare_input_adapter_refreshed') {
    debugLog('cloudflare_realtime.input_adapter_refreshed', {
      inFrames: payload.inputAudioFrames,
      result: payload.result,
    })
  } else if (payload.type === 'cloudflare_input_adapter_refresh_failed') {
    debugLog('cloudflare_realtime.input_adapter_refresh_failed', {
      message: payload.message,
    })
  } else if (String(payload.type ?? '').startsWith('gemini_')) {
    debugLog(String(payload.type).split('_').join('.'), {
      messageType: payload.messageType,
      code: payload.code,
      reason: payload.reason,
      message: payload.message,
      keys: payload.keys,
      model: payload.model,
      responseModalities: payload.responseModalities,
    })
  } else if (payload.type === 'error') {
    debugLog('voice.error', { message: payload.message ?? 'unknown' })
  }
}

function canSendVoiceControl() {
  return Boolean(interviewId.value && realtimeSessionId)
}

function sendOpeningQuestionToGemini() {
  if (openingQuestionSent || !pendingOpeningQuestion.value) return
  if (!canSendVoiceControl()) return
  openingQuestionSent = true
  openingSpeechActive.value = true
  openingTurnComplete = false
  openingOutputStarted = false
  window.clearTimeout(openingCompleteTimer)
  const openingPrompt = pendingOpeningQuestion.value
  debugLog('opening.send', { chars: openingPrompt.length })
  sendVoiceControl({ type: 'text', text: openingPrompt })
}

function completeOpeningSpeechIfReady() {
  if (!openingSpeechActive.value || !openingTurnComplete || !openingOutputStarted || isSpeaking.value) {
    if (openingSpeechActive.value && openingTurnComplete && !openingOutputStarted) {
      debugLog('opening.turn_complete_without_output')
    }
    return
  }
  window.clearTimeout(openingCompleteTimer)
  debugLog('opening.complete_pending')
  openingCompleteTimer = window.setTimeout(() => {
    if (!openingSpeechActive.value || !openingTurnComplete || !openingOutputStarted || isSpeaking.value) return
    finishOpeningSpeech()
  }, 900)
}

function finishOpeningSpeech() {
  debugLog('opening.finished')
  if (activeMode.value === 'discussion' && !elapsedTimerStarted) {
    startElapsedTimer()
  }
  openingSpeechActive.value = false
}

function handleTurnComplete() {
  if (activeMode.value !== 'discussion') return
  if (conclusionPendingAfterUserAnswer && !discussionConclusionRequested.value) {
    debugLog('discussion.pre_conclusion_response_complete', { suppressed: suppressingAnswerResponseBeforeConclusion })
    requestDiscussionConclusion(true)
    return
  }
  if (discussionEnding.value) {
    if (discussionConclusionRequested.value && !finalTurnComplete) {
      debugLog('discussion.turn_complete_waiting_conclusion_audio')
      return
    }
    finalTurnComplete = true
    finishWhenPlaybackEnds.value = true
    scheduleFinalFinishCheck()
    return
  }
  if (concludeAfterNextAssistantTurn) {
    concludeAfterNextAssistantTurn = false
    debugLog('discussion.answer_response_complete_before_conclusion', { elapsedSec: elapsedSec.value })
    requestDiscussionConclusion()
    return
  }
  if (elapsedSec.value >= durationSec.value) {
    handleDiscussionTimeLimit()
  }
}

function handleDiscussionTimeLimit() {
  if (activeMode.value !== 'discussion') return
  if (discussionEnding.value || discussionConclusionRequested.value) return
  if (timeLimitConclusionScheduled) return
  timeLimitConclusionScheduled = true
  debugLog('discussion.time_limit_concluding_now', { elapsedSec: elapsedSec.value })
  playTimeLimitChime()
  timeLimitConclusionTimer = window.setTimeout(() => {
    if (activeMode.value !== 'discussion') return
    if (discussionEnding.value || discussionConclusionRequested.value) return
    requestDiscussionConclusion(true)
  }, 1250)
}

function playTimeLimitChime() {
  try {
    const bell = timeLimitBellAudio ?? new Audio('/audio/time-limit-bell.mp3')
    timeLimitBellAudio = bell
    bell.pause()
    bell.currentTime = 0
    bell.volume = 1
    const outputElement = bell as HTMLAudioElement & {
      setSinkId?: (sinkId: string) => Promise<void>
    }
    const play = () => bell.play()
    if (outputDeviceId.value && outputDeviceId.value !== 'default' && outputElement.setSinkId) {
      void outputElement.setSinkId(outputDeviceId.value).then(play).catch((error: unknown) => {
        debugLog('discussion.time_limit_chime_sink_failed', {
          message: error instanceof Error ? error.message : String(error),
        })
        void play()
      })
    } else {
      void play()
    }
    debugLog('discussion.time_limit_chime')
  } catch (error) {
    debugLog('discussion.time_limit_chime_failed', { message: error instanceof Error ? error.message : String(error) })
  }
}

function shouldWaitForUserAnswerBeforeConclusion() {
  if (discussionConclusionRequested.value || awaitingUserAnswerBeforeConclusion) return false
  if (assistantQuestionPendingBeforeConclusion) return true
  const text = latestAssistantText()
  if (!text) return false
  if (detectAssistantEndedSession(text)) return false
  return containsAssistantQuestion(text)
}

function containsAssistantQuestion(text: string) {
  const normalized = text.trim()
  if (!normalized) return false
  const questionPattern = /[?？]|(いかがですか|どう思いますか|どう考えますか|どうでしょうか|ありますか|聞かせてください|教えてください)/
  return questionPattern.test(normalized)
}

function waitForUserAnswerBeforeConclusion() {
  awaitingUserAnswerBeforeConclusion = true
  window.clearTimeout(conclusionAnswerWaitTimer)
  debugLog('discussion.waiting_user_answer_before_conclusion', { elapsedSec: elapsedSec.value })
  conclusionAnswerWaitTimer = window.setTimeout(() => {
    if (!awaitingUserAnswerBeforeConclusion || discussionEnding.value) return
    awaitingUserAnswerBeforeConclusion = false
    debugLog('discussion.answer_wait_timeout_before_conclusion', { elapsedSec: elapsedSec.value })
    requestDiscussionConclusion()
  }, 90000)
}

function handleUserAnswerBeforeConclusion(text: string) {
  if (!awaitingUserAnswerBeforeConclusion || activeMode.value !== 'discussion') return
  if (discussionEnding.value || discussionConclusionRequested.value) return
  const trimmed = text.trim()
  if (!trimmed) return
  awaitingUserAnswerBeforeConclusion = false
  assistantQuestionPendingBeforeConclusion = false
  concludeAfterNextAssistantTurn = false
  conclusionPendingAfterUserAnswer = true
  suppressingAnswerResponseBeforeConclusion = false
  window.clearTimeout(conclusionAnswerWaitTimer)
  window.clearTimeout(conclusionAfterAnswerTimer)
  debugLog('discussion.user_answer_received_before_conclusion', { chars: trimmed.length })
  if (aiAudio.value) {
    aiAudio.value.muted = true
    aiAudio.value.pause()
  }
  isSpeaking.value = false
  sendVoiceControl({ type: 'interrupt', reason: 'prepare_conclusion_after_user_answer' })
  conclusionAfterAnswerTimer = window.setTimeout(() => {
    if (!conclusionPendingAfterUserAnswer || discussionConclusionRequested.value || discussionEnding.value) return
    debugLog('discussion.conclusion_after_answer_no_response')
    requestDiscussionConclusion(true)
  }, 1500)
}

function requestDiscussionConclusion(force = false) {
  if (discussionConclusionRequested.value || !canSendVoiceControl()) return
  if (!force && shouldWaitForUserAnswerBeforeConclusion()) {
    waitForUserAnswerBeforeConclusion()
    return
  }
  awaitingUserAnswerBeforeConclusion = false
  concludeAfterNextAssistantTurn = false
  conclusionPendingAfterUserAnswer = false
  suppressingAnswerResponseBeforeConclusion = false
  window.clearTimeout(conclusionAnswerWaitTimer)
  window.clearTimeout(conclusionAfterAnswerTimer)
  if (isSpeaking.value && !force) {
    finishWhenPlaybackEnds.value = true
    debugLog('discussion.finish_waiting_playback')
    return
  }
  stopMicrophone()
  if (force && aiAudio.value) {
    aiAudio.value.muted = true
    aiAudio.value.pause()
  }
  if (force) {
    isSpeaking.value = false
    sendVoiceControl({ type: 'interrupt', reason: 'force_conclusion' })
  }
  discussionConclusionRequested.value = true
  discussionEnding.value = true
  finalTurnComplete = false
  debugLog('discussion.conclusion_requested', { elapsedSec: elapsedSec.value })
  sendVoiceControl({ type: 'discussion_conclusion' })
}

function detectAssistantEndedSession(text: string) {
  if (activeMode.value !== 'discussion') return false
  if (!text.includes('これでディスカッションを終了します')) return false
  if (discussionConclusionRequested.value || discussionEnding.value) return true
  return false
}

function handleAssistantEndedSession() {
  if (discussionEnding.value) return
  discussionConclusionRequested.value = true
  discussionEnding.value = true
  finalTurnComplete = false
  finishWhenPlaybackEnds.value = true
  elapsedSec.value = durationSec.value
  window.clearInterval(elapsedTimer)
  stopMicrophone()
  debugLog('discussion.ended_by_assistant')
  scheduleFinalFinishCheck()
}

function markDiscussionConclusionComplete(turnAudioChunks = 0) {
  discussionConclusionRequested.value = true
  discussionEnding.value = true
  finalTurnComplete = true
  finishWhenPlaybackEnds.value = true
  conclusionPlaybackReadyAt = performance.now() + Math.max(4500, Math.min(9000, turnAudioChunks * 35))
  isSpeaking.value = true
  elapsedSec.value = durationSec.value
  window.clearInterval(elapsedTimer)
  stopMicrophone()
  debugLog('discussion.conclusion_audio_complete')
  scheduleFinalFinishCheck()
}

function handleUnexpectedAssistantConclusion(text: string) {
  if (activeMode.value !== 'discussion') return
  if (discussionConclusionRequested.value || discussionEnding.value) return
  if (!text.includes('これでディスカッションを終了します')) return
  if (containsAssistantQuestion(text)) {
    trimUnexpectedAssistantConclusion()
    assistantQuestionPendingBeforeConclusion = true
    debugLog('discussion.unexpected_conclusion_ignored_waiting_answer')
    return
  }
  trimUnexpectedAssistantConclusion()
  debugLog('discussion.unexpected_conclusion_ignored')
}

function trimUnexpectedAssistantConclusion() {
  const last = messages.value[messages.value.length - 1]
  if (last?.role !== 'assistant') return
  const text = last.content
  const endingIndex = text.indexOf('これでディスカッションを終了します')
  if (endingIndex < 0) return
  const beforeEnding = text.slice(0, endingIndex)
  const questionEnd = Math.max(beforeEnding.lastIndexOf('？'), beforeEnding.lastIndexOf('?'))
  const trimmed = (questionEnd >= 0 ? beforeEnding.slice(0, questionEnd + 1) : beforeEnding).trim()
  if (!trimmed || trimmed === text) return
  last.content = trimmed
  debugLog('discussion.unexpected_conclusion_trimmed', { chars: text.length - trimmed.length })
}

function scheduleFinalFinishCheck() {
  window.clearTimeout(finalFinishTimer)
  finalFinishTimer = window.setTimeout(() => {
    debugLog('discussion.final_finish_check', { finalTurnComplete })
    finishAfterPlaybackIfReady()
  }, 1800)
}

function shouldStopAiSpeechForUserSpeech(now: number) {
  if (!isSpeaking.value) return false
  if (openingSpeechActive.value) return false
  if (!lastQuestionAt.value || now - lastQuestionAt.value < 800) return false
  return true
}

function maybeRequestAiInterruption(now: number) {
  if (activeMode.value !== 'discussion') return
  if (discussionConclusionRequested.value || discussionEnding.value) return
  if (interruptionSentThisSegment || aiInterruptionCount.value >= 2) return
  if (!canSendVoiceControl()) return
  if (isSpeaking.value) return
  if (!userSpeechActive || userSpeechSegmentStartedAt <= 0) return
  if (now - userSpeechSegmentStartedAt < 10000) return

  interruptionSentThisSegment = true
  pendingInterruptionAt = now
  pendingInterruptionResolved = false
  lastAiInterruptionAt = now
  aiInterruptionCount.value += 1
  debugLog('discussion.ai_interruption_requested', { count: aiInterruptionCount.value })
  sendVoiceSocketMetrics({
    event: 'ai_interruption',
    elapsedSec: elapsedSec.value,
    userSpeechDurationSec: (now - userSpeechSegmentStartedAt) / 1000,
  })
  sendVoiceControl({ type: 'discussion_interruption' })
}

function recordPostInterruptionContinuation(now: number) {
  if (pendingInterruptionResolved || pendingInterruptionAt <= 0) return
  if (now - pendingInterruptionAt < 1800) return
  pendingInterruptionResolved = true
  sendVoiceSocketMetrics({
    event: 'post_interruption_continued',
    elapsedSec: elapsedSec.value,
    responseAfterInterruptionSec: (now - pendingInterruptionAt) / 1000,
  })
}

function recordPostInterruptionStall(now: number) {
  if (pendingInterruptionResolved || pendingInterruptionAt <= 0) return
  if (now - pendingInterruptionAt < 4500) return
  pendingInterruptionResolved = true
  sendVoiceSocketMetrics({
    event: 'post_interruption_stalled',
    elapsedSec: elapsedSec.value,
    silenceAfterInterruptionSec: (now - pendingInterruptionAt) / 1000,
  })
}

function scheduleVoiceReconnect(id: string) {
  window.clearTimeout(reconnectTimer)
  reconnectAttempts += 1
  if (reconnectAttempts > 2) {
    shouldReconnectVoice = false
    debugLog('webrtc.reconnect_stopped', {
      reason: 'ice_connection_failed',
      apiBase,
      hint: 'Cloudflare Realtime SFU または bridge endpoint への接続を確認してください',
    })
    return
  }
  debugLog('webrtc.reconnect_scheduled', { id, attempt: reconnectAttempts })
  reconnectTimer = window.setTimeout(() => {
    if (!interviewId.value || interviewId.value !== id) return
    void connectVoiceSocket(id)
  }, 1200)
}

function sendVoiceSocketMetrics(metrics: Record<string, unknown>) {
  sendVoiceControl({ type: 'metrics', metrics })
}

function sendVoiceControl(payload: Record<string, unknown>) {
  if (interviewId.value && realtimeSessionId) {
    void requestJson(`/api/interviews/${interviewId.value}/voice/control`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }).catch((error: unknown) => {
      debugLog('cloudflare_realtime.control_failed', { message: error instanceof Error ? error.message : String(error) })
    })
  }
}

function stopMicrophone() {
  mediaStream?.getTracks().forEach((track) => track.stop())
  mediaStream = null
  isMicActive.value = false
  debugLog('mic.stopped')
}

async function requestWakeLock() {
  wakeLockRequested = true
  if (document.visibilityState !== 'visible') return
  const wakeLockApi = (navigator as WakeLockNavigator).wakeLock
  if (!wakeLockApi) {
    debugLog('wake_lock.unsupported')
    return
  }
  if (wakeLock && !wakeLock.released) return
  try {
    wakeLock = await wakeLockApi.request('screen')
    wakeLock.addEventListener('release', () => {
      debugLog('wake_lock.released')
      wakeLock = null
    })
    debugLog('wake_lock.acquired')
  } catch (error) {
    debugLog('wake_lock.failed', { message: error instanceof Error ? error.message : String(error) })
  }
}

function releaseWakeLock() {
  wakeLockRequested = false
  const currentWakeLock = wakeLock
  wakeLock = null
  if (currentWakeLock && !currentWakeLock.released) {
    void currentWakeLock.release().catch((error: unknown) => {
      debugLog('wake_lock.release_failed', { message: error instanceof Error ? error.message : String(error) })
    })
  }
}

function handleVisibilityChange() {
  if (!wakeLockRequested || document.visibilityState !== 'visible') return
  void requestWakeLock()
}

function latestAssistantText() {
  return [...messages.value].reverse().find((message) => message.role === 'assistant')?.content.trim() ?? ''
}

function finishAfterPlaybackIfReady() {
  if (!finishWhenPlaybackEnds.value) return
  if (discussionEnding.value && !finalTurnComplete) return
  if (discussionEnding.value && conclusionPlaybackReadyAt > 0) {
    const remainingMs = conclusionPlaybackReadyAt - performance.now()
    if (remainingMs > 0) {
      debugLog('discussion.finish_waiting_playback_drain', { remainingMs: Math.ceil(remainingMs) })
      window.clearTimeout(finalFinishTimer)
      finalFinishTimer = window.setTimeout(() => finishAfterPlaybackIfReady(), Math.min(remainingMs, 1000))
      return
    }
    isSpeaking.value = false
  }
  if (isSpeaking.value) return
  if (discussionEnding.value) {
    conclusionVisualEnded.value = true
    isSpeaking.value = false
  }
  debugLog('session.finish_after_playback_ready', {
    elapsedSec: elapsedSec.value,
    durationSec: durationSec.value,
    discussionEnding: discussionEnding.value,
    finalTurnComplete,
  })
  finishWhenPlaybackEnds.value = false
  if (activeMode.value === 'discussion' && elapsedSec.value >= durationSec.value && !discussionEnding.value) {
    handleDiscussionTimeLimit()
    return
  }
  void finishSession()
}

function configureOutputDevice() {
  if (!aiAudio.value) return
  if (!outputDeviceId.value || outputDeviceId.value === 'default') {
    debugLog('playback.output_default')
    return
  }
  const audioElement = aiAudio.value as HTMLAudioElement & {
    setSinkId?: (sinkId: string) => Promise<void>
  }
  if (audioElement.setSinkId) {
    void audioElement.setSinkId(outputDeviceId.value)
  }
  void aiAudio.value.play().catch((error: unknown) => {
    debugLog('playback.play_failed', { message: error instanceof Error ? error.message : String(error) })
  })
}

function startMicMonitor(stream: MediaStream) {
  stopMicMonitor()
  const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!AudioContextClass) return
  try {
    micMonitorContext = new AudioContextClass()
    micMonitorSource = micMonitorContext.createMediaStreamSource(stream)
    micMonitorAnalyser = micMonitorContext.createAnalyser()
    micMonitorAnalyser.fftSize = 1024
    micMonitorSource.connect(micMonitorAnalyser)
    const samples = new Float32Array(micMonitorAnalyser.fftSize)
    micMonitorTimer = window.setInterval(() => {
      if (!micMonitorAnalyser) return
      micMonitorAnalyser.getFloatTimeDomainData(samples)
      let sum = 0
      for (const sample of samples) {
        sum += sample * sample
      }
      const rms = Math.sqrt(sum / samples.length)
      handleMicMonitorRms(rms)
    }, 80)
  } catch (error) {
    debugLog('mic.monitor_failed', { message: error instanceof Error ? error.message : String(error) })
    stopMicMonitor()
  }
}

function stopMicMonitor() {
  window.clearInterval(micMonitorTimer)
  micMonitorTimer = 0
  micMonitorSource?.disconnect()
  micMonitorAnalyser?.disconnect()
  void micMonitorContext?.close().catch(() => undefined)
  micMonitorContext = null
  micMonitorSource = null
  micMonitorAnalyser = null
  playbackInterruptSpeechStartedAt = 0
}

function handleMicMonitorRms(rms: number) {
  const now = performance.now()
  const active = rms >= 0.012
  if (!active) {
    if (userSpeechActive && lastSpeechDetectedAt > 0 && now - lastSpeechDetectedAt > speechTailMs) {
      userSpeechActive = false
      lastUserSpeechAt = lastSpeechDetectedAt
    }
    playbackInterruptSpeechStartedAt = 0
    return
  }
  lastSpeechDetectedAt = now
  if (!userSpeechActive) {
    userSpeechActive = true
    userSpeechSegmentStartedAt = now
    interruptionSentThisSegment = false
    localPlaybackOverlapSentThisSegment = false
  }
  recordLocalPlaybackOverlapIfNeeded(now, rms)
  if (playbackInterruptSpeechStartedAt <= 0) {
    playbackInterruptSpeechStartedAt = now
  }
  if (!shouldInterruptAssistantPlayback(now)) return
  if (now - playbackInterruptSpeechStartedAt < 120) return
  playbackInterruptedThisAssistantTurn = true
  debugLog('playback.local_interrupt', { rms: Number(rms.toFixed(4)) })
  if (aiAudio.value) {
    aiAudio.value.muted = true
    aiAudio.value.volume = 0
  }
  isSpeaking.value = false
  sendVoiceControl({ type: 'interrupt', reason: 'local_user_speech_during_playback' })
}

function recordLocalPlaybackOverlapIfNeeded(now: number, rms: number) {
  if (activeMode.value !== 'discussion') return
  if (openingSpeechActive.value) return
  if (discussionConclusionRequested.value || discussionEnding.value) return
  if (localPlaybackOverlapSentThisSegment) return
  if (!isAssistantPlaybackLikelyActive(now)) return
  localPlaybackOverlapSentThisSegment = true
  debugLog('playback.local_overlap', { rms: Number(rms.toFixed(4)) })
  sendVoiceSocketMetrics({
    event: 'local_playback_overlap',
    elapsedSec: elapsedSec.value,
    rms: Number(rms.toFixed(4)),
  })
}

function isAssistantPlaybackLikelyActive(now: number) {
  if (isSpeaking.value) return true
  if (!aiAudio.value || aiAudio.value.paused || aiAudio.value.muted || aiAudio.value.volume <= 0) return false
  if (assistantResponseStartedAt <= 0 || now - assistantResponseStartedAt > 30000) return false
  return assistantAudioFinishedAt <= 0 || now - assistantAudioFinishedAt < 3500
}

function shouldInterruptAssistantPlayback(now: number) {
  if (activeMode.value !== 'discussion') return false
  if (!isSpeaking.value) return false
  if (openingSpeechActive.value) return false
  if (discussionConclusionRequested.value || discussionEnding.value) return false
  if (playbackInterruptedThisAssistantTurn) return false
  if (!assistantResponseStartedAt || now - assistantResponseStartedAt < 500) return false
  return true
}

function closePeerConnection() {
  stopMicMonitor()
  stopMicrophone()
  window.clearTimeout(connectionWatchdogTimer)
  stopVoiceEventsPolling()
  if (interviewId.value) {
    void requestJson(`/api/interviews/${interviewId.value}/realtime/close`, {
      method: 'POST',
    }).catch((error: unknown) => {
      debugLog('cloudflare_realtime.close_failed', { message: error instanceof Error ? error.message : String(error) })
    })
  }
  peerConnection?.close()
  peerConnection = null
  remoteStream = null
  realtimeSessionId = ''
  if (aiAudio.value) {
    aiAudio.value.srcObject = null
  }
}

function upsertTranscript(role: 'assistant' | 'user', text: string) {
  const trimmed = text.trim()
  if (!trimmed) return
  if (role === 'user' || trimmed.length >= 8) {
    debugLog('transcript.update', { role, chars: trimmed.length })
  }
  if (role === 'assistant' && awaitingOpeningTranscript.value) {
    messages.value = [{ role, content: trimmed }]
    awaitingOpeningTranscript.value = false
    return
  }
  const last = messages.value[messages.value.length - 1]
  const shouldStartNewAssistantMessage =
    role === 'assistant' &&
    last?.role === 'assistant' &&
    discussionConclusionRequested.value &&
    containsAssistantQuestion(last.content) &&
    trimmed.includes('これでディスカッションを終了します')
  if (last?.role === role) {
    if (shouldStartNewAssistantMessage) {
      messages.value.push({ role, content: trimmed })
    } else {
      last.content = `${last.content}${trimmed}`
    }
  } else {
    messages.value.push({ role, content: trimmed })
  }
  if (role === 'assistant' && activeMode.value === 'discussion' && containsAssistantQuestion(latestAssistantText())) {
    assistantQuestionPendingBeforeConclusion = true
  }
  if (role === 'assistant') {
    handleUnexpectedAssistantConclusion(latestAssistantText())
  }
}

const detailedAnalysis = computed(() => feedback.value?.detailedAnalysis ?? null)

const RUBRIC_LEVEL_LABELS: Record<RubricLevel, string> = {
  excellent: '優れている',
  good: '良好',
  fair: '改善余地あり',
  needs_work: '要改善',
}

function rubricLevelLabel(level: RubricLevel) {
  return RUBRIC_LEVEL_LABELS[level] ?? level
}

function formatPercent(value: number | undefined) {
  if (value === undefined || value === null) return '—'
  return `${Math.round(value * 100)}%`
}

function formatNumber(value: number | undefined, unit = '') {
  if (value === undefined || value === null) return '—'
  return `${Math.round(value)}${unit}`
}

function formatSeconds(value: number | undefined) {
  if (value === undefined || value === null) return '—'
  return `${value.toFixed(1)}秒`
}

function formatAudioRange(entry: TimelineEntry) {
  if (entry.audioStartSec === undefined || entry.audioEndSec === undefined) return ''
  return `${formatSeconds(entry.audioStartSec)}〜${formatSeconds(entry.audioEndSec)}`
}

const TRANSCRIPT_SOURCE_LABELS: Record<TranscriptSource, string> = {
  post_audio_transcription: '採点用音声文字起こし',
  realtime_fallback: 'リアルタイム文字起こしを使用',
  mixed: '一部未完了のため混在',
}

function transcriptSourceLabel(source: TranscriptSource | undefined) {
  if (!source) return ''
  return TRANSCRIPT_SOURCE_LABELS[source] ?? source
}

const EMOTION_SIGNAL_LEVEL_LABELS: Record<EmotionSignalLevel, string> = {
  high: '強め',
  medium: '中程度',
  low: '低め',
}

function emotionSignalLevelLabel(level: EmotionSignalLevel) {
  return EMOTION_SIGNAL_LEVEL_LABELS[level] ?? level
}

async function finishSession() {
  if (!interviewId.value) return
  if (isScoringFeedback.value) return
  debugLog('session.finish_requested', {
    elapsedSec: elapsedSec.value,
    durationSec: durationSec.value,
    discussionEnding: discussionEnding.value,
    finishWhenPlaybackEnds: finishWhenPlaybackEnds.value,
  })
  shouldReconnectVoice = false
  releaseWakeLock()
  window.clearTimeout(reconnectTimer)
  window.clearTimeout(finalFinishTimer)
  window.clearInterval(elapsedTimer)
  closePeerConnection()
  stopSpeakingAudio()
  conclusionVisualEnded.value = true
  isScoringFeedback.value = true
  try {
    feedback.value = await requestJson<Feedback>(`/api/interviews/${interviewId.value}/finish`, {
      method: 'POST',
    })
  } finally {
    isScoringFeedback.value = false
  }
}

onMounted(() => {
  document.addEventListener('visibilitychange', handleVisibilityChange)
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  releaseWakeLock()
})

</script>

<template>
  <main class="shell">
    <section v-if="!selectedMode" class="mode-select">
      <div class="intro-copy">
        <p class="eyebrow">RoleTalk</p>
        <h1>3分間でAIとディスカッションをして、あなたの話し方を診断</h1>
        <p>
          対話するAI人格を選び、入出力デバイスを確認して開始します。
          開始後はお題を3種類の中から10秒以内に選び、そのまま3分間AIと議論します。
        </p>
      </div>
      <div class="name-fields">
        <label>
          名字
          <input v-model="candidateLastName" type="text" placeholder="例: 山田" autocomplete="family-name" />
        </label>
        <label>
          読み
          <input v-model="candidateLastNameKana" type="text" placeholder="例: やまだ" />
        </label>
      </div>

      <section class="persona-section" aria-labelledby="persona-heading">
        <div>
          <p class="eyebrow">AI Persona</p>
          <h2 id="persona-heading">対話する相手を選択</h2>
        </div>
        <div class="persona-grid">
          <button
            v-for="persona in discussionPersonas"
            :key="persona.key"
            type="button"
            class="persona-card"
            :class="{ selected: selectedDiscussionPersonaKey === persona.key }"
            @click="selectedDiscussionPersonaKey = persona.key"
          >
            <img :src="persona.imageSrc" :alt="persona.label" />
            <span>{{ persona.label }}</span>
            <strong>{{ persona.lead }}</strong>
            <p>{{ persona.description }}</p>
          </button>
        </div>
      </section>

      <div class="start-panel">
        <div>
          <p class="eyebrow">Selected</p>
          <strong>{{ selectedDiscussionPersona.label }}</strong>
          <span>{{ selectedDiscussionPersona.lead }}</span>
        </div>
        <div class="start-actions">
          <button class="secondary" type="button" @click="openDeviceDialog">入出力デバイス設定</button>
          <button class="primary" :disabled="isLoading || !hasCandidateName" @click="openTopicDialog">
            ディスカッションを開始
          </button>
        </div>
      </div>
    </section>

    <div v-if="isTopicDialogOpen" class="modal-backdrop" @click.self="closeTopicDialog">
      <section class="topic-dialog" role="dialog" aria-modal="true" aria-labelledby="topic-dialog-title">
        <div class="dialog-header">
          <div>
            <p class="eyebrow">ディスカッションのお題</p>
            <h2 id="topic-dialog-title">話したいテーマを選択</h2>
          </div>
          <div class="topic-countdown" :class="{ urgent: topicCountdownSec <= 3 && topicCountdownSec > 0 }">
            <span>残り</span>
            <strong>{{ topicCountdownSec || '—' }}</strong>
            <span>秒</span>
          </div>
        </div>
        <div v-if="isLoadingTopics" class="topic-loading">お題を生成しています...</div>
        <div v-else class="topic-options">
          <button
            v-for="topic in topicOptions"
            :key="topic"
            type="button"
            class="topic-option"
            :class="{ selected: selectedDiscussionTopic === topic }"
            @click="selectDiscussionTopic(topic)"
          >
            {{ topic }}
          </button>
          <p v-if="topicError" class="device-error">お題候補を取得できませんでした: {{ topicError }}</p>
        </div>
        <div class="dialog-footer">
          <button class="secondary" type="button" :disabled="isLoadingTopics || isLoading" @click="loadDiscussionTopics">
            候補を再生成
          </button>
          <button
            class="primary"
            type="button"
            :disabled="isLoadingTopics || isLoading || !selectedDiscussionTopic"
            @click="startSelectedDiscussionTopic"
          >
            選択中のお題で開始
          </button>
        </div>
      </section>
    </div>

    <div v-if="isDeviceDialogOpen" class="modal-backdrop" @click.self="closeDeviceDialog">
      <section class="device-dialog" role="dialog" aria-modal="true" aria-labelledby="device-dialog-title">
        <div class="dialog-header">
          <div>
            <p class="eyebrow">Audio Devices</p>
            <h2 id="device-dialog-title">入出力デバイス設定</h2>
          </div>
          <button class="secondary icon-button" type="button" aria-label="閉じる" @click="closeDeviceDialog">×</button>
        </div>
        <div class="device-actions">
          <button class="secondary" type="button" :disabled="isLoadingDevices" @click="refreshAudioDevicesWithPermission">
            デバイス名を取得
          </button>
          <span v-if="isLoadingDevices">取得中</span>
        </div>
        <p v-if="deviceError" class="device-error">{{ deviceError }}</p>
        <label>
          入力
          <select v-model="inputDeviceId">
            <option value="">自動</option>
            <option v-for="device in inputDevices" :key="device.deviceId" :value="device.deviceId">
              {{ device.label }}
            </option>
          </select>
        </label>
        <label>
          出力
          <select v-model="outputDeviceId" @change="configureOutputDevice">
            <option value="">自動</option>
            <option v-for="device in outputDevices" :key="device.deviceId" :value="device.deviceId">
              {{ device.label }}
            </option>
          </select>
        </label>
        <div class="dialog-footer">
          <button class="primary" type="button" @click="closeDeviceDialog">完了</button>
        </div>
      </section>
    </div>

    <div v-if="selectedMode" class="session-layout">
      <section class="call-column">
        <section class="meeting">
          <div class="video-frame" :class="{ disconnected: callDisconnected }">
            <div v-if="callDisconnected" class="disconnected-screen" aria-live="polite">
              <div class="disconnected-copy">
                <span>ディスカッション終了</span>
                <div v-if="isScoringFeedback" class="scoring-status">
                  <span class="loading-spinner scoring-spinner" aria-hidden="true"></span>
                  <span>採点中…</span>
                </div>
              </div>
            </div>
            <template v-else-if="interviewer">
              <img
                class="interviewer"
                :src="interviewerImageSrc"
                :alt="activeMode === 'discussion' ? 'AIの相手' : 'AI面接官'"
              />
              <img
                v-if="isSpeaking"
                class="interviewer mouth-overlay"
                :src="mouthOverlaySrc"
                :style="mouthOverlayStyle"
                alt=""
                aria-hidden="true"
              />
            </template>
            <div v-else class="empty-avatar">AI</div>
            <audio ref="aiAudio" autoplay playsinline class="remote-audio"></audio>
            <div v-if="awaitingOpeningTranscript" class="loading-overlay" aria-live="polite">
              <span class="loading-spinner" aria-hidden="true"></span>
              <span class="loading-text">接続中</span>
            </div>
            <div class="meeting-bar">
              <span>{{ modeTitle }} / 残り {{ remainingTimeLabel }}</span>
              <span :class="{ live: isSpeaking }">{{ isSpeaking ? '発話中' : '待機中' }}</span>
            </div>
          </div>
        </section>

        <div v-if="scenario" class="meta topic-panel">
          <h2>今回のテーマ</h2>
          <p>{{ scenario.discussionTopic }}</p>
        </div>

        <div v-if="audioInsight.length" class="meta audio-panel">
          <h2>音声面の観測</h2>
          <p v-for="item in audioInsight" :key="item">{{ item }}</p>
        </div>

        <div v-if="debugEvents.length" ref="debugLogList" class="meta debug-panel">
          <h2>デバッグログ</h2>
          <ol>
            <li v-for="item in debugEvents" :key="item.id">{{ item.text }}</li>
          </ol>
        </div>
      </section>

      <section ref="chatLog" class="chat chat-panel">
        <div class="chat-heading">
          <span>会話ログ</span>
          <small>音声からの推定文字起こし</small>
        </div>
        <div v-for="(message, index) in messages" :key="index" class="message" :class="message.role">
          <span>{{ message.role === 'assistant' ? '相手' : 'あなた' }}</span>
          <p>{{ message.content }}</p>
        </div>
      </section>
    </div>

    <section v-if="selectedMode" class="answer-box">
      <div class="actions">
        <button v-if="isSessionRunning" :disabled="isLoading || isScoringFeedback" class="primary" @click="finishSession">
          {{ isScoringFeedback ? '採点中…' : finishButtonLabel }}
        </button>
        <button v-else class="secondary" type="button" @click="returnToModeSelection">モード選択に戻る</button>
      </div>
    </section>

    <section v-if="feedback" ref="feedbackSection" class="feedback">
      <div>
        <p class="score">{{ feedback.overallScore }}</p>
        <h2>{{ feedback.decision }}</h2>
        <p>{{ feedback.recommendation }}</p>
      </div>
      <div>
        <h3>良かった点</h3>
        <ul>
          <li v-for="item in feedback.strengths" :key="item">{{ item }}</li>
        </ul>
      </div>
      <div>
        <h3>改善点</h3>
        <ul>
          <li v-for="item in feedback.improvements" :key="item">{{ item }}</li>
        </ul>
      </div>
    </section>

    <section v-if="detailedAnalysis" class="analysis">
      <header class="analysis-header">
        <p class="eyebrow">詳細分析</p>
        <h2>項目別の採点</h2>
        <p class="analysis-summary">{{ detailedAnalysis.overallComment }}</p>
        <p v-if="detailedAnalysis.transcriptSource" class="transcript-source">
          文字起こし: {{ transcriptSourceLabel(detailedAnalysis.transcriptSource) }}
          <span
            v-if="detailedAnalysis.transcriptQuality && detailedAnalysis.transcriptQuality.segmentCount > 0"
            class="transcript-source-meta"
          >
            ({{ detailedAnalysis.transcriptQuality.completedCount }}/{{
              detailedAnalysis.transcriptQuality.segmentCount
            }}
            セグメント完了<span v-if="detailedAnalysis.transcriptQuality.errorCount > 0">、{{
              detailedAnalysis.transcriptQuality.errorCount
            }}件エラー</span><span v-if="detailedAnalysis.transcriptQuality.pendingCount > 0">、{{
              detailedAnalysis.transcriptQuality.pendingCount
            }}件未完了</span>)
          </span>
        </p>
      </header>

      <ul class="rubric-list">
        <li
          v-for="item in detailedAnalysis.rubricScores"
          :key="item.key"
          class="rubric-item"
          :class="`level-${item.level}`"
        >
          <div class="rubric-head">
            <div>
              <p class="rubric-label">{{ item.label }}</p>
              <p class="rubric-level">{{ rubricLevelLabel(item.level) }} ・ 重み {{ formatPercent(item.weight) }}</p>
            </div>
            <p class="rubric-score">{{ item.score }}</p>
          </div>
          <div v-if="item.evidence.length" class="rubric-evidence">
            <p class="rubric-subhead">根拠</p>
            <ul>
              <li v-for="entry in item.evidence" :key="entry">{{ entry }}</li>
            </ul>
          </div>
          <p v-if="item.advice" class="rubric-advice">
            <span>改善アドバイス</span>
            {{ item.advice }}
          </p>
        </li>
      </ul>

      <div class="analysis-grid">
        <div class="analysis-card">
          <h3>会話メトリクス</h3>
          <dl>
            <div><dt>立場の検出</dt><dd>{{ detailedAnalysis.transcriptStats.stanceDetected }}</dd></div>
            <div><dt>発話ターン数</dt><dd>{{ detailedAnalysis.transcriptStats.userTurnCount }}</dd></div>
            <div><dt>平均発話文字数</dt><dd>{{ detailedAnalysis.transcriptStats.averageUserTurnChars }}</dd></div>
            <div><dt>理由表現</dt><dd>{{ detailedAnalysis.transcriptStats.reasonMarkerCount }}回</dd></div>
            <div><dt>具体表現</dt><dd>{{ detailedAnalysis.transcriptStats.exampleMarkerCount }}回</dd></div>
            <div><dt>譲歩表現</dt><dd>{{ detailedAnalysis.transcriptStats.concessionMarkerCount }}回</dd></div>
            <div><dt>問いへの応答</dt><dd>{{ detailedAnalysis.transcriptStats.questionResponseCount }}回</dd></div>
          </dl>
        </div>

        <div class="analysis-card">
          <h3>音声面・ターンテイキング</h3>
          <template v-if="detailedAnalysis.audioStats.available">
            <dl>
              <div v-if="detailedAnalysis.audioStats.observedDurationSec !== undefined">
                <dt>観測時間</dt>
                <dd>{{ formatNumber(detailedAnalysis.audioStats.observedDurationSec, '秒') }}</dd>
              </div>
              <div v-if="detailedAnalysis.audioStats.responseLatencyAvgSec !== undefined">
                <dt>回答開始までの平均（推定）</dt>
                <dd>{{ formatSeconds(detailedAnalysis.audioStats.responseLatencyAvgSec) }}</dd>
              </div>
              <div v-if="detailedAnalysis.audioStats.responseLatencyMaxSec !== undefined">
                <dt>回答開始までの最大（推定）</dt>
                <dd>{{ formatSeconds(detailedAnalysis.audioStats.responseLatencyMaxSec) }}</dd>
              </div>
              <div v-if="detailedAnalysis.audioStats.longThinkingPauseCount !== undefined">
                <dt>回答前の長い沈黙</dt>
                <dd>{{ detailedAnalysis.audioStats.longThinkingPauseCount }}回</dd>
              </div>
              <div v-if="detailedAnalysis.audioStats.inTurnLongSilenceCount !== undefined">
                <dt>回答中の長い沈黙</dt>
                <dd>{{ detailedAnalysis.audioStats.inTurnLongSilenceCount }}回</dd>
              </div>
              <div v-if="detailedAnalysis.audioStats.overlapCount !== undefined">
                <dt>かぶり発話</dt>
                <dd>{{ detailedAnalysis.audioStats.overlapCount }}回</dd>
              </div>
              <div v-if="detailedAnalysis.audioStats.fillerCount !== undefined">
                <dt>フィラー</dt>
                <dd>{{ detailedAnalysis.audioStats.fillerCount }}回</dd>
              </div>
            </dl>
            <ul v-if="detailedAnalysis.audioStats.observations.length" class="observations">
              <li v-for="entry in detailedAnalysis.audioStats.observations" :key="entry">{{ entry }}</li>
            </ul>
          </template>
          <p v-else class="analysis-empty">音声メトリクスは今回取得できませんでした。</p>
        </div>

        <div
          v-if="detailedAnalysis.audioStats.emotionSignals?.length"
          class="analysis-card signal-card"
        >
          <h3>話し方の兆候</h3>
          <ul class="signal-list">
            <li
              v-for="signal in detailedAnalysis.audioStats.emotionSignals"
              :key="signal.key"
              :class="`signal-${signal.level}`"
            >
              <div class="signal-head">
                <span>{{ signal.label }}</span>
                <strong>{{ emotionSignalLevelLabel(signal.level) }}</strong>
              </div>
              <ul v-if="signal.evidence.length" class="signal-evidence">
                <li v-for="entry in signal.evidence" :key="entry">{{ entry }}</li>
              </ul>
              <p class="signal-advice">{{ signal.advice }}</p>
            </li>
          </ul>
        </div>
      </div>

      <div v-if="detailedAnalysis.timeline.length" class="analysis-card timeline-card">
        <h3>会話タイムライン</h3>
        <ol class="timeline">
          <li v-for="(entry, index) in detailedAnalysis.timeline" :key="index" :class="entry.role">
            <span class="timeline-role">{{ entry.role === 'assistant' ? '相手' : 'あなた' }}</span>
            <p class="timeline-excerpt">{{ entry.excerpt }}</p>
            <p v-if="entry.role === 'user' && formatAudioRange(entry)" class="timeline-audio">
              音声: {{ formatAudioRange(entry) }}
              <span v-if="entry.audioDurationSec !== undefined"> / 長さ {{ formatSeconds(entry.audioDurationSec) }}</span>
              <span v-if="entry.responseLatencySec !== undefined"> / 応答まで {{ formatSeconds(entry.responseLatencySec) }}</span>
              <span v-if="entry.speechActivityRatio !== undefined"> / 有音率 {{ formatPercent(entry.speechActivityRatio) }}</span>
              <span v-if="entry.longPauseCount"> / 長め沈黙 {{ entry.longPauseCount }}回</span>
            </p>
            <p v-if="entry.analysisNote" class="timeline-note">分析メモ: {{ entry.analysisNote }}</p>
          </li>
        </ol>
      </div>
    </section>
  </main>
</template>
