const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const THREE = require("three");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function loadModules(options = {}) {
  const clock = { now: 1000000 }, timers = new Map(), modules = new Map();
  let nextTimer = 0;
  function load(name) {
    if (modules.has(name)) return modules.get(name);
    const extension = fs.existsSync(path.join(__dirname, `../src/notifications/${name}.tsx`)) ? "tsx" : "ts";
    const source = fs.readFileSync(path.join(__dirname, `../src/notifications/${name}.${extension}`), "utf8");
    const context = { exports: {}, Date: { now: () => clock.now },
      performance: { now: () => clock.monotonic ?? clock.now }, AbortController,
      Image: options.Image, URL: options.URL, Blob, atob,
      fetch: options.fetch ?? (async () => ({ ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) })),
      setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, due: (clock.monotonic ?? clock.now) + delay }); return id; },
      clearTimeout: (id) => timers.delete(id),
      require: (specifier) => specifier === "@toposync/plugin-api" ? { resolveToposyncUrl: (url) => `/ingress/test${url}` }
        : specifier === "react" ? options.React ?? React
        : specifier.endsWith(".css") ? {} : specifier.startsWith("./") ? load(specifier.slice(2)) : require(specifier) };
    vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, esModuleInterop: true,
    } }).outputText, context);
    modules.set(name, context.exports);
    return context.exports;
  }
  const runDue = async () => { for (const [id, timer] of [...timers]) if (timers.has(id) && timer.due <= (clock.monotonic ?? clock.now)) { timers.delete(id); await timer.callback(); } };
  return { load, clock, timers, runDue };
}

const { readHumanObservation: read, createHumanObservationReader: reader, HUMAN_OBSERVATION_TYPE: type, humanObservationPayloadPaths } = loadModules().load("humanObservation");
function notification() {
  const identity = { schema_version: 1, actor_subject_id: "person-1", frame_packet_id: "frame-1", timestamp: 42, units: "meters", world_axes: "x_y_up_z", calibration_digest: "calibration-a" };
  const foot = (x) => ({ status: "estimated", position: [x, 0, 3], provenance: "image_estimate", contact: "candidate",
    physical_contact_verified: false, uncertainty_radius_meters: 0.1, valid_for_seconds: 0.8, image_evidence_timestamp: 42 });
  return { id: "notification-1", type, title: "Human", imageUrl: "/not-synchronized.jpg",
    payload: { packet_id: "frame-1", lifecycle: "update", subject: { id: "person-1" }, data: {
      subject: { id: "person-1" }, camera_id: "camera-1", source_stream_id: "source-1",
      capture_evidence: { capture_instance: "capture-1", generation: 1, sequence: 1, published_at: 1000, physical_timestamp_verified: false },
      vision: { pose_media_ts: 42, pose_frame_packet_id: "frame-1", poses: [{ actor_subject_id: "person-1", camera_id: "camera-1", source_stream_id: "source-1" }],
        gestures: { schema_version: 1, status: "active", actor_subject_id: "person-1", camera_id: "camera-1", source_stream_id: "source-1", frame_ts: 42, coordinate_basis: "body_relative_2d",
          active: [{ name: "hands_up", side: "both", evidence: "heuristic_2d", evidence_current: true }], candidates: [] } },
      spatial: {
        camera: { status: "ready", camera_id: "camera-1", frame_packet_id: "frame-1", composition_id: "map-1", physical_view_id: "view-1", geometry: { calibration_digest: "calibration-a" } },
        person_ground: { ...identity, status: "estimated", valid_for_seconds: 0.8,
          body: { status: "estimated", position: [2, 0, 3], provenance: "geometric_hypothesis",
            uncertainty: { kind: "hypothesis_envelope", calibrated: false, minimum: [1.8, 0, 2.8], maximum: [2.2, 0, 3.2] } },
          feet: { left: foot(1.9), right: foot(2.1) } },
        pointing: { ...identity, camera_id: "camera-1", source_stream_id: "source-1", status: "candidate", reason: "intent_and_accuracy_not_confirmed",
          composition_id: "map-1", map_revision: "revision-a", actions_authorized: false,
          provenance: "image_3d_geometric_alignment", origin: [2, 1.5, 3], direction: [1, 0, 0], selected_entity_id: "wall-a",
          candidates: [{ entity_id: "wall-a", central_ray_distance_meters: 4, occlusion: "clear_in_supported_map_geometry" }],
          alignment: { method: "image_rays_and_current_metric_support", scale: 1.15, maximum_reprojection_error: 0.01, metric_accuracy_qualified: false, global_elevation_ambiguity_resolved: false },
          uncertainty: { kind: "uncalibrated_cone", calibrated: false, half_angle_degrees: 12, origin_radius_meters: 0.1 } },
      },
    } },
  };
}

function pairedNotification() {
  const value = notification(), data = value.payload.data;
  Object.assign(data.vision.poses[0], { schema_version: 1, landmark_reference: "stream_image", landmark_units: "image_fraction",
    landmarks: [
      { name: "left_shoulder", position: [0.3, 0.3], visibility: "unknown", provenance: "image_estimate" },
      { name: "left_elbow", position: [0.4, 0.5], visibility: "unknown", provenance: "image_estimate" },
      { name: "left_wrist", position: [1.2, 0.7], visibility: "outside_image", provenance: "image_estimate" },
    ] });
  value.ephemeralImage = { schemaVersion: 1, packetId: "frame-1", parentPacketId: null,
    cameraId: "camera-1", sourceStreamId: "source-1", artifactName: "main", mediaTimestamp: 42,
    captureEvidence: structuredClone(data.capture_evidence), width: 640, height: 480, mimeType: "image/jpeg",
    dataBase64: "AA==", expiresAt: 1000750,
    imageGeometry: { image_size: [640, 480], source_size: [640, 480], to_source: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], capture_evidence: structuredClone(data.capture_evidence) } };
  return value;
}

