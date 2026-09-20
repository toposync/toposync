from __future__ import annotations

import pytest

from toposync_ext_vision.plugin import VisionExtension
from test_extension_manager_compatibility import _FakeEntryPoint, _load_manager


@pytest.mark.parametrize("core_version,accepted", [("0.8.0", False), ("0.9.0.dev0", True)])
def test_vision_checks_real_manifest_before_setup(monkeypatch, core_version, accepted):
    setup_calls = []

    class Probe(VisionExtension):
        async def setup(self, app, *, bus, services):
            setup_calls.append(True)

    manager = _load_manager(
        entry_points=[_FakeEntryPoint("vision", Probe)], monkeypatch=monkeypatch,
        core_version=core_version,
    )
    assert bool(setup_calls) is accepted
    assert bool(manager.public_extensions()) is accepted
