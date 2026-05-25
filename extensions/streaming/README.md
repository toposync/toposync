# Toposync Streaming Extension

Extension ID: `com.toposync.streaming`

This extension provides camera and pipeline video publication in Toposync:

- Users normally publish **camera sources** with a `Transmitir esta fonte` intent.
- The extension reconciles generated **CameraLiveView**, **Transmission**, **Outputs**, and implicit continuous pipelines.
- Advanced pipelines can publish a rendered variant through **`stream.publish_video`**.
- A local **MediaMTX** engine serves RTSP/HLS/WebRTC (WHEP) URLs.
- FFmpeg publishers (one per output) push encoded video into MediaMTX paths.

The core design goal is reliability in local-first setups with highly dynamic pipelines:

- Regular camera playback should work after adding a camera, without manually creating transmissions or pipelines.
- Generated camera pipelines must be continuous, so a video stream is not hidden behind motion/event gates.
- Advanced pipelines can still be intermittent (open/update/close based on detection/tracking).
- Multiple writers can write to the same Transmission when using advanced/manual flows.
- Encoding should happen only when there is actual playback demand.

The canonical product and engineering principles for streaming are documented in
[`docs/toposync-streaming-dossier-solid-priorities.md`](../../docs/toposync-streaming-dossier-solid-priorities.md#00-principios-permanentes-de-streaming).
In short:

- User-facing flows deal with publishable sources and variants; `Transmission`, outputs, engine paths, and quality profile IDs are advanced artifacts.
- Stability wins over latency and quality: live requires fresh selected frames, an active writer, and a healthy selected output.
- Transport is contextual: HLS is the stable baseline, MSE is preferred for passive web when the sidecar is healthy, WebRTC is explicit low latency/PTZ, and JSMpeg is the last visual fallback.
- Home Assistant Cloud support goes through native HA `camera` entities, not direct WebRTC inside the Toposync ingress player.
- Expensive work must be demand driven and scoped to the active stream/output/session.
- The core remains generic; streaming-specific reconciliation and policy live in this extension.

## Implemented features (high level)

- MediaMTX engine lifecycle management (start/stop/restart/status) with on-demand binary install (download + SHA256 verification) and dynamic port resolution.
- Stream publication specs for camera sources and pipeline output variants.
- Reconciliation from publication intent to generated live views, transmissions, outputs, and implicit pipelines.
- Transmission and output CRUD stored in extension settings.
- Per-output URLs for RTSP, HLS, and WebRTC/WHEP.
- Pipeline sink operator `stream.publish_video` that publishes frames into a transmission (multi-writer capable).
- Pipeline sink operator publication mode for generated variants.
- Multi-writer arbitration using lifecycle (`open/update/close`), priority, and a sticky window.
- Output-level placeholder frames when no writer is active.
- Output-level resolution and FPS enforcement, including contain resizing with black padding.
- FFmpeg publishers (one per output) that publish RTSP into MediaMTX.
- On-demand encoding based on MediaMTX viewer count, with debounce stop.
- Demand priming to reduce "first connect" 404/no-stream failures.
- Demand heartbeat for web, app, PiP, PTZ, and Home Assistant entity playback.
- Distributed hosting via `Transmission.host_server_id`, plus URL proxying and processing-side settings sync.
- Dashboard playback using a backend Playback Plan with HLS/WebRTC, MSE through the optional go2rtc sidecar, and JSMpeg as an on-demand emergency visual fallback.

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
- Used by the Toposync dashboard when low latency, PTZ, or another explicit interactive context requests it.
- Current implementation is LAN-first (HTTP, no TLS termination built in). If you expose streaming beyond LAN, plan to terminate TLS.

### MSE
- URL format: `ws://<toposync-host>/api/streams/media/mse/<path>/ws?media_token=...`
- go2rtc consumes `rtsp://127.0.0.1:<mediamtx_rtsp_port>/<path>` from MediaMTX.
- The browser never talks to go2rtc directly. Toposync verifies the signed media token and proxies text control messages plus binary fMP4 fragments.
- Dashboard Auto can prefer MSE for passive web/grid/fullscreen playback when the sidecar is enabled/startable and the backing output is browser-compatible.
- A stopped go2rtc process is normal when no MSE viewer is connected. Toposync returns a signed MSE URL when the sidecar can be started, then starts/updates go2rtc on the first MSE WebSocket session.

### JSMpeg
- URL format: `ws://<toposync-host>/api/streams/media/jsmpeg/<path>/ws?media_token=...`
- Intended only as an emergency visual fallback. It is video-only, low resolution/FPS, and does not carry audio.
- Each browser WebSocket creates one isolated FFmpeg process that converts the selected runtime frame stream to MPEG-TS/MPEG-1. The process is stopped when the WebSocket closes.
- The source is the selected Toposync Transmission frame, or an explicit placeholder while warming up/offline. It never pulls camera RTSP directly.

### Playback Plan candidates

The Playback Plan API can return these transport candidates:

- `hls`: stable baseline, active today.
- `webrtc`: low-latency path, active today when context allows it.
- `mse`: browser-first path through an optional go2rtc sidecar. go2rtc consumes the internal MediaMTX RTSP path, and browsers only connect through a signed Toposync WebSocket proxy.
- `jsmpeg`: emergency visual fallback through a signed Toposync WebSocket. It is selected only after better browser transports are unavailable or fail.

RTSP is not a browser transport. It remains the internal/ecosystem contract for HA Core, VLC/ffplay, Frigate/dev, go2rtc sidecars, and diagnostics.

## Architecture overview

### Data flow

Camera source publication:

`camera source` -> `StreamPublicationSpec` -> reconciler -> implicit pipeline -> `stream.publish_video` -> `TransmissionRuntimeState` -> `StreamWriterBridge` -> `FFmpeg publisher` -> `MediaMTX path` -> viewers (RTSP/HLS/WHEP)

Advanced pipeline publication:

`pipeline frames` -> `stream.publish_video` -> generated/manual `Transmission` -> `TransmissionRuntimeState` -> `StreamWriterBridge` -> `FFmpeg publisher` -> `MediaMTX path` -> viewers (RTSP/HLS/WHEP)

### Components

- `MediaMtxEngineManager`
  - Ensures the correct MediaMTX binary is available for the current OS/arch (downloads on demand when missing).
  - Renders a YAML config and starts/stops/restarts the MediaMTX process.
  - Resolves ports (preferred vs actual) and exposes engine status and test URLs.
- `Go2RtcSidecarManager`
  - Optionally downloads and starts go2rtc `v1.9.14` for browser MSE playback.
  - Renders `runtime/streaming/go2rtc/go2rtc.yaml` from generated streaming outputs.
  - Exposes no direct browser API; the dashboard uses the signed Toposync MSE proxy.
  - Uses internal MediaMTX RTSP URLs only, never direct camera credentials or camera URLs.
  - Does not need to run permanently. URL resolution treats "stopped but startable" as available; the signed MSE proxy starts it when a browser actually connects.
- `JsmpegSessionManager`
  - Starts FFmpeg only while a signed JSMpeg WebSocket session is connected.
  - Reads frames from `TransmissionRuntimeState`, resizes/contains them to the configured fallback profile, and writes MPEG-TS/MPEG-1 bytes to the browser.
  - Enforces global and per-transmission session limits so the fallback cannot silently become the primary load path.
- `TransmissionRuntimeState`
  - Stores latest frame per writer and per transmission.
  - Applies lifecycle and multi-writer arbitration to select the active writer.
  - Stores viewer_count per output (fed by the writer bridge).
- `StreamWriterBridge`
  - Periodic "tick loop" that loads streaming settings, ensures engine is running, refreshes viewer counts, starts/stops publishers on-demand, and pushes frames (or placeholders) into publishers.
  - Implements best-effort demand priming and a fallback "synthetic demand" hint.
  - Implements a bypass mode for simple pipelines (publisher pulls camera RTSP directly).
- Publication reconciler
  - Converts user intent (`StreamPublicationSpec`) into generated `CameraLiveView`, `Transmission`, outputs, and implicit camera pipelines.
  - Preserves generated artifact metadata (`generated_by`, `publication_id`, owner/camera/source/role) for diagnostics and read-only UI display.
  - Runs after camera/source changes, publication updates, pipeline saves, and explicit `POST /api/streams/reconcile`.
- `PublisherManager`
  - Spawns and supervises FFmpeg processes.
  - Supports rawvideo frames over stdin (`rawvideo_pipe`) or RTSP pull (`rtsp_pull`).
  - Maintains per-output logs and runtime status.

## Domain model and settings

### StreamPublicationSpec

`StreamPublicationSpec` is the user-facing intent model for publishing video.

For normal camera playback, the user should not create a Transmission manually. The camera source owns a publication spec, and the streaming extension reconciles the generated artifacts.

Fields:

- `id`: deterministic publication id.
- `owner_kind`: `"camera_source"` or `"pipeline_output"`.
- `camera_id`: camera/live-view group id.
- `camera_source_id`: concrete camera source id when publishing a camera source.
- `pipeline_name` and `publish_node_id`: source pipeline/node when publishing a manual rendered variant.
- `enabled`: turns the publication and generated artifacts on/off.
- `role`: `"main" | "sub" | "zoom" | "custom"`.
- `label`: user-facing label shown in source selectors.
- `live_view_id`: optional live-view group override.
- `host_server_id`: effective stream host.
- `quality_policy`: output/profile hints for generated artifacts.
- `transport_policy`: playback/transport hints for generated artifacts.

Deterministic ids:

```text
camera:{camera_id}:{source_id}
pipeline:{pipeline_name}:{node_id}
```

Generated Transmissions, CameraLiveViews, variants, and implicit pipelines include metadata such as:

```json
{
  "generated_by": "stream_publication",
  "publication_id": "camera:front:sub",
  "owner_kind": "camera_source",
  "camera_id": "front",
  "camera_source_id": "sub",
  "role": "sub"
}
```

Generated artifacts are normal runtime artifacts, but the regular UI treats them as read-only and points the user back to the owning camera source or pipeline operator.

### CameraLiveView and variants

`CameraLiveView` groups the public variants for one camera or logical video group. A variant maps a camera role to a generated Transmission/output.

Context defaults:

- `thumbnail` and `pip`: prefer `sub`.
- `large` and `fullscreen`: prefer `main`.
- `ptz`: prefer `zoom`, then `main`, then `sub`.

This is intentionally separate from technical quality labels. The dashboard source selector should expose labels such as "Principal", "Baixa resolução", "Zoom", or custom names, not internal output ids.

### Reconciliation rules

The reconciler is the owner of generated streaming artifacts.

It runs when:

- camera/source settings are saved;
- ONVIF discovery creates or updates video sources;
- `GET /api/streams/publications` normalizes publication specs for display;
- `PUT /api/streams/publications/camera-sources/{camera_id}/{source_id}` updates a source publication;
- `POST /api/streams/reconcile` is called;
- pipelines are saved, enabled, disabled, or removed.

For `owner_kind="camera_source"`, it creates one generated Transmission per published source and an implicit continuous pipeline named from the generated transmission. That pipeline feeds `camera.source` directly into `stream.publish_video`.

For `owner_kind="pipeline_output"`, it reads publication fields from the `stream.publish_video` node, creates a publication for that rendered variant, creates the generated Transmission, and writes the generated `transmission_id` back into the node when needed.

Generated camera pipelines must be continuous. A stream sink behind a motion gate, event-only detector, or event-only tracker is a manual/diagnostic case and should surface a warning rather than pretending to be a stable live camera stream.

### Transmission

A Transmission is the technical stream entity consumed by MediaMTX/FFmpeg.

For regular camera sources, Transmissions are generated from `StreamPublicationSpec`. Manual CRUD remains available for advanced diagnostics and integration work.

Fields (as implemented by the API model):

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
  "input_artifact_name": "",
  "resize_mode": "contain",
  "writer_priority": 0,
  "bypass_mode": "auto",
  "publication_enabled": false,
  "publication_camera_id": "",
  "publication_camera_source_id": "",
  "publication_live_view_id": "",
  "publication_role": "custom",
  "publication_label": "",
  "publication_show_in_dashboard": true,
  "publication_show_in_home_assistant": false,
  "publication_quality_profile_id": ""
}
```

Notes:
- `transmission_id` is still the runtime target.
- For generated variants, the UI can leave `transmission_id` empty and set `publication_enabled=true`; the reconciler generates the Transmission and later fills the target id.
- `resize_mode` exists in the operator config, but resizing is currently applied by the writer bridge based on output settings.
- Artifact selection:
  - Reads `input_artifact_name` exactly, or `main` when it is empty.
  - There is no artifact fallback or payload image mapping.
  - Normalizes frames to `uint8` BGR and contiguous memory.
- Lifecycle handling:
  - On `close`, the writer is marked closed and becomes ineligible for arbitration.
- Diagnostics:
  - The operator warns when it is downstream of motion gates or event-only detection/tracking, because those graphs do not produce continuous live video.

### Manual publication mode

Advanced pipelines should publish rendered video as a variant instead of asking the user to pick an existing Transmission id.

User-facing fields:

- destination camera/group;
- source role: `main`, `sub`, `zoom`, or `custom`;
- visible label;
- whether the variant appears in the dashboard;
- whether the variant is exported to Home Assistant;
- quality profile override when needed.

Pipeline-owned publications use deterministic ids:

```text
pipeline:{pipeline_name}:{node_id}
```

If the pipeline is disabled or removed, the publication and generated stream artifacts are disabled/removed by reconciliation.

## Advanced wizard: create pipeline from a Transmission

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

This wizard is an advanced/diagnostic shortcut. The normal camera flow is now camera source publication plus reconciliation.

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

### Camera settings

For regular cameras, streaming is configured from the camera source itself:

- each video source can show a `Transmitir esta fonte` checkbox;
- the source role is used as the publication role (`main`, `sub`, `zoom`, `custom`);
- the visible source label becomes the publication label;
- ONVIF-discovered video sources can be published by default;
- saving the source triggers streaming reconciliation.

If the streaming extension is not active, camera settings should not expose streaming controls.

### Settings panel

The extension adds a Streaming/advanced panel in Settings where users can:

- Inspect generated transmissions, outputs, live views, and runtime health.
- Create/edit manual transmissions and outputs for advanced diagnostics.
- Start/stop the engine.
- Resolve URLs (and best-effort prime demand).
- Create a pipeline from a transmission using the advanced wizard.

Generated artifacts are read-only in the normal UI. To change a generated camera stream, edit the camera source publication. To change a generated pipeline variant, edit the owning `stream.publish_video` node.

### Dashboard (Rendering -> Streams)

The main UI includes a "Streams" rendering mode with:

- Grid modes `1x1` and `2x2` with pagination.
- Auto-hide overlay.
- Source/role selector using camera variants, for example Principal, Baixa resolução, Zoom, or custom names.
- Playback strategy:
  1. Pick the best variant for the visual context.
  2. Request the backend Playback Plan for that transmission/output/context.
  3. Prime/heartbeat demand for the selected output.
  4. Use the first available transport from the plan.
  5. Monitor playback and fallback without surfacing non-blocking transport warnings as primary errors.

Default variant selection:

- grid/thumbnail/PiP: prefer `sub`;
- fullscreen/large: prefer `main`;
- PTZ/low latency: prefer `zoom`, then `main`;
- diagnostics/poor network: prefer the smallest available public variant.

Default transport policy:

- web grid/passive: `MSE -> HLS -> JSMpeg`; WebRTC is blocked unless low latency/PTZ is explicit;
- web PTZ/low latency: `WebRTC -> MSE -> HLS -> JSMpeg`;
- app and Home Assistant ingress: HLS-first;
- Home Assistant entity: no web player decision, use the HA camera contract.

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

### Publications and reconciliation
- `GET /api/streams/publications?camera_id=...`
- `PUT /api/streams/publications/camera-sources/{camera_id}/{source_id}`
- `POST /api/streams/reconcile`

### Engine
- `GET /api/streams/engine/status`
- `POST /api/streams/engine/start`
- `POST /api/streams/engine/stop`
- `POST /api/streams/engine/restart`

### MSE sidecar
- `GET /api/streams/mse/status`
- `POST /api/streams/mse/download`
- `POST /api/streams/mse/start`
- `POST /api/streams/mse/stop`
- `POST /api/streams/mse/restart`

### Transmissions CRUD
- `GET /api/streams/transmissions`
- `POST /api/streams/transmissions`
- `PUT /api/streams/transmissions/{transmission_id}`
- `DELETE /api/streams/transmissions/{transmission_id}`

### URL resolution
- `GET /api/streams/transmissions/{transmission_id}/urls` (local or proxy)
- `GET /api/streams/transmissions/{transmission_id}/playback-plan`
- `GET /api/streams/transmissions/{transmission_id}/hls/probe`
- `GET /api/streams/transmissions/{transmission_id}/still.jpg`
- `POST /api/streams/transmissions/{transmission_id}/webrtc/offer` (Home Assistant native WebRTC opt-in)
- `GET /api/streams/internal/transmissions/{transmission_id}/urls` (only on host server)

### Home Assistant
- `GET /api/streams/home-assistant/cameras`

### Distributed settings
- `GET /api/streams/distributed/settings/{server_id}`

### Runtime
- `GET /api/streams/runtime/outputs`
- `GET /api/streams/runtime/health`
- `GET /api/streams/runtime/pipelines`
- `GET /api/streams/runtime/observability`
- `GET /api/streams/runtime/diagnostic-snapshot`
- `GET /api/streams/runtime/diagnostics`
- `POST /api/streams/runtime/playback-events`

### Demand
- `GET /api/streams/transmissions/{transmission_id}/demand`
- `POST /api/streams/transmissions/{transmission_id}/demand/prime`
- `POST /api/streams/transmissions/{transmission_id}/demand/heartbeat`

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
    },
    {
      "output_id": "hls_main",
      "protocol": "mse",
      "resolved_engine_path": "front-door",
      "url": "ws://127.0.0.1:8100/api/streams/media/mse/front-door/ws?media_token=...",
      "requires_auth": false,
      "auth_username": null
    }
  ],
  "warnings": []
}
```