test("paired image requires exact pose frame, source, capture and normalized coordinate contract", () => {
  const runtime = loadModules(), { readHumanObservationImage: paired } = runtime.load("humanObservationImage");
  const input = pairedNotification();
  assert.ok(paired(input, read(input, 1000000), 1000000));
  for (const mutate of [
    (value) => { value.ephemeralImage.packetId = "other"; },
    (value) => { value.ephemeralImage.parentPacketId = "other"; },
    (value) => { value.ephemeralImage.cameraId = "other"; },
    (value) => { value.ephemeralImage.sourceStreamId = "other"; },
    (value) => { value.ephemeralImage.mediaTimestamp = 41; },
    (value) => { value.ephemeralImage.captureEvidence.sequence = 2; },
    (value) => { value.ephemeralImage.imageGeometry.capture_evidence.sequence = 2; },
    (value) => { value.ephemeralImage.imageGeometry.to_source[0][2] = 5; },
    (value) => { value.ephemeralImage.imageGeometry.image_size[0] = 641; },
    (value) => { value.ephemeralImage.imageGeometry.source_size[0] = 641; },
    (value) => { value.ephemeralImage.expiresAt = 1000751; },
    (value) => { value.ephemeralImage.mimeType = "image/svg+xml"; },
    (value) => { value.payload.data.vision.poses[0].landmark_units = "pixels"; },
    (value) => { value.payload.data.vision.poses[0].landmark_reference = "selected_image"; },
    (value) => { value.payload.data.vision.poses[0].landmarks.push(value.payload.data.vision.poses[0].landmarks[0]); },
  ]) {
    const changed = pairedNotification(); mutate(changed);
    assert.equal(paired(changed, read(changed, 1000000), 1000000), null);
  }
  assert.equal(paired(input, read(input, 1000750), 1000750), null);
  delete input.ephemeralImage;
  assert.equal(paired(input, read(input, 1000000), 1000000), null, "historical imageUrl is not a fallback");
});

test("preview primitives omit out-of-frame points and limbs without changing scientific positions", () => {
  const { readHumanObservationImage: paired, humanImagePrimitives: primitives } = loadModules().load("humanObservationImage");
  const input = pairedNotification(), original = JSON.stringify(input);
  const frame = paired(input, read(input, 1000000), 1000000), drawing = primitives(frame);
  assert.equal(drawing.points.length, 2);
  assert.equal(drawing.segments.length, 1);
  assert.equal(drawing.outside, 1);
  assert.equal(frame.points[2].position[0], 1.2);
  assert.equal(JSON.stringify(input), original);
});

function previewHarness() {
  const states = [], images = [], revoked = [];
  let cursor = 0, cleanup, previousDependencies, nextUrl = 0;
  const hooks = { ...React, useMemo: (callback) => callback(),
    useState: (initial) => { const index = cursor++; if (!(index in states)) states[index] = initial;
      return [states[index], (value) => { states[index] = typeof value === "function" ? value(states[index]) : value; }]; },
    useEffect: (callback, dependencies) => {
      if (!previousDependencies || dependencies.some((value, index) => value !== previousDependencies[index])) {
        cleanup?.(); previousDependencies = dependencies; cleanup = callback();
      }
    } };
  class DecodedImage { constructor() { this.naturalWidth = 640; this.naturalHeight = 480; images.push(this); } }
  const runtime = loadModules({ React: hooks, Image: DecodedImage,
    URL: { createObjectURL: () => `blob:frame-${++nextUrl}`, revokeObjectURL: (url) => revoked.push(url) } });
  const { HumanObservationPreview } = runtime.load("HumanObservationPreview");
  const i18n = { useI18n: () => ({ t: (key) => key }) };
  const render = (input) => { cursor = 0; return HumanObservationPreview({ notification: input, model: read(input, runtime.clock.now), i18n }); };
  return { runtime, images, revoked, render, unmount: () => cleanup?.() };
}

test("preview waits for decoded dimensions and releases Blob URLs on replacement, expiry and unmount", async () => {
  const { runtime, images, revoked, render, unmount } = previewHarness();
  const first = pairedNotification();
  assert.equal(render(first).type, "p");
  images[0].onload();
  const ready = renderToStaticMarkup(render(first));
  assert.match(ready, /blob:frame-1/);
  assert.match(ready, /<svg/);
  const next = pairedNotification();
  next.ephemeralImage = { ...next.ephemeralImage };
  assert.doesNotMatch(renderToStaticMarkup(render(next)), /<img|<svg/, "new descriptor is never drawn over old decoded bytes");
  assert.deepEqual(revoked, ["blob:frame-1"]);
  images[1].naturalWidth = 639;
  images[1].onload();
  assert.doesNotMatch(renderToStaticMarkup(render(next)), /<img|<svg/, "dimension mismatch cannot expose a pose overlay");
  assert.deepEqual(revoked, ["blob:frame-1", "blob:frame-2"]);
  const third = pairedNotification();
  render(third); images[2].onload();
  assert.equal(render(third).type, "figure");
  runtime.clock.now = 1000750;
  await runtime.runDue();
  assert.doesNotMatch(renderToStaticMarkup(render(third)), /<img|<svg/);
  unmount();
  assert.equal(revoked.filter((url) => url === "blob:frame-3").length, 1);
  assert.equal(runtime.timers.size, 0);
});

