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

test("manual and generated consumers inherit only unambiguous declared source references without cycles", () => {
  const producer = { graph: { nodes: [{ config: { camera_id: "one", camera_name: "Garage" } }], meta: { outputs: [{ key: "people" }] } } };
  const consumer = { graph: { nodes: [{ operator: "core.result_input", config: { key: "people" } }], meta: { outputs: [{ key: "forward" }] } } };
  const generated = { graph: { nodes: [{ operator: "core.result_input", config: { key: "forward" } }], meta: { automatic: { sources: [{ camera_id: "two" }] } } } };
  const catalog = [producer, consumer, generated];
  assert.deepEqual(inspectionSources(consumer.graph, catalog), [{ id: "one", name: "Garage" }]);
  assert.deepEqual(inspectionSources(generated.graph, catalog), [{ id: "two", name: "two" }, { id: "one", name: "Garage" }]);
  assert.deepEqual(inspectionSources(consumer.graph, [consumer]), []);
  assert.deepEqual(inspectionSources(consumer.graph, [...catalog, { graph: producer.graph }]), []);
  const cycle = { graph: { nodes: [{ operator: "core.result_input", config: { key: "cycle" } }], meta: { outputs: [{ key: "cycle" }] } } };
  assert.deepEqual(inspectionSources(cycle.graph, [cycle]), []);
});
