import type { GraphRuntimeEdgeInfo, GraphRuntimeInfo, GraphRuntimeNodeInfo, GraphRuntimeNodeOccurrence } from "../../../../util/api";

function occurrencesForPipeline(
  graph: GraphRuntimeInfo,
  runtimeNodeId: string,
  pipelineName: string,
): GraphRuntimeNodeOccurrence[] {
  return (graph.node_occurrences?.[runtimeNodeId] ?? []).filter(
    (occurrence) => occurrence.pipeline_name === pipelineName && Boolean(occurrence.node_id),
  );
}

function bundleContainsPipeline(graph: GraphRuntimeInfo, pipelineName: string): boolean {
  if (graph.pipelines?.includes(pipelineName)) return true;
  return Object.values(graph.node_occurrences ?? {}).some((occurrences) =>
    occurrences.some((occurrence) => occurrence.pipeline_name === pipelineName),
  );
}

function projectBundleGraph(graph: GraphRuntimeInfo, pipelineName: string): GraphRuntimeInfo | null {
  const nodes: Record<string, GraphRuntimeNodeInfo> = {};
  const occurrencesByRuntimeNode = new Map<string, GraphRuntimeNodeOccurrence[]>();

  for (const [runtimeNodeId, runtimeNode] of Object.entries(graph.nodes ?? {})) {
    const occurrences = occurrencesForPipeline(graph, runtimeNodeId, pipelineName);
    if (!occurrences.length) continue;
    occurrencesByRuntimeNode.set(runtimeNodeId, occurrences);
    for (const occurrence of occurrences) {
      nodes[occurrence.node_id] = {
        ...runtimeNode,
        uid: occurrence.node_id,
        node_id: occurrence.node_id,
      };
    }
  }

  if (!Object.keys(nodes).length) return null;

  const edges: Record<string, GraphRuntimeEdgeInfo> = {};
  for (const runtimeEdge of Object.values(graph.edges ?? {})) {
    const sourceRuntimeNodeId = runtimeEdge.source?.node;
    const targetRuntimeNodeId = runtimeEdge.target?.node;
    if (!sourceRuntimeNodeId || !targetRuntimeNodeId) continue;
    const sourceOccurrences = occurrencesByRuntimeNode.get(sourceRuntimeNodeId) ?? [];
    const targetOccurrences = occurrencesByRuntimeNode.get(targetRuntimeNodeId) ?? [];
    for (const source of sourceOccurrences) {
      for (const target of targetOccurrences) {
        const sourcePort = runtimeEdge.source?.port ?? "out";
        const targetPort = runtimeEdge.target?.port ?? "in";
        const uid = `${source.node_id}.${sourcePort}->${target.node_id}.${targetPort}`;
        edges[uid] = {
          ...runtimeEdge,
          uid,
          source: { ...runtimeEdge.source, node: source.node_id },
          target: { ...runtimeEdge.target, node: target.node_id },
        };
      }
    }
  }

  return {
    ...graph,
    graph_id: pipelineName,
    pipeline_name: pipelineName,
    nodes,
    edges,
    // Bundle-wide pressure can belong to a different logical pipeline.
    pressure: {},
  };
}

export function projectRuntimeGraphInfoForPipeline(
  graphs: GraphRuntimeInfo[],
  pipelineName: string,
): GraphRuntimeInfo | null {
  const name = String(pipelineName || "").trim();
  if (!name) return null;

  const direct = graphs.find((graph) => graph.pipeline_name === name || graph.graph_id === name);
  if (direct) return direct;

  const bundle = graphs.find((graph) => bundleContainsPipeline(graph, name));
  return bundle ? projectBundleGraph(bundle, name) : null;
}
