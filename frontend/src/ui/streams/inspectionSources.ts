export function inspectionSources(graph: unknown, catalog: Array<{ graph: unknown }> = []): Array<{ id: string; name: string }> {
  const sources = new Map<string, string>();
  const visited = new Set<unknown>();
  function collect(value: unknown) {
    if (!value || typeof value !== "object" || visited.has(value) || visited.size >= 128) return;
    visited.add(value);
    const current = value as { nodes?: unknown[]; meta?: { automatic?: { sources?: unknown[] } } };
    for (const source of Array.isArray(current.meta?.automatic?.sources) ? current.meta.automatic.sources : []) add(source);
    for (const node of Array.isArray(current.nodes) ? current.nodes : []) {
      const item = node as { operator?: string; config?: { key?: unknown } } | null;
      add(item?.config);
      if (item?.operator !== "core.result_input" || typeof item.config?.key !== "string") continue;
      const producers = catalog.filter(pipeline => {
        const outputs = (pipeline.graph as { meta?: { outputs?: Array<{ key?: string }> } } | null)?.meta?.outputs;
        return Array.isArray(outputs) && outputs.some(output => output?.key === item.config?.key);
      });
      if (producers.length === 1) collect(producers[0].graph);
    }
  }
  function add(value: unknown) {
    const config = value as { camera_id?: unknown; camera_name?: unknown } | null;
    if (typeof config?.camera_id !== "string" || !config.camera_id || config.camera_id.length > 128) return;
    sources.set(config.camera_id, typeof config.camera_name === "string" && config.camera_name ? config.camera_name : config.camera_id);
  }
  collect(graph);
  return [...sources].map(([id, name]) => ({ id, name }));
}
