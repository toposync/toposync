"""Real loopback processing/SSE/origin replay, with synthetic PCM and no hardware."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
from pydantic import BaseModel

from toposync.runtime.config_store import Pipeline, ProcessingServer
from toposync.runtime.notifications import NotificationsRuntime
from toposync.runtime.pipelines.distributed.orchestrator import PipelinesOrchestrator
from toposync.runtime.pipelines.distributed.transport import HttpProcessingTransport
from toposync_ext_cameras.audio import decode_wav_pcm
from toposync_ext_cameras.audio_clips import AudioClipStore
from toposync_ext_cameras.audio_events import build_audio_recipe
from toposync_ext_cameras.audio_pipeline import register_audio_operator
from test_private_stream_continuity import dependencies


class Config(BaseModel):
    pass


def test_private_audio_crosses_real_http_and_is_retained_only_at_origin(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {**os.environ, "TOPOSYNC_DATA_DIR": str(tmp_path / "processing"),
                   "TOPOSYNC_PROCESSING_USERNAME": "isolated-test",
                   "TOPOSYNC_PROCESSING_PASSWORD": "disposable-audio-contract",
                   "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    with (tmp_path / "processing.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(root / "tests/fixtures/audio_processing_probe.py"), str(port)],
                                   cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False, timeout=1) as client:
                deadline = time.monotonic() + 20
                while True:
                    assert process.poll() is None, "processing process exited before readiness"
                    try:
                        if client.get(url + "/api/processing/status").status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "processing readiness timed out"
                    time.sleep(0.05)

            async def scenario():
                shared = dependencies(tmp_path / "origin")
                registry = shared["operator_registry"]
                register_audio_operator(registry)
                registry.register_operator(operator_id="test.audio_probe", config_model=Config,
                                           inputs=[], outputs=[{"name": "out"}], capabilities=["source"],
                                           defaults={}, share_strategy="never", owner="test")
                notifications = NotificationsRuntime(data_dir=tmp_path / "origin")
                orchestrator = PipelinesOrchestrator(**shared, notifications=notifications, files_dir=tmp_path / "origin/files")
                transport = HttpProcessingTransport(base_url=url, username="isolated-test", password="disposable-audio-contract")
                graph = build_audio_recipe(graph_uid="audio_http", camera_id="synthetic-camera", source_id="synthetic",
                                           baseline={"reference": "synthetic quiet", "rms": 0.01}, retain_audio=True)
                graph["nodes"][0].update(operator="test.audio_probe", config={})
                graph["nodes"][-1]["config"]["historical_evidence_paths"].append("processing_pid")
                pipeline = Pipeline(name="audio_http", graph=graph, processing_server_id="probe")
                server = ProcessingServer(id="probe", name="Probe", kind="http", url=url,
                                          username="isolated-test", password="disposable-audio-contract")
                try:
                    await orchestrator._start_origin_pipeline_for_remote(pipeline)
                    await orchestrator._start_remote_server(server, [pipeline], settings_payload={})
                    async with asyncio.timeout(15):
                        while True:
                            records, _ = notifications.store.list()
                            if records and records[0].payload.get("status") == "closed":
                                break
                            await asyncio.sleep(0.05)
                    assert len(records) == 1
                    evidence = records[0].payload["data"]["historical_evidence"]
                    assert evidence["processing_pid"] == process.pid != os.getpid()
                    clip = evidence["audio_occurrence"]["clip"]
                    assert clip["status"] == "retained"
                    cameras = []
                    _, wav = AudioClipStore(tmp_path / "origin").read(clip["id"], authorize=cameras.append)
                    samples, rate = decode_wav_pcm(wav)
                    assert rate == 16000 and len(samples) == 16000 and cameras == ["synthetic-camera"]
                    assert not list((tmp_path / "origin/files").rglob("*.wav"))
                    assert AudioClipStore(tmp_path / "processing").read(clip["id"], authorize=cameras.append) is None
                    await orchestrator.stop()
                    async with asyncio.timeout(5):
                        while (await transport.status())["private_stream"]["connections"]:
                            await asyncio.sleep(0.05)
                    (tmp_path / "result.json").write_text(json.dumps({"notifications": 1, "closed": True,
                        "sample_rate": rate, "samples": len(samples), "origin_pid": os.getpid(),
                        "processing_pid": process.pid, "private_clip_at_origin_only": True,
                        "connections_after_stop": 0, "physical_audio_evaluated": False}, indent=2) + "\n")
                finally:
                    await orchestrator.stop()
                    await transport.close()
            asyncio.run(scenario())
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
