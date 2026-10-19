declare const require: any;
import { inspectionSources } from "./inspectionSources";
const test: (name: string, fn: () => void) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

test("camera selection uses declared identifiers, preserves separate cameras and tolerates missing configuration", () => {
  assert.deepEqual(inspectionSources(null), []);
  assert.deepEqual(inspectionSources({ nodes: {} }), []);
  assert.deepEqual(inspectionSources({ nodes: [null, {}, { config: { camera_id: 3 } },
    { config: { camera_id: "one", camera_name: "Garage" } }, { config: { camera_id: "one", camera_name: "Garage" } },
    { config: { camera_id: "two", camera_name: "Front" } }, { config: { camera_id: "x".repeat(129) } }] }),
    [{ id: "one", name: "Garage" }, { id: "two", name: "Front" }]);
});
