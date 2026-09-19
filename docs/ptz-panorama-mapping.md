# PTZ panorama mapping: implementation and validation

Author: Mateus Calza

Planning update, September 5, 2026: the [automatic camera panorama plan](plano-panoramica-automatica.md)
supersedes the acquisition UX and mandatory manual-profile direction described
below. It moves panorama ownership to the camera source and preserves existing
composition mapping contracts. The [source panorama implementation](panoramica-automatica-implementacao.md)
now provides the automatic acquisition workflow. The implementation and
synthetic validation recorded below describe the preserved composition mapping
contracts; they do not establish the new workflow's physical accuracy.

This workflow belongs to the cameras extension. It captures overlapping views
at a fixed optical zoom, constructs a spherical image with known pixel-to-ray
geometry, and associates ground places with composition coordinates. The
assistant presents four stages: prepare, link places, verify, and use.

## Contracts and ownership

- `processing/panorama_mapping.py` contains pure geometry, rendering and robust
  estimation. It neither moves cameras nor reads application configuration.
- `panorama.py` owns authenticated routes, persisted jobs, image storage,
  revisions, physical capture, camera checks and activation.
- `CameraPanoramaMappingModal.tsx` uses the existing submodal, floor-plan replica,
  editor tool session, translations, tokens and ingress URL helpers. Scoped
  styles are loaded through `cameraPanoramaStyles.ts`, because extension
  federation builds do not have a CSS loader.
- `camera.camera_mapping` resolves the canonical active panorama through the
  `cameras.panorama.get_active` service and maps individual detection anchors
  using the camera pose attached to their packet. Without a panorama reference,
  existing calibrated-view behavior remains available.
- `ConfigStore.patch_element_props` provides a small generic atomic update with
  expected-value comparison. Camera-specific state remains in the extension.

The active reference is the camera composition element's
`props.panorama_mapping`: `{job_id, revision, source_id, status: "ready"}`.
`previous_panorama_mapping` retains the previous reference. A second active
registry is not maintained. Jobs and images live privately under the configured
data directory at `runtime/cameras/panorama/<job_id>/`.

Job identifiers, source identity, composition geometry, the fixed optical
profile, point revision and independent verification records bind the result.
Changing points invalidates camera checks. A check result must name both its
point and the specific check attempt. Activation rereads the stored reference;
the editor receives that same reference so its normal autosave preserves it.

## Camera profile

A source may provide `metadata.panorama_profile`. If absent, the assistant
exposes initial installer configuration and does not claim hardware readiness.
The schema is:

```text
lens:
  width, height                 physical captured image dimensions, pixels
  fx, fy, cx, cy                intrinsics at the fixed optical zoom, pixels
  distortion                   Brown coefficients: empty or length 4, 5, 8
pan_axis / tilt_axis:
  position_min, position_max    reported device coordinates
  angle_min_radians             physical angle at position_min
  angle_max_radians             physical angle at position_max
zoom                           fixed reported device zoom coordinate
position_tolerance             optional reported coordinate tolerance
settle_timeout_seconds         optional timeout for each movement
```

Angles are radians in storage. Pan zero points along the ray frame's first
horizontal axis; increasing pan rotates toward its second horizontal axis.
Tilt is elevation, positive upwards. A reversed device axis is represented by
decreasing angle endpoints. Normalized ONVIF coordinates are not radians.
The profile assumes a linear axis conversion and a central rotating camera;
devices with nonlinear or mechanically coupled axes need a suitable adapter
before this workflow can establish correct geometry.

The lens currently accepts rectilinear and Brown distortion models. Existing
calibrated-view support for other lens models does not imply support here.
Do not copy example fixture parameters onto physical hardware.

## Geometry and acceptance

Each captured pixel is undistorted and transformed by its verified capture pose
into a unit ray. The panorama uses equirectangular coordinates:

