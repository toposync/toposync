# Local recognition of people and pets

This feature adds private visual evidence and a curated gallery to individual pipeline events. It is disabled by default. Installing a model does not enable recognition, and a successful inference does not establish identification accuracy.

## Pipeline setup

Place the operators after individual tracking, before grouping:

`vision.track → vision.identity_evidence → vision.recognize_identity → existing consumers`

For notification-first use, a separate branch can retain the same visit link without waiting for every inference:

```text
tracking → vision.identity_context ┬→ existing notification/grouping path
                                  └→ vision.identity_evidence → vision.recognize_identity
```

Enable the context operator explicitly. Keep the recognition branch without a second notification sink; the interface reads its result through the authorized gallery API. Recent missing occurrences and open results are refreshed for up to one minute. Missing results then show a reloadable error; closed results stop polling. Consumers that require an accepted identity must use the resolver output and `read_identity_decision`, which checks current revisions and evidence age.

This branch uses existing bounded queues and **does not isolate backpressure**: a full recognition queue can delay the common source. Preserve lifecycle with blocking queues, monitor depth/bytes/drop reasons, and size them against the actual workload. The current isolated measurements have not met the maximum ten-percent p95 latency increase target; no production performance claim is made.

Keep spatial mapping and the original frame available before extraction. The current extractor requires an individual `subject` with a stable event identifier, correlation identifier, supported category (`person`, `cat`, `dog`) and normalized bounding box. Frames, group events, ambiguous faces, and already cropped or warped frames receive an explicit abstention reason. Recognition preserves the tracking identifier, lifecycle and world coordinates.

1. Add **Extract identity evidence**. Its model panel shows the selected processing server and installed or missing models. Review each source and usage terms before downloading.
2. Add **Recognize people and pets**. This operator owns gallery resolution at the origin when the graph runs remotely.
3. Enable both operators explicitly. Leave the streaming branch outside this path.
4. In each associated notification step, enable **Keep one notification per visit** under advanced settings (`dedupe_by_occurrence=true`). This opt-in preserves the previous behavior of existing pipelines.
5. Identify visits in notifications or review them in **People and pets**.

Occurrence deduplication requires an event or group event with a subject identifier and a correlation renewed for each visit, as provided by the tracking/grouping operators. Its key includes the logical pipeline/node, source, camera, subject, correlation and configured dedupe template. Keep that template stable throughout the visit; the default uses the tracking subject identifier, never a recognized name. Missing occurrence fields fall back to the legacy behavior. Replays of a stored closed occurrence preserve its record, including trail/images and shutdown closure, even after restart. The UI broadcaster may still emit an update containing that unchanged closed record; this does not provide exactly-once delivery for other sinks or external actions. A custom producer that reuses all occurrence fields across visits cannot distinguish a new visit from replay.

Without a calibrated policy, useful photos remain available for curation and the automatic decision reports `calibration_required`. The default configuration does not supply guessed thresholds. Policies belong in the resolver's advanced configuration, must identify the exact embedding space and species, and must be calibrated on independent sessions before enabling automatic decisions. Similarity is not a probability. Infrared or other unvalidated conditions require their own evaluation.

## Private remote stream

Recognition graphs negotiate both `private_artifacts_v1` and `private_stream_v1` with an authenticated processing server. Non-loopback transport requires HTTPS. Existing graphs without private artifacts retain their original protocol.

The private stream retains at most 64 MiB of serialized replay bodies, accepts an envelope up to 8 MiB, and permits four simultaneous stream connections. Subscriber queues contain only sequence markers. Each origin inbox in a server group carrying private data uses eight blocking slots; acknowledgement follows inbox acceptance, not downstream completion. These are transport bounds, not total process memory bounds: serialized and decoded packets, model weights and consumer work also use memory.

Reconnect within the same processing instance resumes the last contiguous sequence. Oversized events, an evicted replay range, an unexpected sequence or a changed processing instance suspend delivery with an explicit continuity error. The origin does not retry across an unknown gap or silently discard a lifecycle boundary. Inspect the processing diagnostics and retained visit history, then explicitly restart the affected origin pipeline after restoring the processing server. This starts a new stream; it cannot reconstruct events already lost at the remote server and is not exactly-once delivery for external actions. Notification occurrence deduplication remains a separate opt-in.

## Spatial inspection

The extractor retains a bounded private snapshot of the individual world anchor and available mapping/capture provenance. It distinguishes decoder generations, rejects conflicting current projections or capture evidence, and degrades malformed optional data without discarding visual inference. The authorized observation context endpoint exposes this snapshot without embeddings; reference reconstruction preserves the original snapshot.

This context is inspection-only. Current producers do not provide conservative spatial error bounds and synchronized physical capture intervals, so confidence, calibration readiness and publication time cannot justify a physical identity veto. Overlapping cameras, distant estimates and different compositions do not create or reject a visual identity. The physical-contradiction acceptance scenario remains unvalidated.

## Models and provenance

The catalog contains these concrete models; weights remain separate from the package and their hashes are checked before loading.

