import asyncio
import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from toposync.runtime.notifications.runtime import NotificationsRuntime
from toposync_ext_ai.visual_history import VisualHistorySearch, read_evidence
from toposync_ext_vision.history_embeddings import image_tensor


def test_visual_preprocessing_preserves_rgb_and_bounds():
    output = io.BytesIO()
    Image.new("RGB", (400, 200), "red").save(output, format="PNG")
    value = image_tensor(output.getvalue())
    assert value.shape == (1, 3, 224, 224)
    assert value.dtype == np.float32
    assert value[0, 0, 0, 0] > 1 and value[0, 2, 0, 0] < -1
    with pytest.raises(ValueError):
        image_tensor(b"")
    extreme = io.BytesIO()
    Image.new("RGB", (10000, 1), "red").save(extreme, format="PNG")
    with pytest.raises(ValueError):
        image_tensor(extreme.getvalue())


def test_visual_history_cache_retention_and_fallback(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    files = tmp_path / "files"
    files.mkdir()
    evidence = files / "cat.png"
    evidence.write_bytes(b"fixture")
    record, _ = runtime.store.upsert(type="event", title="Gato", image_path="cat.png", dedupe_key="cat")
    calls = []

    class Services:
        available = True
        async def call(self, name, **kwargs):
            if name.endswith("_space"):
                return "clip-fixture-v1"
            if not self.available:
                raise ValueError("missing model")
            calls.append(list(kwargs["images"]))
            vector = [1.0] + [0.0] * 511
            return {"space": "clip-fixture-v1", "query": vector,
                    "images": {identifier: vector for identifier in kwargs["images"]}}

    services = Services()
    search = VisualHistorySearch(runtime, services, SimpleNamespace(paths=SimpleNamespace(files_dir=files)))
    def run():
        return asyncio.run(search.search(query="gato", created_after=0))
    assert run()["notifications"][0]["id"] == record.id
    assert run()["search"]["identity_confirmation"] is False
    assert calls == [[record.id], []]
    assert runtime.store._conn.execute("SELECT count(*) FROM ai_notification_embedding").fetchone()[0] == 1
    evidence.unlink()
    removed = run()
    assert removed["notifications"] == []
    assert removed["search"]["unavailable_images"] == 1
    assert runtime.store._conn.execute("SELECT count(*) FROM ai_notification_embedding").fetchone()[0] == 0
    evidence.write_bytes(b"changed image")
    assert run()["notifications"][0]["id"] == record.id
    runtime.store.upsert(type="event", title="Gato", image_path="cat.png", payload={"priority": "silent"}, dedupe_key="cat")
    assert run()["notifications"] == []
    assert runtime.store._conn.execute("SELECT count(*) FROM ai_notification_embedding").fetchone()[0] == 0
    runtime.store.upsert(type="event", title="Gato", image_path="cat.png", dedupe_key="cat")
    services.available = False
    fallback = run()
    assert fallback["search"]["mode"] == "literal"
    assert fallback["search"]["reason"] == "local_visual_embeddings_unavailable"
    assert fallback["search"]["visual_similarity"] is False


def test_visual_history_never_fetches_remote_or_escaped_evidence(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private")
    (files / "link.png").symlink_to(outside)
    for url in ("https://example.com/image.png", "/files/../outside.png", "/files/link.png", "/files//etc/passwd"):
        with pytest.raises(ValueError):
            read_evidence(files, {"imageUrl": url})


def test_rejected_images_do_not_starve_later_batches_and_changed_bytes_retry(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    files = tmp_path / "files"
    files.mkdir()
    records = []
    for index in range(9):
        (files / str(index)).write_bytes(b"invalid")
        record, _ = runtime.store.upsert(type="event", title="Record", image_path=str(index))
        records.append(record.id)
    batches = []

    class Services:
        async def call(self, name, **kwargs):
            if name.endswith("_space"):
                return "clip-fixture-v1"
            batches.append(list(kwargs["images"]))
            return {"space": "clip-fixture-v1", "query": [1.0] + [0.0] * 511,
                    "images": {}, "unavailable": list(kwargs["images"])}

    search = VisualHistorySearch(runtime, Services(), SimpleNamespace(paths=SimpleNamespace(files_dir=files)))
    def run():
        return asyncio.run(search.search(query="gato", created_after=0))
    first = run()
    assert first["search"]["pending"] == 1
    assert first["search"]["unavailable_images"] == 8
    second = run()
    assert second["search"]["pending"] == 0
    assert second["search"]["unavailable_images"] == 9
    run()
    assert list(map(len, batches)) == [8, 1, 0]
    (files / "0").write_bytes(b"replaced image")
    run()
    assert batches[-1] == [records[0]]


def test_visual_filters_scope_corpus_reread_and_literal_fallback(tmp_path):
    runtime = NotificationsRuntime(data_dir=tmp_path)
    files = tmp_path / "files"
    files.mkdir()
    (files / "cat.png").write_bytes(b"fixture")
    cat, _ = runtime.store.upsert(type="event", title="Gato", image_path="cat.png",
        payload={"data": {"camera_id": "patio", "area_id": "water"}}, dedupe_key="cat")
    for index in range(260):
        runtime.store.upsert(type="event", title="Gato", image_path="cat.png",
            payload={"data": {"camera_id": "garage", "area_id": "water"}})

    class Services:
        mode = "available"
        async def call(self, name, **kwargs):
            if name.endswith("_space"):
                return "clip-filter-fixture"
            assert set(kwargs["images"]).issubset({cat.id})
            if self.mode == "unavailable":
                raise ValueError("unavailable")
            if self.mode == "changed":
                runtime.store.upsert(type="event", title="Gato", image_path="cat.png",
                    payload={"data": {"camera_id": "other", "area_id": "water"}}, dedupe_key="cat")
            vector = [1.0] + [0.0] * 511
            return {"space": "clip-filter-fixture", "query": vector,
                "images": {identifier: vector for identifier in kwargs["images"]}}

    services = Services()
    search = VisualHistorySearch(runtime, services, SimpleNamespace(paths=SimpleNamespace(files_dir=files)))
    def run():
        return asyncio.run(search.search(query="gato", created_after=0, data_filter={"camera_id": "patio", "area_id": "water"}))
    result = run()
    assert [item["id"] for item in result["notifications"]] == [cat.id]
    assert result["search"]["corpus_truncated"] is False
    services.mode = "unavailable"
    result = run()
    assert result["search"]["status"] == "degraded"
    assert [item["id"] for item in result["notifications"]] == [cat.id]
    services.mode = "changed"
    assert run()["notifications"] == []


def test_scan_budget_advances_without_stale_or_missing_evidence(tmp_path, monkeypatch):
    import os
    from toposync_ext_ai import visual_history

    monkeypatch.setattr(visual_history, "SCAN_BYTES", 4)
    runtime = NotificationsRuntime(data_dir=tmp_path)
    files = tmp_path / "files"
    files.mkdir()
    records = []
    for index in range(4):
        (files / str(index)).write_bytes(b"data")
        record, _ = runtime.store.upsert(type="event", title="Gato", image_path=str(index))
        records.append(record)
    batches = []
    during_inference = None

    class Services:
        async def call(self, name, **kwargs):
            if name.endswith("_space"):
                return "clip-budget-fixture"
            batches.append(dict(kwargs["images"]))
            if during_inference:
                during_inference()
            vector = [1.0] + [0.0] * 511
            return {"space": "clip-budget-fixture", "query": vector,
                    "images": {key: vector for key in kwargs["images"]}}

    search = VisualHistorySearch(runtime, Services(), SimpleNamespace(paths=SimpleNamespace(files_dir=files)))

    def run():
        return asyncio.run(search.search(query="gato", created_after=0))

    first = run()
    assert first["search"]["indexed"] == 1
    assert first["search"]["pending"] == 3
    assert first["search"]["unavailable_images"] == 0
    run()
    complete = run()
    assert complete["search"]["indexed"] == 4
    assert complete["search"]["pending"] == 0
    assert complete["search"]["status"] == "ready"
    assert sum(len(batch) for batch in batches) == 4
    search = VisualHistorySearch(runtime, Services(), SimpleNamespace(paths=SimpleNamespace(files_dir=files)))
    for _ in range(3):
        resumed = run()
    assert resumed["search"]["indexed"] == 4
    assert sum(len(batch) for batch in batches) == 4
    # Same size and restored mtime must still invalidate via ctime.
    path = files / "0"
    before = path.stat()
    path.write_bytes(b"edit")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    run()
    assert batches[-1] == {records[0].id: b"edit"}
    path.unlink()
    removed = run()
    assert removed["search"]["unavailable_images"] == 1
    assert records[0].id not in {item["id"] for item in removed["notifications"]}
    assert records[0].id not in search.fingerprints
    def replace_during_inference():
        (files / "1").write_bytes(b"race")

    during_inference = replace_during_inference
    changed = run()
    assert records[1].id not in {item["id"] for item in changed["notifications"]}
    assert changed["search"]["pending"] == 1
    during_inference = None
    assert run()["search"]["indexed"] == 3
    assert batches[-1] == {records[1].id: b"race"}
