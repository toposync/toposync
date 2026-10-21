from __future__ import annotations

import io
import json
from types import SimpleNamespace
import zipfile

import numpy as np
import pytest

from benchmarks import visual_state_models as benchmark
from visual_state_corpus import CorpusSequence
from toposync_ext_vision.visual_state.matching import ReferenceEmbedding


class ControlledBackend:
    revision = "model-space"
    providers = ["controlled-test"]

    def __init__(self):
        self.seen = []
        self.closed = False

    def embed(self, frame):
        self.seen.append(int(frame[0, 0, 2]))
        return np.array([0, 1] if frame[0, 0, 0] else [1, 0], dtype=np.float32)

    def close(self):
        self.closed = True


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    sequences = []
    for marker, partition in enumerate(("references", "calibration", "test"), 1):
        frames = np.zeros((5, 4, 4, 3), dtype=np.uint8)
        frames[2:, :, :, 0] = 255
        frames[:, 0, 0, 2] = marker
        asset = tmp_path / f"{partition}.npz"
        np.savez(asset, frames=frames, timestamps_ms=np.array([0, 200, 400, 600, 800]))
        sequences.append({
            "sequence_id": partition, "source_id": "camera", "session_id": partition,
            "partition": partition, "evidence": "synthetic", "label_origin": "synthetic",
            "illumination": "day", "preparation_revision": "p1", "asset": asset.name,
            "sha256": benchmark.digest_file(asset), "duration_ms": 1000,
            "truth": [{"start_ms": 0, "end_ms": 400, "class_id": "a"},
                      {"start_ms": 400, "end_ms": 1000, "class_id": "b"}],
            "transitions": [{"from_class": "a", "to_class": "b", "earliest_ms": 400, "latest_ms": 400}],
        })
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"corpus_id": "controlled", "protocol_revision": "v1", "sequences": sequences}))
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"model_id": "controlled", "display_name": "Controlled", "task": "embedding",
        "runtime": "onnxruntime", "artifact_format": "onnx", "artifact_path": "unused-test-only.onnx"}))
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"corpus_sha256": benchmark.digest_file(corpus),
        "model_manifest_sha256": benchmark.digest_file(model), "configuration": {
            "sensor_id": "sensor", "source_identity": "camera", "use_full_image": True,
            "preparation_revision": "p1", "reference_revision": "r1", "model_revision": "model-space",
            "model_id": "controlled", "comparison": {"enter_threshold": 0.9, "enter_margin": 0.2,
                "hold_threshold": 0.8, "hold_margin": 0.1},
            "classes": [{"class_id": key, "key": key, "label": key, "reference_ids": [f"{key}1"]}
                        for key in ("a", "b")]},
        "references": [{"reference_id": "a1", "sequence_id": "references", "frame_index": 0},
                       {"reference_id": "b1", "sequence_id": "references", "frame_index": 2}]}))
    backends = []
    def factory(_):
        backend = ControlledBackend()
        backends.append(backend)
        return backend
    monkeypatch.setattr(benchmark, "build_embedding_backend", factory)
    return SimpleNamespace(root=tmp_path, corpus=corpus, model=model, protocol=protocol, backends=backends,
        arguments={"protocol_path": protocol, "corpus_path": corpus, "asset_root": tmp_path, "model_path": model})


def calibrate(experiment):
    report = benchmark.run_benchmark(**experiment.arguments, partition="calibration")
    path = experiment.root / "calibration-report.json"
    path.write_text(json.dumps(report))
    return path, report


def test_replays_only_selected_partition_and_synthetic_completion_never_approves_quality(experiment):
    report_path, calibration = calibrate(experiment)
    assert set(experiment.backends[-1].seen) == {1, 2}
    assert experiment.backends[-1].closed
    result = benchmark.run_benchmark(**experiment.arguments, partition="test", calibration_report=report_path)
    assert set(experiment.backends[-1].seen) == {1, 3}
    assert result["status"] == "completed" and result["quality_approved"] is False
    assert result["evidence_kinds"] == ["synthetic"]
    assert result["calibration_report_sha256"] == benchmark.digest_file(report_path)
    assert result["aggregate"]["expected_transitions"] == 1
    assert result["aggregate"]["matched_transitions"] == 1
    assert result["aggregate"]["sessions"] == 1
    assert result["aggregate"]["coverage"] < 1
    assert result["aggregate"]["wrong_state_ms"] >= 200
    assert calibration["by_illumination"]["day"]["processing_p95_ms"] >= 0


def test_test_partition_requires_matching_calibration_and_engine(experiment):
    with pytest.raises(ValueError, match="completed calibration"):
        benchmark.run_benchmark(**experiment.arguments, partition="test")
    report_path, report = calibrate(experiment)
    report["engine_sha256"] = "old-engine"
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="calibration protocol"):
        benchmark.run_benchmark(**experiment.arguments, partition="test", calibration_report=report_path)
    report_path, _ = calibrate(experiment)
    protocol = json.loads(experiment.protocol.read_text())
    protocol["configuration"]["comparison"]["enter_threshold"] = 0.95
    experiment.protocol.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="calibration protocol"):
        benchmark.run_benchmark(**experiment.arguments, partition="test", calibration_report=report_path)


@pytest.mark.parametrize("field", ["model", "corpus"])
def test_pinned_input_changes_fail_before_inference(experiment, field):
    path = getattr(experiment, field)
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="after protocol freeze"):
        benchmark.run_benchmark(**experiment.arguments, partition="calibration")
    assert not experiment.backends


