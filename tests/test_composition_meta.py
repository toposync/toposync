from __future__ import annotations

import asyncio

from pathlib import Path

from toposync.runtime.config_store import Composition, ConfigStore, UserDataPaths


def _create_store(tmp_path: Path) -> ConfigStore:
    data_dir = tmp_path / "data"
    paths = UserDataPaths(
        data_dir=data_dir,
        config_path=data_dir / "config.json",
        files_dir=data_dir / "files",
    )
    return ConfigStore(paths=paths)


def test_composition_meta_roundtrips_via_config_store(tmp_path: Path) -> None:
    store = _create_store(tmp_path)

    created = asyncio.run(store.create_composition(name="RoomPlan meta test", composition_id="c1"))
    updated = Composition(
        id=created.id,
        name=created.name,
        elements=[],
        meta={"source": "roomplan", "geo": {"latitude": -23.55, "longitude": -46.63}},
    )
    asyncio.run(store.set_active_composition(updated))

    reloaded_store = _create_store(tmp_path)
    loaded = asyncio.run(reloaded_store.get_active_composition())
    assert loaded.meta.get("source") == "roomplan"
    assert loaded.meta.get("geo") == {"latitude": -23.55, "longitude": -46.63}



def test_georeference_preserves_internal_coordinates_and_camera_calibration(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    asyncio.run(store.create_composition(name="Map", composition_id="map"))
    original = Composition.model_validate({
        "id": "map", "name": "Map",
        "elements": [{"id": "camera", "type": "camera", "position": {"x": 3, "y": 2, "z": -4},
                      "props": {"calibration": {"world_quad": [[0, 0], [1, 0], [1, 1], [0, 1]]}}}],
        "meta": {"source": "existing", "georeference": {
            "version": 1, "latitude": -23.55, "longitude": -46.63,
            "rotationDegrees": 37, "scale": 1.2, "axes": "x-east-z-south", "units": "metres",
        }},
    })
    asyncio.run(store.set_active_composition(original))
    loaded = asyncio.run(_create_store(tmp_path).get_active_composition())
    assert loaded.model_dump() == original.model_dump()


def test_metadata_patch_preserves_active_composition_and_concurrent_edits(tmp_path: Path) -> None:
    store = _create_store(tmp_path)
    original = asyncio.run(store.create_composition(name="First", composition_id="first"))
    original.meta = {"unrelated": "retained"}
    original.elements = Composition.model_validate({"id": "first", "name": "First", "elements": [{"id": "new", "type": "wall", "position": {"x": 12}}]}).elements
    asyncio.run(store.set_active_composition(original))
    asyncio.run(store.create_composition(name="Other tab", composition_id="second"))
    asyncio.run(store.activate_composition("second"))
    updated = asyncio.run(store.patch_composition_meta("first", changes={"georeference": {"scale": 2}}, expected={"georeference": None}))
    assert updated.elements == original.elements
    assert updated.meta == {"unrelated": "retained", "georeference": {"scale": 2}}
    assert asyncio.run(store.get_active_composition()).id == "second"
    import pytest
    with pytest.raises(ValueError, match="composition_metadata_changed"):
        asyncio.run(store.patch_composition_meta("first", changes={"georeference": {}}, expected={"georeference": None}))


def test_metadata_api_requires_expected_values_and_preserves_active(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from toposync.app import create_app
    import toposync.extensions.manager as extensions

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path / "api"))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "bypass")
    monkeypatch.setattr(extensions, "_iter_entry_points", lambda _: [])
    with TestClient(create_app()) as client:
        first = client.post("/api/compositions", json={"name": "First"}).json()
        second = client.post("/api/compositions", json={"name": "Second"}).json()
        client.post(f"/api/compositions/{second['id']}/activate")
        route = f"/api/compositions/{first['id']}/meta"
        assert client.patch(route, json={"changes": {"georeference": {}}, "expected": {}}).status_code == 400
        assert client.patch(route, json={"changes": {"georeference": {"scale": 1}}, "expected": {"georeference": None}}).status_code == 200
        assert client.patch(route, json={"changes": {"georeference": {}}, "expected": {"georeference": None}}).status_code == 409
        assert client.get("/api/composition").json()["id"] == second["id"]
        assert client.patch("/api/compositions/missing/meta", json={"changes": {}, "expected": {}}).status_code == 404
