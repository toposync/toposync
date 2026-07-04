import type { TopologyEdge, TopologyNode } from "./topologyTypes";

const NODE_WIDTH = 232;
const COLUMN_GAP = 140;
const ROW_GAP = 52;

export function layoutTopologyNodes(nodes: TopologyNode[], edges: TopologyEdge[]): TopologyNode[] {
  const outgoing = new Map<string, string[]>();
  const indegree = new Map<string, number>();
  const nodeIds = nodes.map((node) => node.id);
  for (const id of nodeIds) indegree.set(id, 0);

  for (const edge of edges) {
    if (!indegree.has(edge.source) || !indegree.has(edge.target)) continue;
    outgoing.set(edge.source, [...(outgoing.get(edge.source) ?? []), edge.target]);
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
  }

  const rank = new Map<string, number>();
  const queue = nodeIds.filter((id) => (indegree.get(id) ?? 0) === 0).sort();
  for (const id of queue) rank.set(id, 0);

  while (queue.length) {
    const current = queue.shift() as string;
    const currentRank = rank.get(current) ?? 0;
    for (const next of (outgoing.get(current) ?? []).sort()) {
      rank.set(next, Math.max(rank.get(next) ?? 0, currentRank + 1));
      indegree.set(next, Math.max(0, (indegree.get(next) ?? 0) - 1));
      if ((indegree.get(next) ?? 0) === 0) queue.push(next);
    }
    queue.sort();
  }

  const columns = new Map<number, TopologyNode[]>();
  for (const node of nodes) {
    const column = rank.get(node.id) ?? 0;
    columns.set(column, [...(columns.get(column) ?? []), node]);
  }

  const laidOut: TopologyNode[] = [];
  for (const [column, columnNodes] of [...columns.entries()].sort(([left], [right]) => left - right)) {
    columnNodes.sort((left, right) => left.data.nodeId.localeCompare(right.data.nodeId));
    columnNodes.forEach((node, row) => {
      laidOut.push({
        ...node,
        position: {
          x: column * (NODE_WIDTH + COLUMN_GAP),
          y: row * (124 + ROW_GAP),
        },
      });
    });
  }

  return laidOut;
}
