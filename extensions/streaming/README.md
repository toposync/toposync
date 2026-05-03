# Toposync Streaming Extension

Extension ID: `com.toposync.streaming`

This extension provides "pipeline-rendered streaming" in Toposync:

- Users define **Transmissions** (domain objects) with one or more **Outputs**.
- Pipelines publish frames to a Transmission via the sink operator **`stream.publish_video`**.
- A local **MediaMTX** engine serves RTSP/HLS/WebRTC (WHEP) URLs.
- FFmpeg publishers (one per output) push encoded video into MediaMTX paths.

The core design goal is reliability in local-first setups with highly dynamic pipelines:

- Pipelines can be intermittent (open/update/close based on detection/tracking).
- Multiple pipelines can write to the same Transmission (multi-writer arbitration).
- Encoding should happen only when there is an actual viewer (on-demand).

## Implemented features (high level)

- MediaMTX engine lifecycle management (start/stop/restart/status) with on-demand binary install (download + SHA256 verification) and dynamic port resolution.
- Transmission and output CRUD stored in extension settings.
- Per-output URLs for RTSP, HLS, and WebRTC/WHEP.
- Pipeline sink operator `stream.publish_video` that publishes frames into a transmission (multi-writer capable).
- Multi-writer arbitration using lifecycle (`open/update/close`), priority, and a sticky window.
- Output-level placeholder frames when no writer is active.
- Output-level resolution and FPS enforcement, including contain resizing with black padding.
- FFmpeg publishers (one per output) that publish RTSP into MediaMTX.
- On-demand encoding based on MediaMTX viewer count, with debounce stop.
- Demand priming to reduce "first connect" 404/no-stream failures.
- Distributed hosting via `Transmission.host_server_id`, plus URL proxying and processing-side settings sync.
- Dashboard playback (Rendering -> Streams) using WebRTC/WHEP with HLS fallback.

## Supported protocols (as implemented)

### RTSP
- URL format: `rtsp://<host>:<rtsp_port>/<path>`
- Best for VLC, ffplay, NVRs, or re-streaming with FFmpeg.
- MediaMTX is configured to accept both UDP and TCP transports. For reliability, prefer TCP on the client.

### HLS
- URL format: `http://<host>:<hls_port>/<path>/index.m3u8`
- Intended as the stable baseline for broad playback compatibility (including future mobile/PiP usage).
- Browser:
  - Safari can play HLS natively.
  - Chrome/Firefox need MSE playback; the Toposync dashboard uses `hls.js`.
- MediaMTX is configured with `hlsVariant: mpegts` (non-LL-HLS) by default.

### WebRTC (WHEP)
- URL format: `http://<host>:<webrtc_port>/<path>/whep`
- Used by the Toposync dashboard for lower latency than HLS.
- Current implementation is LAN-first (HTTP, no TLS termination built in). If you expose streaming beyond LAN, plan to terminate TLS.

## Architecture overview

### Data flow

`pipeline frames` -> `stream.publish_video` -> `TransmissionRuntimeState` -> `StreamWriterBridge` -> `FFmpeg publisher` -> `MediaMTX path` -> viewers (RTSP/HLS/WHEP)

### Components

- `MediaMtxEngineManager`
  - Ensures the correct MediaMTX binary is available for the current OS/arch (downloads on demand when missing).
  - Renders a YAML config and starts/stops/restarts the MediaMTX process.
  - Resolves ports (preferred vs actual) and exposes engine status and test URLs.
- `TransmissionRuntimeState`
  - Stores latest frame per writer and per transmission.
  - Applies lifecycle and multi-writer arbitration to select the active writer.
  - Stores viewer_count per output (fed by the writer bridge).
- `StreamWriterBridge`
  - Periodic "tick loop" that loads streaming settings, ensures engine is running, refreshes viewer counts, starts/stops publishers on-demand, and pushes frames (or placeholders) into publishers.
  - Implements best-effort demand priming and a fallback "synthetic demand" hint.
  - Implements a bypass mode for simple pipelines (publisher pulls camera RTSP directly).
