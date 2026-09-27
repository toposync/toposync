import asyncio
import hashlib

import pytest

from toposync_ext_vision.registry.installer import VisionModelInstallManager
from toposync_ext_vision.registry.manifests import ModelManifest, ModelRegistry, ModelAuxiliaryArtifact, build_default_model_registry
from toposync_ext_vision.history_embeddings import MODEL_FILES


def test_visual_manifest_matches_runtime_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    manifest = build_default_model_registry().get_manifest("clip_multilingual_history")
    root = tmp_path / "vision-models"
    files = {str(manifest.resolve_artifact_path().relative_to(root)): manifest.sha256}
    files.update({str(path.relative_to(root)): asset.sha256 for path, asset in manifest.resolve_auxiliary_artifacts(tmp_path)})
    assert files == MODEL_FILES
    assert manifest.supports_capability("history.search")
    assert not manifest.supports_capability("identity.person")


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../../escape", "C:\\escape"])
def test_auxiliary_artifact_path_is_relative(path):
    with pytest.raises(ValueError):
        ModelAuxiliaryArtifact(path=path, source_url="https://example.test/asset", sha256="a" * 64)


def test_artifact_set_resumes_after_failure_and_catalog_requires_every_hash(tmp_path):
    primary = tmp_path / "primary.onnx"
    primary.write_bytes(b"primary fixture")
    first, second = tmp_path / "first-source", tmp_path / "second-source"
    first.write_bytes(b"first fixture")
    second.write_bytes(b"incorrect")
    def digest(data):
        return hashlib.sha256(data).hexdigest()
    manifest = ModelManifest(model_id="test.bundle", display_name="Bundle", task="embedding",
        runtime="onnxruntime", artifact_format="onnx", artifact_path=str(primary), sha256=digest(primary.read_bytes()),
        acquisition={"mode": "auto_download", "source_url": str(primary), "auxiliary_artifacts": [
            {"path": "bundle/first.json", "source_url": str(first), "sha256": digest(first.read_bytes())},
            {"path": "bundle/second.bin", "source_url": str(second), "sha256": digest(b"second fixture")},
        ]})

    async def scenario():
        manager = VisionModelInstallManager(data_dir=tmp_path)
        registry = ModelRegistry([manifest])
        before = primary.stat().st_mtime_ns
        assert manager.acquisition_info(manifest)["artifact_set_complete"] is False
        primary.write_bytes(b"corrupt and other files missing")
        assert manager.acquisition_info(manifest)["artifact_set_reason"] == "artifact_checksum_mismatch"
        primary.write_bytes(b"primary fixture")
        assert manager.acquisition_info(manifest)["artifact_set_reason"] == "artifact_missing"
        before = primary.stat().st_mtime_ns
        manager.start_install(model_id=manifest.model_id, model_registry=registry)
        await manager._tasks[manifest.model_id]
        failed = manager.get_job(manifest.model_id)
        assert failed["status"] == "failed" and failed["files_completed"] == 2
        assert primary.stat().st_mtime_ns == before
        installed = tmp_path / "vision-models/bundle/first.json"
        first_time = installed.stat().st_mtime_ns
        assert not list(tmp_path.rglob("*.part"))
        second.write_bytes(b"second fixture")
        progress = []
        update = manager._update_job
        def observe(identifier, **patch):
            update(identifier, **patch)
            progress.append(manager.get_job(identifier)["progress_pct"])
        manager._update_job = observe
        manager.start_install(model_id=manifest.model_id, model_registry=registry)
        await manager._tasks[manifest.model_id]
        ready = manager.get_job(manifest.model_id)
        assert ready["status"] == "completed" and ready["files_completed"] == ready["files_total"] == 3
        assert installed.stat().st_mtime_ns == first_time
        assert progress == sorted(progress) and progress[-1] == 100
        assert manager.acquisition_info(manifest)["artifact_set_complete"] is True
        installed.write_bytes(b"corruption")
        result = manager.acquisition_info(manifest)
        assert result["artifact_set_complete"] is False and result["artifact_set_reason"] == "artifact_checksum_mismatch"
        installed.unlink()
        installed.symlink_to(first)
        assert manager.acquisition_info(manifest)["artifact_set_complete"] is False

    asyncio.run(scenario())


def test_history_preparation_exposes_only_compact_install_state(tmp_path, monkeypatch):
    from toposync_ext_vision import history_embeddings, registry
    manifest = build_default_model_registry().get_manifest("clip_multilingual_history")
    class Manager:
        def acquisition_info(self, value):
            assert value.model_id == manifest.model_id
            return {"artifact_set_complete": False, "acquisition_supported": True, "artifact_set_reason": "artifact_missing"}
        def get_job(self, identifier):
            assert identifier == manifest.model_id
            return {"job_id": "job", "status": "downloading", "progress_pct": 42, "artifact_path": "/private/path", "source_label": "private source", "error": "private error"}
    monkeypatch.setattr(registry, "get_default_model_install_manager", lambda **kwargs: Manager())
    state = history_embeddings.history_setup_status(data_dir=tmp_path)
    assert state == {"model_id": manifest.model_id, "server_id": "local", "ready": False,
                     "install_supported": True, "reason": "artifact_missing",
                     "job": {"job_id": "job", "status": "downloading", "progress_pct": 42}}
    assert history_embeddings.history_setup_status(data_dir=None)["install_supported"] is False
