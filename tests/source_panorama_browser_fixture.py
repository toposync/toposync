"""Isolated browser boundary fixture, never connected to a real camera.

The HTTP routes, job persistence, artifact publication and crop writes are real.
Only camera acquisition and optical reconstruction are deterministic test doubles.
This fixture cannot establish hardware accuracy or reconstruction quality.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import cv2
from fastapi import HTTPException
import numpy as np
from pydantic import BaseModel
import uvicorn

CAMERA_ID = "source-panorama-synthetic-camera"
SOURCE_ID = "synthetic-wide"
EXTENSION = "com.toposync.cameras"


def fixture_config() -> dict:
    configuration = {
        "schema_version": 1,
        "active_composition_id": "source-panorama-synthetic-composition",
        "compositions": [
            {
                "id": "source-panorama-synthetic-composition",
                "name": "Synthetic panorama browser validation",
                "elements": [],
            }
        ],
        "settings": {
            "core": {},
            "extensions": {
                EXTENSION: {
                    "schema_version": 4,
                    "devices": [
                        {
                            "id": CAMERA_ID,
                            "name": "Synthetic courtyard",
                            "kind": "camera",
                            "enabled": True,
                            "control": {
                                "type": "onvif",
                                "automation_exclusive_control_confirmed": True,
                            },
                            "onvif": {"xaddr": "http://127.0.0.1:1/synthetic-only"},
                            "sources": [
                                {
                                    "id": SOURCE_ID,
                                    "name": "Synthetic wide stream",
                                    "enabled": True,
                                    "is_default": True,
                                    "kind": "video",
                                    "role": "main",
                                    "view_id": SOURCE_ID,
                                    "origin": {
                                        "type": "rtsp",
                                        "rtsp_url": "rtsp://127.0.0.1:1/synthetic-only",
                                        "has_ptz": True,
                                    },
                                    "ingest": {"mode": "direct"},
                                    "video": {"width": 640, "height": 320},
                                },
                                {
                                    "id": "synthetic-tele",
                                    "name": "Synthetic tele stream",
                                    "enabled": True,
                                    "is_default": False,
                                    "kind": "video",
                                    "role": "zoom",
                                    "view_id": "synthetic-tele",
                                    "origin": {
                                        "type": "rtsp",
                                        "rtsp_url": "rtsp://127.0.0.1:1/synthetic-tele-only",
                                        "has_ptz": True,
                                    },
                                    "ingest": {"mode": "direct"},
                                    "video": {"width": 640, "height": 320},
                                },
                            ],
                        }
                    ],
                }
            },
        },
        "pipelines": [],
    }
    other = copy.deepcopy(configuration["settings"]["extensions"][EXTENSION]["devices"][0])
    other["id"] = "source-panorama-other-camera"
    other["name"] = "Another synthetic camera"
    configuration["settings"]["extensions"][EXTENSION]["devices"].append(other)
    return configuration


def photograph(*, partial: bool = False) -> np.ndarray:
    """A conspicuously synthetic, seam-identifiable image for visual crop tests."""
    picture = np.zeros((320, 1280, 3), dtype=np.uint8)
    for index in range(8):
        left = index * 160
        picture[:, left : left + 160] = (40 + index * 18, 140 - index * 10, 70 + index * 20)
        cv2.putText(
            picture, str(index), (left + 62, 190), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3
        )
    cv2.putText(
        picture,
        "SYNTHETIC BROWSER FIXTURE - NO REAL CAMERA",
        (30, 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (255, 255, 255),
        2,
    )
    if partial:
        picture[:, 480:640] = 0
    return picture


class ScenarioBody(BaseModel):
    scenario: str = "ready"
    telemetry: bool = False
    quality: str | None = None


class Fixture:
    def __init__(self) -> None:
        self.scenario = "ready"
        self.telemetry = False
        self.quality: str | None = None
        self.released = asyncio.Event()
        self.events: list[dict] = []
        self.processing_events: list[dict] = []
        self.presets: dict[str, dict] = {}

    def camera_factory(self, **kwargs):
        fixture = self
        identifier = kwargs["job_id"]
        self.events.append(
            {
                "operation": "open_camera",
                "source_id": kwargs["source_id"],
                "camera_id": kwargs["camera_id"],
            }
        )

        class Camera:
            scenario = fixture.scenario
            telemetry = fixture.telemetry
            job_id = identifier
            source_identity = {
                "camera_id": kwargs["camera_id"],
                "source_id": kwargs["source_id"],
                "profile_token": "synthetic-profile",
                "width": 1280,
                "height": 320,
            }

            async def stop(self):
                fixture.events.append({"operation": "stop", "job_id": identifier})
                return {"stopped": True}

            async def discover(self):
                fixture.events.append({"operation": "discover", "job_id": identifier})
                return {"source_identity": self.source_identity}

            async def remove_return(self, destination):
                assert destination["owner_id"] == identifier
                assert destination["binding"] == self.source_identity
                token = destination["preset_token"]
                assert fixture.presets[token] == destination
                failed = self.scenario == "cleanup_failed"
                fixture.events.append({
                    "operation": "remove_return", "job_id": identifier,
                    "token": token, "outcome": "failed" if failed else "removed",
                })
                if failed:
                    raise RuntimeError("Synthetic preset removal failure")
                del fixture.presets[token]

            async def close(self):
                fixture.events.append({"operation": "release", "job_id": identifier})

        return Camera()

    async def scan(self, camera, output, *, checkpoint, progress, cancelled, return_only):
        self.events.append(
            {"operation": "return" if return_only else "scan", "resume": bool(checkpoint)}
        )
        if camera.telemetry:
            # Boundary fixture: deliberately supplies an invalid speed together
            # with absent media time. The real public service must remove it.
            await progress(
                {
                    "telemetry": {
                        "kind": "movement",
                        "outcome": "accepted",
                        "timing_basis": "local_observation",
                        "analysis_width": 960,
                        "command_accepted_seconds": 0.05,
                        "first_target_readback_seconds": 2,
                        "first_motion_transition_seconds": 0.125,
                        "stop_requested_seconds": 14.4,
                        "stop_accepted_seconds": 14.45,
                        "samples": [
                            {
                                "elapsed_seconds": index * 0.125,
                                "motion_pixels": max(0.02, float(6 * np.exp(-index / 18))),
                                "speed_px_s": 999,
                                "media_time": None,
                                "drift_pixels": max(0.01, float(2 * np.exp(-index / 18))),
                                "confidence": 0.93,
                                "state": "observing" if index < 80 else "stable",
                                "pose": {
                                    "pan": index / 256,
                                    "tilt": None,
                                    "native_pan": None,
                                    "native_tilt": None,
                                },
                            }
                            for index in range(128)
                        ],
                    }
                }
            )
        captures = copy.deepcopy((checkpoint or {}).get("captures", []))
        initial_path = output / "synthetic-initial.jpg"
        if not initial_path.is_file():
            assert cv2.imwrite(str(initial_path), photograph())
        pending_destination = None
        if camera.scenario == "cleanup_pending":
            pending_destination = {
                "kind": "preset", "role": "work", "owner_id": camera.job_id,
                "preset_token": f"synthetic-work-{camera.job_id}",
                "preset_name": f"Synthetic work reference {camera.job_id}",
                "binding": camera.source_identity,
            }
            self.presets[pending_destination["preset_token"]] = pending_destination

        def saved_checkpoint(*, stopped=False, relocalization_failed=False):
            reference = captures[0] if captures else None
            anchor = captures[-1] if captures else None
            cursor = {
                "version": 4,
                "stage": "return_reference" if relocalization_failed else "pan",
                "recovery_attempts": {"pan:0:1": 1} if relocalization_failed else {},
                "reference_path": reference["path"] if reference else None,
                "anchor": {
                    "capture_id": anchor["id"], "path": anchor["path"], "row": anchor["row_index"],
                } if anchor else None,
                # Deterministic boundary observations: Stop leaves this synthetic
                # camera at the last image; failed relocalization does not.
                "resume_anchor_verified": stopped,
                "relocalization_failed": relocalization_failed,
                "reference_destination": {
                    "kind": "absolute", "role": "work", "pan": 0.2, "tilt": 0.1,
                    "binding": camera.source_identity,
                    "capture_id": reference["id"], "path": reference["path"],
                } if reference else None,
            }
            return {
                "synthetic": True,
                "mode": "continuous",
                "active_seconds": len(captures) * 0.4,
                "source_identity": camera.source_identity,
                "continuous_cursor": cursor,
                "pending_returns": [pending_destination] if pending_destination else [],
                "captures": captures,
                "initial_path": initial_path.name,
                "return": {
                    "synthetic": True, "kind": "absolute", "role": "original",
                    "pan": 0, "tilt": 0, "binding": camera.source_identity,
                },
            }

        if return_only:
            await progress({"status": "returning", "physical_state": "restored"})
            return {"physical_state": "restored", "checkpoint": checkpoint, "captures": captures}
        if camera.scenario == "failed":
            raise HTTPException(
                409,
                {
                    "code": "synthetic_acquisition_failed",
                    "message": "Synthetic acquisition failure; no real device was used.",
                },
            )
        capture_limit = 1 if camera.scenario == "failed_after_one" else 3
        for index in range(len(captures), capture_limit):
            if cancelled():
                break
            path = output / f"synthetic-{index}.jpg"
            assert cv2.imwrite(str(path), photograph(partial=camera.scenario == "partial"))
            captures.append({
                "id": f"synthetic-{index}", "path": path.name, "synthetic": True,
                "row_index": 0, "quality": {"stable": True},
            })
            await progress(
                {
                    "status": "capturing",
                    "phase": "capturing",
                    "captures": captures,
                    "preview_path": path.name,
                    "checkpoint": saved_checkpoint(),
                }
            )
            await asyncio.sleep(0.4)
        if not cancelled() and camera.scenario in {"failed_after_one", "failed_after_three", "relocalization_failed", "cleanup_pending"}:
            relocalization_failed = camera.scenario in {"relocalization_failed", "cleanup_pending"}
            if relocalization_failed:
                self.events.append({"operation": "relocalization", "outcome": "unconfirmed"})
            await progress({
                "physical_state": "stopped",
                "checkpoint": saved_checkpoint(relocalization_failed=relocalization_failed),
            })
            raise HTTPException(409, {
                "code": "relocalization_required" if relocalization_failed else "frame_acquisition_timeout",
                "message": "Synthetic observation failed; no real device was used.",
            })
        if camera.scenario == "hold" and not cancelled():
            await progress({"status": "capturing", "phase": "waiting_for_stability"})
        while camera.scenario == "hold" and not self.released.is_set() and not cancelled():
            await asyncio.sleep(0.05)
        stopped = cancelled()
        if not stopped:
            self.events.append({"operation": "return"})
        await progress({"physical_state": "stopped" if stopped else "restored"})
        return {
            "physical_state": "stopped" if stopped else "restored",
            "captures": captures,
            "complete": camera.scenario != "partial",
            "coverage": {
                "synthetic": True,
                "progress": {"primary_complete": True, "bands_completed": 1},
            },
            "checkpoint": saved_checkpoint(stopped=stopped),
        }

    async def reconstruct(self, captures, output, *, cancelled, **_kwargs):
        event = {
            "operation": "reconstruct",
            "capture_count": len(captures),
            "capture_sha256": [
                sha256(Path(capture["path"]).read_bytes()).hexdigest() for capture in captures
            ],
            "outcome": "processing",
        }
        self.processing_events.append(event)
        await asyncio.sleep(0.3)
        if self.scenario == "reconstruction_failed":
            event["outcome"] = "failed"
            raise HTTPException(
                409,
                {
                    "code": "synthetic_reconstruction_failed",
                    "message": "Synthetic reconstruction failure; saved photographs remain available.",
                },
            )
        while self.scenario == "reconstruction_hold" and not self.released.is_set():
            if cancelled():
                event["outcome"] = "cancelled"
                raise asyncio.CancelledError
            await asyncio.sleep(0.05)
        image = cv2.imread(captures[0]["path"])
        partial = bool(np.all(image[100:200, 500:600] == 0))
        image = photograph(partial=partial)
        mask = np.full(image.shape[:2], 255, dtype=np.uint8)
        if partial:
            mask[:, 480:640] = 0
        assert cv2.imwrite(str(output / "panorama.png"), image)
        assert cv2.imwrite(str(output / "thumbnail.jpg"), cv2.resize(image, (640, 160)))
        assert cv2.imwrite(str(output / "coverage.png"), mask)
        model = {
            "synthetic": True,
            "not_hardware_validation": True,
            # This fixture declares a complete reachable-domain scan. Its
            # presentation orientation must therefore carry the same explicit
            # evidence required from the production reconstruction result.
            "presentation": {
                "status": "verified",
                "method": "synthetic_horizontal_axis",
            },
        }
        (output / "model.json").write_text(json.dumps(model))
        (output / "report.json").write_text(
            json.dumps({"synthetic": True, "not_hardware_validation": True})
        )
        malformed_quality = self.quality == "malformed_reasons"
        event["outcome"] = "partial" if self.quality and not malformed_quality else "ready"
        quality = {"status": "ready", "reasons": [], "synthetic": True}
        if self.quality:
            if self.quality == "malformed_reasons":
                quality.update(status="ready", reasons=())
            else:
                quality.update(
                    status="review",
                    reasons=[] if self.quality == "review" else [self.quality],
                )
        return {
            "status": "partial" if self.quality and not malformed_quality else "ready",
            "width": 1280,
            "height": 320,
            "coverage": {
                "pixel_ratio": 0.875 if partial else 1,
                "solid_angle_ratio": 0.875 if partial else 1,
                "provenance": "synthetic_coverage_v1",
                "synthetic": True,
            },
            "quality": quality,
            "model": model,
            "algorithm_version": "browser-boundary-fixture",
            "files": {
                "panorama": "panorama.png",
                "thumbnail": "thumbnail.jpg",
                "coverage": "coverage.png",
                "model": "model.json",
                "report": "report.json",
            },
        }


def make_app(data_dir: Path):
    sentinel = data_dir / ".source-panorama-browser-fixture"
    if data_dir.exists() and any(data_dir.iterdir()) and not sentinel.is_file():
        raise RuntimeError("Choose an empty dedicated source panorama fixture directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("Synthetic browser boundary tests only.\n")
    (data_dir / "config.json").write_text(json.dumps(fixture_config()))
    os.environ["TOPOSYNC_DATA_DIR"] = str(data_dir.resolve())
    os.environ["TOPOSYNC_AUTH_MODE"] = "bypass"
    from toposync.app import create_app

    app = create_app()
    original_lifespan = app.router.lifespan_context
    fixture = Fixture()

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            # Assert real plugin wiring. Only replace hardware/compute boundaries.
            service = application.state.camera_source_panorama
            # Keep existing legacy UI scenarios stable; new-region scenarios
            # supply their policy and six-view evidence separately.
            service.acquisition_policy = None
            service.camera_factory = fixture.camera_factory
            service.scan_runner = fixture.scan
            service.reconstruct_runner = fixture.reconstruct
            yield
            await service.shutdown()

    app.router.lifespan_context = lifespan

    @app.get("/api/__source_panorama_fixture")
    async def evidence():
        return {
            "synthetic": True,
            "scenario": fixture.scenario,
            "events": fixture.events,
            "processing_events": fixture.processing_events,
            "presets": list(fixture.presets),
        }

    @app.post("/api/__source_panorama_fixture/scenario")
    async def scenario(body: ScenarioBody):
        if body.scenario not in {
            "ready",
            "partial",
            "hold",
            "failed",
            "failed_after_one",
            "failed_after_three",
            "relocalization_failed",
            "cleanup_pending",
            "cleanup_failed",
            "reconstruction_failed",
            "reconstruction_hold",
        }:
            raise HTTPException(400, "Unknown synthetic scenario")
        if body.quality not in {
            None,
            "review",
            "independent_alignment_error",
            "optimizer_budget_reached",
            "malformed_reasons",
        }:
            raise HTTPException(400, "Unknown synthetic quality reason")
        fixture.scenario = body.scenario
        fixture.telemetry = body.telemetry
        fixture.quality = body.quality
        fixture.released.clear()
        return {"synthetic": True, "scenario": fixture.scenario}

    @app.post("/api/__source_panorama_fixture/release")
    async def release():
        fixture.released.set()
        return {"synthetic": True}

    @app.post("/api/__source_panorama_fixture/reset")
    async def reset():
        service = app.state.camera_source_panorama
        if service.tasks:
            raise HTTPException(409, "Wait for synthetic jobs to finish")
        for name in ("jobs", "artifacts"):
            directory = service.root / name
            if directory.exists():
                shutil.rmtree(directory)
        service.jobs.clear()
        service.cancelled.clear()
        fixture.__init__()
        await app.state.config_store.update_extension_settings(
            EXTENSION,
            lambda _current: fixture_config()["settings"]["extensions"][EXTENSION],
        )
        return {"synthetic": True, "reset": True}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8108)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(".toposync-data/source-panorama-browser-fixture")
    )
    arguments = parser.parse_args()
    uvicorn.run(
        make_app(arguments.data_dir), host="127.0.0.1", port=arguments.port, log_level="warning"
    )
