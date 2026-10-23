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

The operator also preserves a subject's image box as `framing_observation` when
its source, capture and image transformation are known. Ground coordinates remain
the region/routing anchor. Status and decision details expose `observed_framing` with
the box in the original source, extent, margin, boundary contact, publication age
and whether it belongs to the selected source. An update without this evidence
clears the current observation. Contact with a crop boundary remains visible even
when the recovered box is inside the original image. This observation does not
certify arrival, subject completeness or association with another lens. A single
subject box is not used as evidence for a world-space group envelope.

When visual mapping supplies the same frame's calibrated lens and rotation,
the observation also carries the center and corner directions in panorama space,
bound to mapping revision, source artifact and geometry digest. It uses the
original image transformation and never substitutes current device pose or
projects the subject's upper body onto the ground. Missing or incompatible
geometry leaves the image observation available without panorama directions.
The navigator can aim at this observed center while retaining the ground anchor
for region validation and the returned destination. It checks the exact optical
reference, center/corner support and observation age before navigation and before
subsequent pulses, using the profile's existing following observation timeout.
Frame updates at an unchanged target refresh evidence without requesting another
approach. Missing, expired or incompatible evidence during framed following stops
that navigation; it never silently switches to aiming at the feet. Return uses
only its own reference and rejects subject framing. These checks do not certify
full subject visibility, group framing or association with another lens.

`shadow` records decisions with zero motor commands. Optional following
uses fresh `capture_evidence`, stable capture instance/generation and increasing
sequence/publication time to confirm target changes. Minimum dwell, movement
budget, hard timeout and stale timeout remain active. Manual ownership interrupts
preview and requires explicit resumption.

`live_panorama` dispatches `cameras.panorama.navigate_attention` under the existing
attention lease, without a second acquisition or new presets. Zoom stays fixed.
Fresh visual arrival must be within 12 pixels, return within 3 pixels, with
confirmed stop, exact destination/command/lease/fence and matching stable motion
epoch. Cancellation drains navigation before fenced cleanup. Manual takeover
never triggers a return. Interrupted sessions remain faulted until explicit,
qualified recovery; process restart never guesses a movement.

With `follow_view_updates`, `live_panorama` supplies the latest qualified ray to
the visual navigator during an approach. `follow_interval_seconds` limits new
approaches; `follow_observation_timeout_seconds` stops corrections when images
expire. A fresh observation can restart confirmation after temporary loss;
explicit CLOSE and changed capture generations cannot reopen that event.
Higher-priority events cancel and drain the previous approach before taking
control. This is bounded visual feedback, with physical latency dependent on
capture, localization and the device's observed response.

Serial panorama attention operations may reuse protocol discovery for up to
30 seconds under the same lease, fence, source, mapping and geometry revision.
Only a successfully stopped and closed operation supplies this metadata. Each
operation still refreshes pose and native automation, checks current configuration
and ownership, and obtains a new stopped image. The original discovery age is
preserved across reuse; failures discard the entry. No control lease, capture
consumer, image or Stop receipt is copied. `discovery_reused` in the navigation
trace identifies the optimization; it does not certify a physical latency gain.
Successful stopped operations can retain their measured local motor response in
memory for up to five minutes, bound to the exact optical identity and panorama
revision. Fresh localization invalidates an axis response beyond 15 degrees of
view change. Reuse never substitutes for fresh arrival evidence, extends its own
expiry, or survives a process restart. Uncertain stop or navigation failures
discard it; observation expiry does not qualify a new response.

Software tests do not qualify hardware. Live attention requires an exact private
qualification record for the mapping revision, source identity, composition,
source artifact and geometry, with independently verified arrival and return.
An explicitly requested
`POST /api/cameras/cameras/{camera_id}/panorama/{job_id}/qualify-attention`,
with `revision`, `home` and a distinct `target` in world coordinates, performs a
supervised home→target→home trip. Only observed movement, arrival within 12
pixels, home/return within 3 pixels and confirmed Stop can produce the record.
Failure or cancellation removes qualification. Reads never qualify a device.
Qualification of one source does not certify a second zoom lens or moving-target
performance; both need observed image evidence.