| Model | Use | Weight license | Primary source |
|---|---|---|---|
| YuNet 2023mar | Locate one face inside a tracked person | MIT | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet) |
| SFace 2021dec | Aligned human facial embedding | Apache-2.0 | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface) |
| Open Noodle Pet Small | Cat and dog individual embedding | Apache-2.0 | [Producer model card](https://huggingface.co/open-noodle/pet-recognition-small) |

The manifests record immutable revisions, hashes, preprocessing and acquisition metadata. Training-data provenance is only partially documented by the producers; the weight license alone does not resolve every data-use question. No claim of complete licensing clearance or production recognition accuracy follows from this catalog entry.

CPU inference uses OpenCV for YuNet/SFace and ONNX Runtime for pets. The existing scheduler runs extraction in one spawned process per server, shared across its pipelines and configuration reloads. That process caches at most two extractor configurations. Images must be uint8, three-channel arrays of at most 12 million pixels before serialization and are checked again by the worker. An embedding space includes weights, preprocessing and face alignment/detector, so vectors of equal length are not necessarily comparable.

Extraction requests a two-second timeout by default (`inference_timeout_seconds` in advanced configuration). A timeout reports unavailable and keeps the concurrency slot occupied until the native call actually finishes, so later timed-out requests do not accumulate inferences. Native imports cannot hold the application's interpreter lock across this process boundary. If the worker dies, its broken executor is discarded and the next request creates another; the failed request is not replayed. A separate 30-second process deadline includes startup and native inference. It remains active after the caller times out and kills only that exclusive extraction process if work does not finish. Shutdown also stops this opted-in process. The next request may recreate it; repeated failures remain unavailable and are not retried automatically. The scheduler uses Python 3.14’s public `kill_workers()` where available and a localized compatibility path to the executor’s owned `multiprocessing.Process` handles on Python 3.11–3.13. The native-hang/deadline/next-worker tests were executed on macOS with Python 3.12. Standalone checks of the same scheduler also passed on installed Python 3.11 and 3.13, preserving another healthy pool and reaping owned workers. These checks do not validate all application dependencies on those versions; the Python 3.14 public path and other operating systems still require validation. See [Python process-pool documentation](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.ProcessPoolExecutor.kill_workers).

A fresh macOS installation previously reproduced an 8.53-second event-loop gap during an OpenCV import in a thread. A process-isolation probe reduced the largest measured gap to 38 milliseconds; the integrated process path passed real inference for all three species. These bounded checks do not establish cold-start performance on every installation or sustained throughput. Initial observations can still time out while the worker starts; the extension does not increase timeouts or silently classify them as unknown. Missing/corrupt weights return unavailable; poor images return insufficient evidence.

## Curation and access

Naming a visit, confirming an association and admitting a photo as a reference are separate actions. The reference checkbox is initially off. Only eligible, explicitly confirmed photos enter matching; repeated frames and automatic predictions never become independent confirmations by themselves.

Group notifications show separate member photos. Select one person or pet before identifying or correcting; that action applies only to the selected occurrence. Group payloads retain occurrence links, never a shared recognized identity.

Use the profile to rename, select photos, change their association, add or remove references, or merge identities. Merge and association corrections can be undone from recent changes; a later incompatible human edit produces a conflict rather than being overwritten. Deletion explicitly removes the identity, associated photos, embeddings and related undo history. Deletion cannot be undone through the gallery.

Photo review is paginated. **Reload** starts a fresh view and clears selection. New observations do not silently shift an already loaded page. A curation revision invalidates old page cursors. A proposed cluster is only a review suggestion, and selecting a visible group applies to the photos actually shown in that selection.

Gallery access requires the vision identity permission and access to all sources associated with the identity. Historical source scope persists after unassignment. Profiles with unknown provenance are restricted to the owner. Read-only users can inspect authorized photos; the server checks write permissions again for every operation. Notification cards request only their authorized current identity and suggestions; the full picker loads when identification is opened. Processing and generic notifications do not expose names, candidates or embedding vectors as ordinary payload fields.

## Storage, backup and recovery

The gallery uses SQLite with transactional updates. Names, evidence, crops and feedback bodies are encrypted with a local Fernet key. The installation gallery directory is private to the service account, and key/database files use restrictive permissions. Anyone who obtains both the key and database can decrypt them; encryption does not protect against compromise of the running service account.

From a source checkout with the project environment installed:

```sh
uv run python scripts/identity/backup_gallery.py backup \
  --data-dir /absolute/path/to/disposable-or-authorized-data \
  --destination /absolute/path/to/new-private-backup

uv run python scripts/identity/backup_gallery.py restore \
  --backup /absolute/path/to/private-backup \
  --new-data-dir /absolute/path/to/new-recovery-directory
```

The backup includes the encryption key. Keep the entire backup private. The SQLite backup API takes a consistent snapshot, and a completion manifest is written last. Recovery checks hashes, SQLite integrity, source scope and encrypted content, then creates a fresh directory. It refuses to overwrite an existing gallery. An interrupted or rejected recovery remains separate from the running application.

This is a **gallery backup**, not a complete application backup. Recovery does not switch the application's data directory, restore notifications/configuration, download weights, or promise compatibility with older binaries. Validate the recovered gallery in an isolated application before any authorized operational switch. Backups can contain identities deleted after the snapshot; their lifecycle must follow the installation's data policy.

Automatic retention is off by default, including migrated installations. In **People and pets → Photo retention**, a curator with access to every gallery source can review the impact and explicitly enable expiration. The fixed policy keeps non-reference observations for seven days, references for 365 days, and undo history for 90 days. Observation age uses local storage time; promoting an old photo to a reference does not reset its age. Visit decisions expire after 90 days without new locally received evidence once no photos remain. Old galleries begin that visit-history clock at migration because previous local receipt times cannot be established safely.

Maintenance runs once a minute and deletes at most 128 observations, 128 history operations and 128 empty visit records per cycle. Backlogs drain over multiple cycles; these periods are eligibility thresholds, not hard deletion deadlines. Erasure removes encrypted crops, original vectors and all derived representations transactionally, and advances the gallery revision so in-flight inference, staged reconstruction and old page cursors cannot commit against erased data. Human names and historical camera authorization remain. An undo whose photos have expired is rejected rather than recreating them. Removing references can reduce future recognition coverage; enroll new authorized photos when needed.

Minimal opaque observation identifiers and lifecycle tombstones remain to reject delayed replay; they contain no photos or vectors and are not pruned automatically. They accumulate over installation lifetime and are outside the 10,000 live-observation budget. This is a deliberate integrity tradeoff, not total erasure of every identifier. Backup snapshots are unaffected: a snapshot taken before deletion may restore older data, so protect and expire backups separately. Disabling the policy stops future maintenance deletions but cannot undo past expiration. A full gallery still abstains safely if maintenance cannot keep up.

## Disable and rollback

Disable both recognition operators to stop inference and gallery writes in the pipeline. Existing tracking and notification consumers keep their original identifiers and lifecycle. Remove the operators before running a version that does not know their types. Preserve a verified gallery backup before a software or model change; do not overwrite embeddings in place or mix incompatible spaces. `scripts/identity/rebuild_references.py` stages derived references in batches of at most 32 photos, without holding the gallery transaction during inference. It resumes completed batches after restart and rejects a batch if human curation changed its revision. Staging never participates in matching until explicit activation; missing or unusable retained photos block activation. Corrupt photos are recorded as unusable per item, allowing healthy references in the batch to finish. Output dimensions must match the selected adapter. Derived spaces are limited to three; an explicit `discard --expected-revision N` frees an inactive space while preserving original observations and human feedback. Active representations must first be deactivated; disposal is never automatic. Removing a reference or correcting its identity applies to both original and derived representations.

Use the command with `--data-dir`, `--species` and an installed `--model-id`, followed by `status`, `stage --expected-revision N`, `activate --expected-revision N`, or `deactivate --expected-revision N`. Optionally provide `--model-data-dir` for a separate data directory containing installed `vision-models/`, such as when checking a restored gallery. The reported revision must match the current gallery. A failed stage can be retried explicitly with `--retry-failed` after its cause is resolved. Activation changes only eligibility of the staged representation: it does not select a pipeline model, install a policy or establish accuracy. Switching models still requires a calibrated profile for the exact new space; reverting pipeline model/policy to the previous space retains the original reference vectors. A tested storage rollback is not proof of compatibility with an older application binary.

## Reproducible checks

Focused software tests cover private transport, access, transactional curation, clustering, paging, restart and fault recovery. `scripts/identity/real_pipeline_smoke.py` additionally accepts three hash-pinned, public test photos and installed models, then runs the actual scheduler, both recognition operators and notification persistence in a new directory. It checks lifecycle, deduplication, retained evidence and reopen consistency. It intentionally repeats one photo per species and therefore **does not evaluate recognition accuracy or independent visits**. `scripts/identity/real_distributed_smoke.py` exercises a separate authenticated processing server with the actual models, resolution/notifications at the origin and partial replay after closure. It prepares these same photos at a maximum of 1024×1024 pixels and explicitly enables occurrence deduplication; it does not evaluate transport saturation or biometric accuracy.

Release acceptance additionally requires independent enrollment/development/holdout sessions, unknown identities and similar pets, frozen coverage/eligibility criteria, false-identification bounds, equivalent-load latency measurements, and rendered user journeys. Those conclusions cannot be obtained from the smoke check.

Use the [offline evaluation protocol](IDENTITY_EVALUATION.md) to validate a frozen, authorized dataset and report visit-level results with uncertainty. Synthetic accounting tests of that tool do not establish recognition quality.

## Paired development packages

This development snapshot uses core and vision version `0.9.0.dev0`; it is not a published stable release. Vision requires core `>=0.9.0.dev0` both in wheel metadata and its extension manifest. The existing installer pins the running core during extension resolution, so installing this vision package on an older core is rejected before replacement. The extension manager also rejects an incompatible core before setup. Upgrade the paired core package before enabling this vision version; a wheel built from the older `0.8.0` core does not implement private artifacts or the bounded process executor used here.
