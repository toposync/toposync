from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel, ConfigDict

from toposync.runtime.config_store import Pipeline
from toposync.runtime.pipelines import (
    Artifact,
    Lifecycle,
    OperatorRegistry,
    Packet,
    PipelineGraphCompiler,
    PipelineRuntime,
    SinkRuntime,
    SourceOperatorRuntime,
    register_builtin_operators,
)
from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.images import MAIN_ARTIFACT_NAME
from toposync.runtime.pipelines.templates import build_pipeline_graph_v2
from toposync.runtime.services import ServiceRegistry
from toposync_ext_cameras.pipelines import register_camera_pipeline_operators
from toposync_ext_cameras.pipelines.operators import (
    CameraSourceRuntime,
    MotionBgSubAdaptiveRuntime,
    MotionGateRuntime,
    MotionSampleBgRuntime,
    _PTZ_PHYSICAL_REFRESH_AFTER_BY_DEVICE,
)
from toposync_ext_cameras.pipelines.postprocess import (
    _control_point_set_matches_source_scope,
)
from toposync_ext_cameras.processing.mapping import ControlPointSet


class _SequenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_id: str = "camera:test"


class _SinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SequenceRuntime(SourceOperatorRuntime):
    def __init__(self, config: dict[str, Any], sequence: list[dict[str, Any]]) -> None:
        self._stream_id = _SequenceConfig.model_validate(config).stream_id
        self._sequence = deque(sequence)

    async def produce(self, context) -> Packet | None:  # noqa: ANN001, ARG002
        if not self._sequence:
            return None
        await asyncio.sleep(0.01)
        item = self._sequence.popleft()
        return Packet.create(
            stream_id=self._stream_id,
            lifecycle=Lifecycle.UPDATE,
            payload=dict(item),
        )


class _SinkRuntime(SinkRuntime):
    def __init__(self, collector: list[Packet]) -> None:
        self._collector = collector

    async def process_packet(self, packet: Packet, context) -> list[Packet]:  # noqa: ANN001, ARG002
        self._collector.append(packet)
        return []


def _calibrated_view(
    *,
    view_id: str,
    source_id: str,
    role: str,
    world_offset: float,
    pose_bound: bool = True,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": view_id,
        "label": view_id,
        "stream_scope": {
            "compatible_source_ids": [source_id],
            "compatible_roles": [role],
        },
        "projection_model": {
            "type": "image_quad_on_world",
            "image_region": {
                "top_left": {"x": 0.0, "y": 0.0},
                "bottom_right": {"x": 1.0, "y": 1.0},
            },
            "world_quad": {
                "top_left": {"x": world_offset, "z": world_offset},
                "top_right": {"x": world_offset + 10.0, "z": world_offset},
                "bottom_right": {"x": world_offset + 10.0, "z": world_offset + 10.0},
                "bottom_left": {"x": world_offset, "z": world_offset + 10.0},
            },
        },
    }
    if pose_bound:
        view["pose_reference"] = {"preset_token": "door"}
    return view


def _packet_payload(
    *,
    source_id: str,
    role: str,
    geometry_safe: bool | None,
    motion_epoch: int,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "move_status": "IDLE",
        "motion_state": "stable",
        "motion_epoch": motion_epoch,
        "preset_token": "door",
    }
    if geometry_safe is not None:
        state["geometry_safe"] = geometry_safe
    return {
        "camera_id": "front",
        "camera_source_id": source_id,
        "source": {
            "device_id": "front",
            "source_id": source_id,
            "role": role,
            "kind": "camera",
            "modality": "video",
        },
        "image_uv": {"u": 0.5, "v": 0.5},
        "pan_tilt_zoom_state": state,
    }


def test_unscoped_calibration_defaults_to_wide_roles_only() -> None:
    unscoped = ControlPointSet(
        id="unscoped",
        label="Unscoped",
        pose_reference=None,
        control_points=(),
    )

    assert _control_point_set_matches_source_scope(
        unscoped,
        source_id="wide_main",
        source_role="main",
    )
    assert not _control_point_set_matches_source_scope(
        unscoped,
        source_id="zoom_main",
        source_role="zoom",
    )
    assert not _control_point_set_matches_source_scope(
        unscoped,
        source_id=None,
        source_role=None,
    )


