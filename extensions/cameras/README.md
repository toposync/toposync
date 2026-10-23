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

The read-only `capture-observation` diagnostic separates opening, observation and
release costs using the process monotonic clock. `first_frame_elapsed_seconds`
counts from request admission in the capture service, including opening, to the
first distinct decoded sample; it is null when no sample was observed. This is
not camera exposure latency or physical arrival confirmation. Existing
`duration_seconds` retains its post-opening meaning, including cleanup;
`request_elapsed_seconds` includes all three phases, without HTTP overhead.
Samples include capture instance, generation and sequence so overlapping consumers
can be compared without treating decoder replacement as stream continuity.

The person/vehicle stopped recipes sample continuously at four frames per second:
motion gating would stop supplying evidence after the subject stops. Detection and
mapping still determine whether a stationary event has sufficient evidence; this
sampling rate does not certify physical accuracy or camera availability. Existing
saved graphs are not rewritten when a recipe changes.

Before enabling a pipeline, `camera.source` rechecks that its saved camera and
selected (or default) video source still exist and are enabled. A removed or
disabled source blocks activation with a review message; disabled drafts remain
editable. Explicit stream URLs retain their independent configuration path.
This check does not open a camera or certify stream health, calibration, or
recognition quality; those require current runtime evidence.

Area presence and entry recipes report repeated observed entries within a five-minute
window by default (`visit_window_seconds`). Only confirmed outside-to-inside crossings
count; updates and replay do not. Counts refer to one continuous tracking episode,
not a recognized identity across sessions. Missing evidence, source reset, tracking
termination or eviction clears the history. Each subject retains at most 256 entry
timestamps; saturation is reported as a lower bound until discarded entries expire.
The notification description and structured area evidence retain this distinction.

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

Composition elements with `panorama_mapping` use its selected source, element
and revision for ground projection and spatial video. Invalid selected references
fail closed instead of reviving old view geometry. Attention consumes mapped rays,
explicit region and return through the panorama services at fixed zoom; physical
qualification remains independent of software delivery.

`metric_calibrations` is a separate explicit record list for `camera_ray_ground_v2`
lens, pose, stream scope and ground correspondence data. The editor can copy existing
metric records without altering the original records. An explicit empty list is
authoritative. Body pointing and metric projection retain their own validators;
a panorama does not supply metric extrinsics. Older metric records remain readable
until an explicit choice. Inline projection APIs retain their calibrated record
payload for independent metric calculations.

`/api/cameras/projection/map` is the public point-query endpoint. `/api/cameras/projection/solve`
validates a v2 draft without persisting it. UI and extension
code should use the appropriate panorama or explicit metric reference; runtime
control-point pairs remain internal projection details.

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

For an already-associated subject with capture-bound panoramic directions, visual
navigation can compensate briefly for movement during its last measured command,
settling and localization cycle. Three distinct observations from the same source
and reference are required; repeated polls do not add samples. The horizon cannot
exceed one second or the observed history. Stops, reversals, discontinuities,
unsupported directions and disagreement between recent derivatives disable the
forecast. Each available axis must already have a measured response.

The forecast guides correction only: arrival remains measured against the observed
target. `navigation_step.prediction` separates the forecast from
`observed_target_ray`; `target_predictions` counts accepted planning forecasts,
not physical commands. Timing uses source publication, not verified exposure, and
the last cycle does not model latency variance. These bounds and synthetic replays
do not qualify physical tracking, full subject framing or another optical source.

Moving-subject navigation rechecks its observation after command queue and
readiness waits through a synchronous, in-process `cameras.control.submit`
dispatch guard. Lost observations abort; a planning frame older than one second
or a direction change exceeding half the arrival tolerance discards the proposal
and replans. Smaller changes require qualified projection on the same reference.
The guard is temporary, is not persisted, and never applies to Stop or a later
return. Rejected attempts remain bounded and visible in the navigation trace.
This checks controller admission to transport; later ONVIF preparation and network
waits still need their own timing/validity checks before physical qualification.

### Bounded PTZ control leases

The internal `cameras.control.acquire` service accepts optional
`maximum_duration_s`, a finite positive duration measured from request admission,
including discovery and preemption waits. The limit uses the controller's
monotonic clock. Renewing or reacquiring the same lease cannot extend or remove
it; a shorter limit may tighten it. `expires_at` continues to report the actual
lease expiry as a civil timestamp for clients, without changing the monotonic
enforcement. Requests without a maximum keep the existing renewable lease policy.

