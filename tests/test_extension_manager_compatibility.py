from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
import pytest

from toposync.extensions.manifest import ExtensionManifest
from toposync.extensions.manager import ExtensionManager
import toposync.extensions.manager as ext_manager_mod
from toposync.runtime.event_bus import EventBus
from toposync.runtime.services import ServiceRegistry


class _FakeEntryPoint:
    def __init__(self, name: str, plugin_type: type) -> None:
        self.name = name
        self.value = f"tests:{plugin_type.__name__}"
        self._plugin_type = plugin_type

    def load(self):
        return self._plugin_type


def _make_plugin(manifest_payload: dict[str, object], setup_calls: list[str]) -> type:
    class _Plugin:
        def manifest(self) -> ExtensionManifest:
            return ExtensionManifest.model_validate(manifest_payload)

        async def setup(self, app, *, bus, services) -> None:  # noqa: ANN001, ARG002
            setup_calls.append(str(manifest_payload["id"]))

    _Plugin.__name__ = f"Plugin_{str(manifest_payload['id']).split('.')[-1]}"
    return _Plugin


def _load_manager(
    *,
    entry_points: list[_FakeEntryPoint],
    monkeypatch: pytest.MonkeyPatch,
    core_version: str = "0.3.0",
    disabled_extension_ids: set[str] | None = None,
) -> ExtensionManager:
    monkeypatch.setattr(ext_manager_mod, "_iter_entry_points", lambda _group: entry_points)
    monkeypatch.setattr(ext_manager_mod, "_current_core_version", lambda: core_version)
    manager = ExtensionManager(
        group="toposync.extensions",
        disabled_extension_ids=disabled_extension_ids,
    )
    asyncio.run(manager.load(app=FastAPI(), bus=EventBus(), services=ServiceRegistry()))
    return manager


