declare const require: any;
import { automaticDraft, replaceAutomaticResources, type AutomaticSnapshot, type AutomaticScope } from "./automatic";

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

test("new activities prefer a basic observation regardless of extension catalog order", () => {
  const basic = snapshot.catalog[0];
  const external = { ...basic, id: "ai.context", required_consents: ["external"] };
  const advanced = { ...basic, id: "vision.pose", advanced: true };
  const catalog = [external, advanced, basic];
  assert.equal(automaticDraft({ ...snapshot, catalog }, "en")?.capability, basic.id);
  const onlyExternal = automaticDraft({ ...snapshot, catalog: [external] }, "en")!;
  assert.equal(onlyExternal.capability, external.id);
  assert.deepEqual(onlyExternal.consents, []);
});

test("detail changes preserve duration and consent; changing the AI profile requires a new specific choice", () => {
  const intent = automaticDraft(snapshot, "en")!;
  intent.condition = { minimum_duration_seconds: 27, resource_parameters: { profile_id: "first", confidence_threshold: .5 } };
  intent.consents = ["external", "recognition"];
  const before = structuredClone(intent);
  const capability = { ...snapshot.catalog[0], required_consents: ["external"], resource_consent_fields: ["profile_id"] };
  const refined = replaceAutomaticResources(intent, capability, { profile_id: "first", confidence_threshold: .8 });
  assert.deepEqual(refined.consents, intent.consents);
  assert.equal(refined.condition.minimum_duration_seconds, 27);
  assert.deepEqual(refined.consequence, intent.consequence);
  const changedProfile = replaceAutomaticResources(refined, capability, { profile_id: "second", confidence_threshold: .8 });
  assert.deepEqual(changedProfile.consents, ["recognition"]);
  assert.deepEqual(intent, before);
});
