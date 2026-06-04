# Toposync Vision extension

First-party extension focused on public task-oriented vision operators for the Pipelines runtime.

## What it provides

- `vision.detect`
- `vision.track`
- `vision.group_events`
- `vision.crop_objects`
- `vision.segment_instances`
- `vision.pose_estimate` (skeleton only; not launched yet)

The public surface is task-based, not vendor-based. The official first-party runtime is ONNX Runtime, with CPU as the default execution path.

## Dependencies

- The default `toposync` application bundle includes the first-party ONNX Runtime CPU stack.
- The GPU-oriented first-party bundles are published separately:
  - `toposync-vision-cuda`
  - `toposync-vision-directml`
- The official extension package now also includes the first-party tracker stack:
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
- `vision.pose_estimate` already reserves the public operator id, config schema, packet contract, and `task=pose` registry path so future pose models can land without breaking the architecture.
- Tracking contracts already carry optional keypoints, so future pose-aware trackers do not require a structural rewrite.
- `ModelManifest` now also accepts optional `capabilities` such as `reid`, so future re-identification models can be cataloged without changing the registry shape.
- The pipeline editor now chooses models by task, not framework/vendor id. Basic setup is guided for common users, while advanced details expose runtime/model internals and custom manifest import when needed.
- The first-party tracking backends are:
  - `simple_iou_kalman`
  - `norfair`
- `vision.track` emits stable per-object lifecycle packets directly. The product identity is `payload["subject"]["id"]`; technical tracker details stay available as `tracklet_id`, `tracklet_ids`, `raw_tracking_id`, and `identity_id`.
- `vision.group_events` consumes individual `subject.type="event"` packets and emits aggregated `subject.type="group_event"` packets with `group_event_id`, `member_event_ids`, `category_summary`, and grouped `subject.bbox01` for quieter storage and notifications.
- `vision.detect` events are short OPEN/CLOSE notifications; use `vision.track` when you need temporal identity, movement, and long-lived per-object lifecycle.
- `vision.crop_objects` crops the bbox from `payload["subject"]["bbox01"]` by default and preserves event/subject identifiers. It is not instance segmentation.

## Future runtime compatibility

- Pipeline configs must stay task-oriented and model-oriented. Do not add runtime, device, delegate, or vendor-specific toggles such as `use_coral` to `vision.detect`, `vision.classify_image`, or related operators.
- New inference stacks should be introduced as optional runtime backends plus `ModelManifest` entries. The manifest owns `runtime`, `artifact_format`, `input.dtype`, hardware accelerators, acquisition metadata, and provenance.
- Do not reuse an existing `model_id` for a materially different artifact/runtime. For example, an Edge TPU/TFLite artifact should get its own model id rather than replacing an ONNX model id.
- Runtime-specific upload, build, and install flows should remain disabled with clear diagnostics until the backend and artifact validation are implemented.
