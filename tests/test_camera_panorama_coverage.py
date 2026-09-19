"""Synthetic scene/actuator inputs; real coverage policy and persistence.

These alternate trajectories are NOT replay or physical-camera evidence.
"""
import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from toposync_ext_cameras import panorama_scan as scan
from toposync_ext_cameras.panorama_capture import PanoramaCaptureError
from toposync_ext_cameras.panorama_region import COVERAGE_REGION_POLICY, acquire_region, region_policy
from toposync_ext_cameras.panorama_coverage import decision, required_captures


def environment(tmp_path, monkeypatch, *, mirrored=False, rejection=None, recovery=True, uncertain=None, small_steps=False, recall_offset=(0.0, 0.0)):
    positions = {0: (0.0, 0.0)}
    moves, recalls = [], []
    rejected = set()
    scanner = scan._Scan(SimpleNamespace(), tmp_path, lambda event: asyncio.sleep(0),
                         lambda: False, None, COVERAGE_REGION_POLICY)
    scanner.capabilities = {"axes": {"pan": True, "tilt": True}, "velocity_supported": True,
                            "source_identity": {"camera_id": "synthetic", "source_id": "wide"}}
    scanner.saved_return = {"kind": "absolute", "pan": 0.0, "tilt": 0.0}
    scanner.last_pose = {"pan": 0.0, "tilt": 0.0, "zoom": None}
    scanner.physical_state = "stopped"
    scanner.acquired = True

    def frame(index):
        return {"image": np.full((100, 160, 3), index, np.uint8), "capture_instance": "synthetic",
                "sequence": index + 1, "generation": 1, "received_monotonic": time.monotonic(),
                "published_at": time.time(), "physical_timestamp_verified": False, "media_time": None}

    scanner.last_frame = frame(0)
    scan._write_image(tmp_path / "initial-reference.png", scanner.last_frame["image"])
    scanner.checkpoint.update(initial_path=str(tmp_path / "initial-reference.png"),
                              initial_reference_evidence={"evidence": "local_observation_only", "provisional": True})

    async def observe(**kwargs):
        scanner.last_frame = frame(len(positions) - 1)
        return scanner.last_frame

    def result(index):
        scanner.last_frame = frame(index)
        scanner.physical_state = "stopped"
        return {"frame": scanner.last_frame, "stable": True, "stationary": False,
                "pose": {"pan": positions[index][0], "tilt": positions[index][1], "zoom": None},
                "evidence": {"stable": True, "evidence": "visual_transition_verified", "best_sequence": index + 1}}

    async def pulse(axis, direction, duration, **kwargs):
        scanner._check()
        commands = scanner.checkpoint.setdefault("region_commands", [])
        if len(commands) >= COVERAGE_REGION_POLICY["maximum_commands"] - COVERAGE_REGION_POLICY["return_commands_reserved"]:
            raise PanoramaCaptureError("region_budget_exhausted")
        commands.append({"id": f"synthetic-{len(commands)}", "state": "uncertain"})
        moves.append((axis, direction, duration))
        scanner.checkpoint["continuous_cursor"]["transition"] = {"state": "pending"}
        if uncertain and (len(moves) == 2):
            scanner.physical_state = "unknown"
            scanner._stop_failed = uncertain == "stop_unconfirmed"
            raise PanoramaCaptureError(uncertain)
        x, y = positions[len(positions) - 1]
        amount = .025 if small_steps else duration
        if axis == "pan":
            x -= direction * amount * (-1 if mirrored else 1)
        else:
            y += direction * amount
        index = len(positions)
        positions[index] = (x, y)
        if rejection and axis == "pan" and (1 if x > 0 else -1) == rejection:
            rejected.add(index)
        commands[-1].update(state="observed", outcome="capture_stable")
        return result(index)

    async def return_to(destination):
        recalls.append(copy.deepcopy(destination))
        return {"accepted": True}

    async def move(command, **kwargs):
        scanner._check()
        commands = scanner.checkpoint.setdefault("region_commands", [])
        if len(commands) >= COVERAGE_REGION_POLICY["maximum_commands"] - COVERAGE_REGION_POLICY["return_commands_reserved"]:
            raise PanoramaCaptureError("region_budget_exhausted")
        commands.append({"id": f"synthetic-{len(commands)}", "state": "uncertain"})
        await command()
        index = len(positions)
        positions[index] = recall_offset if recovery else (0.55, 0.55)
        commands[-1].update(state="observed", outcome="capture_stable")
        scanner.checkpoint.setdefault("diagnostics", {}).setdefault("attempts", []).append({
            "kind": "movement", "command_receipt": {"command_id": f"recall-{len(recalls)}"},
            "replay_id": f"replay-recall-{len(recalls)}",
        })
        return result(index)

    def match(first, second):
        a, b = int(first[0, 0, 0]), int(second[0, 0, 0])
        if b in rejected and a != b:
            return {"verified": False, "code": "correspondences_not_distributed", "model_candidates": [{"inliers": 63}]}
        x, y = (positions[b][i] - positions[a][i] for i in (0, 1))
        overlap = max(0, 1 - abs(x)) * max(0, 1 - abs(y))
        return {"verified": overlap >= .25, "overlap": overlap, "shift_x": x * 160, "shift_y": y * 100,
                "displacement": float(np.hypot(x * 160, y * 100)), "analysis_size": [160, 100],
                "support_scope": "distributed_scene", "inliers": 100,
                "homography": [[1, 0, x * 160], [0, 1, y * 100], [0, 0, 1]]}

    scanner.camera.return_to = return_to
    monkeypatch.setattr(scanner, "_reference_window", observe)
    monkeypatch.setattr(scanner, "_pulse", pulse)
    monkeypatch.setattr(scanner, "_move", move)
    monkeypatch.setattr(scan, "_match", match)
    return scanner, moves, recalls, positions