- `PublisherManager`
  - Spawns and supervises FFmpeg processes.
  - Supports rawvideo frames over stdin (`rawvideo_pipe`) or RTSP pull (`rtsp_pull`).
  - Maintains per-output logs and runtime status.

## Domain model and settings

### Transmission

A Transmission is the durable configuration entity. Fields (as implemented by the API model):

- `id`: UUID (generated server-side if omitted).
- `name`: display name.
- `enabled`: if `false`, the runtime ignores it.
- `host_server_id`: `"local"` or a processing server id (distributed hosting).
- `path`: slug used in the MediaMTX URL path.
- `placeholder`: `"gray"` or `"black"`.
- `arbitration`: `"latest"` or `"priority_latest"`.
- `outputs`: list of `TransmissionOutput`.
- `created_at`, `updated_at`: managed by the server on update.

Uniqueness rules:
- `Transmission.id` must be unique.
- `(host_server_id, path)` must be unique.
- Output `id` must be unique inside a transmission.

### TransmissionOutput

Each output is independently configurable:

- `id`: stable output id (important for stable URLs when multiple outputs exist).
- `protocol`: `"hls" | "rtsp" | "webrtc"`.
- `enabled`: output-level enable.
- `resolution`: optional `{ width, height }` (defaults are applied in runtime when missing).
- `fps_limit`: optional integer FPS cap.
- `bitrate_kbps`: optional bitrate target.
- `latency_profile`: `"normal" | "low" | "ultra_low"` (maps to FFmpeg preset/tune behavior).
- `authentication`: optional `{ enabled, username, password }` for read/playback.

Notes:
- Authentication is applied only when `enabled == true` and both `username` and `password` are present (non-empty).
- The API model allows extra fields (`extra="allow"`). Some runtime behavior also reads optional extra fields:
  - `output.path` (override the resolved engine path)
  - `output.resize_mode` (`contain` or `none`; best-effort)

### Engine settings

Stored under the extension settings as `engine`:

- `enabled`: start/stop the engine.
- `expose_to_lan`: bind the engine to `0.0.0.0` (otherwise `127.0.0.1`).
- `preferred_ports`: `{ rtsp, hls, webrtc, api }` (preferred; runtime may change if occupied).
- `mediamtx_version`: selects which MediaMTX version to download/install.
- `webrtc_ice_servers`: optional list of `stun:` / `turn:` / `turns:` URLs for NAT traversal.

Additional WebRTC runtime knobs are available as environment variables for container/add-on deployments:
- `TOPOSYNC_STREAMING_WEBRTC_ADDITIONAL_HOSTS`: comma-separated LAN/public hosts advertised to WebRTC clients.
- `TOPOSYNC_STREAMING_WEBRTC_LOCAL_UDP_ADDRESS`: static ICE UDP bind address, for example `:18762`.
- `TOPOSYNC_STREAMING_WEBRTC_LOCAL_TCP_ADDRESS`: optional static ICE TCP bind address when UDP is blocked.

## Engine distribution (on-demand download)

This repo does not ship MediaMTX binaries. The backend downloads the correct release asset at runtime when the engine is started.

Defaults:
- Install location: `~/.toposync/runtime/streaming/mediamtx/<version>/<platform>/mediamtx(.exe)`
- Source: GitHub Releases (`bluenviron/mediamtx`) using the published `checksums.sha256` for verification.

Environment variables:
- `TOPOSYNC_STREAMING_ENGINE_PATH`: use a pre-installed binary (file or directory containing `mediamtx`).
- `TOPOSYNC_STREAMING_ENGINE_CACHE_DIR`: override the cache root (defaults to `~/.toposync/runtime`).
- `TOPOSYNC_STREAMING_ENGINE_DOWNLOAD_BASE_URL`: mirror the download base URL (must keep the same `{version}/{asset}` layout).

Manual download:
- `POST /api/streams/engine/download` downloads and installs the engine without starting it (useful to avoid surprise downloads).

## URL paths and stability

Each output resolves to a MediaMTX engine path:

