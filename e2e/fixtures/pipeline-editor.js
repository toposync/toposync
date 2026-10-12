const { expect } = require("@playwright/test");

// Every API request is intercepted: these fixtures never operate a real pipeline.
async function installPipelineEditorFixture(page) {
  const pipeline = {
    name: "layout_fixture", display_name: "Fluxo de validação — armazenamento e configuração com título longo",
    enabled: false, editor_mode: "interactive", processing_server_id: "local",
    graph: {
      schema_version: 2, uid: "layout_fixture", revision: 1,
      nodes: [
        { id: "source", operator: "camera.source", config: { camera_id: "fixture", source_id: "main" } },
        { id: "store", operator: "core.store_images", config: { layer_label: "Cena" } },
      ],
      edges: [{ from: { node: "source", port: "out" }, to: { node: "store", port: "in" } }],
      limits: { storage_max_bytes: 4 * 1024 ** 3 },
    },
  };
  const layer = { layer_key: "scene", layer_label: "Cena — entrada e circulação de veículos com identificação detalhada", node_id: "store",
    used_bytes: 1048576, file_count: 8, avg_file_bytes: 131072, newest_at: 1791835200, limit_bytes: 2097152, over_limit: false };
  const state = {
    pipeline, saves: [], cleanups: [], unexpected: [], saveError: false, cleanupError: false, storageError: false,
    storage: { pipeline_name: pipeline.name, used_bytes: 2097152, limit_bytes: 4 * 1024 ** 3,
      free_bytes: 10 * 1024 ** 3, min_free_bytes: 1024 ** 3, over_limit: false,
      layers: [layer, { ...layer, layer_key: "crop", layer_label: "Recorte", node_id: "crop" }] },
  };
  const composition = { id: "fixture", name: "Fixture", elements: [] };
  const reads = {
    "/api/auth/status": { mode: "bypass", authenticated: true },
    "/api/extensions": [], "/api/settings": { core: {}, extensions: {} },
    "/api/composition": composition, "/api/compositions": { active_composition_id: "fixture", compositions: [composition] },
    "/api/processing-servers": { servers: [{ id: "local", name: "Local" }] },
    "/api/processing-servers/local/status": { ok: true, status: {} },
    "/api/cameras/index": { cameras: [{ id: "fixture", name: "Câmera simulada", sources: [{ id: "main", name: "Entrada", ingest: { mode: "direct" } }] }] },
    "/api/pipelines/operators": { operators: ["camera.source", "core.store_images"].map(id => ({
      id, description: id, inputs: id === "camera.source" ? [] : [{ name: "in", type: "frame" }],
      outputs: [{ name: "out", type: "frame" }], capabilities: [], defaults: {}, config_schema: {}, share_strategy: "never",
    })) },
    "/api/pipelines/runtime/graph-info": { graphs: [], status: { running: true }, execution_observations: {} },
    "/api/notifications/count": { count: 0, unviewed_count: 0 },
    "/api/notifications": { items: [], total: 0 },
  };
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const json = body => route.fulfill({ json: body });
    const fail = detail => route.fulfill({ status: 503, json: { detail } });
    if (path === "/api/pipelines/compile") return json({ graph: request.postDataJSON().pipeline.graph, alerts: [] });
    if (path === `/api/pipelines/${pipeline.name}` && request.method() === "PUT") {
      state.saves.push(request.postDataJSON());
      if (state.saveError) return fail("Save rejected by fixture");
      Object.assign(pipeline, request.postDataJSON());
      return json(pipeline);
    }
    if (path.endsWith("/storage/cleanup") && request.method() === "POST") {
      state.cleanups.push(url.searchParams.get("purge"));
      if (state.cleanupError) return fail("Cleanup rejected by fixture");
      state.storage = { ...state.storage, used_bytes: 0, layers: [] };
      return json(state.storage);
    }
    if (request.method() === "GET") {
      if (path === "/api/pipelines") return json({ pipelines: [pipeline, { ...pipeline, name: "implicit__fixture", display_name: "Generated fixture" }] });
      if (path.endsWith("/storage")) {
        if (state.storageWait) await state.storageWait;
        return state.storageError ? fail("Storage unavailable") : json(state.storage);
      }
      if (path.endsWith("/stats")) return json({ node_outputs: {} });
      if (path.endsWith("/telemetry/image-markers")) return json({ markers: [] });
      if (path.endsWith("/telemetry/numeric")) return json({ series: [], points: [], histogram_bins: [] });
      if (path === "/api/notifications/stream") return route.fulfill({ contentType: "text/event-stream", body: "" });
      if (path in reads) return json(reads[path]);
    }
    state.unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 501, json: { detail: "Unexpected fixture request" } });
  });
  return state;
}

async function openPipelineEditor(page, name = "layout_fixture") {
  await page.goto(`/settings/pipelines/${name}`);
  await expect(page.locator("#pipeline-enabled")).toBeVisible();
  await expect(page.locator(".pipelinesStorageLayerList")).toBeVisible();
}

module.exports = { installPipelineEditorFixture, openPipelineEditor };
