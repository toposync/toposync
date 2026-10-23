# Toposync Vision extension

First-party extension focused on public task-oriented vision operators for the Pipelines runtime.

## What it provides

- `vision.detect`
- `vision.track`
- `vision.associate_entities` (manual multicamera continuity; [configuration and review](docs/MULTICAMERA_CONTINUITY.md))
- `vision.group_events`
- `vision.spatial_relation_event` confirms proximity after observed dwell. A valid matched pair stays stable when another member moves closer. Replacing either participant closes the previous occurrence and starts a new dwell; time without a new observation cannot confirm a candidate. These events describe proximity, not intent or recognition across separate visits.
- `vision.identity_evidence` and `vision.recognize_identity` (explicit activation; [setup, curation and recovery](docs/IDENTITY_RECOGNITION.md))
- `vision.crop_objects`
- `vision.segment_instances`
- `vision.pose_estimate` (experimental MediaPipe Pose 33 and RTMPose-M Halpe26 backends)
- `vision.gesture_recognize` (experimental temporal body gestures)
- `vision.visual_state` (experimental local comparison with private reference images)

For visual sensors, see the [Portuguese configuration and recovery guide](../../docs/sensores-visuais/guia.md)
and [installation validation protocol](../../docs/sensores-visuais/validacao-de-instalacao.md).
They distinguish local software checks from authorized field observation and physical qualification.

The public surface is task-based, not vendor-based. The official first-party runtime is ONNX Runtime, with CPU as the default execution path.

For spatial relations, `dwell_seconds` accumulates intervals between consecutive positive observations. Loss of evidence pauses this duration; recovery preserves the occurrence within its grace period without adding the missing interval. Closing grace and source timeouts never extend the observed duration. This remains an estimate from observation cadence, not proof of continuous physical presence between frames.

Spatial relation state is isolated by source, camera and group. Replayed terminal
groups cannot open another occurrence. Group producers attach a runtime epoch and
monotonic lifecycle sequence, allowing the shared terminal-subject ledger to compact
closed groups without dropping replay protection after 4,096 occurrences. A new
producer epoch can reuse a closed identifier; this does not establish person identity
across sessions or qualify recovery when the previous group never closed.

Proximity observations require valid, recent capture evidence. Dwell follows media
time rather than processing delay; same-frame member updates may update geometry
but cannot add elapsed time. Older capture generations/sequences are rejected.
A capture reset closes the old relation and starts fresh confirmation; a late close
from the old generation cannot terminate the new relation. Publication freshness
does not prove physical camera latency or synchronized remote clocks.

## Visual reference API

The visual sensor foundation exposes `/api/vision/visual-state/drafts` for creating and reading draft class configurations. `PUT /drafts/{draft_id}` requires the current `expected_revision`; removing a reference from the configuration is a logical removal, and reattaching its identifier supports undo while the draft remains valid. Drafts expire after one hour. The experimental operator has a class gallery; local activation also requires an explicit image embedding manifest, its verified revision, and calibrated comparison parameters. No model or accuracy threshold is selected implicitly. Multimodal fallback and direct analysis require a compatible AI profile, explicit image permissions and available budgets; latency and physical accuracy still require qualification.

## Visual state consumers

`GET /api/vision/visual-state/states` reads the current local runtimes without capturing
images, running inference or renewing observations. It requires extension access
(`core:extension:use`, `com.toposync.vision`) and filters each sensor using
`core:pipeline_results:read` on the `core:pipeline_result` selector
`visual_state:<sensor_id>`. Existing result revocations also apply. A service account
can receive individual state grants without access to reference images or pipeline
configuration. No grants are added automatically.

The private, non-cacheable response contains `schema_version: 1`, `server_epoch`,
an increasing `snapshot_version`, `capabilities` (`local_states: true`,
`remote_states: false`) and `states`. Each entry identifies the sensor, pipeline and
node, lists class identifiers/keys/labels/enabled flags, and contains a `state`.
The state includes runtime epoch/version, status, current class identifier/key,
reason, observation and confirmation times, remaining `valid_for_ms`, sample
identity, method, freshness basis and preparation/reference/model revisions.
It excludes pixels, reference identifiers, descriptions and provider responses.

Remaining validity is calculated from the runtime's monotonic clock on every read.
An expired observation becomes unknown with no current class; a stopped or disabled
pipeline is unavailable. A changed configuration or upstream preparation cannot
expose the previous runtime's value. Duplicate sensor identities produce one
unavailable entry instead of selecting a pipeline. Removed nodes disappear from the
inventory. Remote pipelines are explicitly unavailable with
`remote_server_unsupported`; this endpoint does not proxy them. Previews never register
as published sensors. Non-running entries have null runtime epoch/version.

