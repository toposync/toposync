# Home Assistant

First-party extension scaffold to configure one or more Home Assistant servers (host + API token) via the global Settings modal.

## Special visualizations

In the **Home Assistant item** element editor, you can choose a special
visualization for a single item:

- **Light fixture** for compatible domains.
- **Climate airflow** for climate entities.
- **3D model** uploads a `.glb/.gltf` file and uses the model as the
  device/entity visual. When the entity is on or active, the model plays its
  animations when available.

## Spoken event output

Add the `home_assistant.speak` step after the event filter. Its extension panel discovers the server, voice provider (`tts.*`), and speaker (`media_player.*`). The user must explicitly enable spoken output; adding the operator does not enable it. The target must advertise `PLAY_MEDIA`, and `tts.speak` must exist. Discovery and runtime both fail closed for unavailable targets. A TTS entity may report `unknown` before its first use; this is not the same as an unavailable entity.

The optional description uses the existing event template fields. Its default reports the observed category, source/area, and timestamp; it does not infer intent, danger, identity, or unobserved actions. Select relevant events upstream instead of attaching this step to every video frame. Keep a parallel visual notification branch.

The operator retains one active request and one pending occurrence. A newer pending occurrence supersedes the previous one. Repeated occurrence identifiers are deduplicated in a bounded history. Events expire after 30 seconds by default, checked again after discovery and before dispatch. Timing uses the existing immutable `capture_evidence` contract (publication age is explicitly not proof of physical capture age), or an explicit `event_at` Unix timestamp/timezone-bearing ISO timestamp. A new packet envelope never refreshes event time. Missing, invalid, future or expired time prevents speech.

Closing an event cancels its pending/in-flight local request. Once Home Assistant has accepted a request, cancelling the local request cannot retract it. The optional close interruption calls `media_player.media_stop` only when the action and entity STOP feature are present and the dispatch remains within the event validity interval. This option may interrupt other audio on that speaker and defaults to disabled. There is no implicit camera backchannel support.

The output packet includes `speech_output.status`, `playback_confirmed: false`, and advisory player state readback when available. `accepted_unconfirmed` means the service accepted the request, even if readback says `playing`; neither proves audible reproduction. Failed or timed-out requests can have an unknown outcome. Text-to-speech caching is disabled for these event descriptions. No additional text/audio history is stored by this extension; downstream event retention continues to apply.

Use existing upstream schedule filters and Home Assistant volume policies. Packets tagged as playback or already containing a speech receipt are suppressed, but this does **not** prevent acoustic feedback from a microphone hearing a speaker. That requires an explicit microphone mute window or reliable playback causal association. Physical playback, interruption, microphone feedback and provider retention must be tested on an authorized installation before claiming an end-to-end audio acceptance.

Protocol references: [Home Assistant TTS](https://www.home-assistant.io/integrations/tts/), [Media player](https://www.home-assistant.io/integrations/media_player/), and [entity feature values](https://github.com/home-assistant/core/blob/dev/homeassistant/components/media_player/const.py). No new provider is installed or contracted by this extension.
