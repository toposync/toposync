# Toposync Cameras extension

First-party extension focused on camera integration for the global Pipelines runtime.

## What it provides

- RTSP camera settings and indexing (`/api/cameras/index`)
- RTSP snapshot endpoints used by UI/tools
- Control-point-set mapping endpoint for camera/composition interpolation
- Camera/vision pipeline operators registry integration
- Camera element/editor UI in the composition

The old per-camera detections runtime (`/api/cameras/detections/*`, `cameras.tracking`, and `toposync_ext_cameras.processor_server`) is no longer part of this extension.

## APIs

- `GET /api/cameras/index`
- `POST /api/cameras/rtsp/snapshot`
- `GET /api/cameras/cameras/{camera_id}/snapshot`
- `POST /api/cameras/control_points/map`

`camera.camera_mapping` and the editor now use `control_point_sets` as the canonical mapping model:

- fixed camera: one set with `pose_reference = null`
- PTZ camera: one or more sets, optionally bound to a `pose_reference`
- preview API: receives a single `control_point_set` payload and maps `image <-> world`

## Pipeline operators (registered by this extension)

- `camera.source`
- `camera.motion_gate`
- `vision.object_tracking_yolo`
- `vision.object_detection_yolo`
- `camera.object_segmentation`
- `camera.camera_mapping`
- `camera.area_restriction`
- `camera.velocity_estimation`
- `camera.best_frame_selector`

## Dependencies

- `ffmpeg` must be available in `PATH` for snapshot capture.
- OpenCV is required by frame/motion processing:
  - `uv pip install opencv-python-headless`
- YOLO is optional (heavy dependency):
  - `uv pip install -e "extensions/cameras[yolo]"`

## Snapshot tuning

- `TOPOSYNC_CAMERA_SNAPSHOT_TTL_S` (default: `0.8`)
- `TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY` (default: `2`)

## RTSP note

Some cameras expose `/stream1` and `/stream2`. If `/stream1` is unstable for snapshots, configure `rtsp_url` with the substream (`/stream2`) explicitly.