test("preview reserves only a decoded aspect ratio through loading, expiry and failure", async () => {
  const { runtime, images, render, unmount } = previewHarness();
  const first = pairedNotification();
  assert.doesNotMatch(renderToStaticMarkup(render(first)), /aspect-ratio|<img|<svg/);
  images[0].onload();
  assert.match(renderToStaticMarkup(render(first)), /aspect-ratio:640\s*\/\s*480/);
  const next = pairedNotification();
  let html = renderToStaticMarkup(render(next));
  assert.match(html, /aspect-ratio:640\s*\/\s*480/);
  assert.doesNotMatch(html, /<img|<svg/);
  images[1].onerror();
  html = renderToStaticMarkup(render(next));
  assert.match(html, /aspect-ratio:640\s*\/\s*480/);
  assert.doesNotMatch(html, /<img|<svg/);
  const third = pairedNotification();
  render(third); images[2].onload();
  runtime.clock.now = 1000750;
  await runtime.runDue();
  html = renderToStaticMarkup(render(third));
  assert.match(html, /aspect-ratio:640\s*\/\s*480/);
  assert.match(html, /<figcaption/);
  assert.doesNotMatch(html, /<img|<svg|blob:/);
  unmount();
});

test("preview changes reserved proportions only after successful decode of new dimensions", () => {
  const { images, render, unmount } = previewHarness();
  const first = pairedNotification();
  render(first); images[0].onload(); render(first);
  const next = pairedNotification();
  Object.assign(next.ephemeralImage, { width: 800, height: 400 });
  Object.assign(next.ephemeralImage.imageGeometry, { image_size: [800, 400], source_size: [800, 400] });
  assert.match(renderToStaticMarkup(render(next)), /aspect-ratio:640\s*\/\s*480/);
  Object.assign(images[1], { naturalWidth: 800, naturalHeight: 400 });
  images[1].onload();
  const ready = renderToStaticMarkup(render(next));
  assert.match(ready, /aspect-ratio:800\s*\/\s*400/);
  assert.match(ready, /<img/);
  assert.match(ready, /<svg/);
  unmount();
});

test("preview selection changes remove both old pixels and reserved geometry before another decode", () => {
  const { images, revoked, render, unmount } = previewHarness();
  const first = pairedNotification();
  render(first); images[0].onload(); render(first);
  const next = { ...first, id: "notification-2" };
  const loading = renderToStaticMarkup(render(next));
  assert.doesNotMatch(loading, /aspect-ratio|<img|<svg|blob:/, "even the same descriptor object belongs to a different selection");
  assert.deepEqual(revoked, ["blob:frame-1"]);
  images[1].onload();
  assert.match(renderToStaticMarkup(render(next)), /aspect-ratio:640\s*\/\s*480/);
  const history = { ...notification(), id: "notification-3" };
  assert.doesNotMatch(renderToStaticMarkup(render(history)), /aspect-ratio|<img|<svg|blob:/);
  unmount();
  assert.equal(revoked.length, 2);
});

test("reserved preview space cannot revive pixels after CLOSE or a late decode", () => {
  const { images, revoked, render, unmount } = previewHarness();
  const first = pairedNotification();
  render(first); images[0].onload(); render(first);
  const next = pairedNotification();
  render(next);
  const lateDecode = images[1].onload;
  const closed = pairedNotification();
  closed.payload.lifecycle = "close";
  render(closed);
  lateDecode();
  const html = renderToStaticMarkup(render(closed));
  assert.match(html, /aspect-ratio:640\s*\/\s*480/);
  assert.match(html, /aria-hidden="true" style="grid-area:1 \/ 1;visibility:hidden"/);
  assert.doesNotMatch(html, /<img|<svg|blob:/);
  assert.deepEqual(revoked, ["blob:frame-1", "blob:frame-2"]);
  unmount();
});

test("detail deadline expires textual gestures even when the civil clock moves backwards", () => {
  const hooks = { ...React, useMemo: (callback) => callback(), useState: () => [0, () => {}], useEffect: (callback) => callback() };
  const runtime = loadModules({ React: hooks });
  const { HumanObservationDetails } = runtime.load("HumanObservationRenderer");
  const { createHumanObservationReader } = runtime.load("humanObservation");
  const session = createHumanObservationReader(), input = notification();
  HumanObservationDetails({ notification: input, i18n: { useI18n: () => ({ t: (key) => key }) }, session });
  const entry = [...runtime.timers.entries()].find(([, timer]) => timer.due === 1000750);
  assert.ok(entry);
  runtime.clock.now = 1000001;
  runtime.timers.delete(entry[0]); entry[1].callback();
  const model = session(input, runtime.clock.now);
  assert.equal(model.state, "expired");
  assert.equal(model.gestures.length, 0);
});

test("valid data keeps body, feet and target separate without changing input", () => {
  const input = notification(), before = JSON.stringify(input);
  const model = read(input, 1000100, "map-1");
  assert.equal(model.state, "current");
  assert.equal(model.body.position[0], 2);
  assert.equal(model.left.position[0], 1.9);
  assert.equal(model.right.position[0], 2.1);
  assert.equal(model.pointing.selected, "wall-a");
  assert.equal(model.pointing.ray.length, 4);
  assert.equal(model.gestures[0].evidence, "heuristic_2d");
  assert.equal(model.expiresAt, 1000750);
  assert.equal(JSON.stringify(input), before);
  assert.equal(humanObservationPayloadPaths.length, 16);
});

