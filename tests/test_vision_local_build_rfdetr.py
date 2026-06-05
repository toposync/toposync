from __future__ import annotations

from pathlib import Path

import pytest

from toposync_ext_vision.registry import build_default_model_registry
import toposync_ext_vision.registry.local_build as local_build_mod
from toposync_ext_vision.registry.local_build import _rfdetr_export_shape_args


def test_rfdetr_export_shape_args_omit_override_for_official_resolution() -> None:
    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("rfdetr_det_medium")

    assert _rfdetr_export_shape_args(manifest) == []


def test_rfdetr_export_shape_args_include_override_for_custom_square_resolution() -> None:
    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("rfdetr_det_medium")
    overridden = manifest.model_copy(
        update={
            "input": manifest.input.model_copy(
                update={
                    "width": 560,
                    "height": 560,
                }
            )
        }
    )

    assert _rfdetr_export_shape_args(overridden) == ["--height", "560", "--width", "560"]


def test_rfdetr_export_shape_args_reject_non_square_resolution() -> None:
    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("rfdetr_det_medium")
    overridden = manifest.model_copy(
        update={
            "input": manifest.input.model_copy(
                update={
                    "width": 640,
                    "height": 576,
                }
            )
        }
    )

    with pytest.raises(RuntimeError, match="square input sizes"):
        _rfdetr_export_shape_args(overridden)


def test_rfdetr_local_builder_probe_accepts_available_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("rfdetr_det_nano")

    def _which(candidate: str) -> str:
        return f"/usr/bin/{candidate}"

    monkeypatch.setattr(local_build_mod.shutil, "which", _which)

    probe = local_build_mod.probe_local_builder(manifest, data_dir=tmp_path)

    assert probe["supported"] is True
    assert probe["reason"] == "ok"
    assert probe["missing_tools"] == []


def test_rfdetr_local_builder_probe_blocks_when_cmake_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("rfdetr_det_nano")

    def _which(candidate: str) -> str | None:
        if candidate == "cmake":
            return None
        return f"/usr/bin/{candidate}"

    monkeypatch.setattr(local_build_mod.shutil, "which", _which)

    probe = local_build_mod.probe_local_builder(manifest, data_dir=tmp_path)

    assert probe["supported"] is False
    assert probe["reason"] == "rfdetr_build_tool_missing"
    assert probe["missing_tools"] == ["cmake"]


def test_rfdetr_local_builder_probe_blocks_when_compilers_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = build_default_model_registry()
    manifest = registry.resolve_detector_manifest("rfdetr_det_nano")
    available_tools = {"cmake"}

    def _which(candidate: str) -> str | None:
        if candidate in available_tools:
            return f"/usr/bin/{candidate}"
        return None

    monkeypatch.setattr(local_build_mod.shutil, "which", _which)

    probe = local_build_mod.probe_local_builder(manifest, data_dir=tmp_path)

    assert probe["supported"] is False
    assert probe["reason"] == "rfdetr_build_tool_missing"
    assert probe["missing_tools"] == ["c_compiler", "cxx_compiler"]