@pytest.mark.parametrize("mirrored", [False, True])
def test_real_policy_preserves_reference_and_covers_both_sides_and_below(tmp_path, monkeypatch, mirrored):
    scanner, moves, recalls, positions = environment(tmp_path, monkeypatch, mirrored=mirrored)
    asyncio.run(acquire_region(scanner))
    evaluation = decision(scanner.checkpoint)
    assert evaluation["route_complete"] is True
    assert evaluation["sufficient"] is False
    assert evaluation["coverage_approval"] == "pending_visual_acceptance"
    assert scanner.captures[0]["role"] == "region_reference"
    assert scanner.captures[0]["quality"]["provisional"] is True
    assert scanner.captures[0]["region_command_id"] is None
    assert np.array_equal(cv2.imread(scanner.captures[0]["path"]), np.zeros((100, 160, 3), np.uint8))
    assert set(required_captures(scanner.checkpoint)) == {p["id"] for p in scanner.captures}
    visits = scanner.checkpoint["continuous_cursor"]["region"]["visits"]
    assert [v["name"] for v in visits] == ["lower", "side_seed", "opposite", "side_extension"]
    assert visits[1]["progress"] < COVERAGE_REGION_POLICY["horizontal_extent"]
    assert all(v["localization"] for v in visits)
    assert len(scanner.checkpoint["region_commands"]) == len(moves) + len(recalls) <= 25
    assert all(p["physical_timestamp_verified"] is False for p in scanner.captures)
    assert all(Path(p["path"]).exists() and p["sha256"] for p in scanner.captures)


@pytest.mark.parametrize("rejection", [-1, 1])
def test_simulated_local_rejection_preserves_exact_pair_and_other_regions(tmp_path, monkeypatch, rejection):
    scanner, moves, recalls, positions = environment(tmp_path, monkeypatch, rejection=rejection)
    asyncio.run(acquire_region(scanner))
    region = scanner.checkpoint["continuous_cursor"]["region"]
    assert decision(scanner.checkpoint)["route_complete"] is False
    assert region["extents"]["lower"] >= .2
    assert region["extents"]["left" if rejection == -1 else "right"] >= .8
    assert any(v["state"] == "blocked" for v in region["visits"])
    record = scanner.checkpoint["first_connection_refusal"]
    assert record["status"] == "preserved" and record["metrics"]["verified"] is False
    first, second = [np.load(f["path"], allow_pickle=False) for f in record["files"]]
    assert first.shape == second.shape == (100, 160, 3)
    assert record["observation"]["sequence"] == int(second[0, 0, 0]) + 1
    assert record["command"]["state"] == "observed"
    assert len(list(tmp_path.glob('first-connection-refusal/observation.npy'))) == 1
    assert record["observation"]["sequence"] not in {p["sequence"] for p in scanner.captures}


