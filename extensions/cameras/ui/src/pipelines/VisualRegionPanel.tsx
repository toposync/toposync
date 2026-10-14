import React, { useEffect, useRef, useState } from "react";
import { resolveToposyncUrl, type ToposyncHost, type ImageMaskEditorProps } from "@toposync/plugin-api";
import { SubModal } from "../ui/SubModal";

type Choice = { camera_id: string; camera_name: string; source_id: string; source_name: string; kind: string; reason: string | null };
type Reference = { framing_available: boolean; id: string; revision: number; image_url: string; geometry: { model_digest: string; projection: string } };
type Preview = { image: string; classification: string; reason: string; visibility?: { state: string }; detection_available: boolean; frame_age_seconds?: number; capture_evidence?: { published_at: number }; observations?: { classification: string }[] };

async function request<T>(path: string, signal: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(resolveToposyncUrl(path), { signal, method: body ? "POST" : "GET", headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  const result = await response.json();
  if (!response.ok) throw new Error(result?.detail?.message || result?.detail?.code || `HTTP ${response.status}`);
  return result;
}

export function VisualRegionPanel({ host, config, updateConfig, processingServerId, cameraId, area = false }: {
  host: ToposyncHost; config: Record<string, unknown>; updateConfig: (patch: Record<string, unknown>) => void;
  processingServerId?: string; cameraId?: string; area?: boolean;
}): React.ReactElement {
  const { t } = host.i18n.useI18n();
  const [choices, setChoices] = useState<Choice[]>([]);
  const [reference, setReference] = useState<Reference | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const [areaSource, setAreaSource] = useState("");
  const selectedCamera = area ? cameraId || "" : String(config.camera_id || cameraId || "");
  const selectedSource = area ? areaSource : String(config.source_id || "");
  const signature = JSON.stringify(config);
  const local = !processingServerId || processingServerId === "local";
  useEffect(() => {
    const controller = new AbortController();
    setError(""); setChoices([]);
    if (local) void request<{ choices: Choice[] }>("/api/cameras/live-panorama", controller.signal).then(result => {
      if (!controller.signal.aborted) setChoices(result.choices.filter(item => item.kind === "active" && !item.reason));
    }).catch(error => { if (!controller.signal.aborted) setError(String(error.message)); });
    return () => controller.abort();
  }, [processingServerId]);
  useEffect(() => {
    if (!area) return;
    const compatible = choices.filter(item => item.camera_id === selectedCamera);
    setAreaSource(previous => compatible.some(item => item.source_id === previous) ? previous : compatible.length === 1 ? compatible[0]!.source_id : "");
  }, [choices, area, selectedCamera]);
  useEffect(() => {
    operation.current?.abort(); setPreview(null); setBusy(false); setDrawing(false);
    return () => operation.current?.abort();
  }, [signature, processingServerId, selectedSource, selectedCamera]);
  useEffect(() => {
    const controller = new AbortController();
    setReference(null);
    if (!area && local && selectedCamera && selectedSource) {
      void request<Reference>(`/api/cameras/cameras/${encodeURIComponent(selectedCamera)}/sources/${encodeURIComponent(selectedSource)}/visual-region-reference`, controller.signal)
        .then(value => { if (!controller.signal.aborted) setReference(value); })
        .catch(error => { if (!controller.signal.aborted) setError(String(error.message)); });
    }
    return () => controller.abort();
  }, [selectedCamera, selectedSource, area, processingServerId]);
  const selectedReference = config.reference as { artifact_id?: string; geometry_digest?: string } | null;
  const changed = Boolean(selectedReference && reference && (selectedReference.artifact_id !== reference.id || selectedReference.geometry_digest !== reference.geometry.model_digest));
  const strokes = (Array.isArray(config.strokes) ? config.strokes : []) as ImageMaskEditorProps["strokes"];
  const MaskEditor = host.ui.ImageMaskEditor;
  const label = (key: string) => t(`ext.cameras.visual_region.${key}`);
  const compatible = choices.filter(item => !area || item.camera_id === selectedCamera);

  async function showPreview() {
    operation.current?.abort();
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setPreview(null); setError(""); setPreviewOpen(true);
    try {
      const body = area ? { camera_id: selectedCamera, source_id: selectedSource, composition_id: config.composition_id,
        area_ids: (config.areas as { id: string }[] || []).map(item => item.id) } : config;
      const value = await request<Preview>(area ? "/api/cameras/areas/preview" : "/api/cameras/visual-regions/preview", controller.signal, body);
      if (!controller.signal.aborted) setPreview(value);
    } catch (error) { if (!controller.signal.aborted) setError(String((error as Error).message)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  return <div className="pipelinesOperatorConfigCard">
    {!local ? <p role="status">{label("remote_unavailable")}</p> : <>
      {(!area || compatible.length > 1) && <label className="pipelinesLabel"><span>{label("source")}</span>
        <select className="pipelinesSelect" value={`${selectedCamera}/${selectedSource}`} onChange={event => {
          const choice = compatible.find(item => `${item.camera_id}/${item.source_id}` === event.target.value);
          setError("");
          if (area) setAreaSource(choice?.source_id || "");
          else updateConfig({ camera_id: choice?.camera_id || "", source_id: choice?.source_id || "", reference: null, strokes: [] });
        }}><option value="">{label("select_source")}</option>{compatible.map(item => <option key={`${item.camera_id}/${item.source_id}`} value={`${item.camera_id}/${item.source_id}`}>{item.camera_name} · {item.source_name}</option>)}</select>
      </label>}
      {!area && <>
        <label className="pipelinesLabel"><span>{label("name")}</span><input className="pipelinesInput" maxLength={100} value={String(config.name || "")} onChange={event => updateConfig({ name: event.target.value })} /></label>
        <button className="chipButton" type="button" disabled={!reference} onClick={() => setDrawing(true)}>{label(strokes.length ? "edit" : "draw")}</button>
        {changed && <p role="status">{label("changed")}</p>}
        <label className="pipelinesLabel"><span>{label("criterion")}</span><select className="pipelinesSelect" value={String(config.criterion || "base")} onChange={event => updateConfig({ criterion: event.target.value })}>
          {["base", "center", "overlap", "whole"].map(value => <option key={value} value={value}>{label(value)}</option>)}
        </select></label>
        {config.criterion === "overlap" && <label className="pipelinesLabel"><span>{label("overlap_amount")}</span><input type="number" min={1} max={100} value={Math.round(Number(config.minimum_overlap ?? .25) * 100)} onChange={event => updateConfig({ minimum_overlap: Math.min(100, Math.max(1, Number(event.target.value))) / 100 })} /></label>}
        <label className="pipelinesLabel"><span>{label("framing")}</span><select className="pipelinesSelect" value={String(config.framing || "passive")} onChange={event => updateConfig({ framing: event.target.value, native_tracking_disabled_confirmed: false })}>
          <option value="passive">{label("passive")}</option><option value="maintain" disabled={!reference?.framing_available}>{label("maintain")}</option>
        </select></label>
        {!reference?.framing_available && <p className="pipelinesStepHint">{label("framing_unavailable")}</p>}
        {config.framing === "maintain" && <label><input type="checkbox" checked={config.native_tracking_disabled_confirmed === true} onChange={event => updateConfig({ native_tracking_disabled_confirmed: event.target.checked })} />{label("tracking_disabled")}</label>}
      </>}
      <button type="button" className="chipButton" disabled={busy || !selectedSource || (area ? !config.composition_id || !(config.areas as unknown[])?.length : !strokes.length || !String(config.name || "").trim())} onClick={() => void showPreview()}>{label("preview")}</button>
    </>}
    {error && <p role="alert" className="pipelinesInlineError">{error}</p>}
    {!area && reference && <MaskEditor wrapHorizontal={reference.geometry.projection === "equirectangular"} key={reference.id} open={drawing} imageUrl={resolveToposyncUrl(reference.image_url)} title={label("draw")} strokes={changed ? [] : strokes} onClose={() => setDrawing(false)} onApply={next => updateConfig({ strokes: next, reference: { artifact_id: reference.id, revision: reference.revision, geometry_digest: reference.geometry.model_digest } })} />}
    <SubModal open={previewOpen} title={label("preview")} onClose={() => { operation.current?.abort(); setBusy(false); setPreviewOpen(false); }}>
      {busy && <p role="status">{label("loading")}</p>}
      {error && <p role="alert">{error}</p>}
      {preview && <><img src={preview.image} alt={label("preview_alt")} style={{ width: "100%", display: "block" }} /><p>{label("recorded_image")}{preview.capture_evidence?.published_at ? ` · ${new Date(preview.capture_evidence.published_at * 1000).toLocaleString()}` : ""}</p><p role="status">{label(preview.classification === "observed" ? preview.visibility?.state || "partial" : "indeterminate")}</p><p>{label(preview.detection_available ? "actual_detection" : "no_detection")}</p>{preview.detection_available && <ol>{preview.observations?.map((item, index) => <li key={index}>{label(`decision_${item.classification}`)}</li>)}</ol>}{preview.classification !== "observed" && <details><summary>{label("details")}</summary>{preview.reason}</details>}</>}
    </SubModal>
  </div>;
}

export const visualRegionTranslations = {
  "pt-BR": {
    "core.ui.pipelines.operator_name.camera.panorama_region": "Região na panorâmica",
    "core.ui.pipelines.operator_name.camera.region_occurrence": "Ocorrência na região",
    "core.ui.pipelines.panels.visual_region.brush": "Pincel",
    "core.ui.pipelines.panels.visual_region.limit": "Limite de desenho atingido. Desfaça um traço ou limpe a região para continuar.",
    "core.ui.pipelines.panels.visual_region.keyboard": "Setas movem o cursor; Enter aplica o pincel ou a borracha",
    "core.ui.pipelines.panels.visual_region.undo": "Desfazer",
    "core.ui.pipelines.panels.visual_region.navigate": "Navegar",
    ...Object.fromEntries(Object.entries({ observation_connection: "Conecte à saída observations da região para acompanhar presença e saída, inclusive quando a imagem ficar indeterminada.", occurrence: "Observar", presence: "Presença", entry: "Entrada observada", duration: "Duração mínima (segundos)", framing: "Enquadramento", maintain: "Solicitar enquadramento enquanto ativo", framing_unavailable: "Enquadramento automático requer um perfil de atenção qualificado para esta referência. Intervenção manual e outras prioridades podem interrompê-lo.", tracking_disabled: "Desativei o tracking automático da câmera", decision_inside: "Dentro da região", decision_outside: "Fora da região", decision_indeterminate: "Posição indeterminada", recorded_image: "Imagem registrada; não é transmissão ao vivo", actual_detection: "Última detecção compatível avaliada pelo pipeline. A conferência não dispara ações.", inside: "Dentro da região", source: "Transmissão", select_source: "Escolher transmissão", name: "Nome da região", draw: "Desenhar na panorâmica", edit: "Editar região", criterion: "Considerar dentro quando", base: "A base estiver dentro", center: "O centro estiver dentro", overlap: "Parte da detecção estiver dentro", whole: "Toda a detecção estiver dentro", overlap_amount: "Sobreposição mínima (%)", passive: "Observar quando aparecer", preview: "Conferir na imagem", changed: "A panorâmica foi substituída. A região salva continua vinculada à referência anterior; editar inicia um novo desenho.", remote_unavailable: "A prévia não está disponível neste servidor de processamento.", loading: "Conferindo uma imagem recente…", preview_alt: "Imagem da câmera com a região projetada", full: "Região inteira na imagem", partial: "Região parcialmente na imagem", outside: "Região fora da imagem", indeterminate: "Não foi possível confirmar a correspondência", no_detection: "Prévia de enquadramento. Nenhuma detecção foi executada e nenhuma ação do pipeline foi disparada.", details: "Detalhes" }).map(([key, value]) => [`ext.cameras.visual_region.${key}`, value])),
  },
  en: {
    "core.ui.pipelines.operator_name.camera.panorama_region": "Region in panorama",
    "core.ui.pipelines.operator_name.camera.region_occurrence": "Region occurrence",
    "core.ui.pipelines.panels.visual_region.brush": "Brush",
    "core.ui.pipelines.panels.visual_region.limit": "Drawing limit reached. Undo a stroke or clear the region to continue.",
    "core.ui.pipelines.panels.visual_region.keyboard": "Arrow keys move the cursor; Enter applies the brush or eraser",
    "core.ui.pipelines.panels.visual_region.undo": "Undo",
    "core.ui.pipelines.panels.visual_region.navigate": "Navigate",
    ...Object.fromEntries(Object.entries({ observation_connection: "Connect to the region’s observations output to track presence and exit, including indeterminate images.", occurrence: "Observe", presence: "Presence", entry: "Observed entry", duration: "Minimum duration (seconds)", framing: "Framing", maintain: "Request framing while active", framing_unavailable: "Automatic framing requires a qualified attention profile for this reference. Manual control and other priorities may interrupt it.", tracking_disabled: "I disabled the camera’s automatic tracking", decision_inside: "Inside the region", decision_outside: "Outside the region", decision_indeterminate: "Indeterminate position", recorded_image: "Recorded image; this is not a live stream", actual_detection: "Last compatible detection evaluated by the pipeline. This review does not trigger actions.", inside: "Inside the region", source: "Stream", select_source: "Choose stream", name: "Region name", draw: "Draw on panorama", edit: "Edit region", criterion: "Consider inside when", base: "The base is inside", center: "The center is inside", overlap: "Part of the detection is inside", whole: "The entire detection is inside", overlap_amount: "Minimum overlap (%)", passive: "Observe when visible", preview: "Check in image", changed: "The panorama was replaced. The saved region remains bound to the previous reference; editing starts a new drawing.", remote_unavailable: "Preview is unavailable on this processing server.", loading: "Checking a recent image…", preview_alt: "Camera image with projected region", full: "Entire region in image", partial: "Region partially in image", outside: "Region outside image", indeterminate: "Could not confirm correspondence", no_detection: "Framing preview. No detection was run and no pipeline action was triggered.", details: "Details" }).map(([key, value]) => [`ext.cameras.visual_region.${key}`, value])),
  },
};
