import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const ts = require("typescript");
function load(name, imports) {
  const source = readFileSync(new URL(`../extensions/spatial_video/ui/src/${name}.ts`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(compiled, { module, exports: module.exports, URLSearchParams,
    require: (id) => { assert.ok(Object.hasOwn(imports, id), `Unexpected import ${id}`); return imports[id]; },
  });
  return module.exports;
}
const areaClip = load("areaClip", {});
const { resolveProjectionCandidates } = load("candidates", { "./areaClip": areaClip });
const views = [{ id: "live", camera_id: "camera", name: "Camera", variants: [
  { id: "main", role: "main", camera_source_id: "main", enabled: true },
  { id: "sub", role: "sub", camera_source_id: "sub", enabled: true },
] }];
const elements = [{ id: "element", type: "camera", props: { camera_id: "camera", control_point_sets: [{
  id: "view", label: "View", control_points: [
    { image: { x: 0, y: 0 }, world: { x: 0, z: 0 } },
    { image: { x: 1, y: 0 }, world: { x: 1, z: 0 } },
    { image: { x: 1, y: 1 }, world: { x: 1, z: 1 } },
    { image: { x: 0, y: 1 }, world: { x: 0, z: 1 } },
  ],
}] } }];
for (const [label, camera, expected] of [
  ["fixed selected source", { control: { has_ptz: true }, sources: [{ id: "main", has_ptz: true }, { id: "sub", has_ptz: false }] }, false],
  ["capable selected source", { control: { has_ptz: true }, sources: [{ id: "sub", has_ptz: true }] }, true],
  ["device explicitly unavailable", { control: { has_ptz: false }, sources: [{ id: "sub", has_ptz: true }] }, false],
  ["missing metadata", {}, null],
  ["index unavailable", null, null],
]) {
  const controller = new AbortController();
  const { fetchLiveViews } = load("api", { "@toposync/plugin-api": {
    requestJson: async (url, options) => {
      assert.equal(options.signal, controller.signal);
      if (url === "/api/streams/live-views") return structuredClone(views);
      assert.equal(url, "/api/cameras/index");
      if (camera === null) throw new Error("unavailable");
      return { cameras: [{ id: "camera", ...camera }] };
    },
  } });
  const candidates = resolveProjectionCandidates(elements, {}, await fetchLiveViews(controller.signal));
  assert.equal(candidates.length, 1, `${label}: capability must not remove video`);
  assert.equal(candidates[0].cameraSourceId, "sub", label);
  assert.equal(candidates[0].hasPtz, expected, label);
}
const cancelled = new AbortController();
const aborted = new Error("cancelled metadata request");
const { fetchLiveViews: fetchCancelled } = load("api", { "@toposync/plugin-api": {
  requestJson: async (url) => {
    if (url === "/api/streams/live-views") return structuredClone(views);
    cancelled.abort();
    throw aborted;
  },
} });
await assert.rejects(fetchCancelled(cancelled.signal), (error) => error === aborted);
console.log("Spatial video PTZ capabilities: selected source, device constraint and unknown metadata preserved");