def test_reference_partition_and_independent_labels_are_enforced(experiment):
    protocol = json.loads(experiment.protocol.read_text())
    protocol["references"][0]["sequence_id"] = "test"
    experiment.protocol.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="cannot use calibration or test"):
        benchmark.run_benchmark(**experiment.arguments, partition="calibration")
    assert not experiment.backends
    protocol["references"][0]["sequence_id"] = "references"
    protocol["references"][0]["frame_index"] = 3
    experiment.protocol.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="independently annotated"):
        benchmark.run_benchmark(**experiment.arguments, partition="calibration")
    assert experiment.backends[-1].closed


def test_unobservable_labels_do_not_act_as_an_oracle_for_the_classifier(experiment):
    corpus = json.loads(experiment.corpus.read_text())
    sequence = corpus["sequences"][1]
    sequence["truth"][1].update(class_id=None, uncertainty="unobservable")
    sequence["transitions"] = []
    experiment.corpus.write_text(json.dumps(corpus))
    protocol = json.loads(experiment.protocol.read_text())
    protocol["corpus_sha256"] = benchmark.digest_file(experiment.corpus)
    experiment.protocol.write_text(json.dumps(protocol))
    result = benchmark.run_benchmark(**experiment.arguments, partition="calibration")
    assert result["aggregate"]["unobservable_claim_ms"] > 0
    assert result["aggregate"]["transition_recall"] is None


def test_aggregation_weights_time_instead_of_averaging_sequence_percentages():
    base = {"source_id": "camera", "session_id": "one", "observable_ms": 100,
        "conclusive_ms": 100, "wrong_state_ms": 0, "expected_transitions": 1, "matched_transitions": 1,
        "false_transitions": 0, "unobservable_claim_ms": 0, "confusion_ms": {"a": {"a": 100}},
        "observable_by_class_ms": {"a": 100},
        "latency_intervals": [{"minimum_ms": 10, "maximum_ms": 50}], "processing_ms": [10]}
    second = {**base, "session_id": "two", "observable_ms": 900, "conclusive_ms": 0,
        "expected_transitions": 0, "matched_transitions": 0, "confusion_ms": {}, "latency_intervals": [],
        "observable_by_class_ms": {"b": 900}}
    result = benchmark.aggregate_results([base, second])
    assert result["coverage"] == 0.1 and result["sessions"] == 2
    assert result["transition_latency_upper_p95_ms"] == 50
    assert result["transition_recall"] == 1
    assert result["error_fraction_of_conclusive_time"] == 0
    assert result["by_class"]["b"]["coverage"] == 0


def test_hostile_array_header_is_rejected_before_numpy_allocation(experiment, monkeypatch):
    header = io.BytesIO()
    np.lib.format.write_array_header_1_0(header, {"descr": "|u1", "fortran_order": False,
                                               "shape": (10**12, 3, 4, 3)})
    path = experiment.root / "hostile.npz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("frames.npy", header.getvalue())
        archive.writestr("timestamps_ms.npy", b"unused")
    raw = json.loads(experiment.corpus.read_text())["sequences"][0]
    sequence = CorpusSequence.model_validate({**raw, "asset": path.name, "sha256": benchmark.digest_file(path)})
    monkeypatch.setattr(np, "load", lambda *args, **kwargs: pytest.fail("must reject before allocation"))
    with pytest.raises(ValueError, match="declared replay array"):
        benchmark.load_frames(sequence, experiment.root)


def replay_fixture(experiment):
    protocol = benchmark.BenchmarkProtocol.model_validate_json(experiment.protocol.read_text())
    sequence = CorpusSequence.model_validate(json.loads(experiment.corpus.read_text())["sequences"][1])
    frames, timestamps = benchmark.load_frames(sequence, experiment.root)
    references = {key + "1": ReferenceEmbedding(key, "model-space", "p1", np.array(vector, dtype=np.float32))
                  for key, vector in [("a", [1, 0]), ("b", [0, 1])]}
    return protocol.configuration, sequence, frames, timestamps, references


def test_slow_inference_discards_backlog_instead_of_replaying_every_old_frame(experiment, monkeypatch):
    config, sequence, frames, _, references = replay_fixture(experiment)
    ticks = iter(index * 0.4 for index in range(20))
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    result = benchmark._replay(sequence, frames, np.array([0, 100, 200, 700, 800]),
                               config, references, ControlledBackend())
    assert result["discarded_frames"] == 2
    assert result["inference_samples"] == 3


def test_silence_expires_confirmed_evidence_in_time_metrics(experiment, monkeypatch):
    config, sequence, frames, _, references = replay_fixture(experiment)
    payload = config.model_dump()
    payload["temporal"].update(validity_ms=300, maximum_frame_age_ms=300, pending_hold_ms=100)
    config = type(config).model_validate(payload)
    payload = sequence.model_dump()
    payload.update(truth=[{"start_ms": 0, "end_ms": 1000, "class_id": "a"}], transitions=[])
    sequence = CorpusSequence.model_validate(payload)
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(perf_counter=lambda: 0.0))
    result = benchmark._replay(sequence, frames[:2], np.array([0, 200]), config, references, ControlledBackend())
    assert result["conclusive_ms"] == 299
    assert result["abstention_ms"] == 701