Consumers must reject reordered snapshots, reset on a changed server/runtime epoch,
subtract request duration from the remaining validity, and expire locally even if
the next request never completes. Repeating a snapshot must never extend its sample's
deadline. Absence, unknown and unavailable must not be converted to a negative class.
The Home Assistant client reports a missing endpoint (HTTP 404/405) as unsupported,
distinct from an empty inventory, authentication failure or transient server failure.
Entity coordination and receiver expiration are separate integration requirements;
this API alone does not certify them or qualify alarm use.

Reference capture uses the host preview endpoint with `purpose: "reference"`. It accepts a restricted local camera path, preserves privacy controls, and returns PNG pixels, capture evidence and an opaque receipt. The receipt binds the exact bytes, authenticated user and preparation to the server process for 60 seconds. Restarting the process invalidates pending receipts. Receipt timing does not certify physical camera exposure or PTZ pose.

Upload the unmodified PNG to `POST /drafts/{draft_id}/captures` with query parameters `class_id`, `operation_id` and `expected_revision`, and the `X-Toposync-Capture-Receipt` header. Retry with the same operation identifier to recover an accepted write without duplicating the reference. A conflict requires refreshing the draft; an expired receipt requires recapture. Uploads are limited to 8 MiB, two in flight and a 15-second body deadline.

All reference routes require the installation owner, extension access and pipeline read/write permission as appropriate. Owners share the installation reference store; these routes do not grant member access through camera-specific permissions. Images live under the private `visual-references` data directory, outside generic `/files` serving. `GET /drafts/{draft_id}/references/{reference_id}/image` only serves references linked to the active draft, defaults to a 160-pixel thumbnail, and sends `Cache-Control: private, no-store`; `thumbnail=false` returns the normalized original.

### Persistence

Saving a pipeline reserves its visual-state references before the configuration
file is written, including disabled pipelines and bulk configuration saves.
Changed sensor configurations must reference intact images with compatible
classes, source and preparation. A failed write preserves both the previous and
newly reserved images.

Duplicating a pipeline creates a new `sensor_id` for every visual sensor and
disables both the copied pipeline and its Home Assistant publication. Classes,
descriptions, reference images, region and explicit binary mappings are preserved;
the original pipeline is unchanged. Review the copy before enabling it. Reference
sharing stays within the installation owner's existing authorization: copying a
pipeline does not grant access to private images or drafts.

Python copies append explicit `configure_node(node_id, **patch)` calls so later
compilation and saving retain their new identities. Patches merge nested objects;
lists and scalars replace their previous values. They apply in source order after
the pipeline expression is built and undergo operator configuration validation.
Configure stream splitting in the operator expression, since edge defaults are
already chosen. Keep node identifiers stable, or update these calls along with
their `_id` expressions. For identifiers based on `PIPELINE_NAME`, use the same
expression in `configure_node` before another rename or duplication. A missing
node or a shadowed helper rejects duplication rather than saving a broken copy.
To enable publication in a Python copy, edit its final publication override too.

Extension authors can register the generic `duplicate_config` callback to return
a recursive configuration patch. It receives an isolated copy of the node's
configuration and must not perform side effects. Pipelines containing such an
operator are duplicated disabled; operators without this callback retain the
existing duplication behavior. The core does not know visual sensor identities.

#### Sensor backup API

`GET /api/vision/visual-state/drafts/{draft_id}/backup?expected_revision=...`
downloads the exact draft revision as a private ZIP attachment. Synchronize the
draft first; an outdated revision is rejected. The package contains versioned
sensor configuration, confirmed reference metadata and PNG images with SHA-256
hashes. It excludes pipeline/camera connection settings, provider credentials,
model weights, vector caches, pending suggestions and current runtime values.
Class descriptions and images are included; treat the downloaded file as private.

`POST /api/vision/visual-state/backups/restore?operation_id=...` accepts
`Content-Type: application/zip` and returns a new draft. Retry an uncertain request
with the same operation identifier and identical bytes to recover that draft;
using the identifier for different content is rejected. Idempotency survives
restart and lasts as long as the draft. Restoration preserves sensor/class/image
identities, changes the reference revision and disables Home Assistant publication.
It never edits or enables a pipeline. Review and explicitly save the draft through
the usual pipeline revision contract. Models, profiles and source/preparation
compatibility remain activation requirements; an absent model does not prevent
recovering the reference images themselves. Old live state is never restored.

