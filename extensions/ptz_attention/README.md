# Toposync PTZ Attention Extension

First-party Toposync extension for lifecycle-aware, event-directed PTZ camera
attention.

It registers the `ptz_attention.request` sink operator, applies strict event
policies per profile, arbitrates every pipeline by physical `ptz_device_id`,
and controls cameras only through the fenced `cameras.control.*` and semantic
`cameras.views.resolve_target` service contracts. Shadow mode records the same
decisions without moving a camera. Runtime events remain in memory; SQLite
persists only profiles, decisions, completed or interrupted sessions, and
recovery-required markers.

Each camera device is one PTZ head (`ptz_device_id == camera_id`); wide and zoom
lenses are sources of that device. After an interrupted active session, the
extension stays faulted and performs no automatic camera I/O until an explicit
return-home completes with fenced ownership and a geometry-safe stable pose.
