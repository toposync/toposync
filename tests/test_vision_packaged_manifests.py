from __future__ import annotations

import json
from pathlib import Path

import pytest

import toposync_ext_vision.registry.manifests as manifests_mod


def test_build_default_model_registry_reads_packaged_builtin_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "site-packages" / "toposync_ext_vision"
    registry_dir = package_root / "registry"
    manifests_dir = package_root / "manifests"
    models_dir = package_root / "models" / "rtmdet"
    registry_dir.mkdir(parents=True)
    manifests_dir.mkdir(parents=True)
    models_dir.mkdir(parents=True)

    manifest_path = manifests_dir / "packaged_det.json"
    model_path = models_dir / "packaged_det.onnx"
    model_path.write_bytes(b"onnx")
    manifest_path.write_text(
        json.dumps(
            {
                "model_id": "packaged.detector",
                "display_name": "Packaged Detector",
                "task": "detection",
                "runtime": "onnxruntime",
                "artifact_format": "onnx",
                "artifact_path": "../models/rtmdet/packaged_det.onnx",
            }
        ),
        encoding="utf-8",
    )
    fake_module_path = registry_dir / "manifests.py"
    fake_module_path.write_text("# test placeholder\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOPOSYNC_VISION_MANIFEST_PATHS", raising=False)
    monkeypatch.delenv("TOPOSYNC_VISION_MANIFESTS_DIR", raising=False)
    monkeypatch.delenv("TOPOSYNC_DATA_DIR", raising=False)
    monkeypatch.setattr(manifests_mod, "__file__", str(fake_module_path))

    registry = manifests_mod.build_default_model_registry()
    manifest = registry.get_manifest("packaged.detector")

    assert manifest is not None
    assert manifest.resolve_artifact_path() == model_path.resolve()