The existing expiry watchdog revokes ownership and schedules fenced Stop without
an attention-controller tick, browser or API poll. Commands queued under expired
ownership cannot start. This does not cancel an already transmitted command or
prove the camera stopped: transport completion and physical Stop still require
their existing verification. Process loss also requires a qualified device-side
motion timeout; an in-process watchdog alone cannot provide that guarantee.

The ceiling belongs to one lease. A campaign must separately preserve its end
time and refuse a new lease after expiry, including after restart; callers must
pass their remaining budget on every new acquisition. The lease primitive alone
does not implement a complete campaign lifecycle or certify hardware behavior.

## Camera recipes

A recipe is a ready-made configuration in the camera's existing catalog. Configuring
one creates a disabled pipeline, which is then reviewed and managed under Pipelines.
Both creation forms follow this same path; they do not create a second recipe catalog.
`preset` remains the internal request field and API route terminology. It is not a
separate user-facing category. Camera position presets are a different device feature.

Recipe graphs should keep product identity on `payload.subject.id`. Raw tracker
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

Velocity output includes signed `vector_mps` and `vector_raw_mps` (`x`/`z`),
using the same window and most recent interval as the existing speed estimates.
Invalid estimates expose null vectors. `world_reference` identifies the source
and mapping used by the samples; a source, mapping revision or timestamp-field
change resets the history, stationary memory and arrival evidence. Ordinary PTZ
pose changes within the same mapped reference do not reset it.
`time_basis` names the resolved media timestamp field or the packet creation-time
fallback; neither certifies physical exposure timing. Windowed direction can lag
behind a stop or reversal, so it is not sufficient by itself for motor prediction.
These fields add no camera commands or required configuration.

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

## Area and activity recipes

Camera settings offer litter-box activity, drinking, presence in an area, and entry into an area in the same recipe catalog. Created pipelines are saved disabled for review in the pipeline editor. Area recipes require an explicit mapped area and an available detection model; they distinguish people, cats and dogs without inferring age or identity.

`POST /api/cameras/cameras/{camera_id}/pipelines/presets/preview` accepts the recipe request and returns a compiled, disabled `pipeline` without saving it. It requires pipeline read and compile permissions. Creation uses the same preparation path. An explicit composition confines area lookup to that composition; ambiguous area identifiers without a composition are rejected.

Mapped presence, entry, group-entry and doorbell recipes preserve the selected
area element identifier as `area.id` in their occurrence configuration. The
occurrence emits `area_id` and `area_label`; its notification projects the identifier
through the existing `include_payload_paths` contract so history filters can match
it exactly. Manually configured polygons without an identifier retain their name
but do not inherit or invent an area identifier. Existing historical records are
not backfilled from names alone.

New mapped presence, entry, grouped-entry and visitor recipes bind their area to its identifier in the selected composition. Renaming or moving that area closes an existing observation without claiming a physical exit; later evidence starts a new observation using the current boundary. A removed, invalid or replaced area prevents activation, and an unavailable runtime reference cannot fall back to its saved polygon. Restore the original area or create a recipe for another selection. Inline area operators and earlier recipes without a composition reference retain their explicit geometry. This configuration check does not certify physical calibration. The recipe review resolves the current area name through the existing camera contexts API; it distinguishes missing or inaccessible areas from failed lookups.

New stopped-person, stopped-vehicle and person–vehicle proximity recipes with a selected area bind their filter to the selected composition and area definition. Removing, replacing, renaming or moving that area blocks activation and further admission by the filter. Create a new recipe for the current definition, or restore the original definition. The review distinguishes a changed definition from an unavailable area. This prerequisite check does not by itself qualify downstream observation duration or closure; manually configured filters without a composition reference keep their explicit polygons.

Stopped-person and stopped-vehicle observations expire after five seconds without valid evidence by default. The closing event preserves the last confirmed sample and its stationary duration; it does not claim that the person or vehicle moved. Invalid positions or velocity, stale captures and source resets interrupt the observation. Fresh evidence must qualify a new stop, while duplicated, older or terminal subject packets cannot extend the previous one. Short movements grouped into one occurrence do not count as stationary seconds. New recipes retain this evidence and the closure reason in notification history. Timestamp fences and retained observation packets are bounded; reaching capacity reports a runtime error and refuses new subjects until capacity is released or the pipeline is restarted.

`camera.area_occurrence` groups evidence independently per tracked subject. Entry requires a preceding observation outside the area; first sighting inside is not an entry. Duration ends at the last confirmed observation. Missing observations, source loss and tracking closure end the occurrence without claiming the physical activity ended. Stale or terminal replay cannot reopen a subject, and active state is bounded. Cat recipes use semantic occurrence filtering; descriptions are observations, not proof of ingestion or elimination.