Settings > PTZ Attention creates panorama profiles by default. Previous view
profiles and direct operator configurations remain available for explicit review.
Migrating a profile clears old destination choices in the editor; source mapping,
region and return must be selected and saved. Existing data is not inferred or
rewritten during reads. SQLite retains profiles, decisions, sessions and recovery
markers; runtime events remain in memory.

Decision timing distinguishes event observation age (`event_to_decision_ms`, a
wall-clock age of the latest intent) from elapsed controller time. It is not
detector processing time or camera exposure latency. `candidate_elapsed_ms`
starts when a valid candidate request enters the controller and survives UPDATE
and reconfirmation; it includes resolution, selection waits and arbitration after
the device lock is acquired. It does not include time waiting for that lock.
`candidate_confirm_seconds` reports the configured confirmation wait, not a
measured duration to subtract from that total. A fresh candidate after observation
expiry starts a new interval. `command_elapsed_ms` starts at navigation dispatch,
after lease acquisition, and includes preparation and verification, not just motor
travel. These elapsed intervals use the controller's monotonic clock.

Only verified arrival produces `candidate_to_arrival_ms` and
`command_to_arrival_ms`; repeated arrivals for an existing candidate retain its
original candidate interval. Fault decisions retain the interrupted command's
identifier and event key and never imply arrival. Shadow decisions have no physical
arrival metrics. Missing or interrupted acquisitions must remain failures or
incomplete samples in an evaluation, never be dropped from the denominator.

The existing session journal retains the mode, source, panorama binding, initial
capture evidence and command identifier. Verified focus stores its decision
identifier and timing before the session ends. `arrival_verified` describes event
acquisition; `return_verified` is separate and requires a confirmed physical
return. Shadow never sets either flag. Ending a session merges evidence instead
of discarding it, so pause, preemption, fault and shutdown remain evaluable.
`session_elapsed_ms` includes holding and cleanup; it is not acquisition latency.
Decisions and sessions identify their controller monotonic `clock_scope`.
On restart, open sessions retain their last evidence and end as
`process_restarted`; no elapsed duration is invented across clock scopes. The
physical recovery requirement is immutable through diagnostic updates. These
records do not establish camera exposure time or independently certify tracking.

### Bounded campaigns

Profiles may set `campaign_ends_at` to a finite positive Unix timestamp, also
editable as a local date and time in the policy form. Omitting it retains the
existing continuous profile behavior. The end time is persisted; a monotonic
budget within the running controller prevents clock rollback, UPDATE or pause
from extending that campaign. Each physical acquisition passes its remaining
budget as `maximum_duration_s` to the camera control service. The camera lease
watchdog enforces that ceiling independently of attention ticks or browser polls.

On expiry the profile is persisted as paused before cleanup, new attention is
rejected, and Stop/release is attempted. An unconfirmed position or cleanup keeps
the durable recovery requirement. Resume is refused after expiry, including
after restart. A deadline does not prove arrival, Stop or return and does not
retroactively cancel a command already transmitted. Restart has no trustworthy
cross-process elapsed-time measurement; the persisted end uses civil time and
existing physical recovery guards remain mandatory.

The explicit **Return home** action remains available for recovery after expiry.
It receives a separate lease budget of `settle_timeout_seconds`; it does not
resume the campaign or accept targets. Repeated requests while returning reuse
that attempt and cannot extend its budget. **Stop return** interrupts it while
preserving the unconfirmed return. After verified recovery, edit the campaign
end before resuming. This is a user command, not an automatic post-deadline return.
Only the authorized return API opts into `allow_expired_recovery`; internal
pipeline interlocks use the default refusal after expiry.

The controller reserves `settle_timeout_seconds` plus one controller tick before
the end for return. It pauses new attention and resumes observation of an existing
return without restarting it, or begins a qualified return when focus was active.
An idle campaign closes without moving; a delayed acquisition cannot start focus
inside that reserved window. Late events leave the return state intact. This
uses the configured operation budget, not a claim that hardware can meet it;
the hard limit wins if return is still pending. Qualification of device-side motion timeout
and real Stop/return is still required: process loss or an event-loop stall cannot
be covered by an in-process watchdog alone.
