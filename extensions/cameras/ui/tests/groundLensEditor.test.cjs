const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const vm = require("node:vm");

const context = { exports: {} };
vm.runInNewContext(ts.transpileModule(
  fs.readFileSync(path.join(__dirname, "../src/groundLensEditor.ts"), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText, context);
const { groundLensDraft, validateGroundLensDraft, applyGroundLensDraft, coefficientNames } = context.exports;
const plain = (value) => JSON.parse(JSON.stringify(value));
const view = () => ({
  id: "view-one", label: "Measured view", requires_pose_evidence: true,
  pose_reference: { pan: 0.2, tilt: 0.4, zoom: 0.6 },
  stream_scope: { physical_view_id: "optic", compatible_source_ids: ["source"], compatible_roles: ["main"] },
  projection_model: {
    type: "camera_ray_ground_v2", solver_version: 1,
    source_geometry: { width: 1920, height: 1080, rotation_degrees: 90, mirror_x: true, mirror_y: false, content_rect: { x: 0, y: 0, width: 1, height: 0.8 } },
    lens: { type: "identity_rectilinear_v1" },
    correspondences: [{ id: "p", role: "fit", image: { x: 0.3, y: 0.6 }, world: { x: 2, z: 3 } }],
    visual_pose_signature: { custom: "preserved" },
  },
  projection_quality: { status: "ready", calibration_digest: "old", check_errors_meters: [0.01], fit_inliers: 6 },
});
const draft = (choice = "brown4") => ({
  choice, width: "1920", height: "1080", fx: "1.2", fy: "2.1", cx: "0.49", cy: "0.51",
  coefficients: coefficientNames(choice).map((_, index) => String(index / 100)),
});

for (const choice of ["brown4", "brown5", "brown8", "fisheye4"]) {
  test(`${choice}: normalized measured values survive apply, JSON persistence and reopen`, () => {
    const original = view();
    const before = plain(original);
    const input = draft(choice);
    const updated = applyGroundLensDraft(original, input);
    assert.ok(updated);
    assert.deepEqual(plain(original), before, "input view must not be mutated");
    const reopened = plain(updated);
    assert.deepEqual(plain(groundLensDraft(reopened)), plain(input));
    assert.equal(reopened.projection_model.lens.fx, 1.2);
    assert.equal(reopened.projection_model.lens.fy, 2.1);
    assert.deepEqual(reopened.stream_scope, before.stream_scope);
    assert.deepEqual(reopened.pose_reference, before.pose_reference);
    assert.deepEqual(reopened.projection_model.correspondences, before.projection_model.correspondences);
    assert.deepEqual(reopened.projection_model.source_geometry, before.projection_model.source_geometry);
    assert.deepEqual(reopened.projection_model.visual_pose_signature, before.projection_model.visual_pose_signature);
    assert.deepEqual(reopened.projection_quality, { status: "incomplete", estimated: false });
  });
}

test("identity has no fabricated intrinsics or coefficients", () => {
  const input = groundLensDraft(view());
  for (const key of ["fx", "fy", "cx", "cy"]) assert.equal(input[key], "");
  assert.deepEqual(plain(input.coefficients), []);
  assert.deepEqual(plain(validateGroundLensDraft(input).value.lens), { type: "identity_rectilinear_v1" });
  const measuredButIncomplete = { ...input, choice: "brown4", coefficients: ["", "", "", ""] };
  assert.equal(validateGroundLensDraft(measuredButIncomplete).value, undefined);
  assert.equal(applyGroundLensDraft(view(), measuredButIncomplete), null);
});

test("explicit zeros, outside principal point and focal values above one are not clamped", () => {
  const parsed = validateGroundLensDraft({ ...draft(), fx: "1,25", fy: "2e0", cx: "-0.2", cy: "1.1", coefficients: ["0", "-0.01", "0", "0"] });
  assert.deepEqual(plain(parsed.value.lens), { type: "rectilinear_brown_v1", fx: 1.25, fy: 2, cx: -0.2, cy: 1.1, coefficients: [0, -0.01, 0, 0] });
});

for (const [field, values] of Object.entries({
  fx: ["", " ", "NaN", "Infinity", "1e999", "0", "-0.1", "0x10", "true", "1,2,3"],
  fy: ["0", "-2"], cx: ["", "null"], cy: ["undefined", "Infinity"],
  width: ["0", "1", "1.5", "-1920", "9007199254740992"], height: ["", "NaN", "Infinity"],
})) {
  test(`${field}: malformed, unavailable and out-of-domain values are rejected`, () => {
    for (const value of values) {
      const result = validateGroundLensDraft({ ...draft(), [field]: value });
      assert.equal(result.value, undefined, `${field}=${value}`);
      assert.ok(result.invalidFields.includes(field), `${field}=${value}`);
    }
  });
}

test("Brown accepts exactly 4, 5 or 8 terms; KB4 exactly 4", () => {
  for (const count of [0, 1, 3, 6, 7, 9]) {
    assert.equal(validateGroundLensDraft({ ...draft(), coefficients: Array(count).fill("0") }).value, undefined);
  }
  for (const choice of ["brown6", "brown7", "unknown"]) {
    assert.equal(validateGroundLensDraft({ ...draft(), choice }).value, undefined);
  }
  assert.equal(validateGroundLensDraft({ ...draft("fisheye4"), coefficients: ["0", "0", "0", "0", "0"] }).value, undefined);
  assert.equal(validateGroundLensDraft({ ...draft(), coefficients: ["0", "NaN", "0", "0"] }).value, undefined);
  assert.deepEqual(plain(coefficientNames("brown8")), ["k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"]);
});

test("dimension changes persist exactly and never rescale normalized intrinsics implicitly", () => {
  const updated = applyGroundLensDraft(view(), { ...draft(), width: "1280", height: "720" });
  assert.equal(updated.projection_model.source_geometry.width, 1280);
  assert.equal(updated.projection_model.source_geometry.height, 720);
  assert.equal(updated.projection_model.lens.fx, 1.2);
  assert.equal(updated.projection_model.lens.fy, 2.1);
});