Playback plan (`GET /api/streams/transmissions/{id}/playback-plan?client=web&context=fullscreen`):

```json
{
  "transmission_id": "uuid",
  "client": "web",
  "selected_transport": "mse",
  "transports": [
    {
      "transport": "mse",
      "rank": 0,
      "available": true,
      "output_id": "hls_fullscreen_quality",
      "url": "ws://127.0.0.1:8100/api/streams/media/mse/front-door/ws?media_token=...",
      "fallback_rank": 0
    },
    {
      "transport": "hls",
      "rank": 1,
      "available": true,
      "output_id": "hls_fullscreen_quality",
      "url": "http://127.0.0.1:8100/api/streams/media/hls/...",
      "fallback_rank": 1
    }
  ],
  "hls_warnings": [],
  "webrtc_warnings": []
}
```

Transport policy:

- HLS is the stable browser/app/Home Assistant ingress baseline.
- WebRTC/WHEP is generated for `zoom`/PTZ publications and for publications with `transport_policy.enable_webrtc=true`; regular `main`/`sub` streams stay HLS-only by default.
- MSE is available when MediaMTX is running, the go2rtc sidecar is enabled and startable, a backing HLS/RTSP output exists, and the browser codec path is compatible. The go2rtc process may be stopped until the first MSE WebSocket connects. Otherwise it is blocked with a specific reason.
- JSMpeg is available when FFmpeg is available, the fallback is enabled, a backing HLS output exists, and session limits allow it. The fixed transport debug page does not silently fall back.

