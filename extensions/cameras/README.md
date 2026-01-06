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
- For now, the only implemented detector is **motion** (frame-diff heuristic)

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

- `GET /api/cameras/index`
- `POST /api/cameras/rtsp/snapshot`
- `GET /api/cameras/cameras/{camera_id}/snapshot`
- `GET /api/cameras/detections/recent`
- `GET /api/cameras/detections/stream` (SSE)