1. If `output.path` is set (extra field), it is used.
2. If there is only one enabled output, the path is `transmission.path`.
3. If there are multiple enabled outputs, the default path becomes:

```
{transmission.path}-{output.id}
```

Integration guidance:
- Keep `transmission.path` and `output.id` stable if you plan to integrate external tools (NVRs, mobile apps, dashboards).

## Engine behavior (MediaMTX)

### Default security posture

- Default bind host is localhost only: `127.0.0.1`.
- Publishing is restricted to localhost IPs.
- MediaMTX API access is restricted to localhost IPs.

Read/playback can be open or authenticated per output, depending on `TransmissionOutput.authentication`.

### Ports (preferred vs actual)

Defaults (preferences):
- RTSP: `8554`
- HLS: `8888`
- WebRTC/WHEP: `8889`
- API: `9997`

Also:
- RTP/RTCP UDP pair is auto-managed by the engine manager (defaults to `50000/50001` and auto-picks a free consecutive pair).

Source of truth:
- `GET /api/streams/engine/status` includes the ports the engine is actually using and warnings when remapped.

### Config highlights (as generated)

- `authMethod: internal`
- `api: true` (guarded by localhost IP permissions in `authInternalUsers`)
- `rtsp: true` with `rtspTransports: [udp, tcp]`
- `hls: true` with `hlsVariant: mpegts`
- `webrtc: true` with WHEP enabled at `/<path>/whep`
- `webrtcAdditionalHosts` and static ICE addresses when configured through environment variables.

## Security considerations

- Default bind host is `127.0.0.1` (safe-by-default). Enabling `expose_to_lan` binds MediaMTX to `0.0.0.0`.
- The generated config allows HLS and WebRTC origins with `*` for LAN/dev convenience. If you expose to LAN, consider hardening origins.
- There is no TLS termination built into the streaming engine endpoints in this implementation. If you expose beyond LAN, terminate TLS in front of MediaMTX.
- Output authentication is Basic-style credentials managed by MediaMTX. Some clients require embedding `username:password@` in the URL (notably native HLS playback in browsers), which can leak credentials in logs/history.
- `GET /api/streams/transmissions` and `GET /api/streams/settings` return the full transmission/output objects, including authentication passwords. Protect these endpoints with Toposync platform auth and avoid exposing them to untrusted clients.

## Publisher behavior (FFmpeg)

The streaming runtime starts a publisher per output when demanded.

### Input modes

- `rawvideo_pipe` (default)
  - The writer bridge writes BGR24 frames into FFmpeg stdin.
  - Frame shape must exactly match the output resolution.
- `rtsp_pull` (bypass mode)
  - FFmpeg pulls the camera RTSP URL directly and applies fps + contain scaling/padding.

### Codec and latency

Current default behavior:
- Uses `libx264` (CPU) unless future settings expose hardware selection.
- Applies a GOP close to the configured FPS (keyframe interval ~1s).
- Applies preset/tune based on `latency_profile`.

Debugging knob:
- `TOPOSYNC_STREAMING_FFMPEG_LOGLEVEL` controls FFmpeg loglevel (default: `warning`).

## On-demand encoding

On-demand is enforced per output:

- If `viewer_count > 0`, publishers are started (or kept running).
- If `viewer_count == 0`, publishers stop after a debounce (default: 3s) to avoid flapping.

Viewer count is obtained from MediaMTX API (`/v3/paths/list`) by counting per-path readers.

### Demand priming (important for "first connect" reliability)

Some clients (notably RTSP) can receive "no stream is available" (404) when no publisher exists yet. That can keep `viewer_count` at 0 and prevent on-demand from starting.

This implementation includes:

1. Explicit priming endpoint:
   - `POST /api/streams/transmissions/{transmission_id}/demand/prime`
   - The dashboard calls this before attempting WebRTC/HLS.
   - URL resolution also primes demand best-effort.
2. Synthetic demand hint:
   - The writer bridge scans the MediaMTX log for "no stream is available on path ..." and temporarily treats the path as demanded to let client retries succeed.

