import React, { useEffect, useState } from "react";
import type { HostI18n, PipelineReviewPanel } from "@toposync/plugin-api";
import { fetchCameraContexts, fetchCamerasIndex } from "../api/camerasApi";
import type { CameraContextsResponse, CamerasIndex } from "../types";

type Node = { id: string; operator: string; config: Record<string, unknown> };
type SavedArea = { id?: string; name?: string; points?: Array<{ x: number; z: number }> };
function areasOf(node: Node): SavedArea[] {
  return Array.isArray(node.config.areas) ? node.config.areas.filter(area => area && typeof area === "object") : [];
}
function nodesOf(graph: unknown): Node[] {
  const nodes = (graph as { nodes?: Node[] } | null)?.nodes;
  return Array.isArray(nodes) && nodes.every(node => node && typeof node === "object" && typeof node.operator === "string" && node.config && typeof node.config === "object" && !Array.isArray(node.config)) ? nodes : [];
}
const known = new Set([
  "camera.source", "camera.audio_source", "camera.audio_features", "camera.audio_occurrence", "camera.audio_group", "camera.audio_correlation",
  "camera.area_occurrence", "camera.area_restriction", "camera.camera_mapping", "camera.image_crop", "camera.motion_gate", "camera.velocity_estimation",
  "core.debounce", "core.fps_reducer", "core.notify", "core.stationary_event", "core.store_images", "core.throttle",
  "ai.condition_filter", "ai.smart_crop", "vision.crop_objects", "vision.detect", "vision.group_events", "vision.spatial_relation_event", "vision.track", "ptz_attention.request",
]);
export function supportsRecipeReview(graph: unknown): boolean {
  const nodes = nodesOf(graph);
  return nodes.some(node => node.operator === "camera.source" || node.operator === "camera.audio_source")
    && nodes.some(node => node.operator === "core.notify")
    && nodes.every(node => known.has(node.operator) && node.config && typeof node.config === "object");
}
export function createCameraRecipeReview(i18n: HostI18n): PipelineReviewPanel {
  return { id: "com.toposync.cameras.recipe", supports: supportsRecipeReview,
    render: graph => <CameraRecipeReview graph={graph} i18n={i18n} /> };
}
function CameraRecipeReview({ graph, i18n }: { graph: unknown; i18n: HostI18n }) {
  const { locale } = i18n.useI18n();
  const text = (portuguese: string, english: string) => locale.startsWith("pt") ? portuguese : english;
  const nodes = nodesOf(graph);
  const [cameras, setCameras] = useState<CamerasIndex["cameras"] | null>(null);
  const [failed, setFailed] = useState(false);
  const [areaContexts, setAreaContexts] = useState<CameraContextsResponse[] | null>(null);
  const [areaContextFailed, setAreaContextFailed] = useState(false);
  const boundAreas = nodes.some(node => ["camera.area_occurrence", "camera.area_restriction"].includes(node.operator) && node.config.composition_id);
  const contextCameraIds = JSON.stringify(boundAreas ? [...new Set(nodes.filter(node => node.operator === "camera.camera_mapping")
    .map(node => String(node.config.camera_id || "")).filter(Boolean))] : []);
  const areaReferenceKey = JSON.stringify(nodes.filter(node => ["camera.area_occurrence", "camera.area_restriction"].includes(node.operator))
    .map(node => [node.config.composition_id, (node.config.area as SavedArea | undefined)?.id,
      areasOf(node).map(area => area.id)]));
  useEffect(() => {
    let controller: AbortController | null = null;
    const refresh = () => {
      controller?.abort();
      const current = new AbortController();
      controller = current;
      setAreaContexts(null); setAreaContextFailed(false);
      Promise.all((JSON.parse(contextCameraIds) as string[]).map(id => fetchCameraContexts(id, current.signal)))
        .then(value => { if (!current.signal.aborted) setAreaContexts(value); })
        .catch(() => { if (!current.signal.aborted) setAreaContextFailed(true); });
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { controller?.abort(); window.removeEventListener("focus", refresh); };
  }, [contextCameraIds, areaReferenceKey]);
  useEffect(() => { const controller = new AbortController();
    fetchCamerasIndex(controller.signal).then(value => setCameras(value.cameras)).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, []);
  const sources = nodes.filter(node => node.operator === "camera.source" || node.operator === "camera.audio_source");
  const physical = nodes.some(node => node.operator === "ptz_attention.request");
  function areaName(node: Node, area = node.config.area as SavedArea | undefined): string {
    if (!node.config.composition_id) return area?.name || text("Área sem nome", "Unnamed area");
    if (areaContextFailed) return text("Não foi possível conferir a área", "Area could not be checked");
    if (areaContexts === null) return text("Conferindo área…", "Checking area…");
    const current = areaContexts.flatMap(context => context.compositions)
      .filter(composition => composition.id === node.config.composition_id)
      .flatMap(composition => composition.areas).find(item => item.id === area?.id);
    if (current && node.operator === "camera.area_restriction" && (current.name !== area?.name
      || !Array.isArray(current.vertices) || current.vertices.length !== area?.points?.length
      || !current.vertices.every((point, index) => point.x === area?.points?.[index]?.x && point.z === area?.points?.[index]?.z))) {
      return text("Área alterada; crie uma nova receita", "Area changed; create a new recipe");
    }
    return current?.name || text("Área indisponível ou sem acesso", "Area unavailable or inaccessible");
  }
  return <section className="card" aria-label={text("Revisar fluxo", "Review pipeline")}><div className="cardBody" style={{ display: "grid", gap: 16 }}>
    <strong>{text("Revisar fluxo", "Review pipeline")}</strong>
    {sources.map(node => { const camera = cameras?.find(item => item.id === node.config.camera_id); const source = camera?.sources?.find(item => item.id === node.config.source_id);
      return <div key={node.id}><strong>{text("Fonte", "Source")}</strong><div>{cameras === null && !failed ? text("Conferindo fonte…", "Checking source…") : camera?.name || text("Câmera indisponível ou sem nome", "Camera unavailable or unnamed")} · {source?.name || text("Fonte indisponível ou sem nome", "Source unavailable or unnamed")}</div></div>;
    })}
    {failed && <p role="status">{text("Não foi possível conferir as fontes. Revise a câmera antes de habilitar.", "Sources could not be checked. Review the camera before enabling.")}</p>}
    <div><strong>{text("O que será observado", "What will be observed")}</strong>
      {nodes.filter(node => ["ai.condition_filter", "vision.detect", "camera.area_occurrence", "core.stationary_event", "camera.audio_features"].includes(node.operator)).map(node => <p key={node.id} style={{ margin: "8px 0 0" }}>
        {node.operator === "camera.audio_features" ? text("Som acima da referência fornecida. Suporte de áudio ainda não verificado.", "Sound above the supplied baseline. Audio support has not been verified.")
          : node.operator === "ai.condition_filter" ? String(node.config.condition_description || text("Condição sem descrição", "Condition has no description"))
          : node.operator === "core.stationary_event" ? text("Parada observada", "Observed stop")
          : node.operator === "camera.area_occurrence" ? `${node.config.mode === "entry" ? text("Entrada", "Entry") : text("Presença", "Presence")} · ${areaName(node)}`
          : text("Detecção visual", "Visual detection")}
        {Array.isArray(node.config.categories) ? ` · ${node.config.categories.map(category => ({ person: text("pessoa", "person"), cat: text("gato", "cat"), dog: text("cão", "dog") })[String(category)] || String(category)).join(", ")}` : ""}
        {typeof (node.config.minimum_duration_seconds ?? node.config.min_stationary_seconds) === "number" ? ` · ${text("duração mínima", "minimum duration")}: ${new Intl.NumberFormat(locale, { maximumFractionDigits: 3 }).format((node.config.minimum_duration_seconds ?? node.config.min_stationary_seconds) as number)} s` : ""}
      </p>)}
      {nodes.filter(node => node.operator === "camera.area_restriction").map(node => <p key={node.id} style={{ margin: "8px 0 0" }}>{text("Área", "Area")}: {node.config.composition_id
        ? areasOf(node).map(area => areaName(node, area)).join(", ")
        : String(node.config.area_label || node.config.area_name || text("Área mapeada selecionada", "Selected mapped area"))}</p>)}
      {nodes.filter(node => node.operator === "camera.image_crop").map(node => <p key={node.id} style={{ margin: "8px 0 0" }}>{text("Região fixa na imagem", "Fixed image region")}: {String(node.config.left)}–{String(node.config.right)}% {text("da largura", "of width")}; {String(node.config.top)}–{String(node.config.bottom)}% {text("da altura", "of height")}. {text("Revise a região se a câmera mudou de posição.", "Review this region if the camera moved.")}</p>)}
    </div>
    <div><strong>{text("Saída", "Output")}</strong><p style={{ margin: "8px 0 0" }}>{text("Ocorrência no histórico", "History occurrence")}{nodes.some(node => node.operator === "core.store_images") ? text(" com imagem registrada", " with a saved image") : ""}.</p>
      {nodes.filter(node => ["camera.audio_occurrence", "camera.audio_group"].includes(node.operator)).map(node => <p key={node.id} style={{ margin: "8px 0 0" }}>{node.config.retain_audio ? text("Trecho privado de áudio retido por até 24 horas.", "Private audio clip retained for up to 24 hours.") : text("Sem retenção de áudio para escuta posterior.", "No audio retention for later playback.")}</p>)}
    </div>
    {physical && <p role="alert">{text("Esta receita solicita movimento da câmera. Revise o controle físico e a autorização antes de habilitar.", "This recipe requests camera movement. Review physical control and authorization before enabling.")}</p>}
    <p style={{ margin: 0 }}>{text("Habilitar e salvar inicia o processamento. A configuração não comprova reconhecimento, cobertura, identidade ou funcionamento; confira os primeiros acontecimentos.", "Enabling and saving starts processing. Configuration does not prove recognition, coverage, identity or operation; review the first events.")}</p>
  </div></section>;
}
