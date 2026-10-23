# Toposync Home Assistant integration

This custom integration embeds Toposync in Home Assistant and exposes Toposync
transmissions as native Home Assistant `camera` entities.

The integration registers:

- a sidebar panel at `/toposync`;
- a Lovelace card named `custom:toposync-embed-card`;
- native `camera` entities for Toposync transmissions published to Home Assistant.

The embed surface uses a persistent Toposync Home Assistant service token. Do
not paste a short-lived `toposync_at` browser cookie into the integration.

Create a token from an authenticated Toposync owner/admin session:

```sh
curl -X POST http://127.0.0.1:8000/api/auth/home-assistant/token \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer <owner-or-admin-token>' \
  -d '{"label":"Home Assistant","username":"home_assistant","display_name":"Home Assistant"}'
```

Use the returned `token` in the Home Assistant config flow. The flow asks for
two URLs:

- `Toposync URL used by Home Assistant`: the URL reachable from the Home
  Assistant runtime, for example `http://toposync:8000` in Docker Compose.
- `Toposync URL used by browsers`: the URL reachable from the user's browser,
  for example `http://127.0.0.1:18000` in the onboarding lab.

Add the dashboard card with:

```yaml
type: custom:toposync-embed-card
title: Toposync
path: /
height: 720px
show_header: true
allow_fullscreen: true
open_in_new_tab: true
```

The card and panel ask Home Assistant for a short-lived Toposync embed URL. That
URL sets a Toposync session cookie inside the iframe and redirects to the
requested Toposync path. If the browser blocks iframe cookies because Home
Assistant and Toposync are on incompatible sites or insecure cross-site origins,
the card falls back to a clear error and an external-open action.

Each entity is created from `GET /api/streams/home-assistant/cameras`.
`stream_source()` renews a Toposync demand heartbeat with
`source=home_assistant_entity` and returns the internal Toposync/MediaMTX RTSP
URL for the selected `output_id`/`quality_profile_id`.

For Docker Compose, that RTSP URL must be reachable from the Home Assistant
container. Configure Toposync streaming with `engine.expose_to_lan=true`,
restart the streaming engine, and set `TOPOSYNC_HOME_ASSISTANT_RTSP_HOST` to
the Toposync service name visible inside the Compose network, for example
`toposync`. If Toposync would otherwise advertise a loopback RTSP URL to a
non-loopback Home Assistant request, the camera manifest reports a blocking
error instead of creating a broken `rtsp://127.0.0.1:...` camera stream.

Native WebRTC is present as an opt-in scaffold and should stay disabled until
the Toposync `/api/streams/transmissions/{id}/webrtc/offer` path is validated
with Home Assistant Cloud relay/TURN in a real environment.

When native WebRTC is enabled, Home Assistant treats the entity as a WebRTC
camera path. Keep it off unless losing the normal stream-component/HLS fallback
is acceptable for that installation.

## Experimental visual state coordinator

The [visual sensor guide](../../docs/sensores-visuais/guia.md) covers setup, mappings,
compatibility and rollback. Use the [installation protocol](../../docs/sensores-visuais/validacao-de-instalacao.md)
before claiming field qualification; the tests below do not establish it.

Each configured Toposync connection owns a `ToposyncVisualStateCoordinator` in
`hass.data[DOMAIN][entry_id][DATA_VISUAL_STATE_COORDINATOR]`. It reads
`/api/vision/visual-state/states` once during setup and polls at a one-second
interval while it has subscribers. The Home Assistant coordinator serializes
refreshes; each request has a two-second deadline. Missing endpoint support and
HTTP 401/403 use a thirty-second retry interval. Access denial requires checking
the integration token and explicit `visual_state:<sensor_id>` result grants;
this does not automatically grant access or replace camera configuration.

The receiver validates the envelope, class mapping, sample identity and revisions.
It rejects reordered snapshots and retired execution epochs. Remaining validity
is reduced by the entire request duration using the receiver's monotonic clock.
Repeated evidence can only shorten its existing deadline. An independent timer
clears the current class when that deadline expires, including during a blocked
request. Network, authorization and malformed-response failures clear current
values as unavailable; expiration in an accessible installation is unknown.
Neither is a negative class. A restart begins with no restored current value.

Unloading cancels polling, the active HTTP request and the expiration timer.
Responses arriving after cancellation or their request deadline cannot restore a
value. Replay fences retain at most 128 retired server epochs and 128 retired
runtime epochs per configured sensor; exceeding either bound fails closed until
the integration is reloaded. No stale fence is silently evicted. Removed sensor
histories are released with the latest complete inventory.

### Native visual sensor entities

Publication is opt-in per `vision.visual_state` operator. Its `home_assistant`
configuration defaults to disabled. For example:

```json
{
  "enabled": true,
  "kind": "binary",
  "name": "Garage door",
  "on_class_ids": ["open-class-id"],
  "off_class_ids": ["closed-class-id"]
}
```

Use immutable class identifiers in the mapping, not labels or class order.
Binary publication requires at least one enabled class on each side; the sets
must be disjoint. Other classes remain unknown. For enumerated sensors use
`kind: "enum"`; enabled class keys become the options and current value. Labels
remain available as metadata. Unknown and unavailable are reserved states and
cannot be class keys.

