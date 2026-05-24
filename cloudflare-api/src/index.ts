import { Container, getContainer } from "@cloudflare/containers";

const CONTAINER_INSTANCE_NAME = "roletalk-api-v29";
const CONFIG_KV_KEY = "runtime-config";
const RUNTIME_CONFIG_HEADER = "x-roletalk-runtime-config";

const DEFAULT_CONFIG: Record<string, string> = {
  TZ: "Asia/Tokyo",
  PYTHONUNBUFFERED: "1",
  BACKEND_LOG_FILE: "/tmp/roletalk.log",
  BACKEND_LOG_TIMEZONE: "Asia/Tokyo",
  BACKEND_LOG_MAX_BYTES: "5242880",
  BACKEND_LOG_BACKUP_COUNT: "3",
  VOICE_PROVIDER: "gemini-live",
  VOICE_TRANSPORT: "mock",
  VOICE_BRIDGE_MODE: "gemini",
  GEMINI_LIVE_MODEL: "gemini-3.1-flash-live-preview",
  GEMINI_LIVE_VOICE: "Charon",
  GEMINI_LIVE_LANGUAGE: "ja-JP",
  GEMINI_VAD_TURN_COVERAGE: "TURN_INCLUDES_ALL_INPUT",
  GEMINI_VAD_SILENCE_MS: "1000",
  GEMINI_VAD_PREFIX_PADDING_MS: "500",
  GEMINI_VAD_START_SENSITIVITY: "START_SENSITIVITY_HIGH",
  GEMINI_VAD_END_SENSITIVITY: "END_SENSITIVITY_LOW",
  GEMINI_TRANSCRIPT_NORMALIZE: "1",
  GEMINI_OUTPUT_PITCH_FACTOR: "0.92",
  GEMINI_TRANSLATION_MODEL: "gemini-2.5-flash",
  WEBRTC_UDP_PORT_MIN: "50000",
  WEBRTC_UDP_PORT_MAX: "50050",
  WEBRTC_ICE_HOST: "",
  CLOUDFLARE_REALTIME_APP_ID: "",
  CLOUDFLARE_REALTIME_BASE_URL: "https://rtc.live.cloudflare.com/v1",
  VOICE_BRIDGE_PUBLIC_BASE_URL: "",
  GOOGLE_CLOUD_PROJECT: "",
  GOOGLE_CLOUD_LOCATION: "us-central1",
};

const SECRET_CONFIG_KEYS = [
  "ADMIN_CONFIG_TOKEN",
  "GEMINI_API_KEY",
  "GOOGLE_API_KEY",
  "CLOUDFLARE_REALTIME_APP_SECRET",
  "GOOGLE_APPLICATION_CREDENTIALS",
] as const;

const ALLOWED_CONFIG_KEYS = new Set(Object.keys(DEFAULT_CONFIG));

export class RoleTalkBackend extends Container {
  defaultPort = 8000;
  sleepAfter = "10m";
  envVars = {
    TZ: "Asia/Tokyo",
    PYTHONUNBUFFERED: "1",
    BACKEND_LOG_FILE: "/tmp/roletalk.log",
    BACKEND_LOG_TIMEZONE: "Asia/Tokyo",
    VOICE_PROVIDER: "gemini-live",
    VOICE_TRANSPORT: "mock",
    VOICE_BRIDGE_MODE: "gemini",
    GEMINI_LIVE_MODEL: "gemini-3.1-flash-live-preview",
    GEMINI_LIVE_VOICE: "Charon",
    GEMINI_LIVE_LANGUAGE: "ja-JP",
    GEMINI_VAD_TURN_COVERAGE: "TURN_INCLUDES_ALL_INPUT",
    GEMINI_VAD_SILENCE_MS: "1000",
    GEMINI_VAD_PREFIX_PADDING_MS: "500",
    GEMINI_VAD_START_SENSITIVITY: "START_SENSITIVITY_HIGH",
    GEMINI_VAD_END_SENSITIVITY: "END_SENSITIVITY_LOW",
    GEMINI_TRANSCRIPT_NORMALIZE: "1",
    GEMINI_OUTPUT_PITCH_FACTOR: "0.92",
    CLOUDFLARE_REALTIME_BASE_URL: "https://rtc.live.cloudflare.com/v1",
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/api/admin/config") {
      return handleConfigRequest(request, env);
    }

    const container = getContainer(env.ROLETALK_BACKEND, CONTAINER_INSTANCE_NAME);
    return container.fetch(await withRuntimeConfig(request, env));
  },
};

interface Env {
  ROLETALK_BACKEND: DurableObjectNamespace<RoleTalkBackend>;
  ROLETALK_CONFIG: KVNamespace;
  ADMIN_CONFIG_TOKEN?: string;
  GEMINI_API_KEY?: string;
  GOOGLE_API_KEY?: string;
  CLOUDFLARE_REALTIME_APP_SECRET?: string;
  GOOGLE_APPLICATION_CREDENTIALS?: string;
}

async function handleConfigRequest(request: Request, env: Env): Promise<Response> {
  if (!isAuthorized(request, env)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  if (request.method === "GET") {
    const config = await getPublicConfig(env);
    return Response.json({
      config,
      secretConfigured: secretConfigured(env),
    });
  }

  if (request.method === "PUT") {
    let payload: unknown;
    try {
      payload = await request.json();
    } catch {
      return Response.json({ error: "Invalid JSON" }, { status: 400 });
    }
    const nextConfig = sanitizeConfigPayload(payload);
    if (!nextConfig) {
      return Response.json({ error: "Expected object with string config values" }, { status: 400 });
    }
    await env.ROLETALK_CONFIG.put(CONFIG_KV_KEY, JSON.stringify(nextConfig));
    return Response.json({
      config: nextConfig,
      secretConfigured: secretConfigured(env),
    });
  }

  return new Response("Method Not Allowed", {
    status: 405,
    headers: { Allow: "GET, PUT" },
  });
}

async function withRuntimeConfig(request: Request, env: Env): Promise<Request> {
  const headers = new Headers(request.headers);
  headers.set(RUNTIME_CONFIG_HEADER, JSON.stringify(await getRuntimeConfig(env)));
  return new Request(request, { headers });
}

async function getRuntimeConfig(env: Env): Promise<Record<string, string>> {
  const config = await getPublicConfig(env);
  for (const key of SECRET_CONFIG_KEYS) {
    const value = env[key];
    if (value) config[key] = value;
  }
  return config;
}

async function getPublicConfig(env: Env): Promise<Record<string, string>> {
  const stored = await env.ROLETALK_CONFIG.get(CONFIG_KV_KEY, "json");
  return sanitizeConfigPayload(stored) ?? { ...DEFAULT_CONFIG };
}

function sanitizeConfigPayload(payload: unknown): Record<string, string> | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const config = { ...DEFAULT_CONFIG };
  for (const [key, value] of Object.entries(payload)) {
    if (!ALLOWED_CONFIG_KEYS.has(key)) continue;
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") {
      return null;
    }
    config[key] = String(value);
  }
  return config;
}

function isAuthorized(request: Request, env: Env): boolean {
  const expected = env.ADMIN_CONFIG_TOKEN;
  if (!expected) return false;
  const authorization = request.headers.get("authorization") ?? "";
  return authorization === `Bearer ${expected}`;
}

function secretConfigured(env: Env): Record<string, boolean> {
  return Object.fromEntries(SECRET_CONFIG_KEYS.map((key) => [key, Boolean(env[key])]));
}
