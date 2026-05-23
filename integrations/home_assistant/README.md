# TopoSync Home Assistant integration

This custom integration exposes TopoSync transmissions as native Home Assistant
`camera` entities. This is the path intended for Home Assistant Cloud support:
Home Assistant owns the camera playback contract, while TopoSync provides RTSP
and still-image endpoints backed by Transmission outputs.

Each entity is created from `GET /api/streams/home-assistant/cameras`.
`stream_source()` renews a TopoSync demand heartbeat with
`source=home_assistant_entity` and returns the internal TopoSync/MediaMTX RTSP
URL for the selected `output_id`/`quality_profile_id`.

Native WebRTC is present as an opt-in scaffold and should stay disabled until
the TopoSync `/api/streams/transmissions/{id}/webrtc/offer` path is validated
with Home Assistant Cloud relay/TURN in a real environment.

When native WebRTC is enabled, Home Assistant treats the entity as a WebRTC
camera path. Keep it off unless losing the normal stream-component/HLS fallback
is acceptable for that installation.