def test_extension_manager_skips_incompatible_core_version(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup_calls: list[str] = []
    incompatible_plugin = _make_plugin(
        {
            "id": "com.test.future",
            "name": "Future",
            "version": "0.1.0",
            "requires_core_version": ">=0.4",
        },
        setup_calls,
    )

    caplog.set_level(logging.WARNING, logger="toposync.extensions")
    manager = _load_manager(
        entry_points=[_FakeEntryPoint("future", incompatible_plugin)],
        monkeypatch=monkeypatch,
    )

    assert manager.public_extensions() == []
    assert setup_calls == []
    assert "requires Toposync core version" in caplog.text


def test_extension_manager_skips_when_required_extension_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup_calls: list[str] = []
    dependent_plugin = _make_plugin(
        {
            "id": "com.test.dependent",
            "name": "Dependent",
            "version": "0.1.0",
            "requires_extensions": ["com.test.base"],
        },
        setup_calls,
    )

    caplog.set_level(logging.WARNING, logger="toposync.extensions")
    manager = _load_manager(
        entry_points=[_FakeEntryPoint("dependent", dependent_plugin)],
        monkeypatch=monkeypatch,
    )

    assert manager.get("com.test.dependent") is None
    assert setup_calls == []
    assert "requires extension 'com.test.base'" in caplog.text


def test_extension_manager_skips_dependents_of_rejected_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[str] = []
    base_plugin = _make_plugin(
        {
            "id": "com.test.base",
            "name": "Base",
            "version": "0.1.0",
            "requires_core_version": ">=0.4",
        },
        setup_calls,
    )
    dependent_plugin = _make_plugin(
        {
            "id": "com.test.dependent",
            "name": "Dependent",
            "version": "0.1.0",
            "requires_extensions": ["com.test.base"],
        },
        setup_calls,
    )
    ok_plugin = _make_plugin(
        {
            "id": "com.test.ok",
            "name": "OK",
            "version": "0.1.0",
        },
        setup_calls,
    )

    manager = _load_manager(
        entry_points=[
            _FakeEntryPoint("base", base_plugin),
            _FakeEntryPoint("dependent", dependent_plugin),
            _FakeEntryPoint("ok", ok_plugin),
        ],
        monkeypatch=monkeypatch,
    )

    assert [item["id"] for item in manager.public_extensions()] == ["com.test.ok"]
    assert setup_calls == ["com.test.ok"]


def test_extension_manager_skips_dependents_of_disabled_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[str] = []
    base_plugin = _make_plugin(
        {
            "id": "com.test.base",
            "name": "Base",
            "version": "0.1.0",
        },
        setup_calls,
    )
    dependent_plugin = _make_plugin(
        {
            "id": "com.test.dependent",
            "name": "Dependent",
            "version": "0.1.0",
            "requires_extensions": ["com.test.base"],
        },
        setup_calls,
    )

    manager = _load_manager(
        entry_points=[
            _FakeEntryPoint("base", base_plugin),
            _FakeEntryPoint("dependent", dependent_plugin),
        ],
        monkeypatch=monkeypatch,
        disabled_extension_ids={"com.test.base"},
    )

    assert manager.public_extensions() == []
    assert setup_calls == []


def test_extension_manager_validates_required_extension_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[str] = []
    base_plugin = _make_plugin(
        {
            "id": "com.test.base",
            "name": "Base",
            "version": "0.1.0",
        },
        setup_calls,
    )
    dependent_plugin = _make_plugin(
        {
            "id": "com.test.dependent",
            "name": "Dependent",
            "version": "0.1.0",
            "requires_extensions": ["com.test.base>=0.2"],
        },
        setup_calls,
    )

    manager = _load_manager(
        entry_points=[
            _FakeEntryPoint("base", base_plugin),
            _FakeEntryPoint("dependent", dependent_plugin),
        ],
        monkeypatch=monkeypatch,
    )

    assert [item["id"] for item in manager.public_extensions()] == ["com.test.base"]
    assert setup_calls == ["com.test.base"]


def test_extension_manager_registers_manifest_api_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[str] = []
    plugin = _make_plugin(
        {
            "id": "com.test.api",
            "name": "API",
            "version": "0.1.0",
            "api_prefixes": ["/api/test-api"],
        },
        setup_calls,
    )

    manager = _load_manager(
        entry_points=[_FakeEntryPoint("api", plugin)],
        monkeypatch=monkeypatch,
    )

    assert setup_calls == ["com.test.api"]
    routes = manager.auth_routes()
    assert len(routes) == 1
    assert routes[0].extension_id == "com.test.api"
    assert routes[0].prefix == "/api/test-api"
    assert routes[0].action == "core:extension:use"


def test_extension_manager_reports_missing_frontend_remote_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[str] = []
    plugin = _make_plugin(
        {
            "id": "com.test.frontend",
            "name": "Frontend",
            "version": "0.1.0",
            "frontend": {
                "kind": "module-federation",
                "remote_entry": "remoteEntry.js",
                "scope": "test_frontend",
                "module": "./activate",
            },
        },
        setup_calls,
    )

    manager = _load_manager(
        entry_points=[_FakeEntryPoint("frontend", plugin)],
        monkeypatch=monkeypatch,
    )

    diagnostics = manager.public_diagnostics()
    assert any(
        item["extension_id"] == "com.test.frontend"
        and item["code"] == "frontend_static_missing"
        for item in diagnostics
    )


def test_extension_manager_reports_incompatible_extensions_in_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[str] = []
    plugin = _make_plugin(
        {
            "id": "com.test.future",
            "name": "Future",
            "version": "0.1.0",
            "requires_core_version": ">=0.4",
        },
        setup_calls,
    )

    manager = _load_manager(
        entry_points=[_FakeEntryPoint("future", plugin)],
        monkeypatch=monkeypatch,
    )

    diagnostics = manager.public_diagnostics()
    assert any(
        item["extension_id"] == "com.test.future"
        and item["code"] == "incompatible_extension"
        for item in diagnostics
    )
