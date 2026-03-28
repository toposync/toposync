from __future__ import annotations

import pytest

from toposync_ext_vision.registry import build_default_model_registry
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