def test_unavailable_localization_stops_before_another_visit(tmp_path, monkeypatch):
    scanner, moves, recalls, _ = environment(tmp_path, monkeypatch, recovery=False)
    with pytest.raises(PanoramaCaptureError, match="relocalization_required"):
        asyncio.run(acquire_region(scanner))
    assert len(recalls) == 1
    assert all(axis == "tilt" for axis, _, _ in moves)
    assert all(Path(p["path"]).exists() for p in scanner.captures)


@pytest.mark.parametrize("offset", [(0.0, -.18), (.07, -.18), (-.07, -.18)])
def test_connected_recall_endpoint_is_preserved_without_claiming_exact_return(tmp_path, monkeypatch, offset):
    scanner, moves, recalls, positions = environment(tmp_path, monkeypatch, recall_offset=offset)
    asyncio.run(acquire_region(scanner))
    assert decision(scanner.checkpoint)["route_complete"] is True
    region = scanner.checkpoint["continuous_cursor"]["region"]
    bridges = [p for p in scanner.captures if p["role"] == "region_localization"]
    assert len(bridges) == len(recalls) == 3
    assert all(Path(p["path"]).exists() for p in bridges)
    assert [p["control_command_id"] for p in bridges] == [f"recall-{i}" for i in range(1, 4)]
    assert [p["observation_replay_id"] for p in bridges] == [f"replay-recall-{i}" for i in range(1, 4)]
    assert all(any(e["source"] == region["reference_id"] and e["target"] == p["id"] for e in region["links"]) for p in bridges)
    for visit in region["visits"][1:]:
        assert visit["recovery"]["state"] == "connected"
        assert visit["localization"]["kind"] == "connected_reference"
        assert visit["localization_offset"] == pytest.approx(abs(offset[0]))
    assert scanner.physical_state == "stopped"
    assert len(scanner.checkpoint["region_commands"]) == len(moves) + len(recalls) <= 25


def test_connected_recall_localizes_current_observation_without_relabeling_old_photo(tmp_path, monkeypatch):
    scanner, moves, recalls, _ = environment(tmp_path, monkeypatch, recall_offset=(.07, -.18))
    original_move = scanner._move
    original_observe = scanner._reference_window
    endpoints = []

    async def move(*args, **kwargs):
        result = await original_move(*args, **kwargs)
        # Simulated readback latency after a causally qualified endpoint.
        result["frame"]["received_monotonic"] -= 1.1
        endpoints.append(copy.deepcopy(result["frame"]))
        return result

    async def observe(**kwargs):
        frame = await original_observe(**kwargs)
        frame["sequence"] += 100
        return frame

    monkeypatch.setattr(scanner, "_move", move)
    monkeypatch.setattr(scanner, "_reference_window", observe)
    asyncio.run(acquire_region(scanner))
    assert decision(scanner.checkpoint)["route_complete"] is True
    photographs = [p for p in scanner.captures if p["role"] == "region_localization"]
    assert len(photographs) == len(recalls) == len(endpoints) == 3
    for photo, endpoint in zip(photographs, endpoints):
        assert photo["sequence"] == endpoint["sequence"]
        assert photo["received_monotonic"] == endpoint["received_monotonic"]
        assert photo["quality"]["evidence"] == "visual_transition_verified"
    for visit, photo in zip(scanner.checkpoint["continuous_cursor"]["region"]["visits"][1:], photographs):
        assert visit["localization"]["capture_id"] == photo["id"]
        assert visit["localization"]["sequence"] > photo["sequence"]
    assert len(scanner.checkpoint["region_commands"]) == len(moves) + len(recalls)


