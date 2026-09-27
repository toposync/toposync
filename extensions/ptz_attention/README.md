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

The operator offers an opt-in preview of discrete following between calibrated
views (`preview_follow_views`). It creates a shadow profile and never acquires
control or sends movement commands. Mapped event updates come from perception;
the controller does not detect objects or infer their relevance. The preview
requires fresh `capture_evidence`, a stable capture instance and generation,
and increasing frame sequence and publication time. A changed capture epoch
requires a new event occurrence.

A changed view needs repeated observations over the confirmation interval;
oscillation back to the current view resets the candidate. Minimum dwell,
movement budget, maximum focus duration, and target timeout still apply. A
cached manual-control or competing lease signal interrupts the preview and
requires explicit resumption. Decisions and the current view expose the
hypothetical effect. This is discrete view selection, not continuous tracking
or evidence of physical PTZ performance. Preview and physical operators cannot
share the same head.
