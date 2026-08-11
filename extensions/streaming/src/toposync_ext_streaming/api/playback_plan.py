from __future__ import annotations

import re
from typing import Any, Literal

from .models import (
    StreamingCameraLiveContext,
    StreamingPlaybackClientKind,
    StreamingPlaybackPlanResponse,
    StreamingPlaybackPlanTransport,
    StreamingRuntimeTransmissionHealth,
    TransmissionOutputUrl,
    TransmissionUrlsResponse,
)


def is_webrtc_contract_message(message: str) -> bool:
    lowered = str(message or "").lower()
    return "webrtc" in lowered or "whep" in lowered or re.search(r"\bice\b", lowered) is not None


def _playback_plan_transport_from_output(
    *,
    transport: Literal["webrtc", "hls", "mse", "jsmpeg"],
    rank: int,
    output: TransmissionOutputUrl | None,
    available: bool,
    blocking_errors: list[str] | None = None,
    warnings: list[str] | None = None,
    health: dict[str, Any] | None = None,
) -> StreamingPlaybackPlanTransport:
    return StreamingPlaybackPlanTransport(
        transport=transport,
        rank=rank,
        available=bool(available and output is not None),
        output_id=output.output_id if output is not None else None,
        protocol=output.protocol if output is not None else transport,
        url=output.url if output is not None else None,
        media_auth_type=output.media_auth_type if output is not None else "none",
        requires_auth=bool(output.requires_auth) if output is not None else False,
        quality_profile_id=output.quality_profile_id if output is not None else None,
        resolution=output.resolution if output is not None else None,
        fps_limit=output.fps_limit if output is not None else None,
        bitrate_kbps=output.bitrate_kbps if output is not None else None,
        latency_profile=output.latency_profile if output is not None else None,
        blocking_errors=list(blocking_errors or []),
        warnings=list(warnings or []),
        health=dict(health or {}),
    )


def _best_hls_output(
    *,
    urls: TransmissionUrlsResponse,
    quality_profile_id: str | None = None,
) -> TransmissionOutputUrl | None:
    requested_profile_id = str(quality_profile_id or "").strip()
    candidates = [item for item in urls.outputs if item.protocol == "hls"]
    if not candidates:
        return None
    if requested_profile_id:
        for item in candidates:
            if item.quality_profile_id == requested_profile_id:
                return item
    for preferred in ("stable_apple_tv", "quad_grid"):
        for item in candidates:
            if item.quality_profile_id == preferred:
                return item
    return candidates[0]


def _best_webrtc_output(*, urls: TransmissionUrlsResponse) -> TransmissionOutputUrl | None:
    return next((item for item in urls.outputs if item.protocol == "webrtc"), None)


def select_webrtc_output(*, urls: TransmissionUrlsResponse) -> TransmissionOutputUrl | None:
    return _best_webrtc_output(urls=urls)


def _best_mse_output(
    *,
    urls: TransmissionUrlsResponse,
    quality_profile_id: str | None = None,
) -> TransmissionOutputUrl | None:
    requested_profile_id = str(quality_profile_id or "").strip()
    candidates = [item for item in urls.outputs if item.protocol == "mse"]
    if not candidates:
        return None
    if requested_profile_id:
        for item in candidates:
            if item.quality_profile_id == requested_profile_id:
                return item
    for preferred in ("stable_apple_tv", "quad_grid", "fullscreen_quality"):
        for item in candidates:
            if item.quality_profile_id == preferred:
                return item
    return candidates[0]


def _best_jsmpeg_output(
    *,
    urls: TransmissionUrlsResponse,
    quality_profile_id: str | None = None,
) -> TransmissionOutputUrl | None:
    requested_profile_id = str(quality_profile_id or "").strip()
    candidates = [item for item in urls.outputs if item.protocol == "jsmpeg"]
    if not candidates:
        return None
    if requested_profile_id:
        for item in candidates:
            if item.quality_profile_id == requested_profile_id:
                return item
    for preferred in ("diagnostic_low", "quad_grid", "stable_apple_tv", "fullscreen_quality"):
        for item in candidates:
            if item.quality_profile_id == preferred:
                return item
    return candidates[0]