test("publication expiry is exact, capped, independent of updatedAt and shortened by foot evidence", () => {
  const input = notification();
  input.updatedAt = new Date().toISOString();
  assert.equal(read(input, 1000749).state, "current");
  assert.equal(read(input, 1000750).state, "expired");
  assert.equal(read(input, 999999).reason, "capture_clock_skew");
  input.payload.data.spatial.person_ground.feet.left.valid_for_seconds = 0.1;
  assert.equal(read(input, 1000100).state, "expired");
});

test("duplicate updates, clock rollback and renewed same-capture timestamps do not revive expired data", () => {
  const session = reader("map-1"), input = notification();
  assert.equal(session(input, 1000000).state, "current");
  assert.equal(session(input, 1000750).state, "expired");
  assert.equal(session(input, 1000300).state, "expired");
  input.payload.data.capture_evidence.published_at = 1000.7;
  assert.equal(session(input, 1000750).state, "expired");
});

test("CLOSE hides before inspecting data and cannot be reversed by late updates", () => {
  const input = notification(), session = reader();
  input.payload.lifecycle = "close";
  Object.defineProperty(input.payload, "data", { get() { throw new Error("CLOSE must not inspect geometry"); } });
  assert.equal(read(input, 1000000).state, "closed");
  assert.equal(session(input, 1000000).state, "closed");
  assert.equal(session(notification(), 1000010).state, "closed");
});

for (const [name, mutate] of [
  ["actor", (data) => { data.spatial.person_ground.actor_subject_id = "other"; }],
  ["packet", (data) => { data.vision.pose_frame_packet_id = "other"; }],
  ["camera", (data) => { data.spatial.camera.camera_id = "other"; }],
  ["camera frame", (data) => { data.spatial.camera.frame_packet_id = "other"; }],
  ["source", (data) => { data.vision.poses[0].source_stream_id = "other"; }],
  ["calibration", (data) => { data.spatial.pointing.calibration_digest = "other"; }],
  ["composition", (data) => { data.spatial.pointing.composition_id = "other"; }],
  ["timestamp", (data) => { data.spatial.person_ground.timestamp = 41; }],
  ["NaN", (data) => { data.spatial.person_ground.body.position[0] = NaN; }],
  ["Infinity", (data) => { data.spatial.pointing.direction[0] = Infinity; }],
  ["missing publication", (data) => { delete data.capture_evidence; }],
  ["invalid capture sequence", (data) => { data.capture_evidence.sequence = 0; }],
  ["invalid physical clock flag", (data) => { data.capture_evidence.physical_timestamp_verified = "false"; }],
  ["invented flat digest", (data) => { data.spatial.camera.calibration_digest = "calibration-a"; delete data.spatial.camera.geometry; }],
]) test(`${name} conflict rejects spatial rendering`, () => {
  const input = notification(); mutate(input.payload.data);
  const result = read(input, 1000000);
  assert.equal(result.state, "unavailable");
  assert.equal(result.body.position, null);
  assert.equal(result.pointing.ray, null);
});

test("uncalibrated replay retains identity, pose and gestures but no spatial geometry", () => {
  const input = notification(), data = input.payload.data;
  data.spatial.camera = { status: "unavailable", reason: "ground_calibration_required" };
  data.spatial.person_ground = { status: "unavailable", reason: "metric_intrinsics_required" };
  data.spatial.pointing = { status: "unavailable", reason: "current_metric_camera_required" };
  data.vision.poses[0].landmarks = [{ name: "left_wrist", position: [0.4, 0.2], provenance: "image_estimate" }];
  const result = read(input, 1000000);
  assert.equal(result.state, "current");
  assert.equal(result.actor, "person-1");
  assert.equal(result.camera, "camera-1");
  assert.equal(result.pose2D[0].name, "left_wrist");
  assert.equal(result.gestures[0].name, "hands_up");
  assert.equal(result.body.position, null);
  assert.equal(result.body.reason, "metric_intrinsics_required");
  assert.equal(result.pointing.reason, "current_metric_camera_required");
  assert.equal(result.pointing.ray, null);
  assert.equal(read(input, 1000750).pose2D.length, 0);
});

test("overlay recreation cannot renew the same capture or reopen a closed subject", async () => {
  const runtime = loadModules(), { createHumanObservationRenderer } = runtime.load("HumanObservationRenderer");
  const renderer = createHumanObservationRenderer({ i18n: {} });
  const ctx = { compositionId: "map-1", elements: [] };
  let overlay = renderer.create2DOverlay(ctx, notification());
  await runtime.runDue();
  assert.ok(overlay.pin());
  overlay.dispose();
  runtime.clock.now = 1000750;
  const renewed = notification(); renewed.payload.data.capture_evidence.published_at = 1000.7;
  overlay = renderer.create2DOverlay(ctx, renewed);
  await runtime.runDue();
  assert.equal(overlay.pin(), null);
  overlay.dispose();
  const closed = notification(); closed.payload.lifecycle = "close";
  overlay = renderer.create2DOverlay(ctx, closed);
  overlay.dispose();
  overlay = renderer.create2DOverlay(ctx, renewed);
  await runtime.runDue();
  assert.equal(overlay.pin(), null);
  overlay.dispose();
});

test("unknown dimensions of pointing do not fabricate a ray; unavailable foot stays separate", () => {
  const input = notification();
  input.payload.data.spatial.pointing.candidates[0].central_ray_distance_meters = null;
  input.payload.data.spatial.person_ground.feet.right = { status: "unavailable", reason: "occluded" };
  const result = read(input, 1000000);
  assert.equal(result.pointing.ray, null);
  assert.equal(result.right.position, null);
  assert.equal(result.body.position[0], 2);
  assert.equal(result.left.position[0], 1.9);
});