Transport frame checks can be run with:

```bash
node scripts/check_stream_transport_frames.mjs --base-url http://127.0.0.1:8100 --matrix --transports hls,webrtc,mse,jsmpeg
```

Camera source publication (`GET /api/streams/publications?camera_id=front`):

```json
[
  {
    "id": "camera:front:sub",
    "owner_kind": "camera_source",
    "camera_id": "front",
    "camera_source_id": "sub",
    "enabled": true,
    "role": "sub",
    "label": "Baixa resolução",
    "host_server_id": "local",
    "quality_policy": {},
    "transport_policy": {}
  }
]
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

## Practical quickstart

### Camera-source flow

For normal use:

1. Add/discover a camera in the Cameras extension.
2. Keep `Transmitir esta fonte` enabled on the desired video sources.
3. Save the camera/source.
4. The streaming reconciler creates the live view, generated transmissions, outputs, and implicit pipelines.
5. Open the dashboard and select the camera/source role.

The direct streaming API for that intent is:

```bash
curl http://127.0.0.1:8100/api/streams/publications?camera_id=<camera_id>

curl -X PUT http://127.0.0.1:8100/api/streams/publications/camera-sources/<camera_id>/<source_id> \
  -H 'content-type: application/json' \
  -d '{"enabled": true, "role": "sub", "label": "Baixa resolução"}'