def _runtime_health_for_playback_plan(
    runtime_health: StreamingRuntimeTransmissionHealth | None,
    output_id: str | None,
) -> dict[str, Any]:
    if runtime_health is None:
        return {}
    health: dict[str, Any] = {
        "status": runtime_health.status,
        "selected_frame_age_seconds": runtime_health.selected_frame_age_seconds,
        "last_incoming_frame_age_seconds": runtime_health.last_incoming_frame_age_seconds,
        "fallback_active": runtime_health.fallback_active,
        "fallback_reason": runtime_health.fallback_reason,
        "stale": runtime_health.stale,
        "placeholder_active": runtime_health.placeholder_active,
    }
    if output_id:
        output = next((item for item in runtime_health.outputs if item.output_id == output_id), None)
        if output is not None:
            health.update(
                {
                    "transport_health": output.status,
                    "viewer_count": output.viewer_count,
                    "publisher_running": output.publisher_running,
                    "publisher_frames_sent": output.publisher_frames_sent,
                    "publisher_last_error": output.publisher_last_error,
                }
            )
    return health


def build_playback_plan_response(
    *,
    transmission_id: str,
    client: StreamingPlaybackClientKind,
    urls: TransmissionUrlsResponse,
    runtime_health: StreamingRuntimeTransmissionHealth | None = None,
    quality_profile_id: str | None = None,
    visual_context: StreamingCameraLiveContext | None = None,
    transmission_role: str | None = None,
    low_latency_requested: bool = False,
) -> StreamingPlaybackPlanResponse:
    contract = urls.network_contract
    home_assistant_proxy_hls = (
        contract is not None
        and contract.environment == "home_assistant_addon"
        and contract.public_hls_mode == "proxy"
    )
    hls_output = _best_hls_output(urls=urls, quality_profile_id=quality_profile_id)
    webrtc_output = _best_webrtc_output(urls=urls)
    mse_output = _best_mse_output(urls=urls, quality_profile_id=quality_profile_id)
    jsmpeg_output = _best_jsmpeg_output(urls=urls, quality_profile_id=quality_profile_id)

    hls_blocking: list[str] = []
    if hls_output is None:
        hls_blocking.append("No HLS output is available.")

    webrtc_blocking: list[str] = []
    if webrtc_output is None:
        webrtc_blocking.append(
            "This transmission has no WebRTC/WHEP output. Enable low-latency playback on a zoom/PTZ publication or set transport_policy.enable_webrtc=true."
        )
    if client == "ha_ingress":
        webrtc_blocking.append(
            "Home Assistant ingress must use the Home Assistant native camera path for Cloud/WebRTC relay; direct Toposync WebRTC is disabled by default."
        )
    if client == "ha_entity":
        webrtc_blocking.append(
            "Home Assistant entity playback is negotiated by the Home Assistant camera platform."
        )
    web_webrtc_contextual = (
        client == "web"
        and (
            bool(low_latency_requested)
            or str(visual_context or "").strip().lower() == "ptz"
            or str(transmission_role or "").strip().lower() == "zoom"
        )
    )
    if client == "web" and not web_webrtc_contextual:
        webrtc_blocking.append("WebRTC is reserved for explicit low-latency or PTZ playback.")
    if urls.network_contract is not None:
        for message in urls.network_contract.blocking_errors:
            if is_webrtc_contract_message(message):
                webrtc_blocking.append(message)
    for message in urls.webrtc_warnings:
        if message not in webrtc_blocking:
            webrtc_blocking.append(message)

    mse_blocking: list[str] = []
    if not urls.engine_running:
        mse_blocking.append("MediaMTX engine is not running; MSE needs the internal RTSP output.")
    if hls_output is None:
        mse_blocking.append("No HLS backing output is available for MSE.")
    if mse_output is None:
        sidecar_warning = next(
            (
                message
                for message in urls.warnings
                if "mse" in message.lower() or "go2rtc" in message.lower()
            ),
            "",
        )
        mse_blocking.append(sidecar_warning or "MSE is not available for this output.")
    jsmpeg_blocking: list[str] = []
    if hls_output is None:
        jsmpeg_blocking.append("No HLS backing output is available for JSMpeg.")
    if jsmpeg_output is None:
        jsmpeg_hint = next(
            (
                message
                for message in urls.warnings
                if "jsmpeg" in message.lower() or "ffmpeg" in message.lower()
            ),
            "",
        )
        jsmpeg_blocking.append(
            jsmpeg_hint
            or "JSMpeg fallback is unavailable. Check /api/streams/jsmpeg/status for FFmpeg and session limits."
        )

    if client == "ha_entity":
        return StreamingPlaybackPlanResponse(
            transmission_id=transmission_id,
            client=client,
            transports=[],
            selected_transport=None,
            warnings=[
                *list(urls.warnings),
                "Home Assistant entity playback uses the Home Assistant camera contract from /api/streams/home-assistant/cameras.",
            ],
            hls_warnings=list(urls.hls_warnings),
            webrtc_warnings=list(urls.webrtc_warnings),
            blocking_errors=list(urls.blocking_errors),
        )

    if client in {"app", "ha_ingress"} or home_assistant_proxy_hls:
        order: list[Literal["hls", "mse", "webrtc", "jsmpeg"]] = ["hls", "mse", "jsmpeg", "webrtc"]
    elif web_webrtc_contextual:
        order = ["webrtc", "mse", "hls", "jsmpeg"]
    else:
        order = ["mse", "hls", "jsmpeg", "webrtc"]

    transports: list[StreamingPlaybackPlanTransport] = []
    for rank, transport in enumerate(order):
        if transport == "hls":
            transports.append(
                _playback_plan_transport_from_output(
                    transport="hls",
                    rank=rank,
                    output=hls_output,
                    available=hls_output is not None and not hls_blocking,
                    blocking_errors=hls_blocking,
                    warnings=list(urls.hls_warnings),
                    health=_runtime_health_for_playback_plan(runtime_health, hls_output.output_id if hls_output else None),
                )
            )
        elif transport == "webrtc":
            warnings = [
                *list(urls.webrtc_warnings),
                *(
                    ["WebRTC is reserved for Home Assistant native camera/WebRTC relay in HA ingress mode."]
                    if client == "ha_ingress"
                    else ["WebRTC is reserved for low-latency/PTZ in Home Assistant proxy mode."]
                    if home_assistant_proxy_hls
                    else []
                ),
            ]
            transports.append(
                _playback_plan_transport_from_output(
                    transport="webrtc",
                    rank=rank,
                    output=webrtc_output,
                    available=webrtc_output is not None and not webrtc_blocking and client not in {"app", "ha_ingress"},
                    blocking_errors=webrtc_blocking if client not in {"app"} else [*webrtc_blocking, "Native app playback uses HLS first."],
                    warnings=warnings,
                    health=_runtime_health_for_playback_plan(runtime_health, webrtc_output.output_id if webrtc_output else None),
                )
            )
        elif transport == "mse":
            if mse_output is not None:
                transports.append(
                    _playback_plan_transport_from_output(
                        transport="mse",
                        rank=rank,
                        output=mse_output,
                        available=not mse_blocking,
                        blocking_errors=mse_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, mse_output.output_id),
                    )
                )
            else:
                transports.append(
                    StreamingPlaybackPlanTransport(
                        transport="mse",
                        rank=rank,
                        available=False,
                        protocol="mse",
                        blocking_errors=mse_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, None),
                    )
                )
        elif transport == "jsmpeg":
            if jsmpeg_output is not None:
                transports.append(
                    _playback_plan_transport_from_output(
                        transport="jsmpeg",
                        rank=rank,
                        output=jsmpeg_output,
                        available=not jsmpeg_blocking,
                        blocking_errors=jsmpeg_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, jsmpeg_output.output_id),
                    )
                )
            else:
                transports.append(
                    StreamingPlaybackPlanTransport(
                        transport="jsmpeg",
                        rank=rank,
                        available=False,
                        protocol="jsmpeg",
                        blocking_errors=jsmpeg_blocking,
                        health=_runtime_health_for_playback_plan(runtime_health, None),
                    )
                )

    selected = next((item.transport for item in transports if item.available), None)
    plan_warnings = list(urls.warnings)
    if home_assistant_proxy_hls:
        plan_warnings.append("Home Assistant proxy mode prefers signed HLS for stable playback.")
    if client == "ha_ingress":
        plan_warnings.append("Home Assistant ingress prefers HLS; use HA camera entities for Home Assistant Cloud/WebRTC relay.")
    return StreamingPlaybackPlanResponse(
        transmission_id=transmission_id,
        client=client,
        transports=transports,
        selected_transport=selected,
        warnings=plan_warnings,
        hls_warnings=list(urls.hls_warnings),
        webrtc_warnings=list(urls.webrtc_warnings),
        blocking_errors=list(urls.blocking_errors),
    )