```text
pan = 2π (u - 1/2)
tilt = π (1/2 - v)
ray = [cos(tilt) cos(pan), cos(tilt) sin(pan), sin(tilt)]
```

The renderer keeps coverage and source-image provenance. It chooses the most
frontal contributing image instead of cosmetically warping or filling holes.
Missing image coverage cannot be used as a correspondence.

A projective plane-to-ray matrix maps `[world_x, world_z, 1]` to a ray direction.
Normalized direct linear transformation, bounded deterministic robust sampling
and refinement estimate the matrix from fit pairs. Checks do not participate
in fitting. The inverse supplies ray-to-plane queries with signed-ray, horizon
and support-polygon guards.

Readiness requires at least six fit inliers, at least 80 percent inliers,
nondegenerate geometry and two distinct independent check places inside the
fit support polygon. Angular errors use a one-degree default threshold. This
threshold measures geometric consistency; it is not a measured positioning
accuracy in centimeters. Camera verification separately moves to the check
places and asks the operator to inspect a physically fresh returned image.

Neither good fit residuals nor an operator check proves arbitrary extrapolation.
Runtime accepts only the supported polygon, current source geometry, matching
image dimensions, fixed zoom and safe idle pose evidence attached to the frame.
Missing or conflicting evidence produces an explicit unmapped result.

## Motion and recovery

All effects use the existing single-writer camera controller and its lease,
fence and command receipts. Every move waits for safe settled telemetry at the
requested coordinates. Capture uses the existing physical-fresh snapshot path;
a cached frame is not sufficient evidence. The scan uses an overlapping,
alternating row grid bounded by the selected device limits.

Cancellation must stop only the operation's owned camera session. Losing a
lease cannot justify stopping its new owner. Restarted processes mark unfinished
capture jobs interrupted and require a deliberate user action before moving
again. Saved points and images remain available for recovery. Browser draft
storage assists an unfinished pair; server persistence is authoritative for
saved pairs and activation. Storage errors must remain visible. An inactive
draft can be discarded explicitly in the assistant. Active, previous and busy
jobs are protected from deletion. Restoring the previous valid mapping uses an
atomic expected-reference check and does not move the camera.

Limits are 48 capture positions, 64 million decoded capture pixels per scan,
12 MiB per image, 512 MiB total image storage and 128 persisted jobs. A limit
failure retains the existing active mapping; reduce the scan or discard unused
drafts before retrying.

Images are authenticated application resources, not public extension assets.
No external vision service or new runtime dependency is introduced. Do not put
camera credentials, stream addresses or frame contents into analytics.

## Automated validation

Run the smallest relevant suites while changing each layer, then run the
combined suite before accepting the integrated workflow:

```sh
.venv/bin/python -m pytest -q tests/test_camera_panorama_mapping.py tests/test_camera_panorama_api.py tests/test_camera_panorama_runtime.py tests/test_composition_element_props.py tests/test_camera_ray_ground_mapping.py tests/test_camera_ptz_geometry_safety_v2.py tests/test_cameras_mapping_api.py tests/test_camera_ptz_controller.py
npx tsc -p extensions/cameras/ui/tsconfig.json --noEmit
npm run build:extension-ui -- cameras
npx playwright test --config playwright.panorama.config.js
npm run docs:build
```

Geometry tests use known rays and plane coordinates, outliers, independent
checks, degeneracy, wraparound, image projection and inverse consistency.
Service tests exercise authenticated routes, physical freshness, motion fences,
revision conflicts, cancellation, restart, configuration drift and activation.
Runtime tests verify packet pose safety, source/zoom/frame mismatches, invalid
anchors and compatibility with existing mappings. The ConfigStore test verifies
concurrent additive updates, stale expected values and persistent readback.

The browser fixture in `tests/panorama_browser_fixture.py` runs real extension
routes with a synthetic camera boundary. Its floor image is generated through
an independent pinhole projection, rather than a precomputed calibration result.
It has an isolated data directory and loopback-only inert source addresses.
Its reset endpoint exists only in that test fixture, never the product.

