# Toposync Vision extension

First-party extension focused on public task-oriented vision operators for the Pipelines runtime.

## What it provides

- `vision.detect`
- `vision.track`
- `vision.segment_instances`
- `vision.pose_estimate` (skeleton only; not launched yet)

The public surface is task-based, not vendor-based. The first-party detection and segmentation runtimes now use ONNX Runtime by default.

## Dependencies

- The official extension package now includes the first-party ONNX Runtime stack.
- The official extension package now also includes the first-party tracker stack:
  - `simple_iou_kalman`
  - `norfair`

## Notes

- `vision.detect` resolves `ModelManifest` entries and builds an ONNX Runtime backend automatically when `runtime=onnxruntime`.
- Manifest files can be loaded from `TOPOSYNC_VISION_MANIFESTS_DIR` or `TOPOSYNC_VISION_MANIFEST_PATHS`.
- Custom manifests imported from the UI are persisted under `.toposync-data/vision-manifests/` for the selected processing server.
- The extension now ships a built-in RTMDet detection shortlist in `extensions/vision/manifests/`:
  - `rtmdet_det_tiny`
  - `rtmdet_det_small`
  - `rtmdet_det_medium`
- The extension now also ships a built-in RTMDet-Ins segmentation shortlist:
  - `rtmdet_ins_tiny`
  - `rtmdet_ins_small`
  - `rtmdet_ins_medium`
- RTMDet manifests use the dedicated `mmdet_rtmdet` parser, letterbox preprocessing, and COCO-80 labels.
- RTMDet-Ins manifests use the dedicated `mmdet_rtmdet_ins` parser and produce real binary mask artifacts.
- The processing server status now exposes:
  - heuristic hardware recommendations
  - a per-task model catalog with availability (`available`, `manifest_only`, `incompatible`)
  - badges such as `recommended`, `fastest`, `best_quality`, `edge`
- `vision.detect` remains annotate-first in this phase: it writes `payload["vision"]` plus compatibility fields used by downstream camera operators.
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
- `vision.track` supports:
  - `emit_mode="events"` for split-stream lifecycle packets per object
  - `emit_mode="annotate"` for frame passthrough with `payload["vision"]["tracks"]`
- Crop by bbox remains in `com.toposync.cameras` as `camera.object_crop`; it is not instance segmentation.
