declare const require: any;
import { sharedInspectionExecutions } from "./inspectionSharing";
const test: (name: string, fn: () => void) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

test("sharing requires physical identity and producer epoch, with independent logical consumers", () => {
  const source = { node_id: "source", operator: "test.source", physical_node_id: "physical", producer_epoch: "first" };
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
