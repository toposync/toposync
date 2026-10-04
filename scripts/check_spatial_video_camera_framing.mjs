import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import * as THREE from "three";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("../extensions/spatial_video/ui/src/SpatialVideo3DView.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("SpatialVideo3DView.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declaration = tree.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "fitCameraAngledOverview");
assert.ok(declaration, "test the framing function used by the rendered view");
const compiled = ts.transpileModule(`${declaration.getText(tree)}\nmodule.exports = fitCameraAngledOverview;`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const module = { exports: {} };
vm.runInNewContext(compiled, { THREE, module });
const fit = module.exports;

for (const aspect of [1280 / 900, 390 / 844]) {
  for (const bounds of [
    new THREE.Box3(new THREE.Vector3(-2, 0, -3), new THREE.Vector3(4, 3, 10)),
    new THREE.Box3(new THREE.Vector3(-20, 0, -5), new THREE.Vector3(40, 3, 120)),
  ]) {
    const camera = new THREE.PerspectiveCamera(65, aspect, 0.1, 250);
    const controls = {
      target: new THREE.Vector3(), minDistance: 0.3, maxDistance: 60,
      update() { camera.lookAt(this.target); camera.updateMatrixWorld(true); },
    };
    fit(camera, controls, bounds);
    assert.ok(camera.position.toArray().every(Number.isFinite));
    assert.ok(camera.position.distanceTo(controls.target) < 1000, "framing must not run away when the far plane clips a corner");
    for (const x of [bounds.min.x, bounds.max.x]) {
      for (const y of [bounds.min.y, bounds.max.y]) {
        for (const z of [bounds.min.z, bounds.max.z]) {
          const projected = new THREE.Vector3(x, y, z).project(camera);
          assert.ok(Math.abs(projected.x) <= 1 && Math.abs(projected.y) <= 1 && Math.abs(projected.z) <= 1,
            `scene corner must remain visible at aspect ${aspect}: ${projected.toArray()}`);
        }
      }
    }
  }
}
console.log("Spatial video framing: desktop and narrow viewport corners remain visible");