test("temporal feet require their original image timestamp and never become observed", () => {
  const input = notification(), left = input.payload.data.spatial.person_ground.feet.left;
  Object.assign(left, { provenance: "temporal_prediction", age_seconds: 0.2, image_evidence_timestamp: 41.8, valid_for_seconds: 0.3 });
  assert.equal(read(input, 1000000).left.provenance, "temporal_prediction");
  left.image_evidence_timestamp = 42;
  assert.equal(read(input, 1000000).left.position, null);
});

test("notification scope cannot change across actor, calibration or composition updates", () => {
  for (const modify of [
    (input) => { input.id = "other-notification"; },
    (input) => { input.payload.data.spatial.camera.physical_view_id = "other-view"; },
    (input) => { const data = input.payload.data; data.spatial.camera.geometry.calibration_digest = "new-calibration";
      data.spatial.person_ground.calibration_digest = "new-calibration"; data.spatial.pointing.calibration_digest = "new-calibration"; },
  ]) {
    const session = reader(), input = notification();
    assert.equal(session(input, 1000000).state, "current");
    modify(input);
    assert.equal(session(input, 1000100).state, "unavailable");
  }
});

test("a newer coherent frame can advance, but a delayed older frame cannot replace it", () => {
  const session = reader(), first = notification(), next = notification();
  assert.equal(session(first, 1000000).state, "current");
  const data = next.payload.data;
  next.payload.packet_id = data.vision.pose_frame_packet_id = data.spatial.camera.frame_packet_id = "frame-2";
  data.spatial.person_ground.frame_packet_id = data.spatial.pointing.frame_packet_id = "frame-2";
  data.vision.pose_media_ts = data.spatial.person_ground.timestamp = data.spatial.pointing.timestamp = data.vision.gestures.frame_ts = 42.1;
  data.capture_evidence.sequence = 2;
  data.capture_evidence.published_at = 1000.1;
  assert.equal(session(next, 1000100).state, "current");
  assert.equal(session(first, 1000120).reason, "superseded_frame");
  data.capture_evidence.sequence = 1;
  data.vision.pose_media_ts = data.spatial.person_ground.timestamp = data.spatial.pointing.timestamp = data.vision.gestures.frame_ts = 42.2;
  assert.equal(session(next, 1000120).reason, "superseded_capture_sequence");
});

test("unqualified or missing alignment cannot supply a spatial ray", () => {
  for (const mutate of [
    (value) => { value.actions_authorized = true; },
    (value) => { value.alignment.method = "2d_line"; },
    (value) => { value.alignment.maximum_reprojection_error = 0.4; },
    (value) => { value.uncertainty.calibrated = true; },
    (value) => { delete value.map_revision; },
  ]) {
    const input = notification(); mutate(input.payload.data.spatial.pointing);
    assert.equal(read(input, 1000000).pointing.ray, null);
  }
});

test("2D only returns the body after independent verification, invalidates cached pins at deadline", async () => {
  const runtime = loadModules(), { createHumanObservation2D } = runtime.load("humanObservationOverlays");
  const input = notification();
  let redraws = 0;
  const overlay = createHumanObservation2D({ compositionId: "map-1", elements: [], requestRender: () => redraws++ }, input);
  assert.equal(overlay.pin(), null);
  await runtime.runDue();
  assert.equal(overlay.pin().x, 2);
  assert.equal(overlay.pin().z, 3);
  runtime.clock.now = 1000750;
  const previous = redraws;
  await runtime.runDue();
  assert.ok(redraws > previous);
  assert.equal(overlay.pin(), null);
  runtime.clock.now = 1000001;
  assert.equal(overlay.pin(), null);
  assert.equal(createHumanObservation2D({}, input), null);
  assert.equal(createHumanObservation2D({ compositionId: "other" }, input).pin(), null);
  overlay.dispose();
});

for (const withGestures of [false, true]) test(`ground-only operators render without pointing (gestures=${withGestures})`, async () => {
  const runtime = loadModules(), input = notification();
  delete input.payload.data.spatial.pointing;
  if (!withGestures) delete input.payload.data.vision.gestures;
  input.payload.data.spatial.person_ground.map_revision = "revision-a";
  const { createHumanObservation2D, createHumanObservation3D } = runtime.load("humanObservationOverlays");
  const map = { compositionId: "map-1", elements: [], THREE };
  const flat = createHumanObservation2D(map, input), spatial = createHumanObservation3D(map, input);
  assert.equal(flat.pin(), null, "independent map verification still required");
  await runtime.runDue();
  assert.equal(flat.pin()?.x, 2);
  assert.equal(spatial.object.visible, true);
  assert.ok(spatial.object.getObjectByName("human-body-geometric_hypothesis"));
  assert.ok(spatial.object.getObjectByName("human-left-image_estimate"));
  assert.ok(spatial.object.getObjectByName("human-right-image_estimate"));
  assert.equal(spatial.object.getObjectByName("conditional-pointing-ray"), undefined);
  input.payload.data.spatial.person_ground.map_revision = "changed-map";
  flat.update(input); spatial.update(input);
  assert.equal(flat.pin(), null);
  assert.equal(spatial.object.visible, false);
  await runtime.runDue();
  assert.equal(flat.pin(), null, "stale ground revision is not accepted");
  flat.dispose(); spatial.dispose();
  assert.equal(runtime.timers.size, 0);
});

test("ground and pointing cannot mix revisions of the same map", () => {
  const input = notification();
  input.payload.data.spatial.person_ground.map_revision = "other-revision";
  const result = read(input, 1000000);
  assert.equal(result.state, "unavailable");
  assert.equal(result.reason, "spatial_map_revision_mismatch");
});

