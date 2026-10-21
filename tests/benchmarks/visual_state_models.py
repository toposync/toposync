"""Offline visual-sensor benchmark; never opens a camera or downloads a model.

Run with --help. Assets are NPZ archives containing BGR uint8 `frames` (T,H,W,3)
and integer `timestamps_ms` (T). Labels and provenance come from the corpus
manifest. Calibration evaluates explicit policies; it does not choose thresholds.
Test evaluation requires a completed calibration report for the identical protocol.
"""
from __future__ import annotations

import argparse
import hashlib
import io
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import sys
import time
from typing import Literal
import zipfile

import numpy as np
from pydantic import Field

from toposync_ext_vision.processing.runtime_backends import (
    OnnxRuntimeEmbeddingBackend, build_embedding_backend, prepare_onnx_input,
)
from toposync_ext_vision.registry.manifests import ModelManifest
from toposync_ext_vision.visual_state.matching import ReferenceEmbedding, compare_references
from toposync_ext_vision.visual_state.schemas import Identifier, Observation, VisualStateConfig, VisualStateModel
from toposync_ext_vision.visual_state.state import VisualStateReducer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visual_state_corpus import CorpusSequence, Decision, Digest, evaluate_sequence, load_corpus  # noqa: E402


class ReferenceSelection(VisualStateModel):
    reference_id: Identifier
    sequence_id: Identifier
    frame_index: int = Field(strict=True, ge=0)


class BenchmarkProtocol(VisualStateModel):
    schema_version: Literal[1] = 1
    corpus_sha256: Digest
    model_manifest_sha256: Digest
    configuration: VisualStateConfig
    references: tuple[ReferenceSelection, ...] = Field(min_length=2, max_length=300)


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def engine_digest() -> str:
    modules = {item.__module__ for item in (OnnxRuntimeEmbeddingBackend, prepare_onnx_input, ModelManifest,
        Observation, compare_references, VisualStateReducer, evaluate_sequence)}
    files = {name: digest_file(Path(sys.modules[name].__file__)) for name in sorted(modules)}
    files["benchmark_runner"] = digest_file(Path(__file__))
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def load_frames(sequence: CorpusSequence, root: Path) -> tuple[np.ndarray, np.ndarray]:
    path = (root / sequence.asset).resolve(strict=True)
    maximum_bytes = 256 * 1024 * 1024
    if not path.is_relative_to(root.resolve(strict=True)) or path.stat().st_size > maximum_bytes:
        raise ValueError("replay asset escaped its root or exceeds 256 MiB")
    with path.open("rb") as stream:
        data = stream.read(maximum_bytes + 1)
    if len(data) > maximum_bytes or hashlib.sha256(data).hexdigest() != sequence.sha256:
        raise ValueError("replay asset changed or escaped its root")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if ({entry.filename for entry in entries} != {"frames.npy", "timestamps_ms.npy"}
                or len(entries) != 2 or sum(entry.file_size for entry in entries) > maximum_bytes):
            raise ValueError("replay must contain only frames and timestamps within 256 MiB")
        for entry in entries:
            with archive.open(entry) as stream:
                version = np.lib.format.read_magic(stream)
                if version not in {(1, 0), (2, 0)}:
                    raise ValueError("unsupported replay array version")
                reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                shape, _, dtype = reader(stream)
                if dtype.hasobject or math.prod(shape) * dtype.itemsize > entry.file_size:
                    raise ValueError("declared replay array exceeds stored data or contains objects")
    with np.load(io.BytesIO(data), allow_pickle=False) as archive:
        frames, timestamps = archive["frames"], archive["timestamps_ms"]
    if (frames.dtype != np.uint8 or frames.ndim != 4 or frames.shape[-1] != 3
            or min(frames.shape[:3]) < 1 or frames.shape[1] * frames.shape[2] > 4_000_000):
        raise ValueError("replay frames must be nonempty BGR uint8 images of at most four megapixels")
    if (timestamps.dtype.kind not in "iu" or timestamps.shape != (len(frames),)
            or timestamps[0] < 0 or timestamps[-1] >= sequence.duration_ms
            or np.any(timestamps[1:] <= timestamps[:-1])):
        raise ValueError("replay timestamps must be ordered integers inside sequence duration")
    return frames, timestamps


