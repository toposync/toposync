"""Nested extension updates preserve concurrent edits and reject stale revisions."""
import asyncio
from pathlib import Path

import pytest

from toposync.runtime.config_store import ConfigStore, UserDataPaths
from toposync.runtime.config_store import AppSettings


def test_nested_extension_updates_are_atomic_and_isolated(tmp_path: Path) -> None:
    async def run() -> None:
        store = ConfigStore(paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        ))
        await store.patch_extension_settings("example", {"revision": 0, "items": {"kept": 1}})

        def modify(key: str):
            def update(current: dict) -> dict:
                current["items"][key] = True
                current["revision"] += 1
                return current
            return update

        first, second = await asyncio.gather(
            store.update_extension_settings("example", modify("first")),
            store.update_extension_settings("example", modify("second")),
        )
        second["items"].clear()
        settings = (await store.get_settings()).extensions["example"]
        assert settings == {"revision": 2, "items": {"kept": 1, "first": True, "second": True}}
        before = (tmp_path / "config.json").read_bytes()

        def stale(current: dict) -> dict:
            current["items"].clear()
            if current["revision"] != first["revision"]:
                raise ValueError("revision_changed")
            return current

        with pytest.raises(ValueError, match="revision_changed"):
            await store.update_extension_settings("example", stale)
        assert (tmp_path / "config.json").read_bytes() == before
        assert (await store.get_settings()).extensions["example"] == settings
        for invalid in ([], {"bad": float("nan")}):
            with pytest.raises(ValueError):
                await store.update_extension_settings("example", lambda _: invalid)
        reloaded = ConfigStore(paths=store.paths)
        assert (await reloaded.get_settings()).extensions["example"] == settings
    asyncio.run(run())


def test_client_patch_filters_protect_managed_values_under_the_lock(tmp_path: Path) -> None:
    async def run() -> None:
        store = ConfigStore(paths=UserDataPaths(
            data_dir=tmp_path, config_path=tmp_path / "config.json", files_dir=tmp_path / "files"
        ))
        await store.patch_extension_settings("example", {"managed": {"revision": 1}, "label": "old"})

        def protect(current: dict, proposed: dict) -> dict:
            proposed["managed"] = current["managed"]
            return proposed

        store.register_extension_settings_patch_filter("example", protect)
        await store.update_extension_settings("example", lambda current: {
            **current, "managed": {"revision": 2}
        })
        updated = await store.patch_extension_settings("example", {
            "managed": {"revision": 1}, "label": "new"
        })
        assert updated == {"managed": {"revision": 2}, "label": "new"}
        await store.replace_settings(AppSettings(extensions={"example": {
            "managed": {"revision": 0}, "label": "replacement"
        }}))
        assert (await store.get_settings()).extensions["example"] == {
            "managed": {"revision": 2}, "label": "replacement"
        }
        before = store.paths.config_path.read_bytes()

        def reject(current: dict, proposed: dict) -> dict:
            current.clear()
            raise ValueError("rejected_patch")

        store.register_extension_settings_patch_filter("example", reject)
        with pytest.raises(ValueError, match="rejected_patch"):
            await store.patch_extension_settings("example", {"label": "lost"})
        assert store.paths.config_path.read_bytes() == before
    asyncio.run(run())