@pytest.mark.parametrize("failure", ["uncertain_command", "uncertain_stop", "stationary", "ambiguous"])
def test_connected_recall_does_not_bypass_operational_or_geometric_evidence(tmp_path, monkeypatch, failure):
    scanner, moves, recalls, positions = environment(tmp_path, monkeypatch, recall_offset=(.07, -.18))
    original = scanner._move

    async def move(*args, **kwargs):
        result = await original(*args, **kwargs)
        if failure == "uncertain_command":
            scanner.checkpoint["region_commands"][-1]["state"] = "uncertain"
        elif failure == "uncertain_stop":
            scanner.physical_state = "unknown"
        elif failure == "stationary":
            result["stationary"] = True
        elif failure == "ambiguous":
            matching = scan._match
            def reject(first, second):
                return {**matching(first, second), "verified": False, "code": "ambiguous_motion_model"}
            monkeypatch.setattr(scan, "_match", reject)
        return result

    monkeypatch.setattr(scanner, "_move", move)
    with pytest.raises(PanoramaCaptureError):
        asyncio.run(acquire_region(scanner))
    assert len(recalls) == 1
    assert all(axis == "tilt" for axis, _, _ in moves)
    assert not any(p["role"] == "region_localization" for p in scanner.captures)
    assert all(Path(p["path"]).exists() for p in scanner.captures)


@pytest.mark.parametrize("failure", ["old_observation", "generation", "instance", "displaced", "observation_failed", "uncertain_stop", "budget"])
def test_connected_historical_endpoint_requires_current_localization(tmp_path, monkeypatch, failure):
    scanner, moves, recalls, positions = environment(tmp_path, monkeypatch, recall_offset=(.07, -.18))
    original_move = scanner._move
    original_observe = scanner._reference_window
    original_check = scanner._check

    async def move(*args, **kwargs):
        result = await original_move(*args, **kwargs)
        result["frame"]["received_monotonic"] -= 1.1
        return result

    async def observe(**kwargs):
        frame = await original_observe(**kwargs)
        if not recalls:
            return frame
        frame["sequence"] += 100
        if failure == "old_observation":
            frame["received_monotonic"] -= 10
        elif failure == "generation":
            frame["generation"] += 1
        elif failure == "instance":
            frame["capture_instance"] = "reconnected"
        elif failure == "displaced":
            index = len(positions)
            positions[index] = (.6, .5)
            frame["image"] = np.full_like(frame["image"], index)
        elif failure == "observation_failed":
            raise PanoramaCaptureError("stop_observation_unconfirmed")
        elif failure == "uncertain_stop":
            scanner.physical_state = "unknown"
        return frame

    def check():
        original_check()
        if failure == "budget" and recalls:
            raise PanoramaCaptureError("region_budget_exhausted")

    monkeypatch.setattr(scanner, "_move", move)
    monkeypatch.setattr(scanner, "_reference_window", observe)
    monkeypatch.setattr(scanner, "_check", check)
    with pytest.raises(PanoramaCaptureError):
        asyncio.run(acquire_region(scanner))
    assert len(recalls) == 1
    assert all(axis == "tilt" for axis, _, _ in moves)
    assert all(Path(p["path"]).exists() for p in scanner.captures)
    # A qualified historical photo can survive failed current localization;
    # it must never authorize another move or acquire a new stream identity.
    for photo in scanner.captures:
        assert photo["capture_instance"] == "synthetic"
        assert photo["generation"] == 1
    assert scanner.checkpoint["continuous_cursor"]["region"]["visits"][-1]["state"] == "localizing"


@pytest.mark.parametrize("uncertain", ["control_lost", "stop_unconfirmed", "motion_not_observed", "stability_timeout"])
def test_uncertainty_never_authorizes_next_policy_command(tmp_path, monkeypatch, uncertain):
    scanner, moves, recalls, _ = environment(tmp_path, monkeypatch, uncertain=uncertain)
    with pytest.raises(PanoramaCaptureError, match=uncertain):
        asyncio.run(acquire_region(scanner))
    assert len(moves) == 2 and len(recalls) == 1
    assert scanner.checkpoint["region_commands"][-1]["state"] == "uncertain"


