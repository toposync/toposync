# Processing Server (Development)

This guide explains how to install dependencies and run the Toposync processing server in development mode.

The processing server is used by distributed pipelines. It receives pipeline config from the origin server, runs processing-side nodes, and streams projected events back to origin.

## Prerequisites

- Python `3.11+`
- `uv`
- Optional for camera pipelines: `ffmpeg` available in `PATH`
- Optional for YOLO operators: install the `cameras-yolo` dependency group

Node.js is not required to run the processing server itself. It is only needed if you also run the frontend UI.

## 1) Install dependencies

From the repository root:

```bash
uv sync
```

If you need YOLO operators (`vision.object_detection_yolo`, `vision.object_tracking_yolo`):

```bash
uv sync --group cameras-yolo
```

`uv sync` is exact and removes packages that are outside selected groups. If you want to keep YOLO installed, keep using `--group cameras-yolo` on later syncs.

## 2) Run the processing server

```bash
uv run toposync processing-serve --host 0.0.0.0 --port 9001 --data-dir .toposync-data
```

Default command values are `--host 127.0.0.1` and `--port 9001`.

## 3) Verify it is online

In another terminal:

```bash
curl http://127.0.0.1:9001/api/processing/status
```

You should get a JSON response. `active: false` is expected until the origin server pushes a pipeline config.

## 4) Optional basic auth

The processing server can require HTTP Basic Auth:

```bash
TOPOSYNC_PROCESSING_USERNAME=dev \
TOPOSYNC_PROCESSING_PASSWORD=devpass \
uv run toposync processing-serve --host 0.0.0.0 --port 9001 --data-dir .toposync-data
```

Quick check:

```bash
curl -u dev:devpass http://127.0.0.1:9001/api/processing/status
```

## 5) Connect it to an origin server (distributed mode)

Start the origin backend:

```bash
uv run toposync serve --host 0.0.0.0 --port 8000 --data-dir .toposync-data
```

Register the processing server in origin:

```bash
curl -X PUT http://127.0.0.1:8000/api/processing-servers/dev_remote \
  -H 'content-type: application/json' \
  -d '{"id":"dev_remote","name":"Dev Remote","kind":"http","url":"http://127.0.0.1:9001","username":"","password":""}'
```

Then assign your final pipeline to `processing_server_id: "dev_remote"` (via UI or `PUT /api/pipelines/{pipeline_name}`).

Connection test through origin:

```bash
curl http://127.0.0.1:8000/api/processing-servers/dev_remote/status
```

## 6) Common issues

- `401 Unauthorized`: `TOPOSYNC_PROCESSING_USERNAME`/`TOPOSYNC_PROCESSING_PASSWORD` do not match the credentials saved in origin.
- Processing server always idle: pipeline `processing_server_id` is still `"local"`.
- Connection refused from origin: processing server is bound to `127.0.0.1` instead of a reachable interface (`0.0.0.0`).
- YOLO operators unavailable: dependencies were installed without `--group cameras-yolo`.
