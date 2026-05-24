# Toposync Cameras extension

First-party extension focused on camera integration for the global Pipelines runtime.

## What it provides

- RTSP camera settings and indexing (`/api/cameras/index`)
- RTSP snapshot endpoints used by UI/tools
- Visual calibration and projection mapping endpoints for camera/composition interpolation
- Camera pipeline operators registry integration
- Camera element/editor UI in the composition

The old per-camera detections runtime (`/api/cameras/detections/*`, `cameras.tracking`, and `toposync_ext_cameras.processor_server`) is no longer part of this extension.

## APIs

- `GET /api/cameras/index`
- `POST /api/cameras/rtsp/snapshot`
- `GET /api/cameras/cameras/{camera_id}/snapshot`
- `POST /api/cameras/projection/map`
- `POST /api/cameras/control_points/map`

`camera.camera_mapping` and the editor use `calibrated_views` as the canonical mapping model:

- fixed camera: one default calibrated view with `pose_reference = null`
- PTZ camera: one or more calibrated views bound to `pose_reference` or presets
- projection model: `image_quad_on_world`, with an image region and a world-space quadrilateral
- stream scope: `main` and `sub` are compatible by default; `zoom` needs an explicit calibrated view/scope
- mapping runtime: derives internal control-point pairs from calibrated views so pipeline outputs keep `payload.world`

The legacy `control_points` mapping endpoint remains as a low-level diagnostic adapter. New UI and extension code should use calibrated views and `/api/cameras/projection/map`.

## Pipeline operators (registered by this extension)

- `camera.source`
- `camera.motion_gate`
- `camera.camera_mapping`
- `camera.area_restriction`
- `camera.velocity_estimation`

Public vision operators are registered by the `com.toposync.vision` extension:

- `vision.detect`
- `vision.track`
- `vision.crop_objects`

Legacy vendor-specific YOLO/Ultralytics runtimes are not part of the official first-party path in this extension. If you need one of those integrations in the future, ship it as a separate package and keep `vision.detect` / `vision.track` as the public operator contract.

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
