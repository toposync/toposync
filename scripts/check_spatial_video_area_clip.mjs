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
      if (id === "three") return require("three");
      if (["react", "@toposync/plugin-api", "../../../cameras/ui/src/elements/panoramaProjection"].includes(id)) return {};
      if (id === "./projection" || id === "./areaClip") return loadTypescriptModule(`extensions/spatial_video/ui/src/${id.slice(2)}.ts`);
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

const { mappedControlPointSets, resolveProjectionCandidates } = loadTypescriptModule("extensions/spatial_video/ui/src/candidates.ts");
const legacyElement = { id: "legacy-camera", type: "camera", props: { camera_id: "legacy", control_point_sets: [controlPointSet] } };
assert.equal(mappedControlPointSets(legacyElement).length, 1);
for (const pointer of [{ job_id: "panorama", revision: 1, source_id: "wide" }, { revision: -1 }, null]) {
  const panoramaElement = { ...legacyElement, id: "panorama-camera", props: { ...legacyElement.props, camera_id: "panorama", panorama_mapping: pointer } };
  assert.equal(mappedControlPointSets(panoramaElement).length, 0, "a selected or invalid panorama must not revive legacy geometry in 2D or 3D");
  const candidates = resolveProjectionCandidates([panoramaElement, legacyElement], {}, [
    { id: "panorama-live", camera_id: "panorama", variants: [{ id: "wide", role: "main", enabled: true }] },
    { id: "legacy-live", camera_id: "legacy", variants: [{ id: "wide", role: "main", enabled: true }] },
  ]);
  assert.equal(candidates.length, 1, "the unrelated legacy camera keeps its layer");
  assert.ok(candidates.every(candidate => candidate.cameraId !== "panorama"), "other sources must not restore the retired layer");
}

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

const uvRectMesh = projectionStrategies.homography_grid.buildMesh(controlPointSet, {
  gridDivisions: 34,
  clipPolygon,
  uvRect: { x: 0.25, y: 0.1, width: 0.5, height: 0.8 },
});
assert.ok(uvRectMesh, "expected clipped projection mesh with UV rect");
for (let index = 0; index < uvRectMesh.uvs.length; index += 2) {
  const u = uvRectMesh.uvs[index];
  const v = uvRectMesh.uvs[index + 1];
  assert.ok(u >= 0.2499 && u <= 0.5001, `U should stay inside remapped clipped content rect, got ${u}`);
  assert.ok(v >= 0.0999 && v <= 0.9001, `V should stay inside remapped content rect, got ${v}`);
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

// Panoramic ground uses the same clipping implementation in both renderers.
const { panoramaGroundMesh } = loadTypescriptModule("extensions/spatial_video/ui/src/panoramaGroundLayer.ts");
const support = [{x:0,z:0}, {x:10,z:0}, {x:10,z:10}, {x:0,z:10}];
const concaveArea = [{x:1,z:1}, {x:7,z:1}, {x:7,z:3}, {x:3,z:3}, {x:3,z:7}, {x:1,z:7}];
function meshArea(mesh) {
  let total = 0;
  for (let offset = 0; offset < mesh.indices.length; offset += 3) {
    const points = Array.from(mesh.indices.slice(offset, offset + 3), index => ({x:mesh.positions[index*3],z:mesh.positions[index*3+2]}));
    const [a,b,c] = points;
    total += Math.abs((b.x-a.x)*(c.z-a.z)-(b.z-a.z)*(c.x-a.x))/2;
    for (const point of [...points, {x:(a.x+b.x+c.x)/3,z:(a.z+b.z+c.z)/3}]) {
      assert.ok(point.x >= 1-1e-6 && point.z >= 1-1e-6 && point.x <= 7+1e-6 && point.z <= 7+1e-6);
      assert.ok(point.x <= 3+1e-6 || point.z <= 3+1e-6, "no triangle may bridge the concave cutout");
    }
  }
  return total;
}
for (const polygon of [concaveArea, [...concaveArea].reverse()]) {
  const mesh = panoramaGroundMesh(support, polygon);
  assert.ok(mesh);
  assert.ok(Math.abs(meshArea(mesh)-20) < 1e-5, "concave area must retain exactly 20 square metres");
}
assert.equal(panoramaGroundMesh(support, [{x:20,z:20},{x:25,z:20},{x:25,z:25},{x:20,z:25}]), null);
assert.equal(panoramaGroundMesh(support, null).indices.length, 6, "removing an area restores the complete support polygon");
console.log("Panorama ground area: concavity, winding, exclusion and removal passed.");