test("real Three.js shapes stay distinct; deadline requests redraw and CLOSE clears objects", async () => {
  const runtime = loadModules(), { createHumanObservation3D } = runtime.load("humanObservationOverlays");
  let redraws = 0;
  const overlay = createHumanObservation3D({ compositionId: "map-1", elements: [], THREE, requestRender: () => redraws++ }, notification());
  assert.equal(overlay.object.visible, false);
  await runtime.runDue();
  const body = overlay.object.getObjectByName("human-body-geometric_hypothesis");
  assert.equal(body.geometry.type, "OctahedronGeometry");
  assert.equal(body.position.x, 2);
  assert.equal(overlay.object.getObjectByName("human-left-image_estimate").geometry.type, "BoxGeometry");
  assert.equal(overlay.object.getObjectByName("human-right-image_estimate").geometry.type, "SphereGeometry");
  assert.ok(overlay.object.getObjectByName("uncalibrated-pointing-cone"));
  runtime.clock.now = 1000750;
  await runtime.runDue();
  assert.equal(overlay.object.visible, false);
  assert.ok(redraws >= 2);
  const closed = notification(); closed.payload.lifecycle = "close";
  overlay.update(closed);
  assert.equal(overlay.object.children.length, 0);
  overlay.dispose();
  assert.equal(runtime.timers.size, 0);
});

test("temporal feet keep laterality but use translucent gray markers and dashed uncertainty rings", async () => {
  for (const side of ["left", "right"]) {
    const runtime = loadModules(), input = notification();
    const foot = input.payload.data.spatial.person_ground.feet[side];
    Object.assign(foot, { provenance: "temporal_prediction", age_seconds: 0.2, image_evidence_timestamp: 41.8 });
    const { createHumanObservation3D } = runtime.load("humanObservationOverlays");
    const overlay = createHumanObservation3D({ compositionId: "map-1", elements: [], THREE }, input);
    await runtime.runDue();
    const predicted = overlay.object.getObjectByName(`human-${side}-temporal_prediction`);
    assert.equal(predicted.geometry.type, side === "left" ? "BoxGeometry" : "SphereGeometry");
    assert.equal(predicted.material.color.getHex(), 0x94a3b8);
    assert.equal(predicted.material.transparent, true);
    assert.equal(predicted.material.opacity, 0.55);
    const ring = overlay.object.getObjectByName(`uncalibrated-${side}-foot-radius`);
    assert.equal(ring.material.type, "LineDashedMaterial");
    assert.ok(ring.geometry.attributes.lineDistance);
    const positions = ring.geometry.attributes.position;
    for (let index = 0; index < positions.count; index++) {
      assert.ok(Math.abs(Math.hypot(positions.getX(index), positions.getZ(index)) - foot.uncertainty_radius_meters) < 1e-7);
      assert.equal(positions.getY(index), 0);
    }
    const other = side === "left" ? "right" : "left";
    const imageEstimate = overlay.object.getObjectByName(`human-${other}-image_estimate`);
    assert.equal(imageEstimate.material.color.getHex(), 0x38bdf8);
    assert.equal(imageEstimate.material.opacity, 1);
    assert.equal(overlay.object.getObjectByName(`uncalibrated-${other}-foot-radius`).geometry.type, "RingGeometry");
    overlay.dispose();
  }
});

test("tracker's immediate child envelope preserves source pose and matching spatial frame", () => {
  for (const spatialFrame of ["frame-1", "tracker-event-1"]) {
    const input = notification();
    Object.assign(input.payload, { packet_id: "tracker-event-1", parent_packet_id: "frame-1" });
    input.payload.data.spatial.camera.frame_packet_id = spatialFrame;
    input.payload.data.spatial.person_ground.frame_packet_id = spatialFrame;
    input.payload.data.spatial.pointing.frame_packet_id = spatialFrame;
    assert.equal(read(input, 1000000).state, "current");
    input.payload.parent_packet_id = "different-frame";
    assert.equal(read(input, 1000000).state, "unavailable");
    input.payload.parent_packet_id = "frame-1";
    input.payload.data.spatial.pointing.frame_packet_id = "other-frame";
    assert.equal(read(input, 1000000).state, "unavailable");
  }
});

test("map revision change, unsaved elements and authorization failure hide all overlays", async () => {
  for (const response of [
    { ok: true, json: async () => ({ composition_id: "map-1", map_revision: "new-revision", elements: [] }) },
    { ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [{ id: "new-wall" }] }) },
    { ok: true, json: async () => ({ composition_id: "other", map_revision: "revision-a", elements: [] }) },
    { ok: false },
  ]) {
    let calls = 0;
    const runtime = loadModules({ fetch: async (url, options) => {
      assert.match(url, /^\/ingress\/test\/api\/cameras\/compositions\/map-1\/observation-revision$/);
      assert.equal(options.cache, "no-store");
      if (++calls > 1) return response;
      return { ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) };
    } });
    const { createHumanObservation3D } = runtime.load("humanObservationOverlays");
    const overlay = createHumanObservation3D({ compositionId: "map-1", elements: [], THREE }, notification());
    await runtime.runDue();
    assert.equal(overlay.object.visible, true);
    runtime.clock.now += 100;
    await runtime.runDue();
    assert.equal(overlay.object.visible, false);
    assert.equal(overlay.object.children.length, 0);
    overlay.dispose();
    assert.equal(runtime.timers.size, 0);
  }
});