curl -X POST http://127.0.0.1:8100/api/streams/reconcile
```

### Advanced manual flow (curl + ffplay)

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

### Dashboard says no pipeline is feeding the stream
Likely causes:
- The camera source publication is disabled.
- Generated implicit pipeline was removed or disabled.
- The manual pipeline that owns a variant is disabled.
- `stream.publish_video` has not received a frame yet.

Fix:
- For camera streams, check the camera source and keep `Transmitir esta fonte` enabled.
- Call `POST /api/streams/reconcile`.
- Check `GET /api/streams/runtime/pipelines` to see which pipeline owns the generated transmission.
- Check `GET /api/streams/runtime/health` for `active_writer_id`, `selected_writer_id`, `fallback_reason`, and frame age.

The primary user-facing message for this class is:

```text
Nenhum fluxo está alimentando esta transmissão.
```

### HLS plays but WebRTC warning appears
If the effective transport is HLS and the HLS probe is healthy, WebRTC network warnings are technical diagnostics only.

Common causes:
- Home Assistant add-on did not publish the UDP WebRTC port.
- The browser is remote or behind NAT without TURN/ICE reachability.
- The context is HA ingress, where direct Toposync WebRTC is blocked by default.

Fix:
- Keep HLS as the stable path for HA/app/passive views.
- Use WebRTC only for explicit low-latency/PTZ contexts.
- For HA Cloud, use the native Home Assistant camera entity path.

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

### MSE does not start or does not render
Common causes:
- go2rtc binary is missing.
- MediaMTX is stopped or the backing RTSP path is still warming up.
- The browser does not support the returned fMP4 MIME/codecs.
- The output is HEVC/H.265 and has not been transcoded to a browser-compatible H.264/AAC path.

Expected behavior:
- `GET /api/streams/mse/status` can show `running=false` with no warning. That means no active MSE viewer is connected.
- `GET /api/streams/transmissions/{id}/playback-plan?client=web` can still return MSE available when the sidecar is startable.
- Opening `/streams/debug?transport=mse...` should start go2rtc on demand and log: demand priming, backing RTSP wait, go2rtc API wait, MIME, binary fragments, and first frame.

Debug:
- `GET /api/streams/mse/status`
- `GET /api/streams/transmissions/{id}/playback-plan?client=web&quality_profile_id=...`
- `node scripts/check_stream_transport_frames.mjs --base-url http://127.0.0.1:8100 --live-view-id <id> --context thumbnail --transports mse`
- If go2rtc is killed, the next signed MSE WebSocket session should restart it automatically.

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
- go2rtc MSE config/log:
  - `runtime/streaming/go2rtc/go2rtc.yaml`
  - `runtime/streaming/go2rtc/go2rtc.log`
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
- Public wheels do not ship go2rtc binaries. The MSE sidecar downloads go2rtc `v1.9.14` automatically on first start and caches it under `~/.toposync/runtime/streaming/go2rtc/<version>/<platform>/`, unless `TOPOSYNC_STREAMING_GO2RTC_PATH` points to an explicit binary. The official Docker/Home Assistant images can pre-bundle `/usr/local/bin/go2rtc` and set that environment variable so MSE does not depend on a runtime download.
- FFmpeg integration expects an external binary by default (`PATH` or `TOPOSYNC_STREAMING_FFMPEG_PATH`). Bundling FFmpeg binaries is optional and must be handled carefully for redistribution.
  - License placeholder: [LICENSE.ffmpeg](LICENSE.ffmpeg)

If you plan to ship FFmpeg binaries, pay attention to LGPL/GPL build flags and codec licensing constraints depending on distribution model.

## Known limitations (current implementation)

- Video-only: audio is not published (`-an` in FFmpeg).
- No built-in TLS for MediaMTX endpoints (LAN-first).
- No Low-Latency HLS by default (to avoid TLS requirements and keep the default simpler).
- MSE requires an enabled/startable go2rtc sidecar and browser-compatible codec output. The process may be stopped while idle. HEVC/H.265 paths must be transcoded to H.264/AAC-compatible browser output before MSE can be selected.
- JSMpeg is video-only and intentionally low quality. It is a last-resort visual fallback, not a replacement for HLS/MSE/WebRTC and not an audio path.
- Hardware encoding selection exists in code paths but is not exposed as a stable user-facing setting yet.
- On-demand stops publishers, but does not stop arbitrary manual pipeline execution; pipeline compute is controlled by pipeline configuration and lifecycle semantics.
