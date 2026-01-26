# Toposync Cameras extension

First-party extension scaffold for:

- Camera registry (currently RTSP only)
- Processing servers registry (future)
- Snapshot endpoints used by the UI

This extension is local-first: credentials are stored in Toposync `config.json`.

## Processing (local + remote)

### Local (default)

When the Toposync backend starts, this extension spins up a background processing loop:

- Each enabled camera with a configured `rtsp_url` is processed continuously
- Processing keeps running even if no UI is open
- Each camera can set `fps` (default: 5) to limit capture/processing rate
- Implemented detectors:
  - **motion** (frame-diff heuristic)
  - **object** (YOLO tracking, when installed)

Detections are stored in SQLite under the user `data_dir` and are also streamed as SSE.

#### Dependency: OpenCV

Local processing uses OpenCV (`cv2`) to read RTSP streams and run basic detectors.

If you see logs like “OpenCV (cv2) is not installed”, install it in the same environment where Toposync runs:

```bash
uv pip install opencv-python-headless
```

If you prefer the full build (includes GUI components), you can use:

```bash
uv pip install opencv-python
```

Then restart `toposync serve`.

#### Dependency: YOLO (Ultralytics)

Object tracking uses Ultralytics (YOLO + ByteTrack/BOTSort). It is optional because it pulls heavy deps (e.g. Torch).

Install it in the same environment where Toposync runs:

```bash
uv pip install -e "extensions/cameras[yolo]"
```

Or directly:

```bash
uv pip install ultralytics
```

Then restart `toposync serve`. If you run the remote processing server, install it there too.

If installing `torch` fails for your current Python version, the recommended approach is to run the **remote processing server**
in a separate environment/machine with a supported Python + Torch build, and point the camera to that server.

## Snapshots (load shedding)

Both the UI and integrations can request camera snapshots. To avoid hammering devices, Toposync prefers:

1) **Local processing frame** (reuse `FrameGrabber` last frame, no new RTSP connection)
2) **Remote processing server snapshot** (when `processing_server_id` is set)
3) **ffmpeg fallback** (opens RTSP on-demand)

Toposync also applies a short cache + de-duplication lock per camera/URL to absorb bursts (e.g. multiple UI panels requesting the same image).

### Snapshot tuning (env vars)

Backend (`toposync serve`):

- `TOPOSYNC_CAMERA_SNAPSHOT_TTL_S` (default: `0.8`) - cache TTL + request de-dupe window
- `TOPOSYNC_CAMERA_SNAPSHOT_MAX_FRAME_AGE_S` (default: `5.0`) - max acceptable age for a processing frame
- `TOPOSYNC_CAMERA_SNAPSHOT_FFMPEG_CONCURRENCY` (default: `2`) - max parallel ffmpeg snapshot processes
- `TOPOSYNC_CAMERA_REMOTE_SNAPSHOT_TIMEOUT_S` (default: `5.0`) - timeout when fetching snapshots from processing servers

Remote processing server (`toposync_ext_cameras.processor_server`):

- `TOPOSYNC_PROCESSOR_SNAPSHOT_TTL_S` (default: `0.8`) - cache TTL + request de-dupe window
- `TOPOSYNC_PROCESSOR_SNAPSHOT_MAX_FRAME_AGE_S` (default: `5.0`) - max acceptable age for a processing frame

## Tracking + mapping (compositions)

The processor emits **events** that can include:

- `tracking_id`: groups multiple occurrences of the same moving blob/object across frames
- `bbox`: normalized bounding box (`x1,y1,x2,y2` in 0..1)
- `image`: a normalized point used for mapping (`u,v` in 0..1), currently the bbox bottom-center

If a camera is placed in one or more **compositions** with at least 4 control points, Toposync will:

- map `image(u,v)` → `world(x,z)` for **each composition that references the camera**
- persist events with `composition_id` + `world`

This is important when the same physical camera is reused across floors/compositions.

### Remote (processing server)

If a camera is assigned to a **processing server**, Toposync will:

1) POST the camera config + detections to the server (`/api/processor/config`)
2) Subscribe to server events (`/api/processor/detections/stream`)
3) Persist + re-broadcast events locally (`/api/cameras/detections/stream`)

To run a processing server (on another machine):

```bash
uv run python -m toposync_ext_cameras.processor_server --host 0.0.0.0 --port 9001 --data-dir /path/to/camera-processor-data
```

Then, in **Settings → Cameras → Processing servers**, set the server URL to `http://<host>:9001` and pick it in the camera config.

## API

Toposync backend:

- `GET /api/cameras/index`
- `POST /api/cameras/rtsp/snapshot`
- `GET /api/cameras/cameras/{camera_id}/snapshot`
- `GET /api/cameras/detections/recent` (filters: `camera_id`, `composition_id`, `tracking_id`)
- `GET /api/cameras/detections/stream` (SSE)

Remote processing server:

- `GET /api/processor/cameras/{camera_id}/snapshot`
- `POST /api/processor/config`
- `GET /api/processor/detections/recent`
- `GET /api/processor/detections/stream` (SSE)

## Notes (RTSP quirks)

Some cameras (notably TP-Link) expose multiple RTSP endpoints, often:

- `/stream1` (main stream)
- `/stream2` (substream, lower resolution)

If `/stream1` fails with “Operation not permitted” while `/stream2` works, Toposync will try a best-effort fallback to
`/stream2` for snapshots and local processing. For reliability (and to avoid surprises), prefer configuring the camera
RTSP URL explicitly with `/stream2`.
