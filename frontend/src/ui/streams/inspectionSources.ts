export function inspectionSources(graph: unknown): Array<{ id: string; name: string }> {
  const nodes = (graph as { nodes?: unknown[] } | null)?.nodes;
  if (!Array.isArray(nodes)) return [];
  const sources = new Map<string, string>();
  for (const node of nodes) {
    const config = (node as { config?: { camera_id?: unknown; camera_name?: unknown } } | null)?.config;
    if (typeof config?.camera_id !== "string" || !config.camera_id || config.camera_id.length > 128) continue;
    sources.set(config.camera_id, typeof config.camera_name === "string" && config.camera_name ? config.camera_name : config.camera_id);
  }
  return [...sources].map(([id, name]) => ({ id, name }));
}