test("verification cannot renew expired evidence or revive a closed/disposed overlay", async () => {
  for (const action of ["expire", "close", "dispose"]) {
    let resolve;
    const runtime = loadModules({ fetch: () => new Promise((done) => { resolve = done; }) });
    const { createHumanObservation2D } = runtime.load("humanObservationOverlays");
    const overlay = createHumanObservation2D({ compositionId: "map-1", elements: [] }, notification());
    const pending = runtime.runDue();
    if (action === "expire") runtime.clock.now = 1000750;
    if (action === "close") { const closed = notification(); closed.payload.lifecycle = "close"; overlay.update(closed); }
    if (action === "dispose") overlay.dispose();
    resolve({ ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) });
    await pending;
    assert.equal(overlay.pin(), null);
    overlay.dispose();
    assert.equal(runtime.timers.size, 0);
  }
});

test("unchanged map checks preserve geometry; duplicate updates do not blink", async () => {
  const runtime = loadModules(), { createHumanObservation3D } = runtime.load("humanObservationOverlays");
  const overlay = createHumanObservation3D({ compositionId: "map-1", elements: [], THREE }, notification());
  await runtime.runDue();
  const body = overlay.object.children[0];
  runtime.clock.now += 50;
  await runtime.runDue();
  assert.equal(overlay.object.visible, true);
  assert.equal(overlay.object.children[0], body, "unchanged verification must not rebuild geometry");
  overlay.update(notification());
  assert.equal(overlay.object.visible, true, "same map and capture stay verified during update");
  overlay.dispose();
});

test("pending map refresh retains geometry only within the original 100 millisecond lease", async () => {
  let calls = 0, resolve, signal;
  const response = { ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) };
  const runtime = loadModules({ fetch: (_url, options) => {
    if (++calls === 1) return Promise.resolve(response);
    signal = options.signal;
    return new Promise((done) => { resolve = done; });
  } });
  const { createHumanObservation2D } = runtime.load("humanObservationOverlays");
  let redraws = 0;
  const overlay = createHumanObservation2D({ compositionId: "map-1", elements: [], requestRender: () => redraws++ }, notification());
  await runtime.runDue();
  runtime.clock.now += 50;
  const pending = runtime.runDue();
  assert.equal(calls, 2);
  assert.ok(overlay.pin(), "rechecking an unchanged map must not immediately hide geometry");
  const before = redraws;
  runtime.clock.now += 50;
  await runtime.runDue();
  assert.equal(overlay.pin(), null);
  assert.ok(redraws > before, "lease expiry invalidates host pin caches");
  runtime.clock.now += 50;
  await runtime.runDue();
  assert.equal(signal.aborted, true, "hung requests have a bounded lifetime");
  resolve(response);
  await pending;
  assert.equal(overlay.pin(), null, "late response cannot renew the expired request lease");
  overlay.dispose();
  assert.equal(runtime.timers.size, 0);
});

test("a new coherent capture reuses only the map lease, while a changed map hides immediately", async () => {
  const runtime = loadModules(), { createHumanObservation2D } = runtime.load("humanObservationOverlays");
  const overlay = createHumanObservation2D({ compositionId: "map-1", elements: [] }, notification());
  await runtime.runDue();
  runtime.clock.now += 25;
  const next = notification(), data = next.payload.data;
  next.payload.packet_id = data.vision.pose_frame_packet_id = data.spatial.camera.frame_packet_id = "frame-2";
  data.spatial.person_ground.frame_packet_id = data.spatial.pointing.frame_packet_id = "frame-2";
  data.vision.pose_media_ts = data.spatial.person_ground.timestamp = data.spatial.pointing.timestamp = data.vision.gestures.frame_ts = 42.025;
  data.capture_evidence.sequence = 2;
  data.capture_evidence.published_at = 1000.025;
  overlay.update(next);
  assert.ok(overlay.pin());
  runtime.clock.now = 1000100;
  assert.equal(overlay.pin(), null, "a new capture cannot extend map verification without a response");
  await runtime.runDue();
  assert.ok(overlay.pin());
  data.spatial.pointing.map_revision = "revision-b";
  overlay.update(next);
  assert.equal(overlay.pin(), null);
  await runtime.runDue();
  assert.equal(overlay.pin(), null, "persisted revision-a cannot verify revision-b");
  overlay.dispose();
});

test("map verification ignores responses completed after clock rollback or the request deadline", async () => {
  for (const elapsed of [-1, 100]) {
    let resolve;
    const runtime = loadModules({ fetch: () => new Promise((done) => { resolve = done; }) });
    const { createHumanObservationMapGate } = runtime.load("humanObservationMap");
    // Stable current model isolates the map clock check from capture clock guards.
    const model = read(notification(), 1000000);
    const gate = createHumanObservationMapGate("map-1", [], () => model, () => {});
    const pending = runtime.runDue();
    runtime.clock.now += elapsed;
    resolve({ ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) });
    await pending;
    assert.equal(gate.allows(model), false);
    gate.dispose();
    assert.equal(runtime.timers.size, 0);
  }
});

test("civil clock rollback during a map request cannot extend its monotonic lease", async () => {
  let resolve;
  const runtime = loadModules({ fetch: () => new Promise((done) => { resolve = done; }) });
  runtime.clock.monotonic = 1000000;
  const { createHumanObservationMapGate } = runtime.load("humanObservationMap");
  const model = read(notification(), 1000000);
  let redraws = 0;
  const gate = createHumanObservationMapGate("map-1", [], () => model, () => redraws++);
  const pending = runtime.runDue();
  runtime.clock.monotonic += 90;
  runtime.clock.now += 1; // Civil clock moved backwards by 89 ms while waiting.
  resolve({ ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) });
  await pending;
  assert.equal(gate.allows(model), true);
  runtime.clock.monotonic += 11;
  assert.equal(gate.allows(model), false, "100 ms is elapsed time, not civil clock time");
  // Execute the lease timer alone: a queued refresh must not delay invalidation.
  const expiry = [...runtime.timers.entries()].find(([, timer]) => timer.due === 1000100);
  assert.ok(expiry);
  runtime.timers.delete(expiry[0]); expiry[1].callback();
  assert.equal(redraws, 2);
  gate.dispose();
  assert.equal(runtime.timers.size, 0);
});