For manual browser inspection, use separate unused ports and the fixture:

```sh
.venv/bin/python tests/panorama_browser_fixture.py --port 8107 --data-dir .toposync-data/panorama-validation-fixture
TOPOSYNC_BACKEND_PORT=8107 TOPOSYNC_FRONTEND_PORT=5177 npm --workspace @toposync/frontend run dev -- --host 127.0.0.1
```

Open the composition named **Validação simulada**, enter editing, open
**Câmera simulada**, and choose **Mapear com panorâmica**. Preserve screenshots
of preparation, point linking, an error/recovery state, camera verification and
activation. Inspect 375, 768 and 1440 pixel widths, keyboard focus, zoom controls,
readability, target size and reduced motion. Automated image fixtures prove
software integration; they do not establish real motor accuracy.

## Measured software validation on September 5, 2026

The combined Python suite above passed 136 tests. The three browser scenarios
passed with automatic isolated server startup and teardown. Camera extension
TypeScript checking and federation build passed; both documentation locales
built successfully. Ruff and whitespace checks passed. The official design gate
accepted the modal and its scoped styles, and screenshots covered 375, 768 and
1440 pixel layouts in both Toposync themes. The browser checked mouse and keyboard
selection, recovery, independent checks, activation and invalidation. These are
software results with a synthetic camera boundary.

Evidence is retained under `.toposync-data/panorama-validation/evidence/`,
including screenshots, design reports and the reproducible runtime benchmark.
The benchmark ran locally on arm64 with Python 3.12.10, warm caches and sequential
calls, using one principal point and nine detection anchors per packet:

| Path | Median | 95th percentile |
| --- | ---: | ---: |
| Panorama geometry with active result in memory | 0.185 ms | 0.204 ms |
| Existing homography geometry | 0.039 ms | 0.043 ms |
| Active panorama service and real ConfigStore, 10 elements | 0.415 ms | 0.478 ms |
| Active panorama service and real ConfigStore, 1,000 elements | 3.636 ms | 3.929 ms |

The service reads configuration once per packet and immediately rechecks
geometry, including mutable in-memory changes. No solver, panorama renderer or
device call runs during these projections. Composition hashing dominates the
large scene case. These measurements exclude inference, capture, HTTP and
concurrent load; they are not a throughput guarantee for Home Assistant hardware.

## Physical and usability validation

Before enabling a physical installation, verify its actual optical profile,
axis conversion, control capabilities, permitted scan sector and planar ground
area. Start the scan explicitly in a suitable operating window. Record the
camera model, firmware, physical source, image dimensions, fixed zoom and
profile provenance alongside the test evidence.

Use distinct ground targets across near, middle, far and edge regions. Fit six
or more, verify independent places, then aim at additional held-out targets
from both approach directions. Record the observed center offset, frame size,
requested and observed poses, capture freshness and movement timing. Repeat
after interruption, reconnect, competing ownership and process restart. Check
the unmapped result while moving and after changing zoom or source geometry.
Do not infer physical accuracy or repeatability from a browser screenshot.

Test with representative first-time users and experienced installers. Observe
whether they complete the task without instruction, recognize suitable ground
places, recover a wrong pair, resume after closing and distinguish saved progress
from confirmed camera accuracy. Record completion, active effort, waiting time,
corrections, abandonment and reported confidence. Progress and quiet milestone
feedback must reflect real work; reward mechanics must never encourage skipping
verification. A user study remains separate evidence from automated tests.

## Scope boundaries

This version does not solve nonplanar ground, multiple floors, variable optical
zoom, unverified optical profiles, object recognition, moving point selection,
arbitrary vendor axis models or full parallax compensation. It does not convert
the panorama into a live 360 video projection or change Semantic PTZ attention's
preset-based view selection. Those existing consumers retain their calibrated
view contract. No real-camera result is claimed by the simulated validation.