In the visual sensor panel, expand **Home Assistant publication → Preview and
mapping**. Choose named states or binary, inspect the preview, and explicitly
map each binary class. Enable publication, apply its settings, then save the
pipeline. Saving alone keeps the previously applied publication settings.
New unmapped classes remain unknown. Unmap a class and apply before removing it;
textual configuration with dangling class identifiers is rejected. Renaming
does not change the mapping. Both sides need an enabled class before binary
publication can be enabled.

The legacy boolean publisher and `input_boolean` do not preserve unknown states
and observation expiry. Connecting a visual sensor to the legacy publisher
produces a qualification warning, including through intermediate operators.
Use native publication for visual sensors. This warning does not change the
legacy publisher's behavior or qualify a sensor for physical alarm use.

Both platforms discover additions from the same coordinator, including when
the initial inventory is empty. Identity includes the integration entry,
sensor identifier and entity kind. Renaming a sensor or class does not change
the entity identity. No value is restored from history at startup. Local expiry
changes an entity to unknown without waiting for another response; transport
failure, removal, lost permission or disabled publication makes it unavailable.
At startup, connection failures and HTTP 408, 429 or 5xx responses use Home
Assistant's automatic setup retry. Authentication and other permanent failures
keep their existing error behavior.

Previously created entities remain unavailable on removal or a change of kind.
They can recover with the same identity when restored; neither the registry nor
history is deleted automatically. After permanently changing the kind, the old
entity can be removed through the usual Home Assistant registry controls.
No sensor invokes Home Assistant services or interprets pipeline lifecycle
events as a boolean value. Reconfirmation timestamps and sample counters are
kept out of entity attributes to avoid a recorder change on every observation.

The installed-integration test below exercises a complete Home Assistant application
bootstrap and the normal configuration flow. The separate process test restarts
Home Assistant and abruptly terminates a synthetic HTTP producer; reloading an
integration alone is not a process restart.
These checks do not qualify physical alarm use.

The entity and coordinator tests run against the official Home Assistant
2026.10.2 Linux image, with networking disabled. A native macOS ARM validation
environment reproduced an interpreter exit crash with an `orjson.Fragment` in
a reference cycle, independently of this integration. Do not treat functional
assertions followed by that crash as a passing test run; use the Linux image
for this validation. No serialization or garbage-collection workaround is
installed by Toposync.

### Isolated application installation test

`tests/test_home_assistant_visual_state_installation.py` copies the integration into
a fresh Home Assistant configuration directory, starts the actual application,
creates its configuration entry through the user flow and discovers both native
entity kinds. Only the Toposync HTTP producer is synthetic. The test checks an
observed state change, expiry while HTTP is blocked, transport timeout, unload,
reload without restoring a valid historical value, identity preservation, recovery
with a new producer epoch and HTTP 403. It asserts that no service is called.
Home Assistant retains registered entities as unavailable after unloading them.

`tests/test_home_assistant_visual_state_processes.py` starts the producer and Home
Assistant in separate owned processes. It kills the producer, checks invalidation,
replaces it with a new epoch and verifies recovery. It then restarts Home Assistant
with its saved configuration while the producer is absent, verifies that history
does not restore a valid value, and checks automatic recovery when the producer
returns. Entity identities survive both restarts and no service is called. This
does not exercise a complete Toposync server or its inference pipeline.

Run from the repository root with Docker and Python 3.10 or later available. The image is pinned to
Home Assistant 2026.10.2; the validated host uses Linux ARM64 inside Docker.
The dedicated internal bridge has no external connectivity, no other containers
and no published ports. A bridge is needed because this Home Assistant version's
HTTP resolver initializes Zeroconf and fails when `--network none` leaves no usable
interface. No host network, Docker socket, device or real configuration is mounted.
The test requires an explicit environment flag and a Docker environment marker;
ordinary test collection must not start discovery on the developer's computer.

```sh
(
  set -eu
  visual_artifacts="$HOME/.codex/task-artifacts/toposync-2/visual-installation"
  mkdir -p "$visual_artifacts"
  visual_run=$(mktemp -d "$visual_artifacts/run.XXXXXX")
  visual_tools="$visual_artifacts/test-tools"
  python3 -m pip install --target "$visual_tools" pytest==9.1.1 pytest-asyncio==1.4.0
  visual_network="toposync-visual-validation-$$"
  docker network create --internal "$visual_network"
  trap 'docker network rm "$visual_network" >/dev/null' EXIT
  docker run --rm --network "$visual_network" --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    -v "$PWD/integrations/home_assistant:/workspace/integrations/home_assistant:ro" \
    -v "$PWD/tests:/workspace/tests:ro" \
    -v "$visual_tools:/test-tools:ro" -v "$visual_run:/artifacts" \
    -e PYTHONPATH=/test-tools:/workspace:/workspace/tests \
    -e TOPOSYNC_HA_INSTALLATION_TEST=1 \
    -e PYTHONDONTWRITEBYTECODE=1 -w /workspace --entrypoint python \
    ghcr.io/home-assistant/home-assistant@sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4 \
    -m pytest -q -p no:cacheprovider \
    tests/test_home_assistant_visual_state_installation.py \
    tests/test_home_assistant_visual_state_processes.py --basetemp /artifacts/data
)
```

The tests write `installation-report.json` and `process-report.json` under their
external test directories.
Its single transition measurement includes receiver polling and entity updates;
it is not a latency percentile, a camera-to-entity measurement or a capacity claim.
Full quality and performance qualification still require the representative corpus
and measurements of the complete producer and inference path.
