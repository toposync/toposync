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

Reports account for every input frame, including frames left queued when the last
inference reaches or exceeds the replay duration. `input_frames` equals
`inference_samples + discarded_frames`; `inference_samples` equals
`observations + late_results`. Late results are measured for processing cost but
cannot produce observations after the replay ends. These counts are also summed
in the aggregate and illumination reports. `missed_transitions` keeps transitions
without a recognized result in the denominator. Transition latency percentiles
describe matched transitions only; always read them together with recall and
missed transitions. They do not establish an all-events latency target.

`status: completed` means the experiment ran, while `quality_approved` remains
false. Synthetic sequences only exercise infrastructure. Reports include the
session denominator and annotation latency intervals; confidence intervals over
independent real sessions and qualification criteria require a separate review.

## Runtime capacity measurement

`tests/test_visual_state_capacity.py` measures the actual visual-state operator,
reference store, embedding backend, shared scheduler, bounded input and temporal
confirmation with a wall clock. Its inputs are synthetic red/blue images matching
the reference images. This isolates execution cost; it cannot approve semantic
accuracy or any field installation. The normal test run skips model execution.

Freeze the model manifest, artifact hash, test/runtime source hashes and experiment
settings in an external record before running. Use an already verified local
`image_embedding` model; this test never downloads weights. For example:

```sh
TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS=CPUExecutionProvider \
VISUAL_STATE_CAPACITY_MODEL=/path/model.json \
VISUAL_STATE_CAPACITY_OUTPUT=/path/new-report-directory \
uv run --no-sync pytest -q tests/test_visual_state_capacity.py \
  --basetemp /path/new-temporary-data-directory
```

Create the report directory first. Both output and temporary data must be outside
the repository. Use a new temporary directory: pytest replaces `--basetemp` before
the test's guards run. Existing reports are rejected. One, four and eight sensors share
two scheduler threads; each receives ten frames per second over thirteen states
lasting 2.25 seconds each. Initialization is separate from the twelve subsequent
transitions. Preparation timings include the fixture's model probe, reference
creation and runtime initialization; they are not isolated session-load timings.
The backend's configured inference threads and effective providers must also be
recorded; scheduler threads do not replace those settings.

For an isolated thread comparison, set `VISUAL_STATE_CAPACITY_INTRA_THREADS` to
`1`, `2` or `4`. The test intercepts session construction only inside its own
process, overrides that option and still executes the native product backend.
It does not change application configuration. Reports read back actual session
thread counts and retain publication traces and input-channel counters for
independent latency and loss checks. Run each comparison in a fresh process and
freeze its order before measuring; a faster isolated inference need not improve
multiple sensors sharing the scheduler.

The fixture defaults to thresholds calibrated for its small color model, not for
arbitrary embeddings. To measure a different model, first evaluate only the two
reference images and freeze an explicit comparison policy in a JSON file. Pass
its path in `VISUAL_STATE_CAPACITY_COMPARISON`. The file contains
`enter_threshold`, `enter_margin`, `hold_threshold` and `hold_margin`; the same
product validation applies. Preserve failed experiments before changing that
policy. Same-image reference calibration enables a timing exercise only; it does
not calibrate quality on independent images or authorize relaxing quality gates.

Each report includes missed transitions, publication latency from first received
frame, input depth, telemetry, source scheduling delays, skipped source slots,
CPU time and process peak resident memory. Peak memory covers the process lifetime
and may include a preceding parameterized case. No all-transition percentile is
reported if any transition was missed. Matched-only percentiles always retain the
miss count beside them. Twelve samples per sensor cannot certify rare tail events;
the host may also be running unrelated work. Neither camera acquisition nor Home
Assistant transport is included. Passing pytest proves the measurement completed
and resources drained, not that its latency or accuracy targets were met.
