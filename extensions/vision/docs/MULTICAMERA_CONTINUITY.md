# Manual multicamera continuity

Connect mapped `vision.track` outputs through `core.merge_streams` to
`vision.associate_entities`. Its configuration panel declares a unique scope,
source and camera identifiers, identity policies, verified clock domain and
uncertainty, coordinate reference, position error and directed transition windows.
The coordinate reference must match the mapper's composition identifier. Missing
clock or valid capture-bound geometry causes cross-camera abstention. PTZ motion,
calibration changes and capture restarts invalidate previous geometric continuity.

Transition residuals and competitor margins are geometric criteria, not calibrated
probabilities or physical speed limits. Observed velocity can predict a candidate
position; an unobserved interval remains a gap. A unique candidate must win both
within its observation and against competing observations in the bounded batch.
Simultaneous views must agree. Local track identifiers never identify a person
across cameras. Categories do not mix.

Use **Review saved observations** in the operator panel to inspect temporary
entities, uncertainty reasons and group episodes. Groups represent observed
proximity and preserve individual members through splits and reunions; they do
not infer intent. The optional group radius must be chosen for the mapped scene.
Select a local observation and an existing entity, or separate it into a new
entity, then apply a human correction. The entire selected local trajectory is
corrected with a revision check. Corrections override inference, survive restart
within retention and never train the identity gallery. Conflicting simultaneous
tracks in the same source cannot be merged. Previous group decisions and compact
correction lineage are retained within the same window.

`no_identification` prevents cross-camera identity continuity. In
`keep_confirmed_identity`, the associator can carry a validated identity from a
`recognize_here` observation. It never creates enrollment or recognition evidence.
The private review rechecks current gallery decisions and camera permissions;
revoked or expired evidence cannot name an entity on refresh. Conflicting names
are marked ambiguous. Generic pipeline output contains temporary entity IDs,
revision, reason and geometric components; it contains no gallery name, identity
identifier or embedding. Existing upstream packet fields remain subject to their
own output contract and authorization.

Declare a named pipeline output with the `association` payload key and one explicit
state owner, using the shared-result contract. The panel's manual consumer lists
available outputs, opens a 30-second lease, reads the most recent result, renews
only on request, and releases its own lease. Slow consumers may coalesce results;
the review holds retained trajectory observations. Inspection uses the existing
pipeline inspection service and reports association decisions without biometrics.

State has one live owner per scope. Defaults are a 120-second anonymous retention
window, 2,048 observations, 128 observations per batch and a 100 ms batch deadline.
The configuration bounds all of these. Persisted state is written atomically in
`vision-associations` under the configured application data directory. Changing
the scope configuration invalidates its previous projection. Restart records a
gap; replayed observations neither create entities nor reopen group episodes.
The correction log is limited to 256 entries and groups to 128 episodes. Expiry is
checked on reads/writes and by maintenance. This is temporary continuity, not a
permanent visit archive.

Post-tracker fixtures and synthetic gallery vectors validate the software
contract. They do not establish detector/tracker quality, clock synchronization,
calibration accuracy or recognition performance on actual video.