The package limit is 64 MiB, with at most 300 reference images, a 4 MiB manifest
and 64 million image pixels in total (64 × 1024 × 1024). Individual image and
installation storage limits still apply. Upload has a 30-second body deadline.
One backup transfer runs at a time; another returns HTTP429 after a bounded wait.
A cancelled request retains that slot until its storage worker exits. Both routes
require the existing owner authorization; responses use private, no-store caching.

Restore validates the complete archive in a private temporary directory before
writing reference metadata. Only uncompressed regular ZIP members with exact
manifest paths are accepted; paths are never extracted. Unknown versions, extra
members, missing images, hash/dimension mismatches and conflicting reference
identities are rejected. Image writes and the new draft are coordinated under the
reference transaction; failures during image or draft writing roll back newly
written images and metadata. Matching metadata with missing or corrupt files can
be repaired by a valid backup. A failed database commit or abrupt process/power
loss can leave unreferenced files for the existing collector; it cannot expose a
partially committed draft. Exported packages are independent of later reference retention.

The operator panel exposes **Backup and restore** with download, file selection,
class/photo preview and explicit application. Importing alone preserves the
current editor and saved pipeline. Applying changes the editor; save the pipeline
separately. Undo restores the complete prior operator configuration until another
edit or leaving the operator. A cancelled transfer can be retried with the same
operation identifier while the panel stays open; a late response cannot apply to
another selected operator or changed input. Photos and transfers preserve ingress
paths. Pending unapplied settings must be resolved before using these controls.

Restoration preserves the sensor identity. Saving that identity into a second
pipeline is rejected; restore in place or remove the original before recreating
it elsewhere. Use pipeline duplication to create an independent sensor. Existing
malformed configurations loaded from disk can still be opened for repair without
blocking unrelated edits; changing or enabling a conflicting sensor requires
resolving its duplicate identity. Enabling a saved pipeline rechecks every
configured reference even when its operator settings did not change. Missing or
corrupt images reject the save without changing the previously saved pipeline.
Disabling a damaged sensor remains possible.

For a rollback in the same installation:

1. Disable and save the existing pipeline. Confirm that the receiver is unavailable
   before replacing its configuration. Merely importing a backup does not stop the
   current runtime.
2. Restore the backup into that sensor's editor, review its classes and references,
   and save while the pipeline remains disabled. The backup contains the operator
   configuration only; restore any changed upstream camera/preparation settings
   separately and verify that they match the captured references.
3. Verify the required model revision and compatible source. Starting the restored
   pipeline creates a new runtime epoch and requires observations made after that
   execution initialized. Buffered images from before initialization cannot vote,
   even when they are younger than the normal maximum frame age.
4. Obtain the configured number of distinct new observations. Home Assistant
   publication stays disabled after restoration until explicitly configured again.
   Restoring a working revision does not restore a previous qualification decision.

`tests/test_visual_state_qualification.py` exercises this sequence with a compiled
pipeline, real ONNX execution of a synthetic model, private backup storage, state
publication and the receiver's validation logic. It verifies preserved references,
new epochs, rejection of buffered observations and fresh confirmation. It does not
exercise physical devices or independently qualify image classification quality.

#### Local qualification records

The owner-only `GET /api/vision/visual-state/sensors/{sensor_id}/qualification`
returns the current context revision and paginated evidence records (`limit` 1–50,
default 20). A null context means that a unique saved local sensor, compatible
preparation, intact references or the declared local model could not be verified.
The check reads and hashes model bytes without running inference or contacting a
camera or provider. Use it when reviewing evidence, not as a frequent polling API.

The operator's collapsed **Validation records** group reads this history on demand.
It shows the query time and explicitly refers to the saved sensor, excluding unsaved
editor changes. Editing configuration or unapplied parameters hides the earlier
result; save and query again. A query is a snapshot, not a live operational badge.
The panel distinguishes favorable and unfavorable reports, suspended records and
superseded history. Expand a record for its reviewer and report/protocol/environment
hashes. Pagination, cancellation and ingress use the existing host controls.
Unsupported hosts explain the missing capability; malformed responses never show
an accepted result. Recording a reviewed report remains an explicit API operation.