def _replay(sequence, frames, timestamps, config, references, backend):
    now_ms = 0
    reducer = VisualStateReducer(config, runtime_epoch="offline", clock=lambda: now_ms / 1000)
    decisions, durations = [], []
    discarded = index = 0
    while index < len(frames):
        now_ms = max(now_ms, int(timestamps[index]))
        latest = max(index, int(np.searchsorted(timestamps, now_ms, side="right")) - 1)
        discarded += latest - index
        index = latest
        observed_ms = int(timestamps[index])
        current = reducer.current_class_id
        started = time.perf_counter()
        vector = backend.embed(frames[index])
        match = compare_references(config, vector, references, model_revision=backend.revision,
                                   preparation_revision=sequence.preparation_revision, current_class_id=current)
        elapsed_ms = (time.perf_counter() - started) * 1000
        durations.append(elapsed_ms)
        now_ms += max(1, math.ceil(elapsed_ms))
        if now_ms >= sequence.duration_ms:
            break
        state = reducer.observe(Observation(
            sensor_id=config.sensor_id, runtime_epoch="offline", source_identity=sequence.source_id,
            preparation_revision=sequence.preparation_revision, reference_revision=config.reference_revision,
            model_revision=backend.revision, sample={"capture_instance": sequence.sequence_id,
                "generation": 0, "sequence": index + 1},
            observed_at=1_000_000.0 + observed_ms / 1000, observed_monotonic=observed_ms / 1000,
            freshness_basis="receipt", region_quality="valid", class_id=match.class_id,
            acceptance=match.acceptance, similarity=match.similarity, margin=match.margin,
            reference_ids=match.reference_ids, method="local", reason=match.reason,
        ))
        # A pending transition can still publish the preceding class with a bounded lease.
        confirmed = state.class_id
        decisions.append(Decision(at_ms=now_ms, class_id=confirmed,
            valid_until_ms=now_ms + state.valid_for_ms if confirmed else None))
        index += 1
    metrics = evaluate_sequence(sequence, decisions)
    observable_by_class = {}
    for interval in sequence.truth:
        if interval.class_id is not None:
            observable_by_class[interval.class_id] = observable_by_class.get(interval.class_id, 0) + interval.end_ms - interval.start_ms
    metrics.update(source_id=sequence.source_id, discarded_frames=discarded,
                   inference_samples=len(durations), processing_ms=durations, observable_by_class_ms=observable_by_class)
    return metrics


def aggregate_results(results: list[dict]) -> dict:
    observable = sum(item["observable_ms"] for item in results)
    conclusive = sum(item["conclusive_ms"] for item in results)
    matched = sum(item["matched_transitions"] for item in results)
    expected = sum(item["expected_transitions"] for item in results)
    false = sum(item["false_transitions"] for item in results)
    wrong = sum(item["wrong_state_ms"] for item in results)
    latencies = [value["maximum_ms"] for item in results for value in item["latency_intervals"]]
    processing = [value for item in results for value in item["processing_ms"]]
    confusion = {}
    observable_by_class = {}
    for item in results:
        for label, duration in item["observable_by_class_ms"].items():
            observable_by_class[label] = observable_by_class.get(label, 0) + duration
        for actual, row in item["confusion_ms"].items():
            target = confusion.setdefault(actual, {})
            for predicted, duration in row.items():
                target[predicted] = target.get(predicted, 0) + duration
    return {
        "sequences": len(results), "sessions": len({(item["source_id"], item["session_id"]) for item in results}),
        "observable_ms": observable, "conclusive_ms": conclusive, "wrong_state_ms": wrong,
        "coverage": conclusive / observable if observable else None,
        "error_fraction_of_conclusive_time": wrong / conclusive if conclusive else None,
        "expected_transitions": expected, "matched_transitions": matched, "false_transitions": false,
        "transition_recall": matched / expected if expected else None,
        "transition_precision": matched / (matched + false) if matched + false else None,
        "false_transitions_per_hour": false * 3_600_000 / observable if observable else None,
        "unobservable_claim_ms": sum(item["unobservable_claim_ms"] for item in results),
        "confusion_ms": confusion,
        "by_class": {label: {
            "observable_ms": duration, "conclusive_ms": sum(confusion.get(label, {}).values()),
            "wrong_state_ms": sum(value for predicted, value in confusion.get(label, {}).items() if predicted != label),
            "coverage": sum(confusion.get(label, {}).values()) / duration,
        } for label, duration in observable_by_class.items()},
        "transition_latency_upper_p95_ms": float(np.percentile(latencies, 95)) if latencies else None,
        "processing_p95_ms": float(np.percentile(processing, 95)) if processing else None,
        "processing_p99_ms": float(np.percentile(processing, 99)) if processing else None,
    }


