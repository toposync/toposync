"""Opt-in structured notification projection; no image persistence or network."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from toposync.runtime.pipelines.execution import PipelineRuntimeDependencies
from toposync.runtime.pipelines.operators_sinks import (
    NotifyConfig,
    NotifyRuntime,
    _copy_notification_projection,
    _merge_notification_projection,
    _project_notification_payload,
    _select_notification_data,
)
from toposync.runtime.pipelines.runtime import Artifact, Lifecycle, Packet


CONTEXT = SimpleNamespace(pipeline_name="fixture", node_id="notify")


def packet(value=0.2, lifecycle=Lifecycle.UPDATE):
    return Packet.create(
        stream_id="camera:fixture",
        lifecycle=lifecycle,
        payload={
            "subject": {"id": "person", "category": "person"},
            "vision": {"poses": [{"landmarks": [{"name": "wrist", "position": [value, 0.3]}]}]},
            "spatial": {"person_ground": {"body": {"position": [1.0, 0, 2.0]}}},
        },
        artifacts={"main": Artifact(name="main", data=b"not persisted", mime_type="image/raw")},
    )


def runtime(config):
    emitted = []

    async def upsert(**kwargs):
        emitted.append(kwargs)

    return NotifyRuntime(config, PipelineRuntimeDependencies(notifications_upsert=upsert)), emitted


@pytest.mark.parametrize(
    "path", ["", " a", "a ", "a..b", ".a", "a.", "a.*", "a[0]", "a.0", "a-b", "a/secret", "x" * 257]
)
def test_projection_path_syntax_is_restricted(path):
    with pytest.raises(ValidationError):
        NotifyConfig(include_payload_paths=[path])


def test_projection_path_limit_deduplication_and_default():
    assert NotifyConfig().include_payload_paths == []
    assert NotifyConfig(include_payload_paths=["a.b", "a.b", "_c2"]).include_payload_paths == [
        "a.b",
        "_c2",
    ]
    assert NotifyConfig(include_payload_paths=["x" * 256]).include_payload_paths == ["x" * 256]
    with pytest.raises(ValidationError):
        NotifyConfig(include_payload_paths=[f"field{index}" for index in range(17)])


def test_default_preserves_legacy_data_and_deduplication_without_writing_images(tmp_path):
    async def scenario():
        notify, emitted = runtime({"update_interval_seconds": 0})
        first = packet()
        await notify.process_packet(first, CONTEXT)
        await notify.process_packet(packet(0.8), CONTEXT)
        assert len(emitted) == 1
        assert emitted[0]["payload"]["data"] == _select_notification_data(first)
        assert "data_projection" not in emitted[0]["payload"]
        assert emitted[0]["image_path"] is None
        assert emitted[0]["payload"]["artifacts"] == {}
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


def test_nested_pose_updates_emit_with_same_title_and_lifecycle_then_dedupe():
    async def scenario():
        notify, emitted = runtime(
            {
                "include_payload_paths": ["vision.poses", "spatial.person_ground"],
                "update_interval_seconds": 0,
            }
        )
        original = packet()
        await notify.process_packet(original, CONTEXT)
        await notify.process_packet(packet(0.8), CONTEXT)
        await notify.process_packet(packet(0.8), CONTEXT)
        assert len(emitted) == 2
        first, second = emitted
        assert first["dedupe_key"] == second["dedupe_key"]
        assert first["title"] == second["title"]
        assert first["payload"]["lifecycle"] == second["payload"]["lifecycle"]
        assert first["payload"]["data"]["vision"] == original.payload["vision"]
        assert first["payload"]["data"]["spatial"] == original.payload["spatial"]
        assert second["payload"]["data"]["vision"]["poses"][0]["landmarks"][0]["position"] == [
            0.8,
            0.3,
        ]
        assert second["payload"]["data_projection"]["status"] == "ready"
        assert first["image_path"] is None
        original.payload["vision"]["poses"][0]["landmarks"][0]["position"][0] = 99
        assert first["payload"]["data"]["vision"]["poses"][0]["landmarks"][0]["position"][0] == 0.2

    asyncio.run(scenario())


def test_projection_rate_limit_close_and_shutdown_remain_existing_lifecycle(monkeypatch):
    async def scenario():
        clock = [10.0]
        monkeypatch.setattr(
            "toposync.runtime.pipelines.operators_sinks.time.monotonic", lambda: clock[0]
        )
        notify, emitted = runtime(
            {"include_payload_paths": ["vision.poses"], "update_interval_seconds": 1}
        )
        await notify.process_packet(packet(), CONTEXT)
        clock[0] = 10.1
        await notify.process_packet(packet(0.8), CONTEXT)
        assert len(emitted) == 1
        clock[0] = 11.1
        await notify.process_packet(packet(0.8), CONTEXT)
        assert len(emitted) == 2
        await notify.process_packet(packet(0.8, Lifecycle.CLOSE), CONTEXT)
        assert len(emitted) == 3
        assert emitted[-1]["payload"]["status"] == "closed"
        assert not notify._state
        await notify.process_packet(packet(), CONTEXT)
        await notify.shutdown()
        assert emitted[-1]["payload"]["reason"] == "shutdown_synthesized"
        assert emitted[-1]["payload"]["status"] == "closed"
        assert not notify._state

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value,reason",
    [
        (float("nan"), "non_finite_number"),
        (float("inf"), "non_finite_number"),
        (b"bytes", "unsupported_value_type"),
        (object(), "unsupported_value_type"),
        ({1: "value"}, "non_string_key"),
        ([0] * 8193, "node_limit"),
        ("x" * 131073, "byte_limit"),
    ],
)
def test_invalid_paths_fail_atomically_without_truncating_coordinates(value, reason):
    selected, status = _project_notification_payload(
        {"bad": {"coordinates": [1, value, 3]}, "good": None}, ["bad", "good", "missing"]
    )
    assert selected == {"good": None}
    assert status["status"] == "partial"
    assert status["paths"]["bad"] == {"status": "unavailable", "reason": reason}
    assert status["paths"]["missing"]["reason"] == "missing_path"
    assert status["paths"]["good"]["status"] == "ready"
    json.dumps({"data": selected, "data_projection": status}, allow_nan=False)


def test_depth_nodes_and_byte_limits_are_cumulative_and_bounded():
    nested = 1
    for _ in range(17):
        nested = {"next": nested}
    selected, status = _project_notification_payload({"deep": nested}, ["deep"])
    assert selected == {}
    assert status["paths"]["deep"]["reason"] == "depth_limit"
    for payload, reason in [
        ({"one": [0] * 4500, "two": [0] * 4500}, "node_limit"),
        ({"one": "x" * 70000, "two": "y" * 70000}, "byte_limit"),
    ]:
        selected, status = _project_notification_payload(payload, ["one", "two"])
        assert selected == {"one": payload["one"]}
        assert status["paths"]["two"]["reason"] == reason
        result = {"data": selected, "data_projection": status}
        assert _copy_notification_projection(result) == result
        assert len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()) <= 131072


def test_overlapping_paths_and_failed_legacy_path_have_no_partial_fallback():
    source = {"world": {"x": float("nan"), "z": 4.0}}
    selected, status = _project_notification_payload(source, ["world", "world.z"])
    assert selected == {"world": {"z": 4.0}}
    assert status["status"] == "partial"
    legacy = {"world": {"x": float("nan"), "z": 4.0}, "subject": {"id": "person"}}
    assert _merge_notification_projection(legacy, selected, ["world", "world.z"]) == {
        "world": {"z": 4.0},
        "subject": {"id": "person"},
    }


def test_missing_invalid_and_recovered_projection_change_signature():
    async def scenario():
        notify, emitted = runtime(
            {"include_payload_paths": ["observation"], "update_interval_seconds": 0}
        )
        await notify.process_packet(packet(), CONTEXT)
        await notify.process_packet(packet(), CONTEXT)
        invalid = packet()
        invalid.payload["observation"] = float("nan")
        await notify.process_packet(invalid, CONTEXT)
        valid = packet()
        valid.payload["observation"] = {"position": [1, 2, 3]}
        await notify.process_packet(valid, CONTEXT)
        assert len(emitted) == 3
        reasons = [
            item["payload"]["data_projection"]["paths"]["observation"].get("reason")
            for item in emitted
        ]
        assert reasons == ["missing_path", "non_finite_number", None]

    asyncio.run(scenario())


def test_json_budget_exact_boundaries_and_escaped_unicode():
    assert len(_copy_notification_projection([None] * 8191)) == 8191
    with pytest.raises(ValueError, match="node_limit"):
        _copy_notification_projection([None] * 8192)
    assert _copy_notification_projection("x" * (131072 - 2)) == "x" * (131072 - 2)
    with pytest.raises(ValueError, match="byte_limit"):
        _copy_notification_projection("x" * (131072 - 1))
    with pytest.raises(ValueError, match="byte_limit"):
        _copy_notification_projection("\u00e1" * 22000)
    nested = 1
    for _ in range(16):
        nested = [nested]
    assert _copy_notification_projection(nested) == nested
    with pytest.raises(ValueError, match="depth_limit"):
        _copy_notification_projection([nested])


def test_cyclic_or_custom_objects_are_unavailable_without_stringification():
    class Unserializable:
        def __str__(self):
            raise AssertionError("Do not stringify arbitrary packet objects")

    cycle = []
    cycle.append(cycle)
    selected, status = _project_notification_payload(
        {"cycle": cycle, "custom": Unserializable()}, ["cycle", "custom"]
    )
    assert selected == {}
    assert status["status"] == "unavailable"
    assert status["paths"]["cycle"]["reason"] == "depth_limit"
    assert status["paths"]["custom"]["reason"] == "unsupported_value_type"
