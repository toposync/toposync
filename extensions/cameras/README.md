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

The person/vehicle stopped recipes sample continuously at four frames per second:
motion gating would stop supplying evidence after the subject stops. Detection and
mapping still determine whether a stationary event has sufficient evidence; this
sampling rate does not certify physical accuracy or camera availability. Existing
saved graphs are not rewritten when a recipe changes.

Area presence/entry occurrences provide a deterministic Portuguese description in
`payload.area_occurrence.description`, retained in the notification with the
structured area, observed category and evidence duration. Entry requires an
observed outside-to-inside transition. Loss of evidence ends observation without
claiming that the subject left; an observed exit is described separately. This is
a factual caption, not visual identification, gaze, intent or package recognition.
Notification timestamps and stored images remain the existing evidence links;
the caption does not reinterpret a media timestamp as wall-clock time.

An explicitly configured `home_assistant.speak` branch can reuse
`{{payload.area_occurrence.description}}` as its message. Speaker selection,
authorization, freshness checks and disabled-by-default behavior still apply;
creating an area recipe does not enable speech or operate a device.

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

## Observation recipes

Camera settings offer litter-box activity, drinking, presence in an area, and entry into an area. Recipes are saved disabled for review in the pipeline editor. Area recipes require an explicit mapped area and an available detection model; they distinguish people, cats and dogs without inferring age or identity.

`POST /api/cameras/cameras/{camera_id}/pipelines/presets/preview` accepts the recipe request and returns a compiled, disabled `pipeline` without saving it. It requires pipeline read and compile permissions. Creation uses the same preparation path. An explicit composition confines area lookup to that composition; ambiguous area identifiers without a composition are rejected.

`camera.area_occurrence` groups evidence independently per tracked subject. Entry requires a preceding observation outside the area; first sighting inside is not an entry. Duration ends at the last confirmed observation. Missing observations, source loss and tracking closure end the occurrence without claiming the physical activity ended. Stale or terminal replay cannot reopen a subject, and active state is bounded. Cat recipes use semantic occurrence filtering; descriptions are observations, not proof of ingestion or elimination.

**People entering together** requires two distinct tracked people crossing into the
selected mapped area within five seconds of the first crossing. Repeated crossings
by the same tracked subject count once in that group. It reuses area occurrences
and `vision.group_events`, with a fixed admission window and minimum member count;
updates remain attached to their existing group. The reported duration ends at
the last evidence, excluding the idle timeout. This describes observed crossings,
not family relationships, intent or biometric identity. Tracking fragmentation
can still overcount a physical person; validate tracking and mapping with the
installation's footage before relying on counts.

## Local audio review and transcription

Camera settings accept a WAV sample of up to 30 seconds, PCM16, mono or stereo,
and 12 MB. Signal measurements and optional reference comparison remain available
without a transcription model. The selected file is processed locally and is not
retained or played. Timestamps are offsets within that file, not verified capture
times. These checks do not establish camera audio capture or sound localization.