Practical tip:
- If ffplay/VLC returns 404 right after a restart, call the prime endpoint (or load URLs in Settings) and retry.

## Multi-writer arbitration

Multiple pipelines can write into the same Transmission.

### Writer identity

Writer id is derived by the operator runtime as:

```
writer_id = "{pipeline_name}:{node_id}"
```

### Eligibility

Writers are eligible only when:
- Lifecycle is `open` or `update`.
- A frame is present.
- The frame is fresh (default freshness window: 2 seconds).

### Arbitration modes

Transmission arbitration is per Transmission:

- `latest`
  - Selects the writer with the most recent frame.
  - `writer_priority` is used only as a tie-breaker.
- `priority_latest`
  - Selects highest `writer_priority`, then most recent frame.

Sticky selection:
- Once selected, the writer is kept for a short sticky window (default 0.5s) if still eligible.

Cardinality limits:
- Max writers per transmission: 32 (oldest writers are evicted with a warning).
- Stale writers are evicted after 30 seconds.

## Placeholder and resizing

### Placeholder

When no eligible writer exists, the bridge publishes a cached placeholder frame at the output resolution.

Modes:
- `gray` (default)
- `black`

### Resizing

The bridge always targets the output resolution. Default behavior is "contain" (letterbox/pillarbox, black padding).

Optional extra field on output:
- `resize_mode: "contain" | "none"`

Current behavior for `none` is best-effort:
- It avoids resizing only when the incoming frame already matches the target.
- Otherwise it falls back to contain to preserve publisher invariants.

## Pipeline operator: `stream.publish_video`

Operator id: `stream.publish_video`

Purpose:
- Writes "latest frame" state into `TransmissionRuntimeState`.
- Does not encode or block the pipeline on I/O.

Config (as implemented):

```json
{
  "transmission_id": "uuid",
  "frame_with_fallback": ["frame", "best_frame", "segmented", "frame_original"],
  "resize_mode": "contain",
  "writer_priority": 0,
  "bypass_mode": "auto"
}
```

Notes:
- `resize_mode` exists in the operator config, but resizing is currently applied by the writer bridge based on output settings.
- Artifact selection:
  - Iterates `frame_with_fallback` and selects the first artifact that contains an image.
  - Supports payload image mapping (`packet.payload.images`) when present.
  - Normalizes frames to `uint8` BGR and contiguous memory.
- Lifecycle handling:
  - On `close`, the writer is marked closed and becomes ineligible for arbitration.

## Wizard: create pipeline from a Transmission

Endpoint:
- `POST /api/streams/wizard/create-pipeline`

Presets (as implemented):
- `simple_stream`
- `motion_gate_stream`
- `detection_stream`
- `tracking_stream`
- `segmentation_stream`

Generated topology (high level):
- Always starts with `camera.source` (with configurable backend).
- Optionally adds `core.fps_reducer`.
- Adds one optional "vision" step depending on preset.
- Ends with `stream.publish_video` configured for the selected transmission.

Critical validation:
- `Transmission.host_server_id` must match the pipeline `processing_server_id` (wizard enforces this).

## Bypass mode (publisher input optimization)

When a pipeline matches a "simple stream" shape, the writer bridge can switch publishers to `rtsp_pull`.

Eligible graph shapes:
- `camera.source` -> `stream.publish_video`
- `camera.source` -> `core.fps_reducer` -> `stream.publish_video`

Config:
- `stream.publish_video.config.bypass_mode`:
  - `auto` (use bypass when eligible)
  - `force_on` (use bypass when eligible; keep trying)
  - `force_off` (never bypass for this writer)

Behavior:
- Publisher FFmpeg reads the camera RTSP URL directly and applies fps + contain scaling/padding.

## Distributed mode (processing servers)

### Transmission.host_server_id

When `host_server_id != "local"`:

- MediaMTX engine runs on the processing server.
- Pipelines that publish to this transmission must run on the same processing server.
- Viewers connect to the processing server host.

### URL resolution proxy

`GET /api/streams/transmissions/{transmission_id}/urls` works for both local and remote transmissions:

- Local: resolves ports locally and returns URLs.
- Remote: calls the processing server internal endpoint and rewrites host based on the processing server URL.

Processing server internal endpoint (used by core proxy):
- `GET /api/streams/internal/transmissions/{transmission_id}/urls`

### Settings sync (processing pulls from core)

Processing servers can periodically pull their filtered streaming settings from core:

- Core endpoint:
  - `GET /api/streams/distributed/settings/{server_id}`
- Processing loop:
  - polls periodically, writes settings into the processing server config store, and applies engine settings.

Environment variables (processing side):
- `TOPOSYNC_ROLE=processing`
- `TOPOSYNC_PROCESSING_SERVER_ID=<server_id>`
- `TOPOSYNC_STREAMING_SYNC_CORE_URL=http://<core_host>:8100` (or `TOPOSYNC_CORE_URL`)
- Optional auth:
  - `TOPOSYNC_STREAMING_SYNC_BEARER_TOKEN`
  - or `TOPOSYNC_STREAMING_SYNC_USERNAME` + `TOPOSYNC_STREAMING_SYNC_PASSWORD`
- Optional tuning:
  - `TOPOSYNC_STREAMING_SYNC_INTERVAL_SECONDS` (default `5`)
  - `TOPOSYNC_STREAMING_SYNC_TIMEOUT_SECONDS` (default `5`)

Core auth (enforced mode):
- The core can accept Basic auth for the distributed settings endpoint by setting:
  - `TOPOSYNC_STREAMING_SYNC_USERNAME`
  - `TOPOSYNC_STREAMING_SYNC_PASSWORD`
- This Basic auth is intentionally scoped to `/api/streams/distributed/settings/*`.

## UI behavior (Settings and Dashboard)

### Settings panel

The extension adds a Streaming panel in Settings where users can:

- Create/edit transmissions and outputs.
- Start/stop the engine.
- Resolve URLs (and best-effort prime demand).
- Create a pipeline from a transmission using the wizard.

### Dashboard (Rendering -> Streams)

The main UI includes a "Streams" rendering mode with:

- Grid modes `1x1` and `2x2` with pagination.
- Auto-hide overlay.
- Playback strategy:
  1. Prime demand for the selected transmission.
  2. Attempt WebRTC/WHEP (low latency).
  3. Fallback to HLS (native HLS when supported, otherwise `hls.js`).

Authentication in browser playback:

- WHEP uses `Authorization: Basic ...` when output auth is enabled.
- HLS:
  - `hls.js` uses Authorization headers for playlist and segments.
  - Native HLS embeds `username:password@` in the URL because `<video>` cannot attach headers.

## WHEP integration notes (as used by this project)

If you build external clients, the dashboard implements a WHEP handshake like:

1. Create `RTCPeerConnection`.
2. Add transceivers:
   - `video` recvonly
   - `audio` recvonly (even when no audio is published)
3. Create offer, setLocalDescription.
4. Wait for ICE gathering to complete (timeout).
5. POST the offer SDP to the WHEP endpoint:
   - `POST http://<host>:<webrtc_port>/<path>/whep`
   - headers:
     - `Accept: application/sdp`
     - `Content-Type: application/sdp`
     - `Authorization: Basic ...` (optional)
   - body:
     - SDP with CRLF line terminators and ending with a trailing CRLF (important for MediaMTX parsing).
6. Read the SDP answer from the response body and setRemoteDescription.
7. If the response includes a `Location` header, store it and call `DELETE` on it to teardown the session when done.

## API reference (implemented)

All endpoints are under `/api/streams`.

### Health
- `GET /api/streams/health`

### Settings (extension storage)
- `GET /api/streams/settings`
- `PATCH /api/streams/settings`

### Engine
- `GET /api/streams/engine/status`
- `POST /api/streams/engine/start`
- `POST /api/streams/engine/stop`
- `POST /api/streams/engine/restart`

### Transmissions CRUD
- `GET /api/streams/transmissions`
- `POST /api/streams/transmissions`
- `PUT /api/streams/transmissions/{transmission_id}`
- `DELETE /api/streams/transmissions/{transmission_id}`

