import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(root, "extensions/spatial_video/ui/src/ptzProjection.ts");
const source = readFileSync(sourcePath, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  },
}).outputText;

const module = { exports: {} };
vm.runInNewContext(compiled, {
  module,
  exports: module.exports,
  console,
  require: (id) => {
    throw new Error(`Unexpected runtime import in PTZ projection test: ${id}`);
  },
});

const { interpolateControlPointSet, resolveActiveProjectionPose } = module.exports;

function set(id, pan, offsetX, refinements = []) {
  return {
    id,
    label: id,
    pose_reference: { pan, tilt: 0, zoom: 1 },
    control_points: [
      { id: "top-left", image: { x: 0, y: 0 }, world: { x: offsetX, z: 0 } },
      { id: "top-right", image: { x: 1, y: 0 }, world: { x: offsetX + 10, z: 0 } },
      { id: "bottom-right", image: { x: 1, y: 1 }, world: { x: offsetX + 10, z: 10 } },
      { id: "bottom-left", image: { x: 0, y: 1 }, world: { x: offsetX, z: 10 } },
    ],
    refinement_points: refinements,
  };
}

function approx(actual, expected, message) {
  assert.ok(Math.abs(actual - expected) < 0.0001, `${message}: expected ${expected}, got ${actual}`);
}

const left = set("left", 0, 0, [
  { id: "paired", image: { x: 0.5, y: 0.5 }, world: { x: 5, z: 5 } },
  { id: "left-only", image: { x: 0.25, y: 0.25 }, world: { x: 2, z: 2 } },
]);
const right = set("right", 1, 20, [{ id: "paired", image: { x: 0.5, y: 0.5 }, world: { x: 25, z: 5 } }]);

const matched = resolveActiveProjectionPose({
  sets: [left, right],
  fallback: left,
  ptzStatus: { pan: 0, tilt: 0, zoom: 1 },
  presets: [],
});
assert.equal(matched.status, "matched");
assert.equal(matched.set.id, "left");

const interpolated = interpolateControlPointSet({
  sets: [left, right],
  fallback: left,
  ptzStatus: { pan: 0.5, tilt: 0, zoom: 1 },
});
assert.equal(interpolated?.status, "interpolated");
approx(interpolated.set.control_points[0].world.x, 10, "interpolated corner");
const paired = interpolated.set.refinement_points.find((point) => point.id === "paired");
assert.ok(paired, "paired refinement point should be present");
approx(paired.world.x, 15, "interpolated refinement");
assert.ok(
  interpolated.set.refinement_points.some((point) => point.id === "left-only"),
  "dominant-only refinement should be preserved",
);

const extrapolated = interpolateControlPointSet({
  sets: [left, right],
  fallback: left,
  ptzStatus: { pan: 1.2, tilt: 0, zoom: 1 },
});
assert.equal(extrapolated?.status, "extrapolated");

const nearest = interpolateControlPointSet({
  sets: [left, right],
  fallback: left,
  ptzStatus: { pan: 1.5, tilt: 0, zoom: 1 },
});
assert.equal(nearest?.status, "nearest_reference");
assert.equal(nearest.set.id, "right");

const single = resolveActiveProjectionPose({
  sets: [left],
  fallback: left,
  ptzStatus: { pan: 0.6, tilt: 0, zoom: 1 },
  presets: [],
});
assert.equal(single.status, "single_reference");
assert.equal(single.set.id, "left");

console.log("Spatial video PTZ projection checks passed");
