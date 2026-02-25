# Toposync Streaming Extension

Extension ID: `com.toposync.streaming`

This extension adds streaming infrastructure to Toposync, including transmission management, MediaMTX runtime control, and a pipeline sink operator (`stream.write`) that publishes frames to RTSP/HLS/WebRTC URLs.

## Current stage (Stage 10)

- Extension backend + UI is loaded and visible in Settings.
- Embedded MediaMTX manager (start/stop/restart/status + dynamic ports).
- Transmission/output settings persisted in extension config.
- `stream.write` sink operator is registered and functional.
- Runtime bridge sends pipeline frames to FFmpeg publishers (rawvideo -> RTSP publish -> MediaMTX serve).
- Placeholder frame (gray/black) when no frame is available.
- Resize mode `contain` with black letterbox/pillarbox.
- Output resolution and FPS limit are applied per output.
- Wizard endpoint and UI flow to create a pipeline from a transmission with camera + preset + `stream.write`.
- Multi-writer arbitration (`open/update/close`, sticky window, placeholder fallback).
- On-demand publishing by viewer count (`viewer_count > 0`).
- Distributed host support with `Transmission.host_server_id`.
- URL resolution proxy for remote processing servers.
- Optional processing-side sync loop to pull filtered streaming settings from core.
- Dashboard mode in Main UI (`Renderização -> Streams`) with grid, pagination and auto-hide overlay.
- Browser playback prefers WebRTC (WHEP) when available and falls back automatically to HLS.
- MediaMTX WebRTC/WHEP enabled with optional STUN/TURN ICE servers in engine settings.

## Distributed settings sync auth (core -> processing)

When Toposync auth is running in `enforced` mode, processing servers can pull their filtered streaming settings
from the core using a dedicated service Basic auth on:

- `GET /api/streams/distributed/settings/{server_id}`

Environment variables:

- Core:
  - `TOPOSYNC_STREAMING_SYNC_USERNAME`
  - `TOPOSYNC_STREAMING_SYNC_PASSWORD`
- Processing:
  - `TOPOSYNC_STREAMING_SYNC_CORE_URL` (or `TOPOSYNC_CORE_URL`)
  - `TOPOSYNC_PROCESSING_SERVER_ID`
  - `TOPOSYNC_STREAMING_SYNC_USERNAME`
  - `TOPOSYNC_STREAMING_SYNC_PASSWORD`

Notes:

- This is separate from the processing server HTTP Basic auth used by core->processing proxy calls
  (`ProcessingServer.username/password` in core settings).
- The Basic auth is intentionally scoped to the distributed settings endpoint.

## Streaming flow

`pipeline frame` -> `stream.write` -> `TransmissionRuntimeState` -> `StreamWriterBridge` -> `FFmpeg publisher` -> `MediaMTX path` -> `RTSP/HLS/WebRTC viewers`

## Dependencies

- MediaMTX binaries are bundled (`linux-x64`, `linux-arm64`, `darwin-x64`, `darwin-arm64`, `windows-x64`).
- FFmpeg is used for publishing (pipeline frames or RTSP bypass) and can be resolved from:
  - embedded binary (if you bundle one under `src/toposync_ext_streaming/bin/ffmpeg/<platform>/ffmpeg(.exe)`), or
  - `ffmpeg` available in `PATH` (system install).

## Endpoints

- `GET /api/streams/health`
- `GET /api/streams/settings`
- `PATCH /api/streams/settings`
- `GET /api/streams/engine/status`
- `POST /api/streams/engine/start`
- `POST /api/streams/engine/stop`
- `POST /api/streams/engine/restart`
- `GET /api/streams/transmissions`
- `POST /api/streams/transmissions`
- `PUT /api/streams/transmissions/{transmission_id}`
- `DELETE /api/streams/transmissions/{transmission_id}`
- `GET /api/streams/transmissions/{transmission_id}/urls`
- `GET /api/streams/internal/transmissions/{transmission_id}/urls`
- `GET /api/streams/distributed/settings/{server_id}`
- `POST /api/streams/wizard/create-pipeline`

## Local development

```bash
uv pip install -e extensions/streaming
npm --workspace @toposync/extension-streaming-ui run build
```

## Manual test (Stage 4)

1. Start engine:

```bash
curl -X POST http://127.0.0.1:8100/api/streams/engine/start
```

2. Create a transmission (single HLS output at 640x360):

```bash
curl -X POST http://127.0.0.1:8100/api/streams/transmissions \
  -H 'content-type: application/json' \
  -d '{
    "name": "Demo stream",
    "path": "demo-stream",
    "outputs": [
      { "id": "main_hls", "protocol": "hls", "enabled": true, "resolution": { "width": 640, "height": 360 }, "fps_limit": 12 }
    ]
  }'
```

3. Option A: Create pipeline via wizard (UI)

- Open `Settings -> Transmissões`.
- Click `Criar pipeline com esta transmissão`.
- Choose camera + preset and create.
- Open `Settings -> Pipelines` to review/edit.

4. Option B: Create or edit a pipeline manually to end with `stream.write` targeting the transmission:

```json
{
  "schema_version": 1,
  "nodes": [
    { "id": "source", "operator": "camera.source", "config": { "camera_id": "camera_1" } },
    {
      "id": "stream_sink",
      "operator": "stream.write",
      "config": {
        "transmission_id": "transmission_id_here",
        "input_with_fallback": ["frame", "frame_original"],
        "resize_mode": "contain",
        "writer_priority": 0,
        "bypass_mode": "auto"
      }
    }
  ],
  "edges": [
    {
      "from": { "node": "source", "port": "out" },
      "to": { "node": "stream_sink", "port": "in" },
      "maxsize": 1,
      "drop_policy": "latest_only"
    }
  ]
}
```

5. Resolve playback URLs:

```bash
curl http://127.0.0.1:8100/api/streams/transmissions/<transmission_id>/urls
```

6. Validate playback:

- RTSP: open URL in VLC.
- HLS: open `.../index.m3u8` in Safari or an HLS player.
- WebRTC/WHEP: use dashboard mode (`Renderização -> Streams`) with a transmission that has `protocol: "webrtc"`.

When the pipeline stops sending frames, a cached placeholder frame is published.

## Distributed setup (Stage 7)

- Every transmission has a `host_server_id`.
- `stream.write` publishes only when pipeline `processing_server_id` matches transmission `host_server_id`.
- Core resolves remote transmission URLs by calling the processing server and returning URLs with the processing host.

### Processing server environment (for distributed sync)

- `TOPOSYNC_PROCESSING_SERVER_ID`: processing server id (must match core `processing_servers.id`).
- `TOPOSYNC_STREAMING_SYNC_CORE_URL`: core base URL, for example `http://192.168.1.10:8100`.
- Optional auth for sync:
  - `TOPOSYNC_STREAMING_SYNC_BEARER_TOKEN`
  - or `TOPOSYNC_STREAMING_SYNC_USERNAME` + `TOPOSYNC_STREAMING_SYNC_PASSWORD`
- Optional tuning:
  - `TOPOSYNC_STREAMING_SYNC_INTERVAL_SECONDS` (default `5`)
  - `TOPOSYNC_STREAMING_SYNC_TIMEOUT_SECONDS` (default `5`)