### URL resolution
- `GET /api/streams/transmissions/{transmission_id}/urls` (local or proxy)
- `GET /api/streams/internal/transmissions/{transmission_id}/urls` (only on host server)

### Distributed settings
- `GET /api/streams/distributed/settings/{server_id}`

### Runtime
- `GET /api/streams/runtime/outputs`
- `GET /api/streams/runtime/diagnostics`

### Demand
- `GET /api/streams/transmissions/{transmission_id}/demand`
- `POST /api/streams/transmissions/{transmission_id}/demand/prime`

### Wizard
- `POST /api/streams/wizard/create-pipeline`

### Minimal payload examples

Engine status (`GET /api/streams/engine/status`):

```json
{
  "running": true,
  "pid": 12345,
  "bind_host": "127.0.0.1",
  "ports": { "rtsp": 8554, "hls": 8888, "webrtc": 8889, "api": 9997 },
  "last_error": null,
  "mediamtx_version": "v1.16.2",
  "log_path": "/.../runtime/streaming/logs/mediamtx-YYYYMMDD-HHMMSS.log",
  "test_path": "test",
  "urls": {
    "rtsp_url": "rtsp://127.0.0.1:8554/test",
    "hls_url": "http://127.0.0.1:8888/test/index.m3u8",
    "webrtc_url": "http://127.0.0.1:8889/test/whep"
  },
  "warnings": []
}
```

Transmission URLs (`GET /api/streams/transmissions/{id}/urls`):

```json
{
  "transmission_id": "uuid",
  "engine_running": true,
  "outputs": [
    {
      "output_id": "hls_main",
      "protocol": "hls",
      "resolved_engine_path": "front-door",
      "url": "http://127.0.0.1:8888/front-door/index.m3u8",
      "requires_auth": false,
      "auth_username": null
    }
  ],
  "warnings": []
}
```

Demand (`GET /api/streams/transmissions/{id}/demand`):

```json
{
  "transmission_id": "uuid",
  "demand_signal": true,
  "viewer_count_total": 1,
  "outputs": [
    { "output_id": "hls_main", "output_key": "uuid:hls_main", "viewer_count": 1 }
  ]
}
```

## Practical quickstart (curl + ffplay)

Start the engine:

```bash
curl -X POST http://127.0.0.1:8100/api/streams/engine/start
```

Create a transmission:

```bash
curl -X POST http://127.0.0.1:8100/api/streams/transmissions \
  -H 'content-type: application/json' \
  -d '{
    "name": "Front Door",
    "path": "front-door",
    "host_server_id": "local",
    "enabled": true,
    "outputs": [
      {
        "id": "hls_main",
        "protocol": "hls",
        "enabled": true,
        "resolution": { "width": 1280, "height": 720 },
        "fps_limit": 12,
        "bitrate_kbps": 2500,
        "latency_profile": "normal",
        "authentication": { "enabled": false }
      }
    ]
  }'
```

Resolve URLs:

```bash
curl http://127.0.0.1:8100/api/streams/transmissions/<transmission_id>/urls
```

Prime demand (recommended before external players, especially right after restart):

```bash
curl -X POST http://127.0.0.1:8100/api/streams/transmissions/<transmission_id>/demand/prime
```

Play RTSP (prefer TCP):

```bash
ffplay -rtsp_transport tcp "rtsp://127.0.0.1:8554/front-door"
```

Play HLS:

```bash
ffplay "http://127.0.0.1:8888/front-door/index.m3u8"
```

## LAN access (non-local viewers)

By default the engine binds to `127.0.0.1`, so URLs only work on the same machine.

To enable LAN viewers:

1. Enable LAN exposure in engine settings (`expose_to_lan: true`).
2. Restart the engine.
3. Fetch URLs while calling the API via the LAN hostname/IP you want to advertise.