test("valid map refresh restores geometry hidden by tick before the expiry callback", async () => {
  let calls = 0, resolve;
  const response = { ok: true, json: async () => ({ composition_id: "map-1", map_revision: "revision-a", elements: [] }) };
  const runtime = loadModules({ fetch: () => ++calls === 1 ? Promise.resolve(response) : new Promise((done) => { resolve = done; }) });
  const { createHumanObservation3D } = runtime.load("humanObservationOverlays");
  const overlay = createHumanObservation3D({ compositionId: "map-1", elements: [], THREE }, notification());
  await runtime.runDue();
  runtime.clock.now += 50;
  const pending = runtime.runDue();
  runtime.clock.now += 51;
  overlay.tick();
  assert.equal(overlay.object.visible, false);
  resolve(response);
  await pending;
  assert.equal(overlay.object.visible, true);
  overlay.dispose();
});

for (const elapsed of [101, 750]) test(`3D expiry at ${elapsed} ms requests a final frame before queued timers run`, async () => {
  const runtime = loadModules();
  let redraws = 0;
  const { createHumanObservation3D } = runtime.load("humanObservationOverlays");
  const overlay = createHumanObservation3D({ compositionId: "map-1", elements: [], THREE,
    requestRender: () => redraws++ }, notification());
  try {
    await runtime.runDue();
    assert.equal(overlay.object.visible, true);
    if (elapsed === 750) {
      // Keep the map lease valid until 800 ms to isolate capture expiration.
      runtime.clock.now = 1000700;
      await runtime.runDue();
      assert.equal(overlay.object.visible, true);
    }
    redraws = 0;
    runtime.clock.now = 1000000 + elapsed;
    // Viewport3D renders an otherwise idle frame only when tick returns true.
    // Animation callbacks may observe the deadline before its timer executes.
    const renderFinalFrame = overlay.tick() === true || redraws > 0;
    assert.equal(overlay.object.visible, false);
    assert.equal(renderFinalFrame, true, "hiding the object must also clear its previous canvas pixels");
    assert.equal(overlay.tick(), false, "an already hidden overlay must not keep rendering");
  } finally {
    overlay.dispose();
    assert.equal(runtime.timers.size, 0);
  }
});

test("deadline callbacks establish a permanent expiry watermark even after wall-clock rollback", async () => {
  for (const kind of ["2D", "3D"]) {
    const runtime = loadModules(), module = runtime.load("humanObservationOverlays");
    const overlay = module[`createHumanObservation${kind}`]({ compositionId: "map-1", elements: [], THREE }, notification());
    await runtime.runDue();
    const entry = [...runtime.timers.entries()].find(([, timer]) => timer.due === 1000750);
    assert.ok(entry);
    runtime.clock.now = 1000001; // Real timer elapsed, but the wall clock was turned back.
    runtime.timers.delete(entry[0]);
    entry[1].callback();
    overlay.update(notification());
    await runtime.runDue();
    if (kind === "2D") assert.equal(overlay.pin(), null);
    else assert.equal(overlay.object.visible, false);
    overlay.dispose();
    assert.equal(runtime.timers.size, 0);
  }
});

test("renderer keeps the list noninteractive and exposes selected details without actions", () => {
  const runtime = loadModules();
  const { humanObservationTranslations: locales } = runtime.load("humanObservationTranslations");
  assert.deepEqual(Object.keys(locales.en).sort(), Object.keys(locales["pt-BR"]).sort());
  const translate = (key) => locales.en[key] ?? key;
  const i18n = { useI18n: () => ({ t: translate }), t: translate };
  const { createHumanObservationRenderer } = runtime.load("HumanObservationRenderer");
  const renderer = createHumanObservationRenderer({ i18n });
  assert.equal(renderer.type, type);
  const summary = renderToStaticMarkup(renderer.render(pairedNotification()));
  assert.match(summary, /No physical action is authorized/);
  assert.doesNotMatch(summary, /<img|<details|<button|role="status"/);
  const html = renderToStaticMarkup(renderer.renderDetails(notification()));
  assert.match(html, /Synchronized image unavailable/);
  assert.match(html, /No physical action is authorized/);
  assert.match(html, /translucent gray marker and dashed ring/);
  assert.match(html, /role="status"/);
  assert.doesNotMatch(html, /<img|not-synchronized.jpg|<button/);
});

test("details expose the independent ground map revision without a pointing operator", () => {
  const runtime = loadModules(), input = notification();
  delete input.payload.data.spatial.pointing;
  input.payload.data.spatial.person_ground.map_revision = "ground-only-revision";
  const { createHumanObservationRenderer } = runtime.load("HumanObservationRenderer");
  const { humanObservationTranslations: locales } = runtime.load("humanObservationTranslations");
  for (const locale of ["en", "pt-BR"]) {
    const t = (key) => locales[locale][key] ?? key;
    const renderer = createHumanObservationRenderer({ i18n: { useI18n: () => ({ t }), t } });
    const html = renderToStaticMarkup(renderer.renderDetails(input));
    assert.match(html, /ground-only-revision/);
    assert.doesNotMatch(html, /ext\.cameras\.human\./);
  }
});
