declare const require: any;
import { automaticDraft, type AutomaticSnapshot, type AutomaticScope } from "./automatic";

const test = require("node:test").test;
const assert = require("node:assert/strict");

const scope: AutomaticScope = { kind: "camera", camera_ids: ["camera:a"], excluded_camera_ids: [], composition_id: "", area_id: "" };
const snapshot: AutomaticSnapshot = {
  configuration: { revision: 3, enabled: false, intents: [], defaults: {}, blocked_camera_ids: [], denied_features: {} },
  activities: {}, provider_errors: [],
  catalog: [{ id: "vision.presence", kind: "observation", extension_id: "vision", title: { en: "Presence", "pt-BR": "Presença" },
    available: true, required_consents: [], missing_operators: [], scopes: ["camera", "physical_area"] }],
  targets: [
    { id: "camera:camera:a", name: "Front", scope, sources: [], reason: null },
    { id: "area:floor:garage", name: "Garage", scope: { ...scope, kind: "physical_area", camera_ids: ["a", "b"], composition_id: "floor", area_id: "garage" }, sources: [], reason: null },
  ],
};

test("camera and area shortcuts prepare the exact place without enabling or granting permissions", () => {
  const before = structuredClone(snapshot);
  const camera = automaticDraft(snapshot, "pt-BR", "camera:camera:a")!;
  const area = automaticDraft(snapshot, "en", "area:floor:garage")!;
  assert.equal(camera.name, "Presença · Front");
  assert.equal(area.name, "Presence · Garage");
  assert.deepEqual(camera.scope, scope);
  assert.equal(area.scope.kind, "physical_area");
  assert.deepEqual(area.scope.camera_ids, ["a", "b"]);
  assert.deepEqual(area.consents, []);
  assert.equal(area.consequence.capability, "core.record");
  assert.notEqual(camera.id, area.id);
  area.scope.camera_ids.pop();
  assert.deepEqual(snapshot, before);
});

test("missing or unsupported shortcut targets never fall back to another camera", () => {
  assert.equal(automaticDraft(snapshot, "en", "deleted-camera"), null);
  assert.equal(automaticDraft({ ...snapshot, catalog: [] }, "en", "area:floor:garage"), null);
  assert.equal(automaticDraft(snapshot, "en")?.scope.camera_ids[0], "camera:a");
});
