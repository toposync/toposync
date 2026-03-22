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