The response also includes `operational_suspension_reasons`, separate from immutable
report history. Existing normalized cadence diagnostics suspend the operational
indication for incompatible or unverified input/analysis cadence. The live state
projection adds suspension when execution is unavailable, observation is unconfirmed,
or its monotonic lease shows an observation gap beyond the configured maximum.
A closed or silent source cannot remain operationally qualified merely because its
saved configuration still matches a report. The panel shows this suspension at query
time without changing the graph, starting capture or renewing any observation.
Empty reasons mean no blocker was observed by these checks; they do not establish
qualification or authorize activation. Older hosts without this field remain explicitly
unassessed. Report attestations can still be recorded while a pipeline is disabled,
and favorable historical reports remain visible alongside operational suspension.

After independently reviewing a local report, `POST` to the same path with:

```json
{
  "operation_id": "review-unique-identifier",
  "context_revision": "<64 lowercase hexadecimal characters from GET>",
  "scope": "laboratory",
  "decision": "passed",
  "report_sha256": "<SHA-256 of the reviewed report>",
  "protocol_sha256": "<SHA-256 of the frozen protocol>",
  "environment_sha256": "<SHA-256 of the environment and data manifest>"
}
```

Scopes are `laboratory`, `replay` and `isolated_consumer`; decisions are `passed`
or `failed`. The environment manifest should identify code revision, hardware,
effective provider, software versions and corpus partitions. Keep those files
outside the repository as appropriate for their private contents. The server
stores hashes, the authenticated actor and its own timestamp; it does not read
those external files or verify the reviewer's conclusions. A hash identifies a
report, not its correctness. Never submit an approval based only on a model's
stated confidence or on matching configuration.

Records are immutable. Retry an uncertain submission with the same operation
identifier and identical fields. This recovers the original record even if its
context has since changed; the returned history still marks it suspended. Reusing
the identifier with another actor, sensor or content fails. A new decision uses a
new operation identifier. The newest record for each scope supersedes earlier
ones, including when the newest decision is a failure.

`recorded_pass` and `recorded_failure` mean the latest owner attestation matches
the checked configuration and resources. `suspended` requires a new review;
`superseded` preserves prior decisions for audit. The initial implementation
conservatively suspends on any installation configuration save, even an unrelated
edit, and on reference/model file replacement or repair. Restoring identical
configuration or bytes does not silently renew the old indication. Responses
always include `physical_qualification: false` and `activation_authorized: false`.
These records neither activate publication nor attest current camera availability,
effective runtime provider, hardware compatibility, latency or physical safety.
Review those external conditions again after software/environment changes.

History is bounded to 1000 records per installation by default; reaching the limit
rejects new records and preserves the audit trail. Requests share the bounded
backup transfer slot and return private, no-store responses. Concurrent saves
reject stale submissions or suspend the resulting report. Qualification records
are not included in a sensor ZIP backup and are never imported as an approval.
The reference database schema is now version 5; versions 1–4 upgrade in place.
Older code rejects this schema. For a downgrade, preserve the whole data directory
and use a compatible earlier data backup or restore sensor ZIPs into a separate
clean installation; do not edit the database version number manually.

Pipeline list/read/create/update responses include a content-based `revision`.
Send that unchanged with `PUT /api/pipelines/{name}`. Visual operators opt into
the generic `edit_precondition` capability: missing revisions return HTTP428;
stale revisions return HTTP409 `pipeline_revision_conflict`, including when
removing the operator. The comparison is checked again under the configuration
write lock. Revisions are response metadata, not persisted configuration fields;
external file edits change them, while unrelated settings do not.

The editor preserves a rejected draft and offers loading the saved version.
Its recovery text remains available to copy while reviewing the current version;
it is cleared after a successful save. It is held in the open page, so copy it
before closing or reloading that page. Saving temporarily disables editing and
uses bounded requests. A timeout does not prove the server rejected the write:
read the saved version before retrying. Clients editing ordinary legacy pipelines
may omit revisions; supplied revisions are always checked.

Startup and external configuration reload protect available references even when
a restoration is incomplete. This does not validate or repair missing images;
reopening an incomplete gallery reports an error. Expired drafts can be collected
without deleting saved references. After a verified save or reload, superseded
pipeline references (including deleted pipelines and abandoned failed saves)
remain recoverable for seven days from reconciliation. Reconciliation never
deletes image files; the bounded collector releases them after that period only
when no current pipeline, explicit pin, live draft or suggestion still uses them.
Shared images remain until their last reference is released. Repeated reloads do
not extend the recovery period. Images continue to count toward storage limits
while retained. Export a sensor backup for recovery beyond this period; an old
pipeline JSON alone does not contain its images.