def test_first_refusal_is_not_overwritten(tmp_path, monkeypatch):
    scanner, _, _, _ = environment(tmp_path, monkeypatch)
    # The accepted-replay cap must not hide a later geometric refusal again.
    (tmp_path / "replay-accepted-one.json").write_text("{}")
    (tmp_path / "replay-accepted-two.json").write_text("{}")
    frame = scanner.last_frame
    asyncio.run(scanner._preserve_first_connection_refusal({}, frame["image"], frame, {"verified": False, "code": "first"}))
    before = (tmp_path / "first-connection-refusal/manifest.json").read_bytes()
    asyncio.run(scanner._preserve_first_connection_refusal({}, frame["image"], frame, {"verified": False, "code": "second"}))
    assert (tmp_path / "first-connection-refusal/manifest.json").read_bytes() == before


def test_policy_budget_and_old_version_are_immutable():
    assert region_policy(COVERAGE_REGION_POLICY)["maximum_commands"] == 35
    historical = {**COVERAGE_REGION_POLICY, "maximum_commands": 30, "maximum_captures": 24}
    assert region_policy(historical) == historical
    with pytest.raises(PanoramaCaptureError, match="region_policy_incompatible"):
        region_policy({**COVERAGE_REGION_POLICY, "maximum_commands": 36})


def test_confirmed_small_advances_exhaust_cumulative_budget(tmp_path, monkeypatch):
    scanner, moves, recalls, _ = environment(tmp_path, monkeypatch, small_steps=True)
    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        asyncio.run(acquire_region(scanner))
    assert len(scanner.checkpoint["region_commands"]) == len(moves) + len(recalls) == 30
    assert len(scanner.captures) <= 32
    assert len(scanner.checkpoint["continuous_cursor"]["region"]["visits"]) >= 2
    assert all(Path(p["path"]).exists() for p in scanner.captures)
    scanner.returning = True
    scanner._check()  # The independent closing reserve remains available.


def test_elapsed_budget_does_not_reset_on_a_new_visit(tmp_path, monkeypatch):
    scanner, moves, recalls, _ = environment(tmp_path, monkeypatch)
    scanner.active_seconds = 270
    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        asyncio.run(acquire_region(scanner))
    assert not moves and not recalls
    scanner.returning = True
    scanner._check()


@pytest.mark.parametrize("absent", [1, 3])
def test_qualified_no_effect_is_bounded_and_does_not_block_other_regions(tmp_path, monkeypatch, absent):
    scanner, moves, _, _ = environment(tmp_path, monkeypatch)
    original = scanner._pulse
    durations = []

    async def pulse(axis, direction, duration, **kwargs):
        if axis == "tilt":
            durations.append(duration)
            if len(durations) <= absent:
                scanner._check()
                scanner.checkpoint.setdefault("region_commands", []).append(
                    {"state": "observed", "outcome": "stationary_boundary_observed"})
                scanner.checkpoint["continuous_cursor"]["transition"] = {"state": "observed"}
                return {"stationary": True}
        return await original(axis, direction, duration, **kwargs)

    monkeypatch.setattr(scanner, "_pulse", pulse)
    asyncio.run(acquire_region(scanner))
    result = decision(scanner.checkpoint)
    assert durations[:2] == pytest.approx([.3, .45])
    assert result["criteria"]["left"] and result["criteria"]["right"]
    assert result["criteria"]["lower"] == (absent == 1)
    assert len(durations) == (2 if absent == 1 else 3)
    assert any(axis == "pan" for axis, _, _ in moves)


def test_refusal_storage_limit_is_explicit_and_retains_no_large_copy(tmp_path, monkeypatch):
    scanner, _, _, _ = environment(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "MAX_FIRST_REFUSAL_BYTES", 1024)
    frame = scanner.last_frame
    asyncio.run(scanner._preserve_first_connection_refusal({}, frame["image"], frame, {"verified": False}))
    assert scanner.checkpoint["first_connection_refusal"]["reason"] == "refusal_storage_limit"
    assert not (tmp_path / "first-connection-refusal").exists()


