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

The coordinator has been exercised with Home Assistant 2026.10.2 in an isolated
environment, including a loopback HTTP timeout. Native visual sensor entities,
mapping configuration and a complete Home Assistant installation trial are
separate delivery steps. These checks do not qualify physical alarm use.
