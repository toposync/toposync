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

Native WebRTC is present as an opt-in scaffold and should stay disabled until
the Toposync `/api/streams/transmissions/{id}/webrtc/offer` path is validated
with Home Assistant Cloud relay/TURN in a real environment.

When native WebRTC is enabled, Home Assistant treats the entity as a WebRTC
camera path. Keep it off unless losing the normal stream-component/HLS fallback
is acceptable for that installation.