def test_refusal_metadata_limit_is_explicit(tmp_path, monkeypatch):
    scanner, _, _, _ = environment(tmp_path, monkeypatch)
    monkeypatch.setattr(scan, "MAX_FIRST_REFUSAL_METADATA_BYTES", 5000)
    frame = scanner.last_frame
    asyncio.run(scanner._preserve_first_connection_refusal(
        {}, frame["image"], frame, {"verified": False, "detail": "x" * 6000}))
    record = scanner.checkpoint["first_connection_refusal"]
    assert record["reason"] == "refusal_metadata_limit"
    assert "metrics" not in record
    assert not (tmp_path / "first-connection-refusal").exists()


def test_existing_route_cannot_restart_or_reset_its_ledger(tmp_path, monkeypatch):
    scanner, moves, recalls, _ = environment(tmp_path, monkeypatch)
    asyncio.run(acquire_region(scanner))
    ledger = copy.deepcopy(scanner.checkpoint["region_commands"])
    counts = (len(moves), len(recalls), len(scanner.captures))
    with pytest.raises(PanoramaCaptureError, match="region_resume_unavailable"):
        asyncio.run(acquire_region(scanner))
    assert scanner.checkpoint["region_commands"] == ledger
    assert (len(moves), len(recalls), len(scanner.captures)) == counts


@pytest.mark.parametrize("returning, consumed", [(False, 30), (True, 35)])
def test_real_move_refuses_exhausted_command_budget(tmp_path, monkeypatch, returning, consumed):
    from test_camera_panorama_scan import SimulatedCamera, _clock, _progress
    camera = SimulatedCamera()
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None, COVERAGE_REGION_POLICY)
    scanner.capabilities = camera.capabilities
    scanner.acquired = True
    scanner.returning = returning
    scanner.checkpoint["region_commands"] = [{"state": "observed"} for _ in range(consumed)]
    sent = []

    async def command():
        sent.append(True)

    with pytest.raises(PanoramaCaptureError, match="region_budget_exhausted"):
        asyncio.run(scanner._move(command, allow_stationary=True))
    assert not sent
    assert len(scanner.checkpoint["region_commands"]) == consumed


@pytest.mark.parametrize("return_verified", [True, False])
def test_product_publishes_integral_without_coverage_approval_or_replacing_active(tmp_path, monkeypatch, return_verified):
    from fastapi.testclient import TestClient
    from test_camera_source_panorama_api import FakeScanner, make_app, create, wait_done, BASE

    class ProductInput(FakeScanner):
        async def scan(self, camera, output, **arguments):
            scanner, _, _, _ = environment(output, monkeypatch)
            scanner.progress = arguments["progress"]
            await acquire_region(scanner)
            scanner.physical_state = "restored" if return_verified else "stopped"
            if not return_verified:
                scanner.issues.append({"code": "return_framing_unconfirmed"})
            await scanner._persist()
            return scanner.result()

        async def reconstruct(self, captures, output, **arguments):
            self.inputs = copy.deepcopy(captures)
            result = await super().reconstruct(captures, output, **arguments)
            result["source_ids"] = [p["id"] for p in captures]
            return result

    fake = ProductInput()
    app, _, service, _ = make_app(tmp_path, fake, acquisition_policy=COVERAGE_REGION_POLICY)
    with TestClient(app) as client:
        first = wait_done(client, create(client, "coverage-first"))
        assert first["outcomes"]["acquisition"] == "completed"
        assert first["outcomes"]["reconstruction"] == "ready"
        assert first["outcomes"]["return"] == ("verified" if return_verified else "unverified")
        assert first["coverage_progress"]["policy_version"] == 4
        assert first["coverage_progress"]["qualified_views"] > 0
        assert first["coverage_progress"]["decision"]["route_complete"] is True
        assert first["coverage_progress"]["region_complete"] is False
        # Previously saved public projections omitted v4. Reopening derives
        # progress from the preserved checkpoint without rewriting history.
        service.jobs[first["id"]]["coverage_progress"] = {"qualified_views": 0}
        reopened = client.get(f"/api/cameras/panorama-jobs/{first['id']}").json()["job"]
        assert reopened["coverage_progress"] == first["coverage_progress"]
        assert first["status"] == "partial"
        source = client.get(BASE).json()
        active = source["active"]
        assert active["region_status"] == "review"
        assert active["acquisition_decision"]["sufficient"] is False
        assert active["acquisition_decision"]["route_complete"] is True
        image = client.get(active["image_url"]).content
        mask = client.get(active["coverage_url"]).content
        assert image and mask
        assert fake.inputs[0]["role"] == "region_reference"
        assert all(p["sha256"] and p["source_identity"] for p in fake.inputs)
        artifact_path = service.root / "artifacts" / active["id"] / "artifact.json"
        artifact = json.loads(artifact_path.read_text())
        artifact["_identity"] = "old-binding-still-used-by-a-mapping"
        artifact_path.write_text(json.dumps(artifact))
        second = wait_done(client, create(client, "coverage-second"))
        assert second["artifact_id"] != first["artifact_id"]
        after = client.get(BASE).json()
        assert after["active"]["id"] == active["id"]
        assert after["candidate"]["id"] == second["artifact_id"]
        assert client.get(active["image_url"]).content == image
        assert client.get(active["coverage_url"]).content == mask
        assert after["candidate"]["crop"] == {"u_start": 0, "u_width": 1, "v_start": 0, "v_height": 1}