New presence and entry recipes emit at most one regular update per second of the
observed media timeline per occurrence before image storage. Every accepted observation still refreshes the
state and last evidence; opening and closing events bypass this interval, and a
close retains the latest confirmed frame and duration. This limits repeated writes,
not the number of subjects or detection work. The operator's
`update_interval_seconds=0` leaves updates unlimited and preserves the storage
advisory. Grouped-entry and doorbell correlation keep their separate contracts.

Area occurrences use the shared executor's processing, latency and error counters.
A normal filter result counts as completed processing even when it emits no event;
idle expiration can emit a close without incrementing the processing counter.
Expiration also runs while observations of other subjects continue arriving.
These counters describe execution, not detection quality or calibration accuracy.

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
The detector compares one-second RMS windows, not sound classes. A short knock
can have a high peak or clipped samples while its window remains below the
reference threshold. Clipping is reported separately; it does not automatically
open an occurrence. A reference from another recording, microphone or gain is
not an environmental calibration, even when both recordings have the same label.
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

To suspend visual work between ONVIF windows without interrupting stationary
observations, give `camera.source` an `observation_window_id` and leave
`keep_capture_when_gate_closed=false`. Insert `camera.observation_hold` with the
same `window_id` after category/area selection and before stationary or grouped
event admission. Fresh tracked subjects retain demand before they qualify an
event; waiting for an already confirmed stop would close capture too early.
Every source and holder must run in the same local processing server. The scope
includes camera and optical source, and separate holders cannot release each
other's observations.

The default visual grace is fifteen seconds, measured from capture publication,
not from a recreated event envelope. It must exceed the longest observation
interval and tolerate tracking gaps. Terminal subjects, stale/duplicate frames,
shutdown and expiration cannot keep renewing capture. An ONVIF window still
retains its configured closing tolerance and minimum usable-frame observation;
unknown or unavailable ONVIF can continue through `fail_open=true`. When neither
window nor visual demand remains, the source releases only its own capture lease,
and detection and mapping receive no new frames. The frame metadata exposes
`visual_observation_retained`; it does not turn an inactive ONVIF state into an
active one. The existing idle visual cadence can still reduce inference while
subjects are stationary. A subject that remains visibly present keeps its window
open, so idle savings do not describe consumption during such an observation.

This control does not certify ONVIF coverage: an object never reported by the
camera while capture is suspended can be missed. Deterministic lifecycle tests
and reduced CPU in a quiet interval do not prove physical recall. Mapping failure
reasons remain attached to the existing spatial diagnostics, and mapped area
admission still depends on valid geometry.

For a vehicle that can approach the selected area after the ONVIF hint expires,
retain moving/unknown-velocity observations on a parallel branch before area
restriction as well. An existing `core.filter` with
`payload.velocity.valid != True or payload.velocity.stopped != True`, followed by
another holder for the same window, preserves that approach. The area branch
then retains stationary observations. Stationary objects outside the selected
area do not continually renew the moving branch.

State gates can also accept explicit `activation_events` filters (qualified topic,
data item, source and key). A fresh affirmative `Changed` message opens a bounded
visual-attention window. This supports cameras that emit additional activity
messages without declaring them in their event properties. It does not promote
those messages into typed persistent states, category detections or confirmed
occurrences. Initial synchronization, historical receipts, mismatched sources,
namespace/key ambiguity and rejected property ordering cannot trigger a new
window. Missing event transport retains the configured fail-open protection.
Diagnostic `onvif` payloads distinguish `activation_hint` from the original
property value. The existing qualified-property selection rules remain intact.

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

`camera.area_restriction` preserves the configured identifier of the primary
matched area as `area_id`, paired with the existing primary `area_label`. With
overlapping polygons, the first match remains primary. Names never substitute
for identifiers. An area without an identifier, an outside point, or an unmapped
packet admitted by `drop_when_unmapped=false` clears previous area identity;
unmapped packets also clear derived labels. Inclusion/exclusion behavior is
unchanged. New stopped-person, stopped-vehicle and person/vehicle interaction
recipes explicitly project `area_id` into notification history. Existing manual
graphs need the same `include_payload_paths` selection; old records are not
backfilled by matching names.


