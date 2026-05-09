from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from toposync.runtime.pipelines.storage import (
    PipelineStorageError,
    PipelineStorageLayerLimit,
    PipelineStorageLimits,
    PipelineStorageManager,
    build_storage_layer_key,
)


def _manager(tmp_path: Path) -> PipelineStorageManager:
    return PipelineStorageManager(
        data_dir=tmp_path / "data",
        files_dir=tmp_path / "files",
    )


def _limits(
    *,
    pipeline_bytes: int | None = 10_000,
    target_ratio: float = 0.9,
    layer_limits: dict[str, PipelineStorageLayerLimit] | None = None,
) -> PipelineStorageLimits:
    return PipelineStorageLimits(
        max_bytes_per_pipeline=pipeline_bytes,
        cleanup_target_ratio=target_ratio,
        min_free_bytes=0,
        layer_limits=layer_limits or {},
    )


def _store(
    manager: PipelineStorageManager,
    *,
    pipeline: str = "pipe",
    node: str = "store",
    artifact: str = "main",
    layer: str = "Original",
    payload: bytes = b"x" * 100,
    limits: PipelineStorageLimits | None = None,
):
    return manager.store_blob(
        pipeline_name=pipeline,
        node_id=node,
        artifact_name=artifact,
        layer_label=layer,
        filename_hint=f"{node}_{artifact}",
        ext=".bin",
        mime_type="application/octet-stream",
        blob=payload,
        frame_ts=1_700_000_000.0,
        limits=limits or _limits(),
    )


def test_storage_manager_writes_atomically_and_records_sqlite(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        stored = _store(manager, payload=b"hello")

        abs_path = tmp_path / "files" / stored.rel_path
        assert abs_path.read_bytes() == b"hello"
        assert stored.rel_path.startswith("pipelines/pipe/store__Original/")

        con = sqlite3.connect(tmp_path / "data" / "storage" / "pipeline_storage.sqlite3")
        try:
            row = con.execute(
                "SELECT pipeline_name, layer_label, rel_path, size_bytes, status FROM storage_object"
            ).fetchone()
        finally:
            con.close()
        assert row == ("pipe", "Original", stored.rel_path, 5, "active")

        summary = manager.summarize_pipeline("pipe", limits=_limits())
        assert summary["used_bytes"] == 5
        assert summary["file_count"] == 1
        assert summary["layers"][0]["avg_file_bytes"] == 5
    finally:
        manager.close()


def test_storage_manager_applies_pipeline_byte_retention(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        limits = _limits(pipeline_bytes=250, target_ratio=0.5)
        first = _store(manager, payload=b"a" * 100, limits=limits)
        _store(manager, payload=b"b" * 100, limits=limits)
        latest = _store(manager, payload=b"c" * 100, limits=limits)

        summary = manager.summarize_pipeline("pipe", limits=limits)
        assert summary["used_bytes"] <= 250
        assert summary["file_count"] <= 2
        assert not (tmp_path / "files" / first.rel_path).exists()
        assert (tmp_path / "files" / latest.rel_path).is_file()
    finally:
        manager.close()


def test_storage_manager_applies_layer_byte_retention(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        layer_key = build_storage_layer_key(
            node_id="store_original",
            layer_label="Original",
            artifact_name="main",
        )
        limits = _limits(
            pipeline_bytes=10_000,
            target_ratio=0.5,
            layer_limits={layer_key: PipelineStorageLayerLimit(max_bytes=180)},
        )
        old = _store(
            manager,
            node="store_original",
            layer="Original",
            payload=b"a" * 100,
            limits=limits,
        )
        new = _store(
            manager,
            node="store_original",
            layer="Original",
            payload=b"b" * 100,
            limits=limits,
        )
        other = _store(
            manager,
            node="store_crop",
            layer="Recorte",
            artifact="crop",
            payload=b"c" * 100,
            limits=limits,
        )

        assert not (tmp_path / "files" / old.rel_path).exists()
        assert (tmp_path / "files" / new.rel_path).is_file()
        assert (tmp_path / "files" / other.rel_path).is_file()
        summary = manager.summarize_pipeline("pipe", limits=limits)
        assert summary["file_count"] == 2
    finally:
        manager.close()


def test_storage_manager_applies_layer_file_count_retention(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        layer_key = build_storage_layer_key(
            node_id="store",
            layer_label="Original",
            artifact_name="main",
        )
        limits = _limits(
            layer_limits={layer_key: PipelineStorageLayerLimit(max_files=2)},
        )
        stored = [_store(manager, payload=bytes([index]) * 10, limits=limits) for index in range(4)]

        summary = manager.summarize_pipeline("pipe", limits=limits)
        assert summary["file_count"] == 2
        assert not (tmp_path / "files" / stored[0].rel_path).exists()
        assert not (tmp_path / "files" / stored[1].rel_path).exists()
        assert (tmp_path / "files" / stored[2].rel_path).is_file()
        assert (tmp_path / "files" / stored[3].rel_path).is_file()
    finally:
        manager.close()


def test_storage_manager_keeps_new_file_when_it_exceeds_limit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        limits = _limits(pipeline_bytes=50, target_ratio=0.9)
        old = _store(manager, payload=b"a" * 20, limits=limits)
        new = _store(manager, payload=b"b" * 100, limits=limits)

        summary = manager.summarize_pipeline("pipe", limits=limits)
        assert summary["used_bytes"] == 100
        assert summary["file_count"] == 1
        assert summary["over_limit"] is True
        assert not (tmp_path / "files" / old.rel_path).exists()
        assert (tmp_path / "files" / new.rel_path).is_file()
    finally:
        manager.close()


def test_storage_manager_blocks_path_traversal(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        with pytest.raises(PipelineStorageError):
            manager._absolute_path_for_rel_path("../outside.bin")
        stored = _store(
            manager,
            pipeline="../evil",
            node="../node",
            layer="../../layer",
            payload=b"ok",
        )
        assert (tmp_path / "files" / stored.rel_path).is_file()
        assert ".." not in Path(stored.rel_path).parts
    finally:
        manager.close()


def test_storage_manager_handles_missing_files_and_delete_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path)
    try:
        limits = _limits(pipeline_bytes=1, target_ratio=0.9)
        missing = _store(manager, payload=b"missing", limits=limits)
        (tmp_path / "files" / missing.rel_path).unlink()

        cleanup = manager.cleanup_pipeline("pipe", limits=limits)
        assert missing.rel_path in cleanup.deleted_rel_paths

        locked = _store(manager, payload=b"locked", limits=limits)
        locked_path = tmp_path / "files" / locked.rel_path
        original_unlink = Path.unlink
        fail_locked_delete = True

        def unlink_once(path: Path, *args, **kwargs):  # noqa: ANN001
            nonlocal fail_locked_delete
            if fail_locked_delete and path == locked_path:
                fail_locked_delete = False
                raise PermissionError("locked")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink_once)
        pending = manager.cleanup_pipeline("pipe", limits=limits)
        assert locked.rel_path in pending.delete_pending_rel_paths
        assert locked_path.exists()

        retried = manager.cleanup_pipeline("pipe", limits=limits)
        assert locked.rel_path in retried.deleted_rel_paths
        assert not locked_path.exists()
    finally:
        manager.close()
