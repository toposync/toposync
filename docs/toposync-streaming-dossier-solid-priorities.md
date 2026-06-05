# Toposync streaming dossier: solid priorities

This dossier records the technical and product decisions that should keep
streaming, camera mapping, Home Assistant, and spatial video work aligned.

It is intentionally stricter than a feature list. If a future change conflicts
with these rules, revisit the rule explicitly instead of hiding the conflict in
UI heuristics or transport fallbacks.

## 00. Principios permanentes de streaming

- The user model is source, publication, live view, and variant. `Transmission`,
  output ids, engine paths, and quality profile ids are advanced artifacts.
- Stability wins over latency and quality. Low latency is contextual, not the
  default for every tile.
- A stream is live only when it has a recent selected frame, an active selected
  writer, and a healthy selected output. A running FFmpeg or MediaMTX process is
  not enough.
- A frozen last frame must never be presented as live. It must become stale,
  placeholder, or an actionable error.
- Transport warnings for inactive transports must not become the primary user
  error while the active transport is healthy.
- Expensive work must be demand driven and scoped to the active
  transmission/output/session.
- Sidecars and fallback encoders never connect to cameras directly. They consume
  Toposync/MediaMTX outputs or Toposync runtime frames.
- Home Assistant Cloud support goes through native Home Assistant `camera`
  entities. The Toposync ingress player remains HLS-first and does not depend on
  direct browser WebRTC.
- The core stays generic. Streaming policy, camera publication reconciliation,
  and spatial video projection belong to extensions.

## 01. Product model

Normal camera playback starts from the camera source:

```text
camera source -> StreamPublicationSpec -> reconciler -> implicit pipeline
  -> stream.publish_video -> TransmissionRuntimeState -> output publisher
  -> MediaMTX / Toposync media proxy -> viewer
```

The user should not need to create a `Transmission` or pipeline for ordinary
camera viewing. A publishable camera source has:

- an enabled/disabled publication intent;
- a role: `main`, `sub`, `zoom`, or `custom`;
- a human label;
- an effective host/server;
- quality and transport policy hints.

Manual pipeline publication is still supported for advanced flows. A
`stream.publish_video` operator publishes a rendered video as a live-view
variant, with its own group name, variant label, role, and visibility flags.
The generated `transmission_id` is an implementation detail that the reconciler
may write back into the node.

## 02. Multi-stream camera rules

Cameras can expose multiple video sources. The source role is semantically more
important than technical quality labels:

- `sub`: grid, thumbnails, low-cost dashboards, passive monitoring;
- `main`: fullscreen, inspection, recordings or higher quality contexts;
- `zoom`: PTZ/autotrack/low-latency contexts;
- `custom`: user-defined variants from camera sources or pipelines.

The dashboard should select by context and role before exposing advanced
transport or quality controls. A fullscreen view should change to the best
`main` variant when available; a grid tile should prefer `sub`.

## 03. Transport policy

Transport order is contextual. The default policy is:

| Context | Preferred order | Notes |
| --- | --- | --- |
| Web grid/passive | MSE -> HLS -> JSMpeg | WebRTC is not opened for every tile. |
| Web fullscreen | MSE -> HLS -> JSMpeg | WebRTC only enters when low latency is requested. |
| Web PTZ/low latency | WebRTC -> MSE -> HLS -> JSMpeg | WebRTC errors are primary only when this context requested WebRTC. |
| Home Assistant ingress | HLS -> MSE -> JSMpeg | Direct browser WebRTC is blocked by default. |
| Home Assistant entity/Cloud | Native HA camera contract | Toposync exports RTSP/still/WebRTC offer metadata; HA chooses playback. |
| App/mobile/PiP | HLS -> JSMpeg | WebRTC is explicit. MSE depends on wrapper support. |
| Diagnostics | Fixed user-selected transport | The debug screen must not silently switch transports. |

RTSP is not a browser transport. It is the internal/ecosystem contract for
Home Assistant Core, VLC/ffplay, Frigate/dev, go2rtc sidecars, and diagnostics.

## 04. Transport contracts

### HLS

HLS is the stable compatibility baseline. It should remain the first answer for
Home Assistant ingress, app/mobile, and unknown network conditions.

HLS health requires more than an URL:

- playlist responds;
- media sequence advances;
- the tail segment is retrievable;
- the selected runtime frame remains fresh.

### MSE