Resource reconciliation failures keep reservations and are retried on a later
configuration load after a 30-second backoff, or after another pipeline change.
They are logged without reporting an already committed save as rejected.
Cancellation waits for reconciliation to finish before allowing another write.
An existing private reference database is reconciled on startup even if all
sensors were deleted before a previous interrupted cleanup. Reference database
versions 1–3 upgrade to version 4 without dropping drafts, images or restore
operation identifiers; the exported sensor package format remains version 1.

Runtime initialization also checks images and the pinned model before publishing
state, including after an external configuration reload. Missing or changed
weights prevent initialization; restoring the compatible model preserves sensor
identity and references but requires fresh temporal confirmation. A previous
confirmed value is not restored as current state.

### Visual sensor telemetry

The existing authenticated numeric telemetry endpoint exposes these series for
the visual operator node. Use `metric_id=visual_state.duration.<name>_ms` at
`GET /api/pipelines/{pipeline_name}/telemetry/numeric?node_id={node_id}&metric_id=...`.
The normal ingress prefix and pipeline read permission apply.

| Duration name | Measured interval |
| --- | --- |
| `acquisition` | Verified physical capture to local receipt; absent when that evidence is unavailable |
| `input_age` | Known capture/receipt timestamp to admission, including upstream delay |
| `admission` | Validation and immutable image copy for accepted frames |
| `preparation` | Canonical region preparation and its evidence checks |
| `local_wait` | Admitted frame waiting in the operator's latest-frame slot |
| `inference_queue` | Native inference submission to entry into the protected model call |
| `inference` | The local model call itself, excluding its queue |
| `comparison` | Local reference comparison |
| `ai_wait` | Prepared frame waiting for the multimodal worker |
| `ai_roundtrip` | Multimodal router call, including policy checks, provider queues and attempts |
| `confirmation` | First supporting observation in the winning sequence to the decision that confirms it |
| `publication` | Local pipeline emit call; does not measure Home Assistant receipt or physical actuation |
| `evidence_age` | Age of the last completed analysis when publishing |

These intervals overlap; do not add them to claim total end-to-end latency.
Receipt-based freshness cannot establish physical acquisition latency. A native
call cancelled before completion has no completed inference-duration sample.
Duration histograms use 100 bins over 0–10,000ms; longer values occupy the last
bin. Raw bucket counts, sums and extrema remain available, but histogram
percentiles are approximate and cannot certify sub-bin latency targets.

`visual_state.events.<name>` records received, replayed and discarded inputs;
unknown acquisition time; replaced, invalidated, expired and cancelled local/AI
work; queued AI work, router requests and reported attempts; AI failures/limits;
and state confirmation, expiration and unavailability transitions. Use the sum of
event values within a time window, not sample count, for totals such as attempts.
Expiry and confirmation are not counted again on every status publication.
Drops before admission remain in the existing input-channel statistics.

`visual_state.reason.<reason>` counts fixed, known abstention/unavailability causes;
unknown provider reasons map to `other`. Series dimensions are only the existing
pipeline and node plus a fixed metric name. No class descriptions, response text,
image, reference identifier, frame identity or provider/model name is added.
The existing bounded telemetry store, retention and eviction limits still apply;
these are windowed diagnostics, not durable lifetime accounting. Diagnostics
failures do not change the classification or publication.

### Local execution

The operator currently accepts a saved local pipeline with one supported camera
preparation path. Its preparation revision must match the references. Input pixels
must have current capture identity and matching image geometry; packet lifecycle
`OPEN` and `CLOSE` do not identify physical classes. Source/capture changes restart
confirmation, and every runtime restart begins without a current value.
`input_before_start` means an observation predates this execution's initialization;
the operator waits for a newer image. Verified capture time takes precedence over
receipt time, so later delivery cannot make an older exposure eligible.

One native inference and one latest pending image are retained per sensor. The
default evaluation interval is 250 milliseconds; the state is checked every
50 milliseconds independently of inference. Output is `payload.visual_state`,
without image artifacts, and includes status, current class or null, evidence
revisions, capture identity and remaining validity. Consumers must honor that
validity; publication time cannot renew old evidence. Native work can finish after
cancellation, but retains its execution slot until then and cannot publish a late
result. A blocked native library cannot be forcibly stopped by a Python thread.

ONNX Runtime platform telemetry is disabled before discovery or session creation
through the shared loader. Application diagnostics remain separate from this
SDK telemetry setting.

