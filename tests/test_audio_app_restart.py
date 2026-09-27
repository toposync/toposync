"""Crash/restart the real app during a synthetic audio occurrence."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

from toposync_ext_cameras.audio import decode_wav_pcm


def test_app_restart_preserves_audio_evidence_and_separates_occurrences(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ, "TOPOSYNC_DATA_DIR": str(tmp_path / "data"),
        "TOPOSYNC_AUTH_MODE": "bypass", "TOPOSYNC_NO_FRONTEND": "1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    }
    process = None
    with (tmp_path / "app.log").open("w") as log, httpx.Client(
        base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2
    ) as client:
        def start():
            return subprocess.Popen(
                [sys.executable, str(root / "tests/fixtures/audio_app_probe.py"), str(port)],
                cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )

        def wait_records(count):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "app.log").read_text()
                try:
                    response = client.get("/api/notifications")
                    if response.status_code == 200:
                        records = response.json()["notifications"]
                        if len(records) == count:
                            return records
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            raise AssertionError("audio occurrence did not reach expected count")

        def clip_id(record):
            return record["payload"]["data"]["historical_evidence"]["audio_occurrence"]["clip"]["id"]

        try:
            process = start()
            first = wait_records(1)[0]
            assert first["payload"]["status"] == "open"
            first_clip = clip_id(first)
            clip_path = f"/api/cameras/audio/clips/{first_clip}"
            original_wav = client.get(clip_path)
            assert original_wav.status_code == 200
            old_pid = process.pid
            process.kill()
            process.wait(timeout=5)
            process = start()
            records = wait_records(2)
            old = next(record for record in records if record["id"] == first["id"])
            new = next(record for record in records if record["id"] != first["id"])
            assert old["payload"]["status"] == "closed"
            assert old["payload"]["reason"] == "runtime_restart"
            assert old["payload"]["event"] == first["payload"]["event"]
            assert old["payload"]["data"] == first["payload"]["data"]
            assert new["payload"]["status"] == "open"
            assert clip_id(new) != first_clip
            restored_wav = client.get(clip_path)
            assert restored_wav.status_code == 200
            assert restored_wav.content == original_wav.content
            samples, rate = decode_wav_pcm(restored_wav.content)
            assert rate == 16000 and len(samples) == 16000
            (tmp_path / "result.json").write_text(json.dumps({
                "old_pid": old_pid, "new_pid": process.pid,
                "notifications": len(records), "old_closed_reason": old["payload"]["reason"],
                "original_interval_preserved": True, "private_wav_preserved": True,
                "new_occurrence_independent": True, "physical_audio_evaluated": False,
            }, indent=2))
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