A receita de pessoa parada aceita uma referência visual opcional para avaliar orientação corporal, como uma fachada distinguível ao fundo. A opção preserva a cena inteira e vincula a avaliação à pessoa acompanhada; usa o perfil visual configurado e mantém a ocorrência de parada mesmo quando a orientação fica indisponível. A notificação diferencia indício positivo, negativo visível e indisponibilidade, e conserva contexto de avaliação para revisão. Não confirma olhar, atenção, intenção nem direção tridimensional. Referência oculta ou ambígua não permite confirmação. O campo é opcional e fica recolhido na configuração; sem referência, a receita de parada mantém seu fluxo normal. A API aceita `orientation_reference` (até 240 caracteres) somente em `person_stopped`. Precisão visual depende de qualificação empírica do perfil.


Na receita com orientação, a cena armazenada conserva os pixels até a avaliação. O fluxo dispensa o debounce de parada para que mudanças entre indício, negativo e indisponível cheguem à notificação; permanece a limitação de frequência do avaliador e da notificação. Evidências positivas anteriores ficam no histórico com imagem, captura e vínculo de sujeito, explicitamente sem certificar o estado atual. O fechamento não apaga esse registro nem o apresenta como uma nova avaliação.


A receita de orientação limita as atualizações por sujeito a uma amostra a cada cinco segundos antes de preparar e armazenar a cena, alinhada ao intervalo do avaliador. Abertura e fechamento atravessam esse limitador. Eventos entre amostras não são avaliados; a receita não comprova orientação contínua nesse intervalo. A amostragem reduz gravações redundantes e não substitui medição de custo com resolução, duração e modelo reais.


Audio temporal correlation retains at most 32 anomalous PCM blocks per
`camera.audio_features` runtime (`retained_blocks_limit`), including later blocks
from the same capture session. Candidates are unique source/capture pairs whose
intervals overlap after declared uncertainty, with the same verified capture-clock
reference. Retention is bounded by arrival count, not a guaranteed time horizon.
Receipt clocks remain ineligible. Temporal overlap does not prove the same sound
and does not merge occurrences or suppress notifications from different cameras.

### Two-source acoustic comparison

`camera.audio_correlation` accepts two explicitly wired audio inputs, `first` and `second`, after per-source `camera.audio_features`. It runs at the origin and requires private artifact transport. It forwards every packet and lifecycle, adding `audio_correlation`; it does **not** merge occurrences or suppress notifications. Independent camera recipes do not automatically connect to this stage.

The evaluated input is a one-second mono PCM16 window at 16 or 48 kHz. Both sources must declare the same capture-clock synchronization reference, at most 50 ms uncertainty each, and start within 100 ms. Receipt clocks remain unavailable for comparison; the current RTSP source supplies receipt time. A matching waveform cannot establish clock calibration.

The method compares temporal log-power envelopes across 12 frequency bands. A score of at least 0.7 is reported as `similar`, with `calibrated=false` and `same_sound_confirmed=false`. Silence, insufficient spectral variation and unsupported windows abstain. Evaluation covers a small LOCATA speech subset, including a held-out recording, not general sounds or calibrated matching accuracy. Identical playback from different locations remains ambiguous; the lag does not provide a location.

Only two signatures are retained in memory, for at most five seconds; raw audio is forwarded through the existing private contract and is not stored by this operator. Source bindings cannot switch ports. Closed captures cannot resume, and 64 closed captures exhaust the session until the operator restarts. Shutdown clears all state. The optional grouping stage below consumes this comparison. The audio recipe can explicitly connect a second camera as described below.


### Acoustic occurrence groups

Place `camera.audio_group` after the comparison stage, followed by `core.notify` with `dedupe_by_occurrence=true` and `audio_group` in `historical_evidence_paths`. This stage reuses the existing per-source baseline occurrence detector, quiet period, timeout and opt-in private clip retention.

Matching source openings share one canonical event identifier and notification. Each member retains its camera/source/capture identifiers, analysis, comparison and occurrence evidence. Closing one member updates the notification; the final member closes it. Divergent evidence splits subsequent observations into a new event, keeping the prior association in the original record. A new member cannot join a group already containing two recorded members.

Packets awaiting the matching peer window can preserve an existing association for at most five seconds since the last match; this does not confirm new acoustic correspondence. Receipt clocks, unavailable comparison and distinct sources without a match open separate occurrences. The canonical timeline projects shared capture timestamps onto the first member's media clock, rather than adding unrelated per-capture offsets. Grouping does not establish a physical sound identity, position or calibrated probability.

No raw audio is retained by grouping, and outgoing artifacts are empty. Private excerpts, when explicitly enabled, use the existing authorized clip store. No sources are started or camera recipes combined automatically.


