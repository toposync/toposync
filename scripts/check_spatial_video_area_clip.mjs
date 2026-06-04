import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function loadTypescriptModule(relativePath) {
  const sourcePath = path.join(root, relativePath);
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
      throw new Error(`Unexpected runtime import in spatial video area clip test: ${id}`);
    },
  });
  return module.exports;
}

const { projectionStrategies } = loadTypescriptModule("extensions/spatial_video/ui/src/projection.ts");
const { resolveAreaClipForElement } = loadTypescriptModule("extensions/spatial_video/ui/src/areaClip.ts");

const controlPointSet = {
  id: "view",
  label: "View",
  control_points: [
    { id: "top-left", image: { x: 0, y: 0 }, world: { x: 0, z: 0 } },
    { id: "top-right", image: { x: 1, y: 0 }, world: { x: 10, z: 0 } },
    { id: "bottom-right", image: { x: 1, y: 1 }, world: { x: 10, z: 10 } },
    { id: "bottom-left", image: { x: 0, y: 1 }, world: { x: 0, z: 10 } },
  ],
  refinement_points: [],
};

const clipPolygon = [
  { x: 0, z: 0 },
  { x: 5, z: 0 },
  { x: 5, z: 10 },
  { x: 0, z: 10 },
];

const clippedMesh = projectionStrategies.homography_grid.buildMesh(controlPointSet, {
  gridDivisions: 34,
  clipPolygon,
});
assert.ok(clippedMesh, "expected clipped projection mesh");
assert.equal(clippedMesh.positions.length % 3, 0);
assert.equal(clippedMesh.uvs.length, (clippedMesh.positions.length / 3) * 2);
for (let index = 0; index < clippedMesh.positions.length; index += 3) {
  assert.ok(clippedMesh.positions[index] <= 5.0001, `x should be clipped to area, got ${clippedMesh.positions[index]}`);
  assert.ok(clippedMesh.positions[index] >= -0.0001, `x should stay inside area, got ${clippedMesh.positions[index]}`);
}
for (const value of clippedMesh.uvs) {
  assert.ok(Number.isFinite(value), "UV should stay finite after clipping");
}

const outsideMesh = projectionStrategies.homography_grid.buildMesh(controlPointSet, {
  gridDivisions: 34,
  clipPolygon: [
    { x: 20, z: 20 },
    { x: 30, z: 20 },
    { x: 30, z: 30 },
    { x: 20, z: 30 },
  ],
});
assert.equal(outsideMesh, null, "outside area should produce no projection geometry");

const cameraElement = {
  id: "camera",
  type: "com.toposync.cameras.camera",
  name: "Camera",
  position: { x: 0, y: 0, z: 0 },
  rotation: { x: 0, y: 0, z: 0 },
  props: { spatial_video: { clip_area_element_id: "area-good" } },
};
const areaGood = {
  id: "area-good",
  type: "com.toposync.structural.area",
  name: "Good area",
  position: { x: 0, y: 0, z: 0 },
  rotation: { x: 0, y: 0, z: 0 },
  props: { vertices: clipPolygon },
};
const areaFar = {
  ...areaGood,
  id: "area-far",
  name: "Far area",
  props: {
    vertices: [
      { x: 20, z: 20 },
      { x: 30, z: 20 },
      { x: 30, z: 30 },
      { x: 20, z: 30 },
    ],
  },
};
const elementTypesById = {
  "com.toposync.structural.area": { layerGroup: "areas" },
};
const resolvedGood = resolveAreaClipForElement(cameraElement, [cameraElement, areaGood, areaFar], elementTypesById, [controlPointSet]);
assert.equal(resolvedGood.clip?.areaElementId, "area-good");
assert.equal(resolvedGood.warning, null);

const resolvedFar = resolveAreaClipForElement(
  { ...cameraElement, props: { spatial_video: { clip_area_element_id: "area-far" } } },
  [cameraElement, areaGood, areaFar],
  elementTypesById,
  [controlPointSet],
);
assert.equal(resolvedFar.clip?.areaElementId, "area-far");
assert.equal(resolvedFar.warning, "A área de recorte não cruza nenhuma vista calibrada.");

console.log("Spatial video area clip checks passed");
