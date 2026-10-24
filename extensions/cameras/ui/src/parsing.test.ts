declare const require: any;
import { parseCameras, serializeCameras } from "./parsing";
const test = require("node:test").test;
const assert = require("node:assert/strict");

test("editing camera settings preserves its configured clock domain", () => {
  const cameras = parseCameras({ devices: [
    { id: "left", clock_domain: "synthetic-shared", sources: [{ id: "main" }] },
    { id: "right", sources: [{ id: "main" }] },
  ] });
  cameras[0].name = "Renamed camera";
  const saved = serializeCameras(cameras).devices as Array<Record<string, unknown>>;
  assert.equal(saved[0].clock_domain, "synthetic-shared");
  assert.equal(saved[1].clock_domain, "device:right");
});

test("editing camera and source names preserves spaces between keystrokes", () => {
  let cameras = parseCameras({ devices: [{ id: "fixture", name: "", sources: [{ id: "main", name: "" }] }] });
  for (const character of "Camera with spaces") {
    cameras[0].name += character;
    cameras = parseCameras(serializeCameras(cameras));
  }
  cameras[0].sources[0].name = "Source";
  for (const character of " with spaces") {
    cameras[0].sources[0].name += character;
    cameras = parseCameras(serializeCameras(cameras));
  }
  assert.equal(cameras[0].name, "Camera with spaces");
  assert.equal(cameras[0].sources[0].name, "Source with spaces");
  assert.equal(cameras[0].sources[0].id, "main");
});
