# Toposync PTZ Attention Extension

First-party event-directed PTZ attention. `ptz_attention.request` sends an explicit
semantic lifecycle to a configured profile; policy priorities arbitrate one
physical head (`ptz_device_id == camera_id`). Wide and zoom lenses remain sources
of that head. The operator does not detect objects or infer event relevance.

Panorama profiles select `source_id`, `composition_id`, `panorama_element_id`, an
explicit `home_target` and a simple `attention_region` polygon in composition
coordinates. The complete event envelope must stay inside the region. Image-only
boxes and legacy preferred destinations are rejected. Resolving through
`cameras.panorama.resolve_target` is read-only and returns a normalized ray bound
to source, composition, element, job and revision.

`shadow` records decisions with zero motor commands. Optional discrete following
uses fresh `capture_evidence`, stable capture instance/generation and increasing
sequence/publication time to confirm target changes. Minimum dwell, movement
budget, hard timeout and stale timeout remain active. Manual ownership interrupts
preview and requires explicit resumption. It is not continuous motor tracking.

`live_panorama` dispatches `cameras.panorama.navigate_attention` under the existing
attention lease, without a second acquisition or new presets. Zoom stays fixed.
Fresh visual arrival must be within 12 pixels, return within 3 pixels, with
confirmed stop, exact destination/command/lease/fence and matching stable motion
epoch. Cancellation drains navigation before fenced cleanup. Manual takeover
never triggers a return. Interrupted sessions remain faulted until explicit,
qualified recovery; process restart never guesses a movement.

Software tests do not qualify hardware. Live attention requires an exact private
qualification record for the mapping revision, source identity, composition,
source artifact and geometry, with independently verified arrival and return.
This delivery does not create that record or promote cameras automatically.

Settings > PTZ Attention creates panorama profiles by default. Previous view
profiles and direct operator configurations remain available for explicit review.
Migrating a profile clears old destination choices in the editor; source mapping,
region and return must be selected and saved. Existing data is not inferred or
rewritten during reads. SQLite retains profiles, decisions, sessions and recovery
markers; runtime events remain in memory.