These mechanisms and synthetic tests do not qualify accuracy, physical capture
latency, PTZ alignment or safety of an alarm or gate. The configuration workflow,
multimodal strategies and native Home Assistant publication are implemented;
representative quality, performance and installation qualification remain pending.

## Dependencies

- The default `toposync` application bundle includes the first-party ONNX Runtime CPU stack.
- The GPU-oriented first-party bundles are published separately:
  - `toposync-vision-cuda`
  - `toposync-vision-directml`
- The official extension package now also includes the first-party tracker stack:
  - `byte_world`
  - `simple_iou_kalman`
  - `norfair`

## Notes

- `vision.detect` resolves `ModelManifest` entries and builds an ONNX Runtime backend automatically when `runtime=onnxruntime`.
- By default, the ONNX Runtime backend prefers `CPUExecutionProvider`. Optional acceleration is opt-in via the `toposync-vision-cuda` / `toposync-vision-directml` bundles or `TOPOSYNC_VISION_ONNXRUNTIME_PROVIDERS`.
- Manifest files can be loaded from `TOPOSYNC_VISION_MANIFESTS_DIR` or `TOPOSYNC_VISION_MANIFEST_PATHS`.
- Custom manifests imported from the UI are persisted under `.toposync-data/vision-manifests/` for the selected processing server.
- Built-in first-party manifests ship inside the wheel, but their ONNX weights do not. When no checkout-local artifact exists, official model ids resolve to `TOPOSYNC_DATA_DIR/vision-models/...` (or `.toposync-data/vision-models/...` by default).
- The extension now ships a built-in RTMDet detection shortlist in `extensions/vision/manifests/`:
  - `rtmdet_det_tiny`
  - `rtmdet_det_small`
  - `rtmdet_det_medium`
- The extension now also ships a built-in RF-DETR detection shortlist in `extensions/vision/manifests/`:
  - `rfdetr_det_nano`
  - `rfdetr_det_small`
  - `rfdetr_det_medium`
- In a source checkout, existing local artifacts under `extensions/vision/models/...` are still honored.
- In installed environments, official artifacts belong under the managed model store in `TOPOSYNC_DATA_DIR/vision-models/...` and are intentionally not bundled in the published package.
- The validated manual provisioning flow is documented in `docs/VISION_MODEL_PROVISIONING.md`.
- The initial assisted-provisioning foundation for RTMDet detection is already exposed in catalog metadata:
  - upstream checkpoint/config/metafile/paper links
  - planned local builder backend
  - planned supported platforms
  - explicit-consent requirement
- The UI can also trigger installation for models that have an admin-configured source. Supported source env vars are:
  - `TOPOSYNC_VISION_MODEL_SOURCE_<MODEL_ID>`
  - `TOPOSYNC_VISION_MODEL_URL_<MODEL_ID>`
  - `TOPOSYNC_VISION_MODEL_PATH_<MODEL_ID>`
  - `TOPOSYNC_VISION_OFFICIAL_MODEL_SOURCE_DIR`
  - `TOPOSYNC_VISION_OFFICIAL_MODEL_BASE_URL`
- RTMDet detection now also has an experimental assisted local build path:
  - Linux only in this phase
  - requires a local container runtime (`docker` or `podman`) on the processing server
  - still downloads the upstream checkpoint directly on that machine
  - still validates the exported `end2end.onnx` against the manifest checksum
  - now records provenance per job, including actor, accepted upstream sources, builder metadata, and final ONNX sha256
  - still keeps manual upload as the stable fallback path
  - can be started from the model recovery card or the Processing Servers screen
- RF-DETR detection now also has an experimental assisted local build path:
  - supports `linux`, `darwin`, and `windows` hosts in this phase
  - uses a host Python builder (`rfdetr[onnx]`) instead of MMDeploy
  - downloads the upstream checkpoint directly on that machine before exporting the ONNX locally
  - keeps manual ONNX upload available as the stable fallback path
  - is prioritized in the operator UI when nothing is installed and the local builder is actually available on that machine
- Remote download sources are only enabled when the manifest explicitly allows redistribution. The current built-in RTMDet/RTMDet-Ins manifests do not, so the safe first-party flow is local admin-managed copy.
- Product policy: RTMDet and RTMDet-Ins stay on `guided_upload` for now. RF-DETR is available only through assisted local build in this phase; it is not mirrored, bundled, or redistributed by Toposync.
- Product policy: Ultralytics/YOLO is not part of the official first-party vision runtime path.
- The extension now also ships a built-in RTMDet-Ins segmentation shortlist:
  - `rtmdet_ins_tiny`
  - `rtmdet_ins_small`
  - `rtmdet_ins_medium`
