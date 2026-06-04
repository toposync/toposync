from __future__ import annotations

import asyncio
import json
from pathlib import Path

from toposync.runtime.config_store import ConfigStore, Pipeline, UserDataPaths


def _create_store(tmp_path: Path) -> ConfigStore:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )
    return ConfigStore(paths=paths)


def _external_append_pipeline(config_path: Path, name: str) -> None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw.setdefault("pipelines", [])
    raw["pipelines"].append(
        {
            "name": name,
            "enabled": True,
            "processing_server_id": "local",
            "editor_mode": "json",
            "python_source": "",
            "graph": {"schema_version": 1},
        }
    )
    config_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_config_store_reloads_after_external_config_json_change(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    asyncio.run(store.create_pipeline(Pipeline(name="p1", graph={"schema_version": 1})))

    _external_append_pipeline(store.paths.config_path, "p2")

    pipelines = asyncio.run(store.list_pipelines())
    names = {p.name for p in pipelines}
    assert names == {"p1", "p2"}


def test_config_store_write_does_not_clobber_external_pipeline_changes(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    asyncio.run(store.create_pipeline(Pipeline(name="p1", graph={"schema_version": 1})))

    _external_append_pipeline(store.paths.config_path, "p2")

    asyncio.run(store.patch_extension_settings("ext.test", {"foo": "bar"}))

    saved = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
    names = {item.get("name") for item in saved.get("pipelines") or []}
    assert names == {"p1", "p2"}


def test_config_store_migrates_track_events_to_event_assembler(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    store.paths.data_dir.mkdir(parents=True, exist_ok=True)
    store.paths.config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pipelines": [
                    {
                        "name": "legacy_tracking",
                        "enabled": True,
                        "processing_server_id": "local",
                        "editor_mode": "json",
                        "python_source": "",
                        "graph": {
                            "schema_version": 1,
                            "nodes": [
                                {"id": "source", "operator": "camera.source", "config": {}},
                                {
                                    "id": "track",
                                    "operator": "vision.track",
                                    "config": {
                                        "emit_mode": "events",
                                        "close_after_seconds": 5,
                                        "default_interval_seconds": 0.25,
                                    },
                                },
                                {
                                    "id": "throttle",
                                    "operator": "core.throttle",
                                    "config": {"key_field": "payload.tracking_id"},
                                },
                                {
                                    "id": "notify",
                                    "operator": "core.notify",
                                    "config": {"dedupe_key_template": "{{tracking_id}}"},
                                },
                            ],
                            "edges": [
                                {"from": {"node": "source", "port": "out"}, "to": {"node": "track", "port": "in"}},
                                {
                                    "from": {"node": "track", "port": "out"},
                                    "to": {"node": "throttle", "port": "in"},
                                    "maxsize": 64,
                                    "drop_policy": "keyed_latest_only",
                                },
                                {"from": {"node": "throttle", "port": "out"}, "to": {"node": "notify", "port": "in"}},
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    config = asyncio.run(store.load())
    graph = config.pipelines[0].graph
    nodes = graph["nodes"]
    edges = graph["edges"]
    nodes_by_id = {node["id"]: node for node in nodes}

    assert nodes_by_id["track"]["config"]["emit_mode"] == "annotate"
    assert nodes_by_id["event"]["operator"] == "vision.event_assembler"
    assert nodes_by_id["event"]["config"] == {
        "max_gap_seconds": 5.0,
        "default_interval_seconds": 0.25,
    }
    assert nodes_by_id["throttle"]["config"]["key_field"] == "payload.event_id"
    assert nodes_by_id["notify"]["config"]["dedupe_key_template"] == "{{event_id}}"
    assert any(edge["from"]["node"] == "track" and edge["to"]["node"] == "event" for edge in edges)
    assert any(edge["from"]["node"] == "event" and edge["to"]["node"] == "throttle" for edge in edges)


def test_config_store_reuses_existing_event_assembler_when_retargeting_legacy_track_edges(
    tmp_path: Path,
) -> None:
    store = _create_store(tmp_path)
    store.paths.data_dir.mkdir(parents=True, exist_ok=True)
    store.paths.config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pipelines": [
                    {
                        "name": "partially_migrated_tracking",
                        "graph": {
                            "schema_version": 1,
                            "nodes": [
                                {
                                    "id": "track",
                                    "operator": "vision.track",
                                    "config": {"emit_mode": "events"},
                                },
                                {
                                    "id": "event",
                                    "operator": "vision.event_assembler",
                                    "config": {"max_gap_seconds": 5.0},
                                },
                                {"id": "store", "operator": "core.store_images", "config": {}},
                            ],
                            "edges": [
                                {"from": {"node": "track", "port": "out"}, "to": {"node": "event", "port": "in"}},
                                {"from": {"node": "track", "port": "out"}, "to": {"node": "store", "port": "in"}},
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    config = asyncio.run(store.load())
    graph = config.pipelines[0].graph
    nodes = graph["nodes"]
    edges = graph["edges"]
    nodes_by_id = {node["id"]: node for node in nodes}

    assert nodes_by_id["track"]["config"]["emit_mode"] == "annotate"
    assert [node["id"] for node in nodes if node["operator"] == "vision.event_assembler"] == ["event"]
    assert sum(1 for edge in edges if edge["from"]["node"] == "track" and edge["to"]["node"] == "event") == 1
    assert any(edge["from"]["node"] == "event" and edge["to"]["node"] == "store" for edge in edges)
    assert not any(edge["from"]["node"] == "track" and edge["to"]["node"] == "store" for edge in edges)
