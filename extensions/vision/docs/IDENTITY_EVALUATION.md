# Evaluating local identity recognition

The offline evaluator at `scripts/identity/evaluate.py` runs installed models and the actual gallery resolver. It does not create a calibrated policy, select a coverage target, download a dataset, certify consent or approve a release. Operational pipeline latency and clustering require separate experiments.

Use consented or otherwise appropriate data with reviewed source terms. Keep images, pseudonymous identity labels, the manifest and generated galleries outside Git. A valid manifest is not evidence that the declared rights or independent collection are true.

## Freeze before the final run

Separate enrollment, development and holdout by independently collected sessions. Record how sessions were separated and audit near duplicates, reencoded copies and adjacent footage manually. The validator rejects repeated sessions within a profile, a session crossing splits, and an identical image hash appearing in separate visits. These checks cannot establish real-world independence by themselves.

Calibrate thresholds on development visits, including unknown individuals. Before running holdout, commit the selected policy, nominal eligibility rule, justified coverage target and exact manifest hash to an external evaluation record. A supplied hash only checks immutability against that record; it cannot prove when the record was created or prevent someone from selecting a favorable holdout later.

A protocol contains:

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `dataset_authorization` | Authorized scope of local processing and its basis |
| `source_and_license` | Data origin, applicable terms and review evidence |
| `independence_method` | How independent sessions/visits were collected and audited |
| `profiles` | One entry per species and evaluated operating condition |
| `visits` | One independent visit per session/profile, each with one to sixteen photos |

Each profile specifies `id`, `species`, catalog `model_id`, optional `face_detector_model_id`, a complete `RecognitionPolicy` in `policy`, `nominal_coverage_target`, `nominal_eligibility_rule` and `coverage_justification`. There is deliberately no default nominal coverage target. The policy's exact embedding space must match the installed model and preprocessing. This requirement applies even if automatic decisions are disabled; such a run cannot demonstrate useful automatic recognition.

Each visit specifies a unique `id`, `session`, `split` (`enrollment`, `development`, `holdout`), `profile`, pseudonymous `identity_label`, predeclared boolean `nominal`, and `photos`. Each photo specifies its relative `path`, exact `sha256` and optional normalized `region` (default whole image). An unknown individual has a label absent from enrollment for that profile. Failed enrollment remains a known individual in evaluation, rather than becoming a convenient unknown.

Every profile needs nominal known visits and unknown visits in both development and holdout. People, cats and dogs require separate profiles and evidence. Missing species are not implicitly validated. The runner rejects more than 10,000 total enrollment/evaluation photos per profile/run, matching the real gallery capacity. It does not increase product capacity to fit an experiment.

## Run in an isolated environment

From a source checkout with the project environment installed:

```sh
uv run python scripts/identity/evaluate.py \
  --protocol /absolute/private/protocol.json \
  --expected-protocol-sha256 THE_EXTERNALLY_FROZEN_SHA256 \
  --data-root /absolute/private/photos \
  --output /absolute/private/new-validation-directory \
  --validate-only

uv run python scripts/identity/evaluate.py \
  --protocol /absolute/private/protocol.json \
  --expected-protocol-sha256 THE_EXTERNALLY_FROZEN_SHA256 \
  --data-root /absolute/private/photos \
  --model-data-dir /absolute/path/to/installed-model-data \
  --output /absolute/private/new-holdout-directory \
  --split holdout
```

Use a new output directory for every attempt. The runner rechecks photo hashes before inference, applies EXIF orientation and RGB conversion, and limits input to twelve million pixels. Enrollment photos can become confirmed references only inside this disposable evaluation gallery. Development and holdout each start from enrollment, and predictions never become references. No pipeline configuration is activated or modified.

Outputs include the exact protocol, implementation hashes, environment versions, model space/hash, per-visit outcomes, retained encrypted enrollment data and `result.json`. The gallery key is included in the output directory; protect and retain the entire directory according to the data authorization. An interrupted attempt remains separate and is not a successful result.

## Interpret the report

The statistical unit is the visit. A wrong accepted identity at any point counts against that visit, even if it is later corrected. A known visit is counted correct only if it has at least one accepted identity and every accepted identity is correct. Repeated photos do not increase the number of independent trials.

The report separates all visits from the nominal subset declared before inference. It shows unknown false identifications, known identification failures, accepted precision, nominal correct coverage, accepted identity switches, usable evidence and technical unavailability. Inference-time usability is an outcome, never a reason to retrospectively exclude an error. Operational false-identification rates include unavailable and unobservable cases; a low operational rate alone can reflect a system that rarely processes unknowns successfully.

Unknown-error uncertainty uses a one-sided 95% exact binomial upper bound by inversion, conditional on independent identically distributed visits within the profile. See the [NIST description of exact binomial confidence limits](https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm). Zero observed errors do not imply zero risk, and no observations produce an unavailable bound. Repeated looks, dependent visits and model selection on holdout invalidate a naive interpretation of that bound.

`arithmetic_checks_only` compares the total-unknown upper bound with 0.1% and nominal-known coverage with the frozen target. It is not an aggregate quality verdict. `release_gate_evaluated` is always false. Technical failures are explicit even when arithmetic checks happen to pass: `query_evaluation_status` describes evaluation queries, and separate enrollment counts record unusable or technically unavailable enrollment visits. Review all denominators, enrollment failures, accepted errors and availability alongside the external collection audit.

Separate checks remain mandatory for spatial consistency, clustering purity/false merges, contamination after feedback, full pipeline resource usage, equivalent-load latency and the complete product acceptance gates. Processing time in this report includes offline preprocessing and gallery work; it is not the pipeline p95 comparison.
