from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy

from ..api.models import (
    EXTENSION_ID,
    StreamingEngineSettings,
    StreamingExtensionSettings,
    list_path_read_auth_for_host,
    normalize_server_id,
    normalize_streaming_settings,
)
from .engine_manager import MediaMtxEngineManager
from .mediamtx_api_client import MediaMtxApiClient
from .mediamtx_config import normalize_path_slug
from .placeholder import get_placeholder_frame
from .publisher_manager import (
    PublisherEncodingSettings,
    PublisherInputSettings,
    PublisherManager,
    PublisherOutput,
)
from .resize import resize_frame_contain
from .runtime_state import SelectedWriterFrame, TransmissionRuntimeState


@dataclass(frozen=True, slots=True)
class ResolvedOutputTarget:
    output_key: str
    output_id: str
    transmission_id: str
    arbitration_mode: str
    protocol: str
    publish_path: str
    placeholder_mode: str
    width: int
    height: int
    fps: float
    bitrate_kbps: int | None
    latency_profile: str
    resize_mode: str


@dataclass(frozen=True, slots=True)
class WriterBypassCandidate:
    writer_id: str
    transmission_id: str
    source_rtsp_url: str
    source_fps: float | None
    source_backend: str
    bypass_mode: str


class StreamWriterBridge:
    def __init__(
        self,
        *,
        config_store,
        engine_manager: MediaMtxEngineManager,
        runtime_state: TransmissionRuntimeState,
        publisher_manager: PublisherManager,
        logger: logging.Logger,
        tick_interval_s: float = 0.1,
        settings_refresh_s: float = 1.0,
        viewer_refresh_s: float = 1.0,
        on_demand_enabled: bool = True,
        on_demand_stop_debounce_s: float = 3.0,
        on_demand_prime_ttl_s: float = 60.0,
        monotonic: Callable[[], float] | None = None,
        host_server_id: str = "local",
        mediamtx_api_client: MediaMtxApiClient | None = None,
    ) -> None:
        self._config_store = config_store
        self._engine_manager = engine_manager
        self._runtime_state = runtime_state
        self._publisher_manager = publisher_manager
        self._logger = logger
        self._tick_interval_s = max(0.02, float(tick_interval_s))
        self._settings_refresh_s = max(0.2, float(settings_refresh_s))
        self._viewer_refresh_s = max(0.2, float(viewer_refresh_s))
        self._on_demand_enabled = bool(on_demand_enabled)
        self._on_demand_stop_debounce_s = max(0.5, float(on_demand_stop_debounce_s))
        self._on_demand_prime_ttl_s = min(120.0, max(1.0, float(on_demand_prime_ttl_s)))
        self._monotonic = monotonic or time.monotonic
        self._host_server_id = normalize_server_id(host_server_id)
        self._mediamtx_api_client = mediamtx_api_client or MediaMtxApiClient(engine_manager=engine_manager)

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._next_due_by_output: dict[str, float] = {}
        self._idle_since_by_output: dict[str, float] = {}
        self._last_settings_load_monotonic = 0.0
        self._cached_engine: StreamingEngineSettings | None = None
        self._cached_targets: tuple[ResolvedOutputTarget, ...] = ()
        self._cached_path_auth_by_path: dict[str, tuple[str, str]] = {}
        self._cached_bypass_by_writer: dict[str, WriterBypassCandidate] = {}
        self._cached_viewer_count_by_path: dict[str, int] = {}
        self._last_viewer_load_monotonic = 0.0
        self._bypass_mode_by_output: dict[str, str] = {}
        self._primed_demand_until_by_output: dict[str, float] = {}
        # Synthetic demand derived from MediaMTX logs for external RTSP clients that get 404 when there is no
        # publisher yet. Keep a short window to allow client retries to succeed.
        self._no_stream_hint_until_by_path: dict[str, float] = {}
        self._mediamtx_log_path: str | None = None
        self._mediamtx_log_offset: int = 0
        self._mediamtx_log_remainder: str = ""
        self._last_log_scan_monotonic: float = 0.0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="streaming.writer_bridge")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                self._logger.exception("Streaming writer bridge stopped with error")

        self._next_due_by_output.clear()
        self._idle_since_by_output.clear()
        self._cached_viewer_count_by_path.clear()
        self._last_viewer_load_monotonic = 0.0
        self._bypass_mode_by_output.clear()
        self._primed_demand_until_by_output.clear()
        self._no_stream_hint_until_by_path.clear()
        self._mediamtx_log_path = None
        self._mediamtx_log_offset = 0
        self._mediamtx_log_remainder = ""
        self._last_log_scan_monotonic = 0.0
        await self._runtime_state.prune_output_viewers(set())
        await self._publisher_manager.stop_all()

    async def snapshot(self) -> dict[str, Any]:
        return {
            "targets": [
                {
                    "output_key": target.output_key,
                    "output_id": target.output_id,
                    "transmission_id": target.transmission_id,
                    "arbitration_mode": target.arbitration_mode,
                    "protocol": target.protocol,
                    "publish_path": target.publish_path,
                    "placeholder_mode": target.placeholder_mode,
                    "width": target.width,
                    "height": target.height,
                    "fps": target.fps,
                    "bitrate_kbps": target.bitrate_kbps,
                    "latency_profile": target.latency_profile,
                    "resize_mode": target.resize_mode,
                }
                for target in self._cached_targets
            ],
            "path_auth_by_path": sorted(self._cached_path_auth_by_path.keys()),
            "bypass_writers": {
                writer_id: {
                    "transmission_id": candidate.transmission_id,
                    "source_backend": candidate.source_backend,
                    "source_rtsp_url": candidate.source_rtsp_url,
                    "source_fps": candidate.source_fps,
                    "bypass_mode": candidate.bypass_mode,
                }
                for writer_id, candidate in self._cached_bypass_by_writer.items()
            },
            "publisher": await self._publisher_manager.snapshot(),
            "runtime": await self._runtime_state.snapshot(),
            "host_server_id": self._host_server_id,
            "primed_demand_until_by_output": {
                output_key: float(until_monotonic)
                for output_key, until_monotonic in self._primed_demand_until_by_output.items()
            },
        }

    async def prime_transmission_demand(self, transmission_id: str, *, ttl_s: float | None = None) -> int:
        normalized_transmission_id = _as_str(transmission_id)
        if not normalized_transmission_id:
            return 0

        now_monotonic = self._monotonic()
        ttl = self._on_demand_prime_ttl_s if ttl_s is None else min(120.0, max(1.0, float(ttl_s)))
        _engine_settings, targets, _path_auth, _bypass = await self._load_settings(now_monotonic)

        primed_count = 0
        until_monotonic = now_monotonic + ttl
        for target in targets:
            if target.transmission_id != normalized_transmission_id:
                continue
            previous_until = float(self._primed_demand_until_by_output.get(target.output_key, 0.0))
            if until_monotonic > previous_until:
                self._primed_demand_until_by_output[target.output_key] = until_monotonic
            primed_count += 1
        return primed_count

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            tick_started = time.monotonic()
            try:
                await self._tick_once(tick_started)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Streaming writer bridge tick failed")

            elapsed = time.monotonic() - tick_started
            sleep_s = max(0.01, self._tick_interval_s - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
            except TimeoutError:
                continue

    async def _tick_once(self, now_monotonic: float) -> None:
        engine_settings, targets, path_auth_by_path, bypass_by_writer = await self._load_settings(now_monotonic)

        if not engine_settings.enabled:
            await self._publisher_manager.stop_all()
            self._next_due_by_output.clear()
            self._idle_since_by_output.clear()
            self._bypass_mode_by_output.clear()
            self._no_stream_hint_until_by_path.clear()
            self._mediamtx_log_path = None
            self._mediamtx_log_offset = 0
            self._mediamtx_log_remainder = ""
            self._last_log_scan_monotonic = 0.0
            await self._runtime_state.prune_transmissions(set())
            await self._runtime_state.prune_output_viewers(set())
            return

        desired_output_ids = {target.output_key for target in targets}
        desired_transmission_ids = {target.transmission_id for target in targets}
        await self._runtime_state.prune_transmissions(desired_transmission_ids)
        if not desired_output_ids:
            await self._publisher_manager.stop_all()
            self._next_due_by_output.clear()
            self._idle_since_by_output.clear()
            self._bypass_mode_by_output.clear()
            self._no_stream_hint_until_by_path.clear()
            self._mediamtx_log_path = None
            self._mediamtx_log_offset = 0
            self._mediamtx_log_remainder = ""
            self._last_log_scan_monotonic = 0.0
            await self._runtime_state.prune_transmissions(set())
            await self._runtime_state.prune_output_viewers(set())
            return

        arbitration_by_transmission: dict[str, str] = {}
        for target in targets:
            arbitration_by_transmission.setdefault(target.transmission_id, str(target.arbitration_mode or "priority_latest"))
        for transmission_id, arbitration_mode in arbitration_by_transmission.items():
            await self._runtime_state.set_transmission_arbitration(
                transmission_id=transmission_id,
                arbitration_mode=arbitration_mode,
            )

        engine_status = await self._engine_manager.ensure_running(
            engine_settings,
            engine_paths=[target.publish_path for target in targets],
            path_auth=path_auth_by_path,
        )
        await self._scan_engine_logs_for_no_stream_demand(engine_status, now_monotonic)
        viewer_count_by_path = await self._load_viewer_count_by_path(now_monotonic)

        target_count_by_transmission: dict[str, int] = {}
        for target in targets:
            target_count_by_transmission[target.transmission_id] = target_count_by_transmission.get(target.transmission_id, 0) + 1

        selected_by_transmission: dict[str, SelectedWriterFrame] = {}
        for target in targets:
            viewer_count = max(0, int(viewer_count_by_path.get(target.publish_path, 0)))
            primed_until = float(self._primed_demand_until_by_output.get(target.output_key, 0.0))
            demand_primed = primed_until > now_monotonic
            hint_until = float(self._no_stream_hint_until_by_path.get(target.publish_path, 0.0))
            demand_hint = hint_until > now_monotonic
            await self._runtime_state.update_output_viewer_count(
                output_key=target.output_key,
                transmission_id=target.transmission_id,
                viewer_count=viewer_count,
            )

            if self._on_demand_enabled and viewer_count <= 0 and not demand_primed and not demand_hint:
                idle_since = self._idle_since_by_output.get(target.output_key)
                if idle_since is None:
                    self._idle_since_by_output[target.output_key] = now_monotonic
                elif (now_monotonic - idle_since) >= self._on_demand_stop_debounce_s:
                    await self._publisher_manager.stop_publisher(target.output_key)
                    self._next_due_by_output.pop(target.output_key, None)
                    self._maybe_log_bypass_state(target.output_key, mode="off", details="viewer_count=0")
                continue

            self._idle_since_by_output.pop(target.output_key, None)

            selected = selected_by_transmission.get(target.transmission_id)
            if selected is None:
                selected = await self._runtime_state.get_selected_writer_frame(target.transmission_id)
                selected_by_transmission[target.transmission_id] = selected

            bypass_candidate = None
            if selected.writer_id:
                bypass_candidate = bypass_by_writer.get(selected.writer_id)

            publish_url = await self._engine_manager.get_publish_url_for_path(target.publish_path, host="127.0.0.1")
            input_settings = PublisherInputSettings()
            bypass_block_reason = "no_simple_candidate"
            if bypass_candidate is not None:
                # Bypass uses FFmpeg to pull RTSP directly from the camera. With multiple outputs, we'd spawn
                # one RTSP pull per output path which is unreliable (many cameras limit concurrent sessions).
                # For reliability, only allow bypass when the transmission resolves to a single active output.
                if int(target_count_by_transmission.get(target.transmission_id, 1)) > 1:
                    bypass_block_reason = "multi_output_transmission"
                    bypass_candidate = None
                else:
                    bypass_block_reason = ""

            if bypass_candidate is not None:
                input_settings = PublisherInputSettings(
                    mode="rtsp_pull",
                    rtsp_url=bypass_candidate.source_rtsp_url,
                    source_fps=bypass_candidate.source_fps,
                )
                self._maybe_log_bypass_state(
                    target.output_key,
                    mode="on",
                    details=(
                        f"writer={bypass_candidate.writer_id} "
                        f"backend={bypass_candidate.source_backend} mode={bypass_candidate.bypass_mode}"
                    ),
                )
            else:
                self._maybe_log_bypass_state(target.output_key, mode="off", details=bypass_block_reason)

            await self._publisher_manager.start_publisher(
                output=PublisherOutput(
                    output_id=target.output_key,
                    transmission_id=target.transmission_id,
                    protocol=target.protocol,
                ),
                engine_path=target.publish_path,
                publish_url=publish_url,
                encoding_settings=PublisherEncodingSettings(
                    width=target.width,
                    height=target.height,
                    fps=target.fps,
                    bitrate_kbps=target.bitrate_kbps,
                    latency_profile=_resolve_latency_profile(target.latency_profile),
                ),
                input_settings=input_settings,
            )

            due_at = self._next_due_by_output.get(target.output_key, 0.0)
            if now_monotonic < due_at:
                continue

            if bypass_candidate is not None:
                self._next_due_by_output[target.output_key] = now_monotonic + (1.0 / max(1.0, target.fps))
                continue

            frame = self._resolve_frame_for_output(selected, target)
            await self._publisher_manager.submit_frame(target.output_key, frame)
            self._next_due_by_output[target.output_key] = now_monotonic + (1.0 / max(1.0, target.fps))

        await self._publisher_manager.stop_missing(desired_output_ids)
        await self._runtime_state.prune_output_viewers(desired_output_ids)

        for output_key in list(self._next_due_by_output.keys()):
            if output_key not in desired_output_ids:
                self._next_due_by_output.pop(output_key, None)
                self._bypass_mode_by_output.pop(output_key, None)
        for output_key in list(self._idle_since_by_output.keys()):
            if output_key not in desired_output_ids:
                self._idle_since_by_output.pop(output_key, None)
        for output_key in list(self._primed_demand_until_by_output.keys()):
            until_monotonic = float(self._primed_demand_until_by_output.get(output_key, 0.0))
            if output_key not in desired_output_ids or until_monotonic <= now_monotonic:
                self._primed_demand_until_by_output.pop(output_key, None)

        for path_slug in list(self._no_stream_hint_until_by_path.keys()):
            until_monotonic = float(self._no_stream_hint_until_by_path.get(path_slug, 0.0))
            if until_monotonic <= now_monotonic:
                self._no_stream_hint_until_by_path.pop(path_slug, None)

    def _resolve_frame_for_output(self, selected: SelectedWriterFrame, target: ResolvedOutputTarget) -> numpy.ndarray:
        frame = selected.frame
        if frame is None:
            return get_placeholder_frame(target.width, target.height, mode=target.placeholder_mode)

        if target.resize_mode == "contain":
            return resize_frame_contain(frame, target.width, target.height)

        source = numpy.asarray(frame)
        if source.shape[1] == target.width and source.shape[0] == target.height:
            return numpy.ascontiguousarray(source)
        return resize_frame_contain(source, target.width, target.height)

    async def _load_settings(
        self,
        now_monotonic: float,
    ) -> tuple[
        StreamingEngineSettings,
        tuple[ResolvedOutputTarget, ...],
        dict[str, tuple[str, str]],
        dict[str, WriterBypassCandidate],
    ]:
        if (
            self._cached_engine is not None
            and self._cached_targets is not None
            and (now_monotonic - self._last_settings_load_monotonic) < self._settings_refresh_s
        ):
            return (
                self._cached_engine,
                self._cached_targets,
                dict(self._cached_path_auth_by_path),
                dict(self._cached_bypass_by_writer),
            )

        settings = await self._config_store.get_settings()
        raw = settings.extensions.get(EXTENSION_ID, None)
        normalized = normalize_streaming_settings(raw)
        normalized_settings = StreamingExtensionSettings.model_validate(normalized)

        engine_settings = StreamingEngineSettings.model_validate(normalized_settings.engine.model_dump(mode="python"))
        transmissions = [item.model_dump(mode="python") for item in normalized_settings.transmissions]
        targets = _resolve_output_targets(transmissions, host_server_id=self._host_server_id)

        path_auth_by_path = list_path_read_auth_for_host(normalized_settings, host_server_id=self._host_server_id)
        bypass_by_writer = await self._resolve_bypass_candidates(settings)

        self._cached_engine = engine_settings
        self._cached_targets = tuple(targets)
        self._cached_path_auth_by_path = dict(path_auth_by_path)
        self._cached_bypass_by_writer = dict(bypass_by_writer)
        self._last_settings_load_monotonic = now_monotonic
        return (
            self._cached_engine,
            self._cached_targets,
            dict(self._cached_path_auth_by_path),
            dict(self._cached_bypass_by_writer),
        )

    async def _load_viewer_count_by_path(self, now_monotonic: float) -> dict[str, int]:
        if (now_monotonic - self._last_viewer_load_monotonic) < self._viewer_refresh_s:
            return self._cached_viewer_count_by_path

        try:
            payload = await self._mediamtx_api_client.get_viewer_count_by_path()
            normalized = {
                normalize_path_slug(path, fallback=""): max(0, int(viewer_count))
                for path, viewer_count in payload.items()
                if normalize_path_slug(path, fallback="")
            }
            self._cached_viewer_count_by_path = normalized
        except Exception:
            self._logger.exception("Failed to refresh MediaMTX viewer counts")
        finally:
            self._last_viewer_load_monotonic = now_monotonic

        return self._cached_viewer_count_by_path

    async def _scan_engine_logs_for_no_stream_demand(self, engine_status: Any, now_monotonic: float) -> None:
        """Update synthetic demand by scanning the MediaMTX logs.

        MediaMTX closes RTSP connections quickly with "no stream is available" when there is no publisher.
        That keeps viewer_count at 0 and prevents on-demand from starting. We treat these events as a short-lived
        demand hint so external clients (ffplay/VLC) can retry and connect.
        """
        if not self._on_demand_enabled:
            return

        # Avoid excessive I/O.
        if (now_monotonic - float(self._last_log_scan_monotonic or 0.0)) < 0.5:
            return
        self._last_log_scan_monotonic = float(now_monotonic)

        log_path = str(getattr(engine_status, "log_path", "") or "").strip()
        if not log_path:
            return

        if log_path != self._mediamtx_log_path:
            self._mediamtx_log_path = log_path
            self._mediamtx_log_remainder = ""
            # Start at EOF to ignore old history.
            try:
                self._mediamtx_log_offset = int(os.path.getsize(log_path))
            except Exception:
                self._mediamtx_log_offset = 0

        try:
            with open(log_path, "rb") as handle:
                handle.seek(max(0, int(self._mediamtx_log_offset)))
                chunk = handle.read(256 * 1024)
        except Exception:
            return

        if not chunk:
            return

        self._mediamtx_log_offset += len(chunk)

        text = chunk.decode("utf-8", errors="ignore")
        if self._mediamtx_log_remainder:
            text = f"{self._mediamtx_log_remainder}{text}"
            self._mediamtx_log_remainder = ""

        # If the chunk ends mid-line, keep the remainder for the next scan.
        if text and not text.endswith("\n"):
            parts = text.splitlines(keepends=True)
            if parts and not parts[-1].endswith("\n"):
                self._mediamtx_log_remainder = parts[-1]
                text = "".join(parts[:-1])

        if not text:
            return

        pattern = re.compile(r"no stream is available on path '([^']+)'")
        hint_ttl_s = 4.0
        until_monotonic = float(now_monotonic) + hint_ttl_s

        for line in text.splitlines():
            match = pattern.search(line)
            if not match:
                continue
            path_slug = normalize_path_slug(match.group(1), fallback="")
            if not path_slug:
                continue
            previous = float(self._no_stream_hint_until_by_path.get(path_slug, 0.0))
            if until_monotonic > previous:
                self._no_stream_hint_until_by_path[path_slug] = until_monotonic

    async def _resolve_bypass_candidates(self, app_settings) -> dict[str, WriterBypassCandidate]:  # noqa: ANN001
        list_pipelines = getattr(self._config_store, "list_pipelines", None)
        if not callable(list_pipelines):
            return {}

        try:
            pipelines = await list_pipelines()
        except Exception:
            self._logger.exception("Failed to list pipelines for streaming bypass analysis")
            return {}

        cameras_ext = app_settings.extensions.get("com.toposync.cameras", {}) if hasattr(app_settings, "extensions") else {}
        cameras_record = cameras_ext if isinstance(cameras_ext, dict) else {}
        cameras_raw = cameras_record.get("cameras")
        cameras = cameras_raw if isinstance(cameras_raw, list) else []
        camera_by_id: dict[str, dict[str, Any]] = {}
        for item in cameras:
            if not isinstance(item, dict):
                continue
            camera_id = str(item.get("id") or "").strip()
            if camera_id:
                camera_by_id[camera_id] = item

        resolved: dict[str, WriterBypassCandidate] = {}
        for pipeline in pipelines:
            pipeline_name = str(getattr(pipeline, "name", "") or "").strip()
            if not pipeline_name:
                continue
            if not bool(getattr(pipeline, "enabled", True)):
                continue
            processing_server_id = normalize_server_id(getattr(pipeline, "processing_server_id", "local"))
            if processing_server_id != self._host_server_id:
                continue

            graph = getattr(pipeline, "graph", None)
            if not isinstance(graph, dict):
                continue

            by_node_id, incoming_by_target, outgoing_by_source = _parse_graph_topology(graph)
            if not by_node_id:
                continue

            for node_id, node in by_node_id.items():
                operator = str(node.get("operator") or "").strip()
                if operator != "stream.write":
                    continue
                config = node.get("config") if isinstance(node.get("config"), dict) else {}
                bypass_mode = str(config.get("bypass_mode") or "auto").strip().lower()
                if bypass_mode not in {"auto", "force_on", "force_off"}:
                    bypass_mode = "auto"
                if bypass_mode == "force_off":
                    continue

                chain = _resolve_simple_chain(
                    target_stream_node_id=node_id,
                    by_node_id=by_node_id,
                    incoming_by_target=incoming_by_target,
                    outgoing_by_source=outgoing_by_source,
                )
                if chain is None:
                    continue

                source_rtsp_url, source_fps, source_backend = _resolve_chain_rtsp_source(
                    camera_node=chain["camera_node"],
                    camera_by_id=camera_by_id,
                )
                if not source_rtsp_url:
                    continue

                writer_id = f"{pipeline_name}:{node_id}"
                transmission_id = str(config.get("transmission_id") or "").strip()
                if not transmission_id:
                    continue
                reduced_fps = chain.get("fps_limit")
                if reduced_fps is not None and source_fps is not None:
                    source_fps = min(float(source_fps), float(reduced_fps))
                elif reduced_fps is not None:
                    source_fps = float(reduced_fps)

                resolved[writer_id] = WriterBypassCandidate(
                    writer_id=writer_id,
                    transmission_id=transmission_id,
                    source_rtsp_url=source_rtsp_url,
                    source_fps=float(source_fps) if source_fps else None,
                    source_backend=str(source_backend or "auto"),
                    bypass_mode=bypass_mode,
                )
        return resolved

    def _maybe_log_bypass_state(self, output_key: str, *, mode: str, details: str) -> None:
        normalized_mode = "on" if mode == "on" else "off"
        previous = self._bypass_mode_by_output.get(output_key)
        if previous == normalized_mode:
            return
        self._bypass_mode_by_output[output_key] = normalized_mode
        log_info = getattr(self._logger, "info", None)
        if not callable(log_info):
            return
        if normalized_mode == "on":
            log_info("Streaming bypass enabled for output '%s' (%s)", output_key, details)
        else:
            log_info("Streaming bypass disabled for output '%s' (%s)", output_key, details)


def _resolve_output_targets(transmissions: list[Any], *, host_server_id: str) -> list[ResolvedOutputTarget]:
    targets: list[ResolvedOutputTarget] = []
    target_host_server_id = normalize_server_id(host_server_id)

    for transmission_raw in transmissions:
        if not isinstance(transmission_raw, dict):
            continue
        if not _as_bool(transmission_raw.get("enabled"), default=True):
            continue
        transmission_host_server_id = normalize_server_id(transmission_raw.get("host_server_id"), fallback="local")
        if transmission_host_server_id != target_host_server_id:
            continue

        transmission_id = _as_str(transmission_raw.get("id")) or _as_str(transmission_raw.get("transmission_id"))
        if not transmission_id:
            continue

        base_path = normalize_path_slug(
            _as_str(transmission_raw.get("path"))
            or _as_str(transmission_raw.get("slug"))
            or transmission_id,
        )

        placeholder_mode = _as_str(transmission_raw.get("placeholder")).lower() or "gray"
        if placeholder_mode not in {"gray", "black"}:
            placeholder_mode = "gray"

        arbitration_mode = _as_str(transmission_raw.get("arbitration")).lower() or "priority_latest"
        if arbitration_mode not in {"latest", "priority_latest"}:
            arbitration_mode = "priority_latest"

        outputs_raw = transmission_raw.get("outputs") if isinstance(transmission_raw.get("outputs"), list) else []
        enabled_outputs_raw = [item for item in outputs_raw if isinstance(item, dict) and _as_bool(item.get("enabled"), default=True)]

        if not enabled_outputs_raw:
            enabled_outputs_raw = [
                {
                    "id": "default",
                    "protocol": "rtsp",
                    "enabled": True,
                }
            ]

        output_count = len(enabled_outputs_raw)

        for output_raw in enabled_outputs_raw:
            output_id = _as_str(output_raw.get("id")) or "default"
            protocol = _as_str(output_raw.get("protocol")).lower() or "rtsp"
            if protocol not in {"rtsp", "hls", "webrtc", "all"}:
                continue

            width, height = _resolve_resolution(output_raw)
            fps = _resolve_fps(output_raw)
            bitrate_kbps = _resolve_bitrate(output_raw)
            latency_profile = _resolve_latency_profile(output_raw.get("latency_profile"))
            resize_mode = _as_str(output_raw.get("resize_mode")).lower() or "contain"
            if resize_mode not in {"contain", "none"}:
                resize_mode = "contain"

            output_path = normalize_path_slug(_as_str(output_raw.get("path")), fallback="")
            if not output_path:
                if output_count <= 1:
                    output_path = base_path
                else:
                    output_path = normalize_path_slug(f"{base_path}-{output_id}")

            output_key = f"{transmission_id}:{output_id}"

            targets.append(
                ResolvedOutputTarget(
                    output_key=output_key,
                    output_id=output_id,
                    transmission_id=transmission_id,
                    arbitration_mode=arbitration_mode,
                    protocol=protocol,
                    publish_path=output_path,
                    placeholder_mode=placeholder_mode,
                    width=width,
                    height=height,
                    fps=fps,
                    bitrate_kbps=bitrate_kbps,
                    latency_profile=latency_profile,
                    resize_mode=resize_mode,
                )
            )

    return targets


def _resolve_resolution(output_raw: dict[str, Any]) -> tuple[int, int]:
    width = output_raw.get("width")
    height = output_raw.get("height")

    resolution = output_raw.get("resolution") if isinstance(output_raw.get("resolution"), dict) else {}
    if width is None:
        width = resolution.get("width")
    if height is None:
        height = resolution.get("height")

    target_width = max(16, int(width)) if _is_int_like(width) else 1280
    target_height = max(16, int(height)) if _is_int_like(height) else 720
    return target_width, target_height


def _resolve_fps(output_raw: dict[str, Any]) -> float:
    value = output_raw.get("fps_limit")
    if value is None:
        value = output_raw.get("fps")
    if value is None:
        return 12.0
    try:
        fps = float(value)
    except Exception:
        return 12.0
    if not fps or fps <= 0.0:
        return 12.0
    return min(60.0, max(1.0, fps))


def _resolve_bitrate(output_raw: dict[str, Any]) -> int | None:
    value = output_raw.get("bitrate_kbps")
    if value is None:
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed <= 0:
        return None
    return min(250_000, max(64, parsed))


def _resolve_latency_profile(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"normal", "low", "ultra_low"}:
        return normalized
    return "normal"


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
    except Exception:
        return False
    return True


def _parse_graph_topology(
    graph: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
    nodes_raw = graph.get("nodes")
    edges_raw = graph.get("edges")
    nodes = nodes_raw if isinstance(nodes_raw, list) else []
    edges = edges_raw if isinstance(edges_raw, list) else []

    by_node_id: dict[str, dict[str, Any]] = {}
    for item in nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "").strip()
        if not node_id:
            continue
        by_node_id[node_id] = item

    incoming_by_target: dict[str, list[str]] = {}
    outgoing_by_source: dict[str, list[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from") if isinstance(edge.get("from"), dict) else {}
        target = edge.get("to") if isinstance(edge.get("to"), dict) else {}
        source_node_id = str(source.get("node") or "").strip()
        target_node_id = str(target.get("node") or "").strip()
        if source_node_id not in by_node_id or target_node_id not in by_node_id:
            continue
        incoming_by_target.setdefault(target_node_id, []).append(source_node_id)
        outgoing_by_source.setdefault(source_node_id, []).append(target_node_id)

    return by_node_id, incoming_by_target, outgoing_by_source


def _resolve_simple_chain(
    *,
    target_stream_node_id: str,
    by_node_id: dict[str, dict[str, Any]],
    incoming_by_target: dict[str, list[str]],
    outgoing_by_source: dict[str, list[str]],
) -> dict[str, Any] | None:
    chain_node_ids = {target_stream_node_id}

    stream_incoming = incoming_by_target.get(target_stream_node_id) or []
    if len(stream_incoming) != 1:
        return None
    upstream_id = stream_incoming[0]
    upstream_node = by_node_id.get(upstream_id)
    if upstream_node is None:
        return None
    upstream_operator = str(upstream_node.get("operator") or "").strip()

    fps_node: dict[str, Any] | None = None
    camera_node: dict[str, Any] | None = None
    fps_limit: float | None = None

    if upstream_operator == "camera.source":
        camera_node = upstream_node
        chain_node_ids.add(upstream_id)
    elif upstream_operator == "core.fps_reducer":
        fps_node = upstream_node
        chain_node_ids.add(upstream_id)
        fps_config = fps_node.get("config") if isinstance(fps_node.get("config"), dict) else {}
        try:
            parsed = float(fps_config.get("target_fps"))
            if parsed > 0:
                fps_limit = parsed
        except Exception:
            fps_limit = None
        fps_incoming = incoming_by_target.get(upstream_id) or []
        if len(fps_incoming) != 1:
            return None
        camera_node = by_node_id.get(fps_incoming[0])
        if camera_node is None:
            return None
        if str(camera_node.get("operator") or "").strip() != "camera.source":
            return None
        chain_node_ids.add(str(camera_node.get("id") or "").strip())
    else:
        return None

    for node_id in chain_node_ids:
        outgoing = outgoing_by_source.get(node_id) or []
        incoming = incoming_by_target.get(node_id) or []
        if node_id == target_stream_node_id:
            if incoming and len(incoming) != 1:
                return None
            continue
        if len(outgoing) != 1:
            return None
        if node_id == str(camera_node.get("id") or "").strip():
            expected_target = str(fps_node.get("id") or "").strip() if fps_node is not None else target_stream_node_id
            if outgoing[0] != expected_target:
                return None
        if fps_node is not None and node_id == str(fps_node.get("id") or "").strip():
            if outgoing[0] != target_stream_node_id:
                return None
            if len(incoming) != 1:
                return None

    if len(chain_node_ids) != len(by_node_id):
        return None
    return {
        "camera_node": camera_node,
        "fps_limit": fps_limit,
    }


def _resolve_chain_rtsp_source(
    *,
    camera_node: dict[str, Any],
    camera_by_id: dict[str, dict[str, Any]],
) -> tuple[str, float | None, str]:
    config = camera_node.get("config") if isinstance(camera_node.get("config"), dict) else {}
    source_backend = str(config.get("backend") or "auto").strip().lower() or "auto"
    direct_rtsp_url = str(config.get("rtsp_url") or "").strip()
    direct_username = str(config.get("username") or "").strip()
    direct_password = str(config.get("password") or "").strip()
    fps_value = _coerce_float(config.get("fps"))
    if direct_rtsp_url:
        return _apply_rtsp_auth(direct_rtsp_url, direct_username, direct_password), fps_value, source_backend

    camera_id = str(config.get("camera_id") or "").strip()
    if not camera_id:
        return "", None, source_backend
    camera = camera_by_id.get(camera_id) or {}
    camera_rtsp = str(camera.get("rtsp_url") or "").strip()
    if not camera_rtsp:
        return "", None, source_backend
    username = str(camera.get("username") or "").strip()
    password = str(camera.get("password") or "").strip()
    camera_fps = _coerce_float(camera.get("fps"))
    if fps_value is not None:
        camera_fps = fps_value
    return _apply_rtsp_auth(camera_rtsp, username, password), camera_fps, source_backend


def _apply_rtsp_auth(url: str, username: str, password: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not user and not pwd:
        return raw
    if raw.startswith("rtsp://"):
        rest = raw[len("rtsp://") :]
        if pwd:
            return f"rtsp://{user}:{pwd}@{rest}"
        return f"rtsp://{user}@{rest}"
    return raw


def _coerce_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed <= 0.0:
        return None
    return min(60.0, max(1.0, parsed))
