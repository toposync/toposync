declare const require: any;

import type { GraphRuntimeInfo } from "../../../../util/api";

import { projectRuntimeGraphInfoForPipeline } from "./runtimeGraphProjection";

const test: (name: string, fn: () => void | Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

function runtimeNode(id: string): GraphRuntimeInfo["nodes"][string] {
  return {
    uid: id,
    node_id: id,
    operator_id: "core.passthrough",
    runtime_state: "active",
    progress: { processed_packets: 12, emitted_packets: 12 },
  };
}

function bundleGraph(): GraphRuntimeInfo {
  return {
    graph_id: "local_bundle",
    pipeline_name: "local_bundle",
    schema_version: 2,
    generated_at: 100,
    running: true,
    nodes: {
      shared_source: runtimeNode("shared_source"),
      isolated_target__detect: runtimeNode("isolated_target__detect"),
    },
    edges: {
      merged_source_detect: {
        uid: "merged_source_detect",
        source: { node: "shared_source", port: "out" },
        target: { node: "isolated_target__detect", port: "in" },
      },
    },
    resources: {},
    pressure: { active: true },
    progress: {},
    pipelines: ["target", "other"],
    node_occurrences: {
      shared_source: [
        { pipeline_name: "target", node_id: "source" },
        { pipeline_name: "other", node_id: "source" },
      ],
      isolated_target__detect: [{ pipeline_name: "target", node_id: "detect" }],
    },
  };
}

test("projects a bundled runtime onto the selected logical pipeline", () => {
  const projected = projectRuntimeGraphInfoForPipeline([bundleGraph()], "target");

  assert.equal(projected?.graph_id, "target");
  assert.equal(projected?.pipeline_name, "target");
  assert.deepEqual(Object.keys(projected?.nodes ?? {}).sort(), ["detect", "source"]);
  assert.equal(projected?.nodes.source.node_id, "source");
  assert.equal(projected?.nodes.detect.node_id, "detect");
  assert.deepEqual(projected?.edges["source.out->detect.in"]?.source, { node: "source", port: "out" });
  assert.deepEqual(projected?.edges["source.out->detect.in"]?.target, { node: "detect", port: "in" });
  assert.deepEqual(projected?.pressure, {});
});

test("prefers a graph already scoped to the selected pipeline", () => {
  const direct = { ...bundleGraph(), graph_id: "target", pipeline_name: "target" };
  assert.equal(projectRuntimeGraphInfoForPipeline([direct, bundleGraph()], "target"), direct);
});

test("returns null when no runtime graph includes the pipeline", () => {
  assert.equal(projectRuntimeGraphInfoForPipeline([bundleGraph()], "missing"), null);
});
