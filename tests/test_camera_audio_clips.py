from concurrent.futures import ThreadPoolExecutor
import sqlite3

import numpy as np
import pytest

from toposync_ext_cameras import audio_clips
from toposync_ext_cameras.audio import decode_wav_pcm


def test_private_audio_roundtrip_revocation_expiry_and_concurrent_budget(tmp_path, monkeypatch):
    store = audio_clips.AudioClipStore(tmp_path)
    samples = np.arange(16000, dtype=np.int16)
    def retain():
        return store.retain(camera_id="camera-one", samples=samples, sample_rate=16000,
                            clock={"started_at": 100, "basis": "synthetic"})
    clip = retain()
    authorized = []
    metadata, wav = store.read(clip["id"], authorize=authorized.append)
    assert authorized == ["camera-one"]
    decoded, rate = decode_wav_pcm(wav)
    assert rate == 16000 and np.array_equal(decoded[:, 0], samples)
    assert metadata["duration_seconds"] == 1
    assert not (tmp_path / "files").exists()
    assert store.path.stat().st_mode & 0o777 == 0o600
    def revoked(camera):
        assert camera == "camera-one"
        raise PermissionError("revoked")
    with pytest.raises(PermissionError):
        store.read(clip["id"], authorize=revoked)
    assert store.read(clip["id"], authorize=authorized.append) is not None
    store.delete(clip["id"], authorize=authorized.append)
    assert store.read(clip["id"], authorize=authorized.append) is None
    monkeypatch.setattr(audio_clips, "MAX_STORED_BYTES", 100_000)
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: retain(), range(12)))
    with sqlite3.connect(store.path) as connection:
        count, size = connection.execute("SELECT count(*),sum(length(wav)) FROM clip").fetchone()
        assert count == 3 and size <= 100_000
    monkeypatch.setattr(audio_clips, "MAX_CLIPS", 2)
    retain()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM clip").fetchone()[0] == 2
    now = audio_clips.time.time()
    monkeypatch.setattr(audio_clips.time, "time", lambda: now + audio_clips.RETENTION_SECONDS + 1)
    store.cleanup()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM clip").fetchone()[0] == 0
        # secure_delete removes the recording from free database pages too.
    assert wav[44:2044] not in store.path.read_bytes()


def test_private_audio_rejects_invalid_source_and_unbounded_pcm(tmp_path):
    store = audio_clips.AudioClipStore(tmp_path)
    for camera, samples, rate in [("*", np.zeros(1, dtype=np.int16), 8000),
                                   ("camera", np.zeros(8000 * 31, dtype=np.int16), 8000),
                                   ("camera", np.zeros(8000, dtype=np.float32), 8000)]:
        with pytest.raises(ValueError):
            store.retain(camera_id=camera, samples=samples, sample_rate=rate,
                         clock={"started_at": 0, "basis": "synthetic"})
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM clip").fetchone()[0] == 0


def test_audio_http_reauthorizes_source_and_never_exposes_public_file(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from toposync.app import create_app
    import toposync.extensions.manager as manager
    from toposync_ext_cameras.plugin import CamerasExtension

    class EntryPoint:
        name = "cameras"
        value = "toposync_ext_cameras.plugin:CamerasExtension"
        def load(self):
            return CamerasExtension

    monkeypatch.setenv("TOPOSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOSYNC_NO_FRONTEND", "1")
    monkeypatch.setenv("TOPOSYNC_AUTH_MODE", "enforced")
    monkeypatch.setattr(manager, "_iter_entry_points", lambda _: [EntryPoint()])
    store = audio_clips.AudioClipStore(tmp_path)
    clips = {camera: store.retain(camera_id=camera, samples=np.zeros(8000, dtype=np.int16), sample_rate=8000,
                                  clock={"started_at": 1, "basis": "synthetic"}) for camera in ["allowed", "denied"]}
    def path(camera):
        return f"/api/cameras/audio/clips/{clips[camera]['id']}"
    with TestClient(create_app()) as client:
        assert client.post("/api/auth/setup", json={"username": "owner", "display_name": "Owner",
                            "password": "password123", "device_label": "test"}).status_code == 200
        response = client.get(path("allowed"))
        assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        assert decode_wav_pcm(response.content)[1] == 8000
        user = client.post("/api/access/users", json={"username": "limited", "display_name": "Limited",
                           "role": "member", "password": "password123"}).json()
        for action, resource, include in [("core:extension:use", "core:extension", ["com.toposync.cameras"]),
                                          ("core:camera:read", "core:camera", ["allowed"])]:
            assert client.post(f"/api/access/users/{user['id']}/grants", json={"action": action,
                               "resource_type": resource, "include": include, "exclude": []}).status_code == 200
        assert client.post("/api/auth/login", json={"username": "limited", "password": "password123",
                           "device_label": "test"}).status_code == 200
        assert client.get(path("allowed")).status_code == 200
        assert client.get(path("denied")).status_code == 403
        assert client.delete(path("allowed")).status_code == 403
        assert client.post("/api/auth/login", json={"username": "owner", "password": "password123",
                           "device_label": "test"}).status_code == 200
        assert client.delete(path("allowed")).status_code == 204
        assert client.get(path("allowed")).status_code == 404
        client.cookies.clear()
        assert client.get(path("denied")).status_code == 401
