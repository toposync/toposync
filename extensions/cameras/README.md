# Toposync Cameras extension

First-party extension focused on camera integration for the global Pipelines runtime.

## What it provides

- RTSP camera settings and indexing (`/api/cameras/index`)
- RTSP snapshot endpoints used by UI/tools
- Ground-plane calibration and projection mapping endpoints for camera/composition correspondence
- Automatic fixed-zoom PTZ panorama capture, guided correspondences, independent camera checks and click-to-aim
- Camera pipeline operators registry integration
- Camera element/editor UI in the composition

The old per-camera detections runtime (`/api/cameras/detections/*`, `cameras.tracking`, and `toposync_ext_cameras.processor_server`) is no longer part of this extension.

## APIs

- `GET /api/cameras/index`
- `POST /api/cameras/rtsp/snapshot`
- `GET /api/cameras/cameras/{camera_id}/snapshot`
- `POST /api/cameras/projection/map`
- `POST /api/cameras/projection/solve`

For static and preset-based views, `camera.camera_mapping` and the editor use `calibrated_views` as the canonical mapping model:

- fixed camera: one default calibrated view with `pose_reference = null`
- PTZ camera: one or more calibrated views bound to a numeric `pose_reference`; presets move the head but are not geometric proof
- preferred projection model: `camera_ray_ground_v2`, with six fit and two independent check correspondences on a bounded ground plane
- legacy projection model: `image_quad_on_world`; retained for existing configurations, not used by the new editor
- stream scope: `main` and `sub` are compatible by default; `zoom` needs an explicit calibrated view/scope
- mapping runtime: caches the solved mapper; it maps individual points only, returns `mapping.status=unmapped` outside the proven ground polygon, and never warps full frames
- mapped tracking presets place `camera.camera_mapping` before `vision.track`, allowing `byte_world` to use per-detection `world_anchor` as a probabilistic association signal.

`/api/cameras/projection/map` is the public point-query endpoint. `/api/cameras/projection/solve`
validates a v2 draft without persisting it. UI and extension
code should use calibrated views; runtime control-point pairs are internal
projection details derived from that canonical model.

### Automatic source panoramas and preserved PTZ mapping

Camera settings now offer **Generate panorama** on each stream, with no lens,
angle or scan fields. The server discovers capabilities, observes movement and
stability, explores both horizontal directions and the available vertical bands,
then estimates a visual panorama from overlapping photographs. Presentation
orientation is checked from confirmed horizontal movements. The user can save a
useful rectangle, including across the horizontal joining edge, without
recapturing or moving the camera. An incomplete result stays partial.

The source resource lives at
`/api/cameras/cameras/{camera_id}/sources/{source_id}/panorama`; its reference is
stored in `source.metadata.panorama` and files are private under
`runtime/cameras/source-panorama/`. Reconstruction does not activate floor-plan
geometry or validate arbitrary PTZ pointing. See the
[implementation record](../../docs/panoramica-automatica-implementacao.md).

The camera editor preserves access to existing panorama mappings and drafts.
Their assistant links six ground places and two independent checks, verifies
camera pointing, then activates the result. These older mappings retain their
verified optical profile and device-position-to-angle conversion; normalized
ONVIF values are not angles. The manual optical form has been removed.

The authenticated endpoint family is
`/api/cameras/cameras/{camera_id}/panorama`: preflight and create at the base,
job readback and images, capture/cancel, points, check/check-result, activation,
aim and restoration of the previous valid mapping. Mutations use revision and
camera ownership checks. No motion resumes automatically after process restart.

The canonical active reference is the composition camera element's
`props.panorama_mapping`. Job images and evidence remain under the configured
data directory's `runtime/cameras/panorama/`. The runtime resolves this reference
through `cameras.panorama.get_active` and uses each frame's safe numeric pose.
It rejects changed zoom/source/image geometry and points outside the supported
ground polygon. Calibrated views continue to serve existing 360 projection and
Semantic PTZ attention; panoramic mapping does not redefine those consumers.

See [implementation and validation](../../docs/ptz-panorama-mapping.md) for the
profile contract, geometry, recovery, automated tests and physical validation.

## Camera pipeline presets

Preset graphs should keep product identity on `payload.subject.id`. Raw tracker
ids are diagnostic only.

- `people_simple`: no mapping requirement, for a first people-detection smoke test.
- `people_individual`: requires mapping, emits individual `event` subjects.
- `people_quiet`: requires mapping, groups activity with `vision.group_events` in `session` mode.
- `presence_area`: requires mapping, groups mapped presence with `vision.group_events` in `proximity` mode.
- `vehicle_stopped`: requires mapping, estimates velocity before notification.

## Pipeline operators (registered by this extension)

- `camera.source`
- `camera.motion_gate`
- `camera.camera_mapping`
- `camera.area_restriction`
- `camera.velocity_estimation`

Public vision operators are registered by the `com.toposync.vision` extension:

- `vision.detect`
- `vision.track`
- `vision.group_events`
- `vision.crop_objects`

Legacy vendor-specific YOLO/Ultralytics runtimes are not part of the official first-party path in this extension. If you need one of those integrations in the future, ship it as a separate package and keep `vision.detect` / `vision.track` / `vision.group_events` as the public operator contract.

## Dependencies

- `ffmpeg` must be available in `PATH` for snapshot capture.
- OpenCV is required by frame/motion processing:
  - `uv pip install opencv-python-headless`
- Vision runtimes are optional and can be installed from `extensions/vision`.

## Snapshot tuning

- `TOPOSYNC_CAMERA_SNAPSHOT_TTL_S` (default: `0.8`)
- `TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY` (default: `2`)

## RTSP note

Some cameras expose `/stream1` and `/stream2`. If `/stream1` is unstable for snapshots, configure `rtsp_url` with the substream (`/stream2`) explicitly.