In the existing audio recipe, expand **Compare another camera**, choose its enabled source and supply that source's normal-sound WAV. Each branch retains its own baseline. The API checks camera-read permission for both cameras and pipeline-write permission, rejects missing/disabled sources, and always saves the recipe disabled. Creating it does not probe or connect to either camera.

The resulting pipeline appears under both cameras. It has two analysis branches, one correlator, one occurrence group and one notifier. Current RTSP sources still use receipt clocks, so they produce separate occurrences even in this recipe; selecting a pair does not establish synchronization.

Notification details show each member progressively, with its observation state, baseline, analysis and optional private excerpt through the existing authorized clip endpoint. Audio is not preloaded. A notification interrupted by restart does not label its saved members as still observing.

Stopped-object recipes preserve recent approach evidence from
`camera.velocity_estimation` before area filtering. `core.stationary_event`
consumes `arrival_evidence_field=payload.velocity.arrival_evidence`, bound to the
subject, source and capture generation. It accepts this evidence only while
fresh (five seconds by default); capture changes, invalid coordinates and real
gaps reset it. Once accepted, arrival remains valid during continuous observation
of that stop. A reset cannot import an older approach again. Legacy graphs can
leave the field empty and continue recognizing arrival locally.

Area selection exposes `area_observation_started_at`. Stopped recipes bind this
to `observation_started_field` so reentry starts a new stationary candidate;
observations outside the region cannot accumulate stationary duration inside it.
The default recipe still requires three valid samples, 1.25 seconds and no more
than 0.35 metres of candidate displacement. A previously parked subject without
observed arrival does not qualify. Person-stopped recipes use the same contract.

Velocity hysteresis keeps a moving subject moving until it slows to 80% of the
configured limit (0.8 km/h for the recipe's 1 km/h default). This transitional
state does not erase arrival or count as stopped. The stationary operator uses
numeric velocity alone only when the boolean state is absent; a stopped flag
with speed above its limit remains invalid. A zero threshold never alternates
zero speed into movement. Generic velocity defaults remain unchanged.

Compact `stationary.decision.*` telemetry distinguishes awaiting arrival,
movement, insufficient samples/duration, excessive displacement, confirmation
and unavailable evidence. Samples are limited to one per reason per second per
operator; they are diagnostic markers, not exhaustive counts of subjects or
proof of physical detection. No full frame logging is required. Gate occlusion
and missing detections still cannot be treated as confirmed stops.

### Regions on a saved panorama

`camera.panorama_region` filters detections using a region painted on the saved
camera/source panorama, including a static image reference. Its panel reuses the
motion-mask drawing surface, with per-stroke brush sizes, erasing, undo and zoom.
The default policy observes the region whenever it is visible; ONVIF and a
composition are not required for passive observation.

Choose the base, center, minimum overlap or whole detection as the spatial
criterion. The reference identifier, revision and geometry remain pinned to the
saved region. Replacing the panorama does not silently relocate an existing
region; editing against the replacement begins a new drawing.

Image matching checks independent correspondences and local support. Cropped
pixels, stale frames, uncertain borders, unsupported geometry and failed matching
are indeterminate, never confirmed absence. Image-plane matching assumes a
locally consistent projective scene; parallax, moving furniture and lighting
changes can make it abstain. This is not a guarantee of physical containment or
pool safety.

The `out` port contains accepted detections. Connect **`observations`** to
`camera.region_occurrence` for presence or observed entry: it retains outside and
indeterminate evidence needed to close/reset temporal events. An entry requires
observed outside evidence first. Downstream notification and storage remain
ordinary pipeline operators.

**Check in image** displays a recorded frame with the projected region. It uses
a recent compatible pipeline decision when available, otherwise explicitly shows
a framing-only preview. Reviewing it never runs notification/storage actions or
requests camera movement. The existing area filter still selects areas created
in the composition; its preview resolves composition and area identifiers and
does not provide a second area editor or change that filter's runtime semantics.

Optional framing requests reuse the existing attention arbitration. They require
one qualified live-panorama profile for the exact source/reference, an enabled
`visual_region` event policy, and confirmation that native tracking is disabled.
Manual control and other priorities can interrupt the request; regions that
cannot fit are refused. An accepted request does not certify actual visibility:
the subsequent image must still qualify. Stopping the pipeline releases its
request. No attention profile or physical calibration is created automatically.

Execution is currently local. Remote activation and preview are explicitly
unavailable until reference artifacts and evaluation can be qualified on that
executor. Automated checks use synthetic images and mocked camera services;
physical alignment, night scenes and PTZ return accuracy require camera-specific
acceptance before relying on the result.