- RTMDet manifests use the dedicated `mmdet_rtmdet` parser, letterbox preprocessing, and COCO-80 labels.
- RF-DETR manifests use the dedicated `rfdetr_detr` parser and the official DETR-style `dets` + `labels` ONNX outputs.
- RTMDet-Ins manifests use the dedicated `mmdet_rtmdet_ins` parser and produce real binary mask artifacts.
- The processing server status now exposes:
  - heuristic hardware recommendations
  - runtime upgrade suggestions for CUDA / DirectML bundles
  - a per-task model catalog with availability (`available`, `manifest_only`, `incompatible`)
  - installation capability and progress for models that can be fetched/copied automatically
  - badges such as `recommended`, `fastest`, `best_quality`, `edge`
- `vision.detect` can emit finite per-detection events (`emit_mode="events"`), filter the stream to packets that contain detections (`emit_mode="filter"`), or keep every frame annotated (`emit_mode="annotate"`).
- `vision.track` is now first-party and detector-agnostic: it consumes `payload["vision"]["detections"]`.
- Every `TrackedObject` now carries `camera_id`, and can optionally carry `world_anchor` plus `appearance_embedding_artifact_name` for future multi-camera association work.
- `vision.segment_instances` writes `payload["vision"]["segmentations"]`, attaches mask artifacts when enabled, and exposes the top mask as the semantic image key `mask`.
- `vision.pose_estimate` consumes existing person detections, runs the selected pose model, and publishes named, unclipped landmarks. The `mediapipe_pose_33` manifest provisions a pinned ONNX artifact on the selected processing server.
- `rtmpose_halpe26` is an experimental 2D alternative with an explicit Halpe26 skeleton. Its guided flow requires extracting `end2end.onnx` from the pinned upstream ZIP and uploading the ONNX, not the archive. The upload validates the pinned checksum and backend contract before replacing an existing artifact. Toposync does not bundle, mirror or automatically download these weights.
- RTMPose preserves raw SimCC scores, including values above one; they are not calibrated probabilities. The legacy pose-level score is the source detection score, explicitly identified in metadata, because this model does not output global pose confidence. Halpe26 does not supply relative 3D or a world pointing direction.
- Pose annotations retain their originating detection through `vision.track`; subject-specific processing does not rerun all people. Legacy keypoints remain available.
- `vision.gesture_recognize` adds time-based hand-raised, both-hands-raised, waving and pointing-candidate states. A two-dimensional pointing candidate is not a three-dimensional target or authorization to act.
- Setup, payload semantics, camera geometry requirements and experimental limitations are documented in [Pose, feet and gestures](../../docs/pose-gestos.md). These capabilities are not qualified for unattended physical actions.
- `ModelManifest` now also accepts optional `capabilities` such as `reid`, so future re-identification models can be cataloged without changing the registry shape.
- The pipeline editor now chooses models by task, not framework/vendor id. Basic setup is guided for common users, while advanced details expose runtime/model internals and custom manifest import when needed.
- The first-party tracking backends are:
  - `byte_world` (primary): ByteTrack-style high/low confidence association with optional world-anchor matching.
  - `simple_iou_kalman`
  - `norfair`
- `simple_iou_kalman` and `norfair` remain useful benchmark baselines, but camera presets and recipes should use `byte_world`.
- `vision.track` emits stable per-object lifecycle packets directly. The product identity is `payload["subject"]["id"]`; technical tracker details stay available as `tracklet_id`, `tracklet_ids`, `raw_tracking_id`, and `identity_id`.
- Frame annotations use `payload["vision"]["detections"]`, `payload["vision"]["tracks"]`, and `payload["vision"]["segmentations"]`. Consumers that need one visual subject from an annotated frame should prefer tracks, then detections, then segmentations.
- `vision.group_events` consumes individual `subject.type="event"` packets and emits aggregated `subject.type="group_event"` packets with `group_event_id`, `member_event_ids`, `category_summary`, and grouped `subject.bbox01` for quieter storage and notifications.
  `group_observation.image_coherent` is true only when every declared member is active and shares the output image's capture, pixel geometry and visual localization. Otherwise the display box combines member history, not simultaneous observed extent; aggregate mapping omits single-frame pose evidence while each member retains its own mapping and image provenance. World-anchor aggregation remains separate from image coherence.
