import type { InspectionSnapshot } from "@toposync/plugin-api";

export type InspectionExecution = Pick<InspectionSnapshot["nodes"][number],
  "node_id" | "operator" | "physical_node_id" | "producer_epoch">;

export function sharedInspectionExecutions(flows: Array<{ name: string; label: string; nodes: InspectionExecution[] }>) {
  const executions = new Map<string, { operator: string; consumers: Array<{ pipeline: string; label: string; node: string }> }>();
  for (const flow of flows) for (const node of flow.nodes) {
    if (!node.physical_node_id || !node.producer_epoch) continue;
    const key = JSON.stringify([node.physical_node_id, node.producer_epoch]);
    let execution = executions.get(key);
    if (!execution) {
      execution = { operator: node.operator, consumers: [] };
      executions.set(key, execution);
    }
    if (!execution.consumers.some(consumer => consumer.pipeline === flow.name && consumer.node === node.node_id))
      execution.consumers.push({ pipeline: flow.name, label: flow.label, node: node.node_id });
  }
  return [...executions].filter(([, execution]) => new Set(execution.consumers.map(consumer => consumer.pipeline)).size > 1);
}
