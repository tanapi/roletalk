from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "https://rtc.live.cloudflare.com/v1"


@dataclass(frozen=True)
class CloudflareRealtimeConfig:
    app_id: str
    app_secret: str
    base_url: str
    bridge_public_base_url: str

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)


def get_cloudflare_realtime_config() -> CloudflareRealtimeConfig:
    return CloudflareRealtimeConfig(
        app_id=os.getenv("CLOUDFLARE_REALTIME_APP_ID", "").strip(),
        app_secret=os.getenv("CLOUDFLARE_REALTIME_APP_SECRET", "").strip(),
        base_url=os.getenv("CLOUDFLARE_REALTIME_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        bridge_public_base_url=os.getenv("VOICE_BRIDGE_PUBLIC_BASE_URL", "").strip().rstrip("/"),
    )


class CloudflareRealtimeError(RuntimeError):
    pass


def summarize_realtime_payload(path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    summary: dict[str, Any] = {"path": path}
    if not payload:
        return summary

    session_description = payload.get("sessionDescription")
    if isinstance(session_description, dict):
        sdp = session_description.get("sdp")
        summary["sessionDescription"] = {
            "type": session_description.get("type"),
            "sdpLength": len(sdp) if isinstance(sdp, str) else None,
            "sdpPrefix": sdp[:40] if isinstance(sdp, str) else None,
        }
    summary["autoDiscover"] = payload.get("autoDiscover")
    tracks = payload.get("tracks")
    if isinstance(tracks, list):
        summary["tracks"] = [
            {
                "location": track.get("location") if isinstance(track, dict) else None,
                "mid": track.get("mid") if isinstance(track, dict) else None,
                "sessionIdSet": bool(track.get("sessionId")) if isinstance(track, dict) else None,
                "trackName": track.get("trackName") if isinstance(track, dict) else None,
                "kind": track.get("kind") if isinstance(track, dict) else None,
                "endpointScheme": track.get("endpoint", "").split(":", 1)[0] if isinstance(track, dict) else None,
                "inputCodec": track.get("inputCodec") if isinstance(track, dict) else None,
                "outputCodec": track.get("outputCodec") if isinstance(track, dict) else None,
                "mode": track.get("mode") if isinstance(track, dict) else None,
            }
            for track in tracks
        ]
    return summary


class CloudflareRealtimeClient:
    def __init__(self, config: CloudflareRealtimeConfig) -> None:
        if not config.configured:
            raise CloudflareRealtimeError("Cloudflare Realtime is not configured")
        self.config = config

    def create_session(self, *, correlation_id: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path = f"/apps/{self.config.app_id}/sessions/new"
        if correlation_id:
            path = f"{path}?correlationId={correlation_id}"
        return self._request("POST", path, payload)

    def add_tracks(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/apps/{self.config.app_id}/sessions/{session_id}/tracks/new", payload)

    def renegotiate(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/apps/{self.config.app_id}/sessions/{session_id}/renegotiate", payload)

    def create_websocket_adapters(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/apps/{self.config.app_id}/adapters/websocket/new", payload)

    def close_websocket_adapters(self, adapter_ids: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/apps/{self.config.app_id}/adapters/websocket/close",
            {"tracks": [{"adapterId": adapter_id} for adapter_id in adapter_ids if adapter_id]},
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.app_secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "RoleTalk/1.0 (+https://roletalk.pages.dev)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            summary = json.dumps(summarize_realtime_payload(path, payload), ensure_ascii=True)
            raise CloudflareRealtimeError(f"Cloudflare Realtime API returned {exc.code}: {error_body}; request={summary}") from exc
        except urllib.error.URLError as exc:
            raise CloudflareRealtimeError(f"Cloudflare Realtime API request failed: {exc.reason}") from exc

        if not response_body:
            return {}
        return json.loads(response_body)
