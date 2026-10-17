# Changelog

## Unreleased

- Add authenticated Home Assistant remote-instance access with a dedicated proxy
  credential, explicit trusted sources and preserved Home Assistant user identity.
- Prefer proxied HLS for remote instances opened through Home Assistant ingress.
- Document the separate Full Application and Remote Instance add-ons, including
  setup, recovery access and Portuguese configuration guidance.
- Published 0.9.0 packages and images do not include this remote proxy contract.

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
