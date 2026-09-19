from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from toposync.runtime.config_store import (
    Composition,
    CompositionElement,
    ConfigStore,
    UserDataPaths,
)


def test_element_patch_is_atomic_and_preserves_other_compositions(tmp_path: Path) -> None:
    async def exercise() -> None:
        paths = UserDataPaths(tmp_path, tmp_path / "config.json", tmp_path / "files")
        store = ConfigStore(paths=paths)
        config = await store.get_config()
        target = Composition(
            id="target",
            name="Target",
            elements=[CompositionElement(id="camera", type="camera", props={"label": "Original"})],
        )
        await store.save_config(
            config.model_copy(update={"compositions": [*config.compositions, target]})
        )

        await asyncio.gather(
            store.patch_element_props(
                composition_id="target", element_id="camera", changes={"label": "Edited"}
            ),
            store.patch_element_props(
                composition_id="target",
                element_id="camera",
                changes={"mapping": "first"},
                expected={"mapping": None},
            ),
        )
        with pytest.raises(ValueError, match="element_properties_changed"):
            await store.patch_element_props(
                composition_id="target",
                element_id="camera",
                changes={"mapping": "stale"},
                expected={"mapping": None},
            )

        reloaded = await ConfigStore(paths=paths).get_config()
        assert reloaded.active_composition_id == config.active_composition_id
        assert reloaded.compositions[0] == config.compositions[0]
        assert reloaded.compositions[1].elements[0].props == {"label": "Edited", "mapping": "first"}

    asyncio.run(exercise())
