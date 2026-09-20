const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const context = { exports: {} };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,
  "../src/notifications/humanObservationImage.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, context);
const { readHumanObservationImage, humanImagePrimitives } = context.exports;

// Official Halpe26 metadata contains 27 edges; its six foot edges join each
// ankle to the same-side heel, big toe and small toe, without a heel-to-toe edge.
// https://github.com/open-mmlab/mmpose/blob/537bd8e543ab463fb55120d5caaa1ae22d6aaf06/configs/_base_/datasets/halpe26.py#L175-L237
const halpeNames = ["nose", "left_eye", "right_eye", "left_ear", "right_ear",
  "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
  "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle",
  "head", "neck", "hip", "left_big_toe", "right_big_toe", "left_small_toe", "right_small_toe",
  "left_heel", "right_heel"];

function fixture(names = halpeNames, skeleton = "halpe26") {
  const capture = { capture_instance: "capture", generation: 1, sequence: 1,
    published_at: 1000, physical_timestamp_verified: false };
  const pose = { schema_version: 1, skeleton_id: skeleton, actor_subject_id: "actor",
    landmark_units: "image_fraction", landmark_reference: "stream_image",
    landmarks: names.map((name, index) => ({ name, index, position: [0.2 + index / 100, 0.6],
      model_score: 1.2, visibility: "unknown", provenance: "image_estimate", invalid_reason: null })) };
  const model = { state: "current", expiresAt: 1000750, camera: "camera", timestamp: 42,
    actor: "actor", scopeKey: "actor-camera-stream" };
  const notification = { payload: { packet_id: "frame", parent_packet_id: null, data: {
    camera_id: "camera", source_stream_id: "stream", capture_evidence: capture,
    vision: { pose_frame_packet_id: "frame", poses: [pose] },
  } }, ephemeralImage: { schemaVersion: 1, packetId: "frame", parentPacketId: null,
    cameraId: "camera", sourceStreamId: "stream", mediaTimestamp: 42, captureEvidence: capture,
    expiresAt: 1000750, mimeType: "image/jpeg", dataBase64: "AA==", width: 640, height: 480,
    imageGeometry: { capture_evidence: capture, image_size: [640, 480], source_size: [640, 480],
      to_source: [[1, 0, 0], [0, 1, 0], [0, 0, 1]] } } };
  return { notification, model, pose };
}

function primitives(input) {
  const frame = readHumanObservationImage(input.notification, input.model, 1000000);
  assert.ok(frame);
  return humanImagePrimitives(frame);
}

function edges(result) {
  return Array.from(result.segments, ({ start, end }) => `${start.name}->${end.name}`).sort();
}

test("Halpe26 draws exactly its six official foot edges without renaming landmarks", () => {
  const input = fixture(), before = JSON.stringify(input);
  const result = primitives(input);
  assert.equal(result.points.length, 26);
  const footEdges = edges(result).filter((edge) => /heel|toe|foot_index/.test(edge));
  assert.deepEqual(footEdges, ["left", "right"].flatMap((side) =>
    ["heel", "big_toe", "small_toe"].map((joint) => `${side}_ankle->${side}_${joint}`)).sort());
  assert.equal(JSON.stringify(input), before, "scientific names, scores and positions must be preserved");
});

test("MediaPipe foot_index topology remains unchanged", () => {
  const names = ["left", "right"].flatMap((side) => ["ankle", "heel", "foot_index"].map((joint) => `${side}_${joint}`));
  const result = primitives(fixture(names, "mediapipe_pose_33"));
  assert.equal(result.points.length, 6);
  assert.deepEqual(edges(result), ["left", "right"].flatMap((side) => [
    `${side}_ankle->${side}_heel`, `${side}_heel->${side}_foot_index`, `${side}_ankle->${side}_foot_index`,
  ]).sort());
});

for (const [label, change] of [
  ["outside coordinates", { position: [1.1, 0.6] }],
  ["outside visibility", { visibility: "outside_image" }],
  ["invalid reason", { invalid_reason: "missing" }],
  ["missing coordinates", { position: null }],
  ["nonfinite coordinates", { position: [NaN, 0.6] }],
  ["unsupported provenance", { provenance: "unavailable" }],
]) test(`Halpe toe with ${label} has neither a point nor a connecting segment`, () => {
  const input = fixture();
  Object.assign(input.pose.landmarks.find((point) => point.name === "left_big_toe"), change);
  const result = primitives(input);
  assert.equal(result.points.some((point) => point.name === "left_big_toe"), false);
  assert.equal(edges(result).some((edge) => edge.includes("left_big_toe")), false);
  assert.ok(edges(result).includes("left_ankle->left_small_toe"), "other valid same-side toe remains independent");
});

test("missing Halpe ankle omits all same-side foot edges without dropping valid toes", () => {
  const input = fixture();
  input.pose.landmarks.find((point) => point.name === "left_ankle").position = null;
  const result = primitives(input);
  assert.equal(edges(result).some((edge) => /left_.*(?:heel|toe)/.test(edge)), false);
  assert.ok(result.points.some((point) => point.name === "left_big_toe"));
  assert.ok(edges(result).includes("right_ankle->right_big_toe"));
});