@pytest.mark.parametrize("failure, receipt_state", [
    ("movement_unconfirmed", "uncertain"), ("motion_not_observed", "uncertain"),
    ("stability_timeout", "uncertain"), ("fresh_frame_unavailable", "observed"),
    ("frame_acquisition_timeout", "observed"),
])
@pytest.mark.parametrize("current_stop_observed", [True, False])
def test_run_closes_without_automatic_return_after_uncertain_command(tmp_path, monkeypatch, failure, receipt_state, current_stop_observed):
    from test_camera_panorama_scan import SimulatedCamera, _clock, _progress
    camera = SimulatedCamera()
    camera.capabilities.update(velocity_supported=True, axes={"pan": True, "tilt": True})
    _clock(monkeypatch, camera)
    scanner = scan._Scan(camera, tmp_path, _progress, lambda: False, None, COVERAGE_REGION_POLICY)

    reference_calls = []

    async def reference(**kwargs):
        reference_calls.append(kwargs)
        if scanner.checkpoint.get("region_commands") and not current_stop_observed:
            raise PanoramaCaptureError("stop_observation_unconfirmed")
        frame = await camera.frame()
        frame["return_reference_evidence"] = {"evidence": "local_observation_only", "provisional": True}
        return frame

    async def interrupted_acquisition(limits):
        scanner.checkpoint.setdefault("region_commands", []).append({"id": "simulated-last", "state": receipt_state})
        raise PanoramaCaptureError(failure)

    monkeypatch.setattr(scanner, "_reference_window", reference)
    monkeypatch.setattr(scanner, "_acquire_panorama", interrupted_acquisition)
    result = asyncio.run(scanner.run(return_only=False))
    assert failure in {issue["code"] for issue in result["issues"]}
    assert not any(kind in {"return", "absolute", "velocity"} for kind, _ in camera.events)
    assert any(kind == "stop" for kind, _ in camera.events)
    assert len(reference_calls) == 3
    assert result["physical_state"] == ("stopped" if current_stop_observed else "stop_unconfirmed")
    assert result["checkpoint"]["region_commands"][-1]["state"] == receipt_state


def test_available_garage_pair_is_positive_not_the_missing_historical_negative():
    directory = Path(__file__).parents[1] / ".toposync-data/runtime/cameras/source-panorama/jobs/2cd035bbf78b412b96481f2f5345c437"
    if not directory.exists():
        pytest.skip("Private historical images are not distributed with the repository")
    manifest = json.loads((directory / "scan-manifest.json").read_text())
    baseline = manifest["continuous_cursor"]["transition"]["baseline"]
    assert baseline["sequence"] == 289
    match = scan._match(cv2.imread(str(directory / "capture-0004.jpg")), cv2.imread(baseline["path"]))
    assert match["verified"] is True and match["overlap"] > .99