MSE is the preferred passive web transport when it is startable and compatible.
It is implemented through go2rtc:

- go2rtc consumes internal MediaMTX RTSP paths;
- the browser connects only to Toposync signed WebSocket proxy URLs;
- a stopped sidecar is normal when no MSE viewer exists;
- the MSE WebSocket start path may start/update go2rtc on demand;
- MSE URLs can be returned when the sidecar is startable, even if the process is
  currently stopped.

MSE is synthetic. It is derived from a healthy HLS/backing output and should not
be persisted as `TransmissionOutput(protocol="mse")`.

### WebRTC

WebRTC/WHEP is for explicit low-latency and PTZ contexts. It is sensitive to
network path, ICE candidates, UDP availability, and Home Assistant add-on port
mapping. A WebRTC warning must not dominate a healthy HLS/MSE playback session.

Generated camera publications should not create WebRTC outputs for every
`main` or `sub` source. Use WebRTC for `zoom`/PTZ or when
`transport_policy.enable_webrtc=true`.

### JSMpeg

JSMpeg is the final visual fallback:

- WebSocket MPEG-TS/MPEG-1 video only;
- no audio;
- low resolution and low FPS;
- each session owns its FFmpeg process;
- the source is the selected Toposync runtime frame or placeholder;
- the encoder stops when the WebSocket closes.

JSMpeg is synthetic and should not be persisted as
`TransmissionOutput(protocol="jsmpeg")`.

## 05. Liveness and failure rules

Primary hints should follow this order:

1. blocking URL/auth error for the active transport;
2. no selected writer/frame;
3. stale selected frame;
4. publisher down while writer exists;
5. HLS/MSE/WebRTC/JSMpeg active transport failure;
6. technical warnings for non-active transports.

Important classifications:

- `no_frame` with no active/selected writer means no pipeline is feeding the
  transmission;
- `publisher_down` with a selected writer means frames exist but media output is
  not being published;
- `source_pipeline_stale` needs actionable evidence: last frame time, expected
  writer, transmission, and selected output;
- event-gated pipelines should be reported as event-gated or idle, not as
  generic live stream failure.

## 06. Demand and resource lifecycle

Work must be scoped to real demand:

- camera publication pipelines can be continuous, but output encoding should
  start only when the selected output has demand;
- dashboard, debug, PiP, PTZ, and Home Assistant entity playback send explicit
  heartbeat leases;
- MSE starts or updates go2rtc when the signed WebSocket opens;
- JSMpeg starts FFmpeg only for a connected WebSocket;
- idle sessions must release heartbeat, encoder, WebSocket, and texture
  resources.

Demand must include the selected output and transport so multi-output cameras do
not accidentally keep the wrong variant hot.

## 07. Home Assistant contract

The Toposync UI inside Home Assistant ingress is not the Home Assistant Cloud
media contract.

Ingress/UI:

- HLS-first;
- MSE may be used through the Toposync proxy when available;
- JSMpeg is fallback;
- direct browser WebRTC remains blocked unless explicitly supported and
  configured.

Native Home Assistant integration:

- exposes Toposync live views as HA `camera` entities;
- `stream_source()` points to internal Toposync/MediaMTX RTSP, never direct
  camera credentials;
- still images come from Toposync still endpoints;
- native WebRTC offer handling is opt-in until validated in the target network
  and HA Cloud path.

## 08. Spatial video and media metadata

Spatial video uses streaming playback but has additional media geometry
requirements:

- camera calibration is based on the useful camera image;
- streaming outputs may contain black padding from `resize_mode="contain"`;
- playback URLs include `content_rect` metadata so spatial video can remap UVs
  to the useful video rectangle;
- `content_rect` is media metadata, not user calibration;
- when metadata is missing, black-padding detection is a defensive fallback only.

Area clipping in Visao 360 is a geometric clip, not letterbox correction. If a
clip area removes part of the projected mesh, the image may look cropped or
zoomed even when `content_rect` is correct.

## 09. Validation expectations

Streaming changes should prefer targeted validation:

- unit tests for policy, model, token, liveness, and resize math;
- frame validation for each active browser transport when possible;
- visual artifacts or contact sheets when debugging projection/cropping;
- Home Assistant ingress URL validation whenever browser-visible paths,
  WebSockets, assets, or diagnostics links change;
- no metadata-only acceptance for camera playback fixes. Fetch and inspect a
  real frame when the change is about whether video is visible.

