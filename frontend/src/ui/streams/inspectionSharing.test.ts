declare const require: any;
import { sharedInspectionExecutions } from "./inspectionSharing";
const test: (name: string, fn: () => void) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

test("sharing requires physical identity and producer epoch, with independent logical consumers", () => {
  const source = { node_id: "source", operator: "test.source", physical_node_id: "physical", producer_epoch: "first", received_from: null };
  const flows = [
    { name: "manual", label: "Manual", nodes: [source, source] },
    { name: "generated", label: "Generated", nodes: [{ ...source, node_id: "reference" }] },
    { name: "restarted", label: "Restarted", nodes: [{ ...source, producer_epoch: "second" }] },
    { name: "separate", label: "Separate", nodes: [{ ...source, physical_node_id: "other" }] },
    { name: "waiting", label: "Waiting", nodes: [{ ...source, producer_epoch: "" }] },
  ];
  assert.deepEqual(sharedInspectionExecutions(flows), [[JSON.stringify(["physical", "first"]), {
    operator: "test.source", consumers: [
      { pipeline: "manual", label: "Manual", node: "source" },
      { pipeline: "generated", label: "Generated", node: "reference" },
    ],
  }]]);
  assert.deepEqual(sharedInspectionExecutions([flows[0]]), []);
  assert.deepEqual(sharedInspectionExecutions([]), []);
});

test("result receipts join only the selected producer execution, without merging consumers or epochs", () => {
  const producer = { node_id: "detect", operator: "vision.detect", physical_node_id: "detector", producer_epoch: "first", received_from: null };
  const reference = { node_id: "reference", operator: "core.demand_consumer", physical_node_id: "consumer", producer_epoch: "own",
    received_from: { physical_node_id: "detector", producer_epoch: "first" } };
  const flows = [
    { name: "producer", label: "Producer", nodes: [producer] },
    { name: "manual", label: "Manual", nodes: [reference, reference] },
    { name: "automatic", label: "Automatic", nodes: [{ ...reference, physical_node_id: "other-consumer" }] },
    { name: "old", label: "Old", nodes: [{ ...reference, physical_node_id: "old-consumer", received_from: { ...reference.received_from, producer_epoch: "old" } }] },
    { name: "other-model", label: "Other model", nodes: [{ ...producer, physical_node_id: "other-detector" }] },
  ];
  assert.deepEqual(sharedInspectionExecutions(flows), [[JSON.stringify(["detector", "first"]), {
    operator: "vision.detect", consumers: [
      { pipeline: "producer", label: "Producer", node: "detect" },
      { pipeline: "manual", label: "Manual", node: "reference" },
      { pipeline: "automatic", label: "Automatic", node: "reference" },
    ],
  }]]);
  assert.deepEqual(sharedInspectionExecutions(flows.slice(1)), []);
  assert.deepEqual(sharedInspectionExecutions([flows[0], { ...flows[1], nodes: [{ ...reference, received_from: null }] }]), []);
});