Notes:
- `GET /api/streams/transmissions/{id}/urls` chooses the host component based on the current request host when `expose_to_lan` is enabled.
- If MediaMTX runs behind Docker/NAT, advertise the reachable LAN host with `TOPOSYNC_STREAMING_WEBRTC_ADDITIONAL_HOSTS` and publish the static ICE UDP port configured by `TOPOSYNC_STREAMING_WEBRTC_LOCAL_UDP_ADDRESS`.
- MediaMTX still restricts publishing and API access to localhost IPs; LAN exposure is for viewer playback only.

## Troubleshooting

### RTSP/HLS returns 404 ("no stream is available")
Likely causes:
- Engine is stopped.
- Transmission/output is disabled.
- On-demand is active and no publisher has been started yet.
- Path mismatch (URL not matching the resolved engine path).

Fix:
- Call `POST /api/streams/transmissions/{id}/demand/prime` and retry.
- Check `GET /api/streams/engine/status`.
- Use `GET /api/streams/runtime/diagnostics` and inspect `engine`, `publisher`, and `runtime_state`.

### RTSP returns 401 Unauthorized
The output requires authentication.

Use credentials in the URL:

```bash
ffplay -rtsp_transport tcp "rtsp://username:password@127.0.0.1:8554/front-door"
```

### WebRTC/WHEP fails in browser (400/404)
Common causes:
- No publisher yet (prime demand and retry).
- Engine ports changed (use engine/status and transmission/urls).
- ICE/NAT issues outside simple LAN (configure STUN/TURN servers).

Debug:
- `GET /api/streams/runtime/diagnostics`
- Check MediaMTX and FFmpeg logs paths from that payload.

### FFmpeg not found
Publishers require `ffmpeg`.

Current behavior:
- Uses `TOPOSYNC_STREAMING_FFMPEG_PATH` when set.
- Falls back to `ffmpeg` from `PATH`.
- Only tries a packaged FFmpeg binary when a custom distribution explicitly ships one.

Install system FFmpeg if needed:
- macOS: `brew install ffmpeg`

## Runtime files, logs, and paths

Under the Toposync data dir (`data_dir`), the extension uses:

- MediaMTX binary cache:
  - `runtime/streaming/mediamtx/<version>/<platform>/mediamtx(.exe)`
- Generated MediaMTX config:
  - `runtime/streaming/mediamtx.yml`
- MediaMTX logs:
  - `runtime/streaming/logs/mediamtx-YYYYMMDD-HHMMSS.log`
- Per-output publisher logs:
  - `runtime/streaming/logs/ffmpeg-<output_key>-YYYYMMDD-HHMMSS.log`
- Publish credential secret (HMAC seed for per-path publish user/pass):
  - `runtime/streaming/publish-secret.key`

Prefer reading these paths via:
- `GET /api/streams/runtime/diagnostics`

## Local development

```bash
uv pip install -e extensions/streaming
npm --workspace @toposync/extension-streaming-ui run build
```

Then run Toposync as usual (see repo `docs/DEVELOPMENT.md`).

## Licensing and packaging notes

- Public wheels do not ship MediaMTX binaries. The extension downloads the correct release asset on demand and caches it under `runtime/streaming/mediamtx/<version>/<platform>/`.
  - License notice: [LICENSE.mediamtx](LICENSE.mediamtx)
- FFmpeg integration expects an external binary by default (`PATH` or `TOPOSYNC_STREAMING_FFMPEG_PATH`). Bundling FFmpeg binaries is optional and must be handled carefully for redistribution.
  - License placeholder: [LICENSE.ffmpeg](LICENSE.ffmpeg)

If you plan to ship FFmpeg binaries, pay attention to LGPL/GPL build flags and codec licensing constraints depending on distribution model.

## Known limitations (current implementation)

- Video-only: audio is not published (`-an` in FFmpeg).
- No built-in TLS for MediaMTX endpoints (LAN-first).
- No Low-Latency HLS by default (to avoid TLS requirements and keep the default simpler).
- Hardware encoding selection exists in code paths but is not exposed as a stable user-facing setting yet.
- On-demand stops publishers, but does not stop pipeline execution; pipeline compute is controlled by pipeline configuration and lifecycle semantics.
