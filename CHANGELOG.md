# Changelog

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

- PTZ Attention is included in the application bundles. Physical operation still
  requires explicit configuration and qualification; package installation does not
  qualify a camera or authorize movement.
- Existing view-based attention profiles require explicit review before migration
  to panoramic profiles. Reads do not infer or rewrite destinations.
- Vision requires ONNX Runtime 1.30 or newer and Toposync Core 0.9.0 or newer.
- To roll back, select the previous package or container version. Preserve a copy
  of the data directory before upgrading; this release does not certify reverse data migrations.
