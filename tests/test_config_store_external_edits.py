from __future__ import annotations

import asyncio
import json
from pathlib import Path

from toposync.runtime.config_store import (
    AppConfig,
    ConfigStore,
    Pipeline,
    UserDataPaths,
    _normalize_config,
)


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


def test_normalize_config_migrates_detect_events_before_tracking_to_annotate() -> None:
    config = AppConfig(
        pipelines=[
            Pipeline(
                name="tracked_people",
                graph={
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "detect",
                            "operator": "vision.detect",
                            "config": {"model_id": "fake.detector", "emit_mode": "events"},
                        },
                        {
                            "id": "track",
                            "operator": "vision.track",
                            "config": {"tracker_id": "simple_iou_kalman"},
                        },
                    ],
                    "edges": [
                        {
                            "from": {"node": "detect", "port": "out"},
                            "to": {"node": "track", "port": "in"},
                        }
                    ],
                },
            )
        ]
    )

    normalized = _normalize_config(config)

    detect = normalized.pipelines[0].graph["nodes"][0]
    assert detect["config"]["emit_mode"] == "annotate"


def test_normalize_config_keeps_detect_events_without_tracking() -> None:
    config = AppConfig(
        pipelines=[
            Pipeline(
                name="detection_events",
                graph={
                    "schema_version": 1,
                    "nodes": [
                        {
                            "id": "detect",
                            "operator": "vision.detect",
                            "config": {"model_id": "fake.detector", "emit_mode": "events"},
                        },
                        {"id": "notify", "operator": "core.notify", "config": {}},
                    ],
                    "edges": [
                        {
                            "from": {"node": "detect", "port": "out"},
                            "to": {"node": "notify", "port": "in"},
                        }
                    ],
                },
            )
        ]
    )

    normalized = _normalize_config(config)

    detect = normalized.pipelines[0].graph["nodes"][0]
    assert detect["config"]["emit_mode"] == "events"
