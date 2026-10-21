# Visual state model benchmark

`visual_state_models.py` runs local image embeddings, reference comparison and the
temporal reducer against an explicit offline corpus. It never opens a camera,
downloads weights, tunes thresholds or approves a model for physical use.

Each corpus asset is a NumPy archive with `frames` (`uint8`, BGR, shape
`[count, height, width, 3]`) and `timestamps_ms` (ordered integers). Archives must
contain only these two arrays, at most 256 MiB expanded, and frames must not exceed
four megapixels. Object arrays and oversized declared shapes are rejected before
allocation. Corpus labels, sessions, partitions and asset hashes follow
`tests/visual_state_corpus.py`.

The protocol JSON follows `BenchmarkProtocol`:

- `corpus_sha256`: hash of the complete corpus manifest.
- `model_manifest_sha256`: hash of the model manifest, including its preprocessing.
- `configuration`: a complete `VisualStateConfig` using strategy `examples`, with
  explicit comparison thresholds and the backend's `revision` as `model_revision`.
- `references`: entries with `reference_id`, `sequence_id` and `frame_index`.
  Every configured reference must appear exactly once, belong to the `references`
  partition and match an independently annotated class at that timestamp.

The ONNX image embedding adapter requires postprocess type `image_embedding`, an
explicit `output_name` and `embedding_dimension`, a pinned artifact hash, three
normalization channels and fixed float32 NCHW input. It loads verified bytes into
one persistent session. Its revision covers the artifact, preprocessing and output
contract. It accepts one vector output and returns a normalized, read-only array.
This adapter does not select an official model or replace field calibration.

Run from the repository with already installed dependencies and prepared assets:

```sh
uv run --no-sync python tests/benchmarks/visual_state_models.py \
  --protocol /path/protocol.json --corpus /path/corpus.json \
  --assets /path/assets --model /path/model.json \
  --partition calibration --output /path/calibration-report.json

uv run --no-sync python tests/benchmarks/visual_state_models.py \
  --protocol /path/protocol.json --corpus /path/corpus.json \
  --assets /path/assets --model /path/model.json \
  --partition test --calibration-report /path/calibration-report.json \
  --output /path/test-report.json
```

Store generated reports outside the repository. Existing output files are never
overwritten. Test evaluation requires the same protocol and engine source digest
as its completed calibration report. A source change requires a new calibration
run; preserve rejected reports and explain the change before another evaluation.
Do not use the reserved test data to adjust thresholds.

Coverage and error are weighted by time, with per-class and illumination results.
The preceding class retained during a pending transition still counts as a
published value, including its errors. Silence expires a value at its lease's end.
Slow processing discards intermediate queued frames. Transition latency includes
the measured preprocessing/inference cost and annotation uncertainty, but does
not include physical capture, the live pipeline scheduler or Home Assistant.

`status: completed` means the experiment ran, while `quality_approved` remains
false. Synthetic sequences only exercise infrastructure. Reports include the
session denominator and annotation latency intervals; confidence intervals over
independent real sessions and qualification criteria require a separate review.