- A member CLOSE publishes a group UPDATE with that member inactive immediately, bypassing the periodic update interval. It does not close the group session; consumers can end relations without waiting for group idle timeout. Repeated member CLOSE packets do not publish another update.
- Spatial relations expose `visits` for the currently followed pair: confirmed approaches after observed separation, within `visit_window_seconds` (default 300). Initial proximity is not an observed entry. Membership loss, capture reset, participant change and timeout clear history; short departures within closing grace remain one occurrence. Each group retains at most 256 entries and reports a lower bound on overflow. This does not identify people across tracking sessions or infer intent. New person/vehicle presets include repeated approaches in their notification description; existing custom descriptions remain unchanged.
- Spatial relations require fresh capture evidence from each matched member in the same capture generation. Group publication does not renew member evidence. Dwell advances using the older member media timestamp and pauses when either member is unavailable; this estimates observed continuity and does not certify what happened between frames.
- Group identifiers and streams include the producer lifecycle epoch. A restarted producer creates independent groups even when its display counter starts at 1; it does not inherit proximity duration or assert continuity with the previous producer. Older groups close independently through their lifecycle or the consumer stale timeout. Consumers must treat identifiers as opaque.
- `vision.detect` events are short OPEN/CLOSE notifications; use `vision.track` when you need temporal identity, movement, and long-lived per-object lifecycle.
- `vision.crop_objects` supports `preserve_scene: true` to retain the selected image while attaching the projected subject context; it avoids a second image copy and keeps references outside the subject box visible. Default behavior still crops the subject.
- Object crops derived from `subject.bbox01` carry `subject_context` with subject identity, original and crop-relative bounding boxes, and capture evidence. Crop-relative coordinates use the actual pixel boundaries after rounding, reusing the crop/warp projection path. Detection fallback does not claim subject binding.
- `vision.crop_objects` crops the bbox from `payload["subject"]["bbox01"]` by default and preserves event/subject identifiers. It is not instance segmentation.

## Experimental visual history backend

Automatic installation requests also verify the checksum of an existing artifact before reporting it ready. This runs in the cancellable installation job, without copying or downloading the file again. A mismatch preserves the existing bytes and requires an explicit reinstall; pose models additionally retain their runtime validation.

The AI history endpoint accepts `mode=visual` for multilingual text-to-image ranking of visible notification images. It reuses notification authorization, time filters and SQLite. Similarity does not confirm identity or trigger automation. The notification filter offers words, text meaning and images. The four-file model set is available to the existing model installer as `clip_multilingual_history`; the simplified setup action in the search interface is still pending.

The Vision extension declares the tokenizer and safetensors reader dependencies. Its model manifest groups four pinned artifacts under the installation's `vision-models` directory. Explicit installation uses one cancellable job, verifies every file and reuses intact local artifacts. A failed transfer keeps earlier verified files for a subsequent attempt. Catalog availability requires all four checksums; search never downloads models. The image encoder is [Xenova CLIP ViT-B/32](https://huggingface.co/Xenova/clip-vit-base-patch32/tree/d15189d7028b43f1d3e65039190477f6af591c2a) and the aligned text encoder is [Sentence Transformers multilingual CLIP](https://huggingface.co/sentence-transformers/clip-ViT-B-32-multilingual-v1/tree/58edf8cada9e398793dca955574a48cbb7f18be2). The current quantized text export was validated on ARM64 CPU; other platforms need their own validation.

Search indexes up to eight images per request from the latest 250 matching notifications, with a 64 MiB image scan budget and two inference threads. Sessions are released after each request. The response reports incomplete indexing, unavailable images, scope truncation and fallback to literal text when the optional backend is unavailable. Changed or deleted evidence is rechecked before returning cached results. Model checksums and preprocessing version isolate the vector space. This bounded backend does not certify retrieval accuracy, all historical records, or unattended safety use.

## Future runtime compatibility

- Pipeline configs must stay task-oriented and model-oriented. Do not add runtime, device, delegate, or vendor-specific toggles such as `use_coral` to `vision.detect`, `vision.classify_image`, or related operators.
- New inference stacks should be introduced as optional runtime backends plus `ModelManifest` entries. The manifest owns `runtime`, `artifact_format`, `input.dtype`, hardware accelerators, acquisition metadata, and provenance.
- Do not reuse an existing `model_id` for a materially different artifact/runtime. For example, an Edge TPU/TFLite artifact should get its own model id rather than replacing an ONNX model id.
- Runtime-specific upload, build, and install flows should remain disabled with clear diagnostics until the backend and artifact validation are implemented.