From a source checkout, enable the optional dependencies with `uv sync --group audio`
and run the backend with `uv run --group audio python -m toposync serve` (plus the
installation's usual arguments). The model directory is
`<data-dir>/audio-models/faster-whisper-tiny`. To provision the pinned public model,
run the following with that environment and the intended data directory:

```python
from pathlib import Path
from hashlib import sha256
from huggingface_hub import snapshot_download

model = Path("/absolute/data-dir/audio-models/faster-whisper-tiny")
snapshot_download(
    "Systran/faster-whisper-tiny",
    revision="d90ca5fe260221311c53c58e660288d3deb8d356",
    allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"],
    local_dir=model, token=False, max_workers=1,
)
assert sha256((model / "model.bin").read_bytes()).hexdigest() == (
    "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"
)
```

The four files total approximately 78 MB. See the
[model repository](https://huggingface.co/Systran/faster-whisper-tiny) and
[Faster Whisper documentation](https://github.com/SYSTRAN/faster-whisper).
Inference never downloads a missing model or sends audio to a remote provider.
It uses CPU/int8, two inference threads, one worker and one admitted transcription
per application process. Busy requests retain signal analysis and report that
transcription is busy. The model is released after each call; leaving the page
does not forcibly interrupt a native call already running.

Choose the speech language; the interface defaults to its own language and also
offers automatic detection. Automatic detection can choose the wrong language
for short recordings. Text and segment timings are model output for review, not
verbatim guarantees, speaker identity or intent. Decoding is bounded to 128 new
tokens; a partial result is labelled. Stereo uses the higher-energy channel to
avoid phase cancellation and does not perform speaker separation.
Each segment retains the decoder's average log probability and no-speech
probability when valid. These are uncalibrated model scores, not a probability
that the words are correct; segment `confidence` remains null. Missing or invalid
scores remain null without discarding otherwise valid text and timings.

`POST /api/cameras/audio/analyze` accepts `transcribe=true` and an optional
`language` code alongside its existing explicit clock parameters.
`camera.audio_features` supports the same optional transcription, disabled by
default for pipelines. Existing packet and occurrence identifiers are preserved.

To retain an analysis in the existing notification history, connect
`camera.audio_features` to `core.notify` with
`historical_evidence_paths: ["audio_analysis"]`. This keeps the latest analysis
after a terminal packet without audio. It stores structured measurements and
optional transcription, not the PCM recording. The standalone upload analysis
does not save a notification automatically.

For replay deduplication, enable `dedupe_by_occurrence` and provide an upstream
event subject (`subject.type: "event"`, `subject.id`), a correlation identifier
renewed for each occurrence, and the canonical `source.source_id`. These fields
must survive closure; audio analysis does not invent them. A closed occurrence
cannot be reopened by replay, and different sources retain separate records.
Temporal overlap candidates do not establish that microphones heard the same
sound and must not be used alone to merge occurrences.

`camera.audio_source` reads the first audio track of an enabled, configured
camera media source using the existing source resolver. It requires the optional
audio dependencies and can feed `camera.audio_features`. It uses one connection
per running node, CPU decoding, five-second open/read timeouts and no automatic
reconnection. Keep it disabled when unused; it does not share the video decoder.
The source emits at most one second of PCM16 at 16 kHz per packet, preserving
mono or stereo, and releases the decoder on completion or cancellation. Other
channel layouts are explicitly unsupported. A stream with no audio track differs
from silent samples or a lost stream. Network end-of-stream counts as stream loss.

Packet media time is decoded sample order; it does not certify uninterrupted
capture or preserve gaps in source presentation timestamps. Audio clock metadata
is local receipt time with unknown uncertainty, never synchronized camera capture
time. Consequently it cannot establish multi-camera time-of-arrival localization.
The source does not infer acoustic occurrences or enable notifications by itself.
Local-file decoder tests do not certify physical camera audio support.

For explainable alerts, connect `camera.audio_source` → `camera.audio_features`
(with an explicit baseline) → `camera.audio_occurrence` → `core.notify`.
The occurrence operator groups consecutive above-baseline windows, closes after
two seconds back within baseline, and closes as uncertain when evidence stops
for five seconds. Quiet and timeout durations are configurable. Baseline changes,
source closure and evidence gaps end the previous occurrence. No baseline means
no acoustic occurrence, and loudness alone does not mean danger, speech or intent.
Use notification occurrence deduplication and retain `audio_analysis` and
`audio_occurrence` as historical evidence. Terminal output keeps the last positive
analysis; quiet periods do not overwrite it or extend the positive evidence time.
Microphones remain separate until same-sound association can be demonstrated.
The operator retains at most 128 source/capture watermarks and drops PCM artifacts
from emitted and retained occurrence packets. Eviction closes an active occurrence.

In camera settings, choose **Sound above reference**, select a normal-sound WAV
from that source at its usual gain, and save the recipe. The interface calculates
the baseline locally; digital silence is rejected. The recipe always starts
disabled, including API requests that ask to enable it. Preparing it does not
probe the camera, install a visual model, transcribe speech or move PTZ. Audio
support remains unverified until an authorized runtime test observes the source.
Missing audio, lost streams, unavailable decoding dependencies and unsupported
channel layouts close the source and register one explanatory node failure in
runtime diagnostics. They do not silently complete successfully or retry in the
background. Check the cause before restarting; an enabled pipeline alone is not
evidence that audio is being received.


## Live ONVIF event sources

`camera.onvif_event_source` emits events received locally since its runtime was
constructed. It does not replay the event manager's earlier buffer on startup.
A missing, non-finite or future local receipt timestamp is rejected, and already
consumed sequence numbers are not emitted again. `received_at_ts` remains in the
payload as local receipt evidence; it does not certify camera capture time or
clock synchronization. Use `camera.onvif_state_gate` for a current boolean state,
rather than interpreting buffered events as new activity.

Topic and item filters must come from the device's discovered event capabilities.
The presence of an ONVIF event source does not prove that a particular camera
reports a doorbell, package or visitor. Correlation with video still requires an
explicit time window, a shared source/area association and fresh visual evidence.


`camera.recent_onvif_event` admits fresh visual packets from the configured camera
within five seconds of a positive boolean `Changed` event for an explicit topic
and item. Initial state snapshots do not count as a new actuation. It reuses the
live ONVIF source, polls at most twice per second with a bounded batch, and requires
frame publication evidence no older than two seconds. An incomplete configuration
is inactive and does not poll any device. Closing packets from the associated
camera pass through so an existing occurrence can terminate.

Place it before `camera.area_occurrence` to retain the existing mapped-area
lifecycle and evidence timeout. The `onvif_context` payload records event sequence,
local receipt, frame publication, window and an explicit unverified causal link.
Include it in stored notification fields when building a correlated recipe.
A timeout ends observation at the last evidence timestamp; it is not a claim that
the person left. This operator alone does not classify a package or an occupation,
detect a doorbell capability, or provide a complete visitor recipe. Topic discovery,
area selection and visual assessment remain necessary. Its window compares local
receipt with source publication and does not certify physical clock alignment.
