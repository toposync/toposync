# Changelog

## 0.11.0 - 2026-10-24

### Added

- Passive pipeline inspection with paired video frames, overlays and independent consumer demands.
- Reviewed automatic activities, bounded activity image sequences and spatial playback.
- Visual sensors with retained references, local and AI inference, validation records, backup and Home Assistant publication.
- Observed PTZ tracking preparation, bounded moving-target control and reproducible motion diagnostics.

### Changed

- Align all 16 Python packages, the Full Application add-on and Remote Instance add-on at 0.11.0.
- Preserve authenticated remote ingress and improve streaming fallback, privacy revocation and accessible configuration controls.
- Keep Vision activity-evidence registration idempotent when camera and vision setup share one operator registry.

### Upgrade Notes

- Back up the data directory before upgrading. Existing schema version 2 requirements remain in effect; reverse data migrations are not certified.
- Update separately installed processing servers and remote instances to 0.11.0. Updating the Remote Instance add-on does not update its external server.
- Visual sensor qualification and physical PTZ operation require installation-specific validation; a successful package build does not certify either.

## 0.10.0 - 2026-10-17

- Add authenticated Home Assistant remote-instance access with a dedicated proxy
  credential, explicit trusted sources and preserved Home Assistant user identity.
- Prefer proxied HLS for remote instances opened through Home Assistant ingress.
- Document the separate Full Application and Remote Instance add-ons, including
  setup, recovery access and Portuguese configuration guidance.
- Remote instance access requires Toposync 0.10.0 or newer. Existing 0.9.0 installations must be upgraded before configuring the connection.
- Configure a local recovery account, a dedicated proxy credential and explicit trusted sources before enabling remote ingress. Existing local and full add-on authentication remains unchanged when no proxy credential is configured.
- Back up the remote data directory before upgrading. Rolling back to 0.9.0 disables access through the remote instance add-on; direct local login remains available.

## 0.9.0 - 2026-10-16

### Added

- PTZ Attention, a first-party extension for event-directed attention with explicit
  profiles, arbitration, shadow decisions, and guarded panoramic navigation.
- Referenced panoramic region filters, area previews, geographic maps, and contextual controls.
- Identity galleries, pose and gesture processing, and spatial relation events in Vision.

### Changed

- Improved camera mapping, panoramic interfaces, pipeline editing, and Portuguese operator translations.
- Improved notification search, time periods, pagination, and unavailable-image feedback.
- Updated all Python packages and the Home Assistant add-on together to 0.9.0.

### Upgrade Notes

- Pipeline graphs now require schema version 2. Version 1 graphs are rejected;
  export and back up existing configurations and migrate graphs before upgrading.
  The `build_pipeline_graph_v2` helper supports constructing version 2 graphs.
- PTZ Attention is included in the application bundles. Physical operation still
  requires explicit configuration and qualification; package installation does not
  qualify a camera or authorize movement.
- Existing view-based attention profiles require explicit review before migration
  to panoramic profiles. Reads do not infer or rewrite destinations.
- Vision requires ONNX Runtime 1.30 or newer and Toposync Core 0.9.0 or newer.
- To roll back, select the previous package or container version. Preserve a copy
  of the data directory before upgrading; this release does not certify reverse data migrations.