def run_benchmark(*, protocol_path: Path, corpus_path: Path, asset_root: Path, model_path: Path,
                  partition: str, calibration_report: Path | None = None) -> dict:
    if partition not in {"calibration", "test"}:
        raise ValueError("benchmark partition must be calibration or test")
    protocol = BenchmarkProtocol.model_validate_json(protocol_path.read_text())
    protocol_hash = digest_file(protocol_path)
    engine_hash = engine_digest()
    if digest_file(corpus_path) != protocol.corpus_sha256 or digest_file(model_path) != protocol.model_manifest_sha256:
        raise ValueError("corpus or model manifest changed after protocol freeze")
    calibration_hash = None
    if partition == "test":
        if calibration_report is None:
            raise ValueError("test requires a completed calibration report")
        calibration = json.loads(calibration_report.read_text())
        if (calibration.get("status") != "completed" or calibration.get("partition") != "calibration"
                or calibration.get("protocol_sha256") != protocol_hash or calibration.get("engine_sha256") != engine_hash):
            raise ValueError("test policy must match its completed calibration protocol")
        calibration_hash = digest_file(calibration_report)
    corpus = load_corpus(corpus_path, asset_root=asset_root)
    config = protocol.configuration
    config.validate_activation()
    if config.strategy != "examples":
        raise ValueError("this benchmark measures local examples only")
    manifest = ModelManifest.model_validate_json(model_path.read_text()).bind_source_path(model_path)
    if config.model_id != manifest.model_id:
        raise ValueError("protocol model identity does not match manifest")
    sequences = {item.sequence_id: item for item in corpus.sequences}
    if any(item.sequence_id not in sequences for item in protocol.references):
        raise ValueError("reference sequence is absent from corpus")
    expected_references = {reference: item.class_id for item in config.classes for reference in item.reference_ids}
    if (len({item.reference_id for item in protocol.references}) != len(protocol.references)
            or set(expected_references) != {item.reference_id for item in protocol.references}):
        raise ValueError("reference selection must exactly match configured references")
    selected = [item for item in corpus.sequences if item.partition == partition
                and item.source_id == config.source_identity]
    if not selected:
        raise ValueError("partition has no sequences for the configured source")
    for sequence in [*selected, *(sequences[item.sequence_id] for item in protocol.references)]:
        if sequence.source_id != config.source_identity or sequence.preparation_revision != config.preparation_revision:
            raise ValueError("sequence source or preparation differs from protocol")
    for selection in protocol.references:
        if sequences[selection.sequence_id].partition != "references":
            raise ValueError("reference selection cannot use calibration or test data")
    started = time.perf_counter()
    backend = build_embedding_backend(manifest)
    creation_ms = (time.perf_counter() - started) * 1000
    try:
        if backend.revision != config.model_revision:
            raise ValueError("protocol embedding revision differs from loaded model")
        references, reference_durations = {}, []
        for sequence_id in dict.fromkeys(item.sequence_id for item in protocol.references):
            sequence = sequences[sequence_id]
            frames, timestamps = load_frames(sequence, asset_root)
            for selection in (item for item in protocol.references if item.sequence_id == sequence_id):
                if selection.frame_index >= len(frames):
                    raise ValueError("reference frame is absent")
                observed = int(timestamps[selection.frame_index])
                label = next(item.class_id for item in sequence.truth if item.start_ms <= observed < item.end_ms)
                if label != expected_references[selection.reference_id]:
                    raise ValueError("reference does not match independently annotated class")
                started = time.perf_counter()
                vector = backend.embed(frames[selection.frame_index])
                reference_durations.append((time.perf_counter() - started) * 1000)
                references[selection.reference_id] = ReferenceEmbedding(label, backend.revision,
                    sequence.preparation_revision, vector)
        results = [_replay(sequence, *load_frames(sequence, asset_root), config, references, backend)
                   for sequence in selected]
        return {
            "status": "completed", "partition": partition, "protocol_sha256": protocol_hash,
            "engine_sha256": engine_hash,
            "calibration_report_sha256": calibration_hash, "corpus_sha256": protocol.corpus_sha256,
            "model_manifest_sha256": protocol.model_manifest_sha256, "artifact_sha256": manifest.sha256,
            "model_revision": backend.revision, "providers": backend.providers,
            "platform": platform.platform(), "session_creation_ms": creation_ms,
            "versions": {name: metadata.version(name) for name in ("numpy", "onnxruntime")},
            "license": manifest.license.model_dump(), "input_precision": manifest.input.dtype,
            "reference_processing_ms": reference_durations,
            "evidence_kinds": sorted({item.evidence for item in selected}),
            "sequences": results, "aggregate": aggregate_results(results),
            "by_illumination": {light: aggregate_results([item for item in results if item["illumination"] == light])
                                for light in sorted({item["illumination"] for item in results})},
            "quality_approved": False,
            "limitations": ["Offline decision replay; not the live pipeline runtime or physical capture latency",
                "No geometry quality oracle: unobservable ground truth only measures false claims",
                "Synthetic sequences cannot qualify semantic accuracy; report completion is not approval",
                "Transition intervals reflect annotation uncertainty; no independent-session confidence interval yet"],
        }
    finally:
        backend.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("protocol", "corpus", "assets", "model", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--partition", choices=["calibration", "test"], required=True)
    parser.add_argument("--calibration-report", type=Path)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("output already exists; preserve previous evidence")
    result = run_benchmark(protocol_path=arguments.protocol, corpus_path=arguments.corpus,
        asset_root=arguments.assets, model_path=arguments.model, partition=arguments.partition,
        calibration_report=arguments.calibration_report)
    with arguments.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