def test_graph_v2_mapping_fails_closed_and_selects_only_matching_lens() -> None:
    async def scenario() -> tuple[int, list[Packet]]:
        sequence = [
            _packet_payload(
                source_id="wide_main",
                role="main",
                geometry_safe=False,
                motion_epoch=1,
            ),
            _packet_payload(
                source_id="wide_main",
                role="main",
                geometry_safe=True,
                motion_epoch=1,
            ),
            _packet_payload(
                source_id="zoom_main",
                role="zoom",
                geometry_safe=True,
                motion_epoch=2,
            ),
            _packet_payload(
                source_id="zoom_main",
                role="zoom",
                geometry_safe=None,
                motion_epoch=2,
            ),
            {
                "camera_id": "front",
                "camera_source_id": "fixed_main",
                "source": {
                    "device_id": "front",
                    "source_id": "fixed_main",
                    "role": "main",
                    "kind": "camera",
                    "modality": "video",
                },
                "image_uv": {"u": 0.5, "v": 0.5},
            },
        ]
        collector: list[Packet] = []
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        registry.register_operator(
            operator_id="test.sequence",
            config_model=_SequenceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            share_strategy="never",
            runtime_factory=lambda config, _deps: _SequenceRuntime(config, sequence),
        )
        registry.register_operator(
            operator_id="test.sink",
            config_model=_SinkConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _SinkRuntime(collector),
        )
        graph = build_pipeline_graph_v2(
            graph_uid="ptz_geometry_mapping",
            nodes=[
                {"id": "source", "operator": "test.sequence", "config": {}},
                {
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "camera_id": "front",
                        "calibrated_views": [
                            _calibrated_view(
                                view_id="wide-door",
                                source_id="wide_main",
                                role="main",
                                world_offset=0.0,
                            ),
                            _calibrated_view(
                                view_id="zoom-door",
                                source_id="zoom_main",
                                role="zoom",
                                world_offset=100.0,
                            ),
                            _calibrated_view(
                                view_id="fixed-door",
                                source_id="fixed_main",
                                role="main",
                                world_offset=200.0,
                                pose_bound=False,
                            ),
                        ],
                    },
                },
                {"id": "sink", "operator": "test.sink", "config": {}},
            ],
            edges=[
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                },
                {"from": {"node": "mapping", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(
            Pipeline(name="ptz_geometry_mapping", graph=graph)
        )
        runtime = PipelineRuntime(compiled=compiled, registry=registry)
        await runtime.run_for(0.2)
        return compiled.schema_version, collector

    schema_version, packets = asyncio.run(scenario())
    assert schema_version == 2
    assert len(packets) == 5
    assert "mapping" not in packets[0].payload
    assert packets[1].payload["mapping"]["calibrated_view_id"] == "wide-door"
    assert packets[1].payload["world"] == pytest.approx({"x": 5.0, "z": 5.0})
    assert packets[2].payload["mapping"]["calibrated_view_id"] == "zoom-door"
    assert packets[2].payload["world"] == pytest.approx({"x": 105.0, "z": 105.0})
    assert "mapping" not in packets[3].payload
    assert packets[4].payload["mapping"]["calibrated_view_id"] == "fixed-door"
    assert packets[4].payload["world"] == pytest.approx({"x": 205.0, "z": 205.0})


def test_graph_v2_mapping_resolves_device_once_then_uses_local_snapshot() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[Packet]]:
        sequence = [
            {
                "camera_id": "front",
                "camera_source_id": "zoom_main",
                "source": {
                    "device_id": "front",
                    "source_id": "zoom_main",
                    "role": "zoom",
                    "kind": "camera",
                    "modality": "video",
                },
                "image_uv": {"u": 0.5, "v": 0.5},
            }
            for _index in range(2)
        ]
        collector: list[Packet] = []
        calls: list[dict[str, Any]] = []
        services = ServiceRegistry()

        async def snapshot(**kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "ptz_device_id": "shared-head",
                "geometry_safe": True,
                "motion_epoch": 9,
                "motion_state": "stable",
                "move_status": "IDLE",
                "last_command": {"command_id": "move-9", "kind": "preset", "preset_token": "door"},
            }

        services.register("cameras.control.snapshot", snapshot)
        registry = OperatorRegistry()
        register_builtin_operators(registry)
        register_camera_pipeline_operators(registry)
        registry.register_operator(
            operator_id="test.sequence",
            config_model=_SequenceConfig,
            inputs=[],
            outputs=[{"name": "out"}],
            share_strategy="never",
            runtime_factory=lambda config, _deps: _SequenceRuntime(config, sequence),
        )
        registry.register_operator(
            operator_id="test.sink",
            config_model=_SinkConfig,
            inputs=[{"name": "in", "required": True}],
            outputs=[],
            share_strategy="never",
            runtime_factory=lambda _config, _deps: _SinkRuntime(collector),
        )
        graph = build_pipeline_graph_v2(
            graph_uid="ptz_mapping_fast_path",
            nodes=[
                {"id": "source", "operator": "test.sequence", "config": {}},
                {
                    "id": "mapping",
                    "operator": "camera.camera_mapping",
                    "config": {
                        "camera_id": "front",
                        "ptz_state_fetch": {"cache_ttl_seconds": 0.0},
                        "calibrated_views": [
                            _calibrated_view(
                                view_id="zoom-door",
                                source_id="zoom_main",
                                role="zoom",
                                world_offset=100.0,
                            )
                        ],
                    },
                },
                {"id": "sink", "operator": "test.sink", "config": {}},
            ],
            edges=[
                {
                    "from": {"node": "source", "port": "out"},
                    "to": {"node": "mapping", "port": "in"},
                },
                {"from": {"node": "mapping", "port": "out"}, "to": {"node": "sink", "port": "in"}},
            ],
        )
        compiled = PipelineGraphCompiler(registry).compile_pipeline(
            Pipeline(name="ptz_mapping_fast_path", graph=graph)
        )
        runtime = PipelineRuntime(
            compiled=compiled,
            registry=registry,
            dependencies=PipelineRuntimeDependencies(services=services),
        )
        await runtime.run_for(0.2)
        return calls, collector

    calls, packets = asyncio.run(scenario())
    assert len(packets) == 2
    assert all(packet.payload["mapping"]["calibrated_view_id"] == "zoom-door" for packet in packets)
    assert len(calls) == 2
    assert calls[0]["camera_id"] == "front"
    assert calls[0]["source_id"] == "zoom_main"
    assert "ptz_device_id" not in calls[0]
    assert calls[1]["ptz_device_id"] == "shared-head"
    assert "camera_id" not in calls[1] and "source_id" not in calls[1]
    assert all(call["include_readiness"] is False for call in calls)
    assert all(call["refresh_physical"] is False for call in calls)


@pytest.mark.parametrize(
    "runtime_factory",
    [
        lambda: MotionGateRuntime({}, PipelineRuntimeDependencies()),
        lambda: MotionBgSubAdaptiveRuntime({}, PipelineRuntimeDependencies()),
        lambda: MotionSampleBgRuntime({}, PipelineRuntimeDependencies()),
    ],
)
def test_motion_runtimes_reset_internal_scene_by_ptz_epoch(runtime_factory) -> None:  # noqa: ANN001
    async def scenario() -> set[str]:
        runtime = runtime_factory()
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        for epoch in (4, 5):
            packet = Packet.create(
                stream_id="camera:front:wide_main",
                payload={"pan_tilt_zoom_state": {"motion_epoch": epoch}},
                artifacts={
                    MAIN_ARTIFACT_NAME: Artifact(
                        name=MAIN_ARTIFACT_NAME,
                        data=frame,
                        mime_type="image/raw",
                    )
                },
            )
            await runtime.process_packet(packet, SimpleNamespace())
        return set(runtime._detector_by_key)  # noqa: SLF001

    assert asyncio.run(scenario()) == {"camera:front:wide_main|ptz:5"}


class _SettingsStore:
    def __init__(
        self,
        *,
        control_type: str,
        ptz_capable: bool | None = None,
        ptz_xaddr: str = "",
        source_has_ptz: bool = False,
        camera_enabled: bool = True,
        source_enabled: bool = True,
        source_kind: str = "video",
    ) -> None:
        self._control_type = control_type
        self.ptz_capable = ptz_capable
        self.ptz_xaddr = ptz_xaddr
        self.source_has_ptz = source_has_ptz
        self.camera_enabled = camera_enabled
        self.source_enabled = source_enabled
        self.source_kind = source_kind
        self.calls = 0

    async def get_settings(self) -> Any:
        self.calls += 1
        control: dict[str, Any] = {
            "type": self._control_type,
            "ptz_device_id": "shared-head" if self._control_type == "onvif" else "",
        }
        if self.ptz_capable is not None:
            control["ptz_capable"] = self.ptz_capable
        return SimpleNamespace(
            extensions={
                "com.toposync.cameras": {
                    "schema_version": 4,
                    "devices": [
                        {
                            "id": "front",
                            "name": "Front",
                            "enabled": self.camera_enabled,
                            "control": control,
                            "onvif": {"ptz_xaddr": self.ptz_xaddr},
                            "sources": [
                                {
                                    "id": "wide_main",
                                    "enabled": self.source_enabled,
                                    "kind": self.source_kind,
                                    "origin": {"has_ptz": self.source_has_ptz},
                                }
                            ],
                        }
                    ],
                }
            }
        )


def test_camera_source_bootstraps_ptz_guard_and_leaves_non_ptz_unchanged() -> None:
    async def scenario() -> tuple[
        list[dict[str, Any]],
        list[Packet | None],
        Packet | None,
        int,
        int,
        int,
    ]:
        class _CaptureService:
            def __init__(self) -> None:
                self.frame_ts = 0.0

            async def open(self, request: Any, _dependencies: Any) -> Any:
                return SimpleNamespace(
                    lease_id=f"lease:{request.camera_id}",
                    hub_key=f"hub:{request.camera_id}",
                    grabber=object(),
                    resolved=SimpleNamespace(
                        camera_id=request.camera_id,
                        camera_name="Front",
                        source_id=request.source_id,
                        source_name="Wide",
                        view_id="front-view",
                        role="main",
                        clock_domain="camera",
                        transport="rtsp",
                        used_ingest=False,
                        ingest_mode="direct",
                        centralizer_server_id="",
                        ingest_path="",
                        ingest_warnings=(),
                        ingest_blocking_errors=(),
                    ),
                )

            async def get_latest(self, _lease_id: str, *, min_frame_ts: float) -> Any:
                self.frame_ts = max(self.frame_ts + 1.0, min_frame_ts + 1.0)
                return SimpleNamespace(
                    released=False,
                    frame=np.zeros((8, 8, 3), dtype=np.uint8),
                    frame_ts=self.frame_ts,
                    fresh=True,
                    height=8,
                    width=8,
                    metrics={"backend": "test", "source_status": "live"},
                )

        _PTZ_PHYSICAL_REFRESH_AFTER_BY_DEVICE.clear()
        calls: list[dict[str, Any]] = []
        physical_seen = False
        services = ServiceRegistry()

        async def snapshot(**kwargs: Any) -> dict[str, Any]:
            nonlocal physical_seen
            refresh = bool(kwargs.get("refresh_physical"))
            calls.append(dict(kwargs))
            if refresh:
                physical_seen = True
                return {
                    "ptz_device_id": "shared-head",
                    "geometry_safe": False,
                    "motion_epoch": 0,
                    "motion_state": "settling",
                    "move_status": "IDLE",
                }
            return {
                "ptz_device_id": "shared-head",
                "geometry_safe": physical_seen,
                "motion_epoch": 0,
                "motion_state": "stable" if physical_seen else "unknown",
                "move_status": "IDLE" if physical_seen else "UNKNOWN",
            }

        services.register("cameras.control.snapshot", snapshot)
        ptz_settings = _SettingsStore(control_type="onvif", source_has_ptz=True)
        ptz_runtime = CameraSourceRuntime(
            {"camera_id": "front", "source_id": "wide_main"},
            PipelineRuntimeDependencies(
                config_store=ptz_settings,
                services=services,
            ),
        )
        ptz_runtime._capture_service = _CaptureService()  # noqa: SLF001
        ptz_runtime._ptz_settings_refresh_interval_s = 60.0  # noqa: SLF001
        ptz_runtime._ptz_physical_refresh_interval_s = 60.0  # noqa: SLF001
        context = SimpleNamespace(
            inputs={},
            pipeline_name="ptz-source-test",
            node_id="source",
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
        packets = [await ptz_runtime.produce(context) for _index in range(6)]

        fixed_calls = 0
        fixed_services = ServiceRegistry()

        async def fixed_snapshot(**_kwargs: Any) -> dict[str, Any]:
            nonlocal fixed_calls
            fixed_calls += 1
            return {"geometry_safe": False}

        fixed_services.register("cameras.control.snapshot", fixed_snapshot)
        fixed_settings = _SettingsStore(control_type="onvif")
        fixed_runtime = CameraSourceRuntime(
            {"camera_id": "front", "source_id": "wide_main"},
            PipelineRuntimeDependencies(
                config_store=fixed_settings,
                services=fixed_services,
            ),
        )
        fixed_runtime._capture_service = _CaptureService()  # noqa: SLF001
        fixed_packet = await fixed_runtime.produce(context)
        return (
            calls,
            packets,
            fixed_packet,
            ptz_settings.calls,
            fixed_settings.calls,
            fixed_calls,
        )

    calls, packets, fixed_packet, ptz_settings_calls, fixed_settings_calls, fixed_calls = (
        asyncio.run(scenario())
    )
    assert [bool(call.get("refresh_physical")) for call in calls] == [
        False,
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    fast_calls = [call for call in calls if not call.get("refresh_physical")]
    assert all(call.get("ptz_device_id") == "front" for call in fast_calls)
    assert all(call.get("include_readiness") is False for call in fast_calls)
    assert all("camera_id" not in call and "source_id" not in call for call in fast_calls)
    assert all(packet is not None for packet in packets)
    first_packet = packets[0]
    assert first_packet is not None
    assert first_packet.payload["pan_tilt_zoom_state"]["geometry_safe"] is False
    assert first_packet.metadata["ptz_geometry_safe"] is False
    safe_packets = [packet for packet in packets[1:] if packet is not None]
    assert len(safe_packets) == 5
    assert all(
        packet.payload["pan_tilt_zoom_state"]["geometry_safe"] is True for packet in safe_packets
    )
    assert all(packet.metadata["ptz_geometry_safe"] is True for packet in safe_packets)
    assert fixed_packet is not None
    assert "pan_tilt_zoom_state" not in fixed_packet.payload
    assert ptz_settings_calls == 1
    assert fixed_settings_calls == 1
    assert fixed_calls == 0


@pytest.mark.parametrize(
    (
        "control_type",
        "ptz_capable",
        "ptz_xaddr",
        "source_has_ptz",
        "camera_enabled",
        "source_enabled",
        "source_kind",
        "expected",
    ),
    [
        ("onvif", None, "", True, True, True, "video", True),
        ("onvif", True, "", False, True, True, "video", False),
        ("onvif", None, "http://camera/ptz", False, True, True, "video", False),
        ("onvif", False, "http://camera/ptz", True, True, True, "video", False),
        ("onvif", None, "", True, False, True, "video", False),
        ("onvif", None, "", True, True, False, "video", False),
        ("onvif", None, "", True, True, True, "audio", False),
        ("none", True, "http://camera/ptz", True, True, True, "video", False),
    ],
)
def test_camera_source_ptz_guard_requires_explicit_capability(
    control_type: str,
    ptz_capable: bool | None,
    ptz_xaddr: str,
    source_has_ptz: bool,
    camera_enabled: bool,
    source_enabled: bool,
    source_kind: str,
    expected: bool,
) -> None:
    async def scenario() -> tuple[bool, str]:
        runtime = CameraSourceRuntime(
            {"camera_id": "front", "source_id": "wide_main"},
            PipelineRuntimeDependencies(
                config_store=_SettingsStore(
                    control_type=control_type,
                    ptz_capable=ptz_capable,
                    ptz_xaddr=ptz_xaddr,
                    source_has_ptz=source_has_ptz,
                    camera_enabled=camera_enabled,
                    source_enabled=source_enabled,
                    source_kind=source_kind,
                )
            ),
        )
        required = await runtime._uses_ptz_geometry_guard()  # noqa: SLF001
        return required, runtime._ptz_device_id  # noqa: SLF001

    required, ptz_device_id = asyncio.run(scenario())
    assert required is expected
    assert ptz_device_id == ("front" if expected else "")


def test_camera_source_periodically_revalidates_safe_physical_state_and_settings() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], int, list[dict[str, Any] | None]]:
        _PTZ_PHYSICAL_REFRESH_AFTER_BY_DEVICE.clear()
        calls: list[dict[str, Any]] = []
        services = ServiceRegistry()

        async def snapshot(**kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "ptz_device_id": "shared-head",
                "geometry_safe": True,
                "motion_epoch": 3,
                "motion_state": "stable",
                "move_status": "IDLE",
            }

        services.register("cameras.control.snapshot", snapshot)
        settings = _SettingsStore(
            control_type="onvif",
            ptz_capable=True,
            source_has_ptz=True,
        )
        runtime = CameraSourceRuntime(
            {"camera_id": "front", "source_id": "wide_main"},
            PipelineRuntimeDependencies(config_store=settings, services=services),
        )
        runtime._ptz_settings_refresh_interval_s = 0.0  # noqa: SLF001
        runtime._ptz_physical_refresh_interval_s = 0.0  # noqa: SLF001

        snapshots = [await runtime._ptz_geometry_snapshot() for _index in range(2)]  # noqa: SLF001
        settings.ptz_capable = False
        snapshots.append(await runtime._ptz_geometry_snapshot())  # noqa: SLF001
        return calls, settings.calls, snapshots

    calls, settings_calls, snapshots = asyncio.run(scenario())
    assert [bool(call.get("refresh_physical")) for call in calls] == [
        False,
        True,
        False,
        True,
    ]
    assert settings_calls == 3
    assert all(snapshot and snapshot["geometry_safe"] is True for snapshot in snapshots[:2])
    assert snapshots[2] is None


def test_camera_source_physical_refresh_failure_fails_closed_and_retries() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any] | None]]:
        _PTZ_PHYSICAL_REFRESH_AFTER_BY_DEVICE.clear()
        calls: list[dict[str, Any]] = []
        services = ServiceRegistry()

        async def snapshot(**kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            if kwargs.get("refresh_physical"):
                raise RuntimeError("status unavailable")
            return {
                "ptz_device_id": "front",
                "geometry_safe": True,
                "motion_epoch": "malformed",
                "motion_state": "stable",
                "move_status": "IDLE",
            }

        services.register("cameras.control.snapshot", snapshot)
        runtime = CameraSourceRuntime(
            {"camera_id": "front", "source_id": "wide_main"},
            PipelineRuntimeDependencies(
                config_store=_SettingsStore(control_type="onvif", source_has_ptz=True),
                services=services,
            ),
        )
        runtime._ptz_settings_refresh_interval_s = 60.0  # noqa: SLF001
        runtime._ptz_physical_refresh_interval_s = 60.0  # noqa: SLF001
        snapshots = [await runtime._ptz_geometry_snapshot() for _index in range(2)]  # noqa: SLF001
        return calls, snapshots

    calls, snapshots = asyncio.run(scenario())
    assert [bool(call.get("refresh_physical")) for call in calls] == [
        False,
        True,
        False,
        True,
    ]
    assert all(snapshot and snapshot["geometry_safe"] is False for snapshot in snapshots)
    assert all(snapshot and snapshot["motion_epoch"] == 0 for snapshot in snapshots)


def test_camera_source_throttles_physical_refresh_per_shared_device() -> None:
    async def scenario() -> list[dict[str, Any]]:
        _PTZ_PHYSICAL_REFRESH_AFTER_BY_DEVICE.clear()
        calls: list[dict[str, Any]] = []
        services = ServiceRegistry()

        async def snapshot(**kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "ptz_device_id": "shared-head",
                "geometry_safe": True,
                "motion_epoch": 4,
                "motion_state": "stable",
                "move_status": "IDLE",
            }

        services.register("cameras.control.snapshot", snapshot)
        dependencies = PipelineRuntimeDependencies(
            config_store=_SettingsStore(control_type="onvif", source_has_ptz=True),
            services=services,
        )
        runtimes = [
            CameraSourceRuntime(
                {"camera_id": "front", "source_id": source_id},
                dependencies,
            )
            for source_id in ("wide_main", "zoom_main")
        ]
        for runtime in runtimes:
            runtime._ptz_physical_refresh_interval_s = 60.0  # noqa: SLF001
            assert await runtime._ptz_geometry_snapshot()  # noqa: SLF001
        return calls

    calls = asyncio.run(scenario())
    assert [bool(call.get("refresh_physical")) for call in calls] == [False, True, False]
