import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { InspectionSnapshot } from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";
import { listPipelines, openPipelineInspection, getPipelineInspection, closePipelineInspection, type Pipeline } from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
import { PairedInspectionVideo } from "./PairedInspectionVideo";
import { inspectionRead } from "./inspectionRead";
import { inspectionSources } from "./inspectionSources";
import { sharedInspectionExecutions, type InspectionExecution } from "./inspectionSharing";
import { readInspectionResponse } from "./pairedInspection";
import "./pipelineInspection.css";

export function PipelineInspectionPanel({ transmissionId, playerElement, onPairedChange, enabled = true }: {
  transmissionId?: string; playerElement?: React.RefObject<HTMLDivElement>; onPairedChange?: (active: boolean) => void; enabled?: boolean;
}) {
  const { t } = i18n.useI18n();
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [focused, setFocused] = useState("");
  const [cameras, setCameras] = useState<string[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [visible, setVisible] = useState(!document.hidden);
  const [executions, setExecutions] = useState<Record<string, InspectionExecution[]>>({});
  const reportExecutions = useCallback((name: string, nodes: InspectionExecution[] | null) => {
    setExecutions(previous => {
      const current = previous[name];
      if (!nodes && !current || nodes && current && nodes.length === current.length && nodes.every((node, index) =>
        node.node_id === current[index].node_id && node.operator === current[index].operator
        && node.physical_node_id === current[index].physical_node_id && node.producer_epoch === current[index].producer_epoch)) return previous;
      const next = { ...previous };
      if (nodes) next[name] = nodes; else delete next[name];
      return next;
    });
  }, []);
  const active = enabled && visible && open;
  const shared = sharedInspectionExecutions(pipelines.filter(pipeline => selected.includes(pipeline.name))
    .map(pipeline => ({ name: pipeline.name, label: pipeline.display_name || pipeline.name, nodes: executions[pipeline.name] || [] })));
  const configuredCameras = [...new Map(pipelines.flatMap(pipeline => inspectionSources(pipeline.graph)).map(source => [source.id, source])).values()];
  const choices = cameras.length ? pipelines.filter(pipeline => inspectionSources(pipeline.graph).some(source => cameras.includes(source.id))) : pipelines;
  useEffect(() => {
    const changed = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!active) return;
    let canceled = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 2500);
    setLoading(true); setError(false);
    listPipelines({ signal: controller.signal }).then(items => { if (!canceled) setPipelines(items); })
      .catch(() => { if (!canceled) setError(true); })
      .finally(() => { window.clearTimeout(timeout); if (!canceled) setLoading(false); });
    return () => { canceled = true; controller.abort(); window.clearTimeout(timeout); };
  }, [active]);
  function close() { setOpen(false); setSelected([]); setFocused(""); setCameras([]); trigger.current?.focus(); }
  useEffect(() => {
    if (!open) return;
    const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [open]);
  return <>
    <button ref={trigger} type="button" className="iconButton streamsTileOverlayButton" title={t("inspection.open")} aria-label={t("inspection.open")} aria-expanded={open} onClick={() => setOpen(true)}><Icon name="eye" /></button>
    {open ? createPortal(<aside className={`pipelineInspectionPanel${selected.length > 1 ? " isMultiple" : ""}`} aria-label={t("inspection.open")}>
      <button type="button" autoFocus onClick={close}>{t("inspection.close")}</button>
      <h2>{t("inspection.open")}</h2>
      <p className="pipelineInspectionLimit">{t("inspection.multiple_limit")}</p>
      {configuredCameras.length > 0 ? <details><summary>{t("inspection.configured_cameras")}</summary><fieldset className="pipelineInspectionChoices"><legend>{t("inspection.configured_cameras")}</legend>
        {configuredCameras.map(camera => <label key={camera.id}><input type="checkbox" checked={cameras.includes(camera.id)} onChange={event =>
          setCameras(previous => event.target.checked ? [...previous, camera.id] : previous.filter(id => id !== camera.id))} />{camera.name}</label>)}
        <small>{t("inspection.camera_filter")}</small>
      </fieldset></details> : null}
      <fieldset className="pipelineInspectionChoices"><legend>{t("inspection.choose_multiple")}</legend>
        {choices.map(pipeline => <label key={pipeline.name}><input type="checkbox" checked={selected.includes(pipeline.name)}
          disabled={!selected.includes(pipeline.name) && selected.length >= 4} onChange={event => {
            setSelected(previous => event.target.checked ? [...previous, pipeline.name] : previous.filter(name => name !== pipeline.name));
            if (event.target.checked && !focused) setFocused(pipeline.name);
            if (!event.target.checked && focused === pipeline.name) setFocused(selected.find(name => name !== pipeline.name) || "");
          }} />{pipeline.display_name || pipeline.name}</label>)}
      </fieldset>
      {error ? <p role="alert">{t("inspection.failed")}</p> : loading ? <p role="status">{t("inspection.loading")}</p> : !pipelines.length ? <p>{t("inspection.empty")}</p> : null}
      {active && shared.length > 0 ? <details className="pipelineInspectionSharing"><summary>{t("inspection.shared_execution")}</summary>
        <p>{t("inspection.shared_selected")}</p>
        <ul>{shared.map(([key, execution]) => <li key={key}>
          <strong>{t(`core.ui.pipelines.operator_name.${execution.operator}`, {}, execution.operator)}</strong>
          <ul>{execution.consumers.map(consumer => <li key={JSON.stringify([consumer.pipeline, consumer.node])}>
            {consumer.label} · {consumer.node}
          </li>)}</ul>
        </li>)}</ul>
      </details> : null}
      {active ? <div className="pipelineInspectionFlows">{pipelines.filter(pipeline => selected.includes(pipeline.name)).map(pipeline =>
        <InspectionFlow key={pipeline.name} pipeline={pipeline} transmissionId={transmissionId}
          reportExecutions={reportExecutions}
          onRemove={() => { setSelected(previous => previous.filter(name => name !== pipeline.name)); if (focused === pipeline.name) setFocused(selected.find(name => name !== pipeline.name) || ""); }}
          playerElement={focused === pipeline.name ? playerElement : undefined}
          onPairedChange={focused === pipeline.name ? onPairedChange : undefined}
          onFocus={playerElement && focused !== pipeline.name ? () => setFocused(pipeline.name) : undefined} />)}</div> : <p>{t("inspection.suspended")}</p>}
    </aside>, document.body) : null}
  </>;
}

function InspectionFlow({ pipeline, transmissionId, playerElement, onPairedChange, onFocus, onRemove, reportExecutions }: {
  pipeline: Pipeline; transmissionId?: string; playerElement?: React.RefObject<HTMLDivElement>;
  onPairedChange?: (active: boolean) => void; onFocus?: () => void;
  onRemove: () => void;
  reportExecutions: (name: string, nodes: InspectionExecution[] | null) => void;
}) {
  const { t } = i18n.useI18n();
  const article = useRef<HTMLElement>(null);
  const preview = useRef<HTMLDivElement>(null);
  const [onscreen, setOnscreen] = useState(false);
  const [snapshot, setSnapshot] = useState<InspectionSnapshot | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [loadedImage, setLoadedImage] = useState("");
  const [image, setImage] = useState("");
  const [frozen, setFrozen] = useState(false);
  const [frozenAt, setFrozenAt] = useState(0);
  const [boxes, setBoxes] = useState(true);
  const [layers, setLayers] = useState<ReadonlySet<string>>(() => new Set(["pose", "tracks", "trails"]));
  const [selectedNodes, setSelectedNodes] = useState<ReadonlySet<string>>();
  useEffect(() => {
    // Keep only execution identifiers offscreen so the summary cannot toggle its own visibility.
    if (error) reportExecutions(pipeline.name, null);
    else if (snapshot) reportExecutions(pipeline.name, snapshot.nodes.map(({ node_id, operator, physical_node_id, producer_epoch }) =>
      ({ node_id, operator, physical_node_id, producer_epoch })));
  }, [error, snapshot, pipeline.name, reportExecutions]);
  useEffect(() => () => reportExecutions(pipeline.name, null), [pipeline.name, reportExecutions]);
  const graph = pipeline.graph as { nodes?: Array<{ id: string; operator: string; config?: { transmission_id?: string } }> };
  const publications = graph.nodes?.filter(node => node.operator === "stream.publish_video") || [];
  const publication = publications.find(node => node.config?.transmission_id === transmissionId) || publications[0];
  const inPlayer = Boolean(playerElement && publication?.config?.transmission_id === transmissionId);
  const target = inPlayer ? playerElement! : preview;
  const paired = Boolean(onscreen && publication && snapshot);
  useEffect(() => { onPairedChange?.(paired && inPlayer); return () => onPairedChange?.(false); }, [paired, inPlayer, onPairedChange]);
  useEffect(() => {
    const observer = new IntersectionObserver(entries => setOnscreen(entries.some(entry => entry.isIntersecting)));
    if (article.current) observer.observe(article.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!onscreen) return;
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [onscreen]);
  useEffect(() => {
    setSnapshot(null); setLoadedImage(""); setLoading(false); setError(false); setFrozen(false);
    if (!onscreen) return;
    let canceled = false, session = "", released = false, timer = 0, after = 0;
    const controller = new AbortController();
    const release = () => {
      if (session && !released) { released = true; void closePipelineInspection(session).catch(() => {}); }
    };
    setLoading(true);
    async function poll() {
      try {
        const value = await inspectionRead(signal => {
          const options = { signal, after };
          return session ? getPipelineInspection(session, options) : openPipelineInspection(pipeline.name, options);
        }, controller.signal);
        session = value.session;
        if (canceled) { release(); return; }
        if (controller.signal.aborted) throw new Error("inspection_timeout");
        after = value.sequence;
        setSnapshot(value); setLoading(false);
        if (!publication) timer = window.setTimeout(poll, 400);
      } catch {
        release();
        if (!canceled) { setSnapshot(null); setLoadedImage(""); setError(true); setLoading(false); }
      }
    }
    void poll();
    return () => { canceled = true; controller.abort(); window.clearTimeout(timer); release(); };
  }, [onscreen, pipeline.name, publication?.id]);
  const current = snapshot?.nodes.find(node => (!selectedNodes || selectedNodes.has(node.node_id)) && node.image_available && node.expires_at !== null && node.expires_at * 1000 > now);
  const imageUrl = !publication && current && snapshot ? resolveToposyncUrl(`/api/pipeline-inspection/${encodeURIComponent(snapshot.session)}/images/${encodeURIComponent(current.node_id)}/${current.image_sequence}`) : "";
  useEffect(() => {
    setImage(""); setLoadedImage("");
    if (!imageUrl) return;
    const controller = new AbortController();
    let url = "";
    void inspectionRead(async signal => {
      const response = await fetch(imageUrl, { signal, credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("inspection_image_unavailable");
      return readInspectionResponse(response);
    }, controller.signal).then(bytes => {
      if (!controller.signal.aborted) { url = URL.createObjectURL(new Blob([new Uint8Array(bytes).buffer], { type: "image/jpeg" })); setImage(url); }
    }).catch(() => {});
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [imageUrl]);
  const sources = [...new Set(snapshot?.nodes.flatMap(node => node.frame ? [`${node.frame.device_id || ""} / ${node.frame.source_id}`] : []))];
  const nodeLabel = (id: string) => {
    const node = snapshot?.nodes.find(value => value.node_id === id);
    return node ? t(`core.ui.pipelines.operator_name.${node.operator}`, {}, id) : id;
  };
  return <article className="pipelineInspectionFlow" ref={article}>
    <h3>{pipeline.display_name || pipeline.name}</h3>
    <button type="button" onClick={onRemove}>{t("inspection.remove_flow")}</button>
    {onFocus && publications.some(node => node.config?.transmission_id === transmissionId) ? <button type="button" onClick={onFocus}>{t("inspection.show_in_player")}</button> : null}
    <p className="pipelineInspectionLimit">{t(publication ? "inspection.paired_video" : "inspection.frame")}</p>
    {sources.length ? <p>{t("inspection.sources")}: {sources.join(" · ")}</p> : null}
    {!inPlayer && publication ? <div className="pipelineInspectionPreview" ref={preview} /> : null}
    {!onscreen ? <p>{t("inspection.suspended")}</p> : error ? <p role="alert">{t("inspection.failed")}</p> : loading ? <p role="status">{t("inspection.loading")}</p> : !snapshot ? <p>{t("inspection.waiting")}</p> : null}
      {snapshot && <details className="pipelineInspectionLayers"><summary>{t("inspection.layers")}</summary><div className="pipelineInspectionControls">
        <label><input type="checkbox" checked={boxes} onChange={event => setBoxes(event.target.checked)} />{t("inspection.boxes")}</label>
        {["pose", "tracks", "trails", "masks", "faces", "identity", "embedding", "groups", "gestures", "mapping", "zones", "motion", "activity"].map(layer => <label key={layer}><input type="checkbox" checked={layers.has(layer)} onChange={event => {
          const next = new Set(layers); if (event.target.checked) next.add(layer); else next.delete(layer); setLayers(next);
        }} />{t(`inspection.${layer}`)}</label>)}
        {paired && <button type="button" aria-pressed={frozen} onClick={() => { setFrozenAt(Date.now()); setFrozen(!frozen); }}>{t(frozen ? "inspection.resume" : "inspection.freeze")}</button>}
        {[...layers].filter(layer => !snapshot.nodes.some(node => node.expires_at !== null && node.expires_at * 1000 > now && (layer === "motion" ? node.motion : node.visuals.some(visual => visual.layer === layer)))).map(layer =>
          <small key={layer}>{t("inspection.layer_unavailable", { layer: t(`inspection.${layer}`) })}</small>)}
      </div></details>}
      <div className="pipelineInspectionGrid">
        {!publication && <section aria-label={t("inspection.frame")}>
          <h3>{t("inspection.frame")}</h3>
          <p>{t("inspection.raw")}</p>
          {image && current ? <div className="pipelineInspectionImage" style={{ aspectRatio: `${current.geometry?.width || 1} / ${current.geometry?.height || 1}` }}>
            <img src={image} alt={t("inspection.frame")} onLoad={() => setLoadedImage(image)} onError={() => setLoadedImage("")} />
            {loadedImage === image ? <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
              {boxes && current.detections.map((detection, index) => <rect key={index} x={detection.box[0]} y={detection.box[1]} width={detection.box[2] - detection.box[0]} height={detection.box[3] - detection.box[1]} />)}
            </svg> : <p>{t("inspection.expired")}</p>}
          </div> : <p>{t("inspection.expired")}</p>}
          {boxes && current && loadedImage === image ? <ul>{current.detections.map((detection, index) => <li key={index}>{detection.label} · {t("inspection.score")}: {detection.score.toFixed(3)}</li>)}</ul> : null}
          {current?.frame ? <details><summary>{current.node_id}</summary><pre>{JSON.stringify({ frame: current.frame, geometry: current.geometry, model: current.model_id, revision: snapshot?.revision }, null, 2)}</pre></details> : null}
        </section>}
        <details className="pipelineInspectionDetails"><summary>{t("inspection.operators")}</summary>
          {snapshot?.nodes.map(node => {
            const expired = node.expires_at !== null && node.expires_at * 1000 <= (frozen && paired ? frozenAt : now);
            const state = expired ? "expired" : node.state;
            return <article className="pipelineInspectionNode" key={node.node_id}>
              <label><input type="checkbox" checked={!selectedNodes || selectedNodes.has(node.node_id)} onChange={event => {
                const next = new Set(selectedNodes || snapshot.nodes.map(value => value.node_id));
                if (event.target.checked) next.add(node.node_id); else next.delete(node.node_id);
                setSelectedNodes(next);
              }} />{t("inspection.show_operator", { operator: node.node_id })}</label>
              <strong>{t(`core.ui.pipelines.operator_name.${node.operator}`, {}, node.node_id)}</strong><span>{node.node_id} · {node.operator}</span>
              <span>{t(state === "waiting" ? "inspection.waiting_state" : state === "expired" ? "inspection.expired" : `inspection.${state}`)}</span>
              <small>{t("inspection.counts")}: {node.received_count} / {node.produced_count} / {node.filtered_count}</small>
              {!expired && node.processing_ms != null ? <small>{t("inspection.processing_ms")}: {node.processing_ms.toFixed(2)}</small> : null}
              {state === "waiting" ? <small>{t("inspection.waiting")}</small> : null}
              {node.reason ? <small>{t(["category_rejected", "image_unavailable", "inference_interval", "sample_expired", "operator_error", "flow_limit", "inspection_capacity", "delivery_not_accepted", "execution_stopped", "mask_geometry_unavailable"].includes(node.reason) ? `inspection.${node.reason}` : "inspection.reason")}</small> : null}
              {!expired && layers.has("motion") && node.motion ? <small>{t("inspection.motion")}: {node.motion.current} / {node.motion.maximum} ({t("inspection.window", {seconds:node.motion.window_seconds,samples:node.motion.samples})})</small> : null}
              {!expired && (!selectedNodes || selectedNodes.has(node.node_id)) && node.visuals.filter(visual => layers.has(visual.layer)).map((visual, index) => <div className="pipelineInspectionVisual" key={index}>
                {t(`inspection.${visual.layer}`, {}, visual.layer)}{visual.subject_id && visual.access_scope === "diagnostic_private" ? ` · ${visual.subject_id}` : ""}
                {visual.label ? ` · ${["groups", "gestures"].includes(visual.layer) ? t(`inspection.${visual.label}`, {}, visual.label) : visual.label}` : ""}
                {visual.score !== null ? ` · ${t(`inspection.${visual.score_kind}`, {}, visual.score_kind || "")} ${visual.score.toFixed(3)}` : ""}
                {visual.state ? ` · ${t(`inspection.${visual.layer === "identity" && visual.state === "unknown" ? "identity_unknown" : visual.state}`, {}, visual.state)}` : ""}
                {visual.local_id ? ` · ${t("inspection.local_id")}: ${visual.local_id}` : ""}
                {visual.raster ? ` · ${visual.raster.width} × ${visual.raster.height} · ${t("inspection.mask_limit")}` : ""}
                {Object.entries(visual.properties).map(([key, value]) => <span key={key}> · {t(`inspection.${key}`, {}, key)}: {
                  ["association_reason", "gesture_reason", "mapping_reason", "activity_reason", "evaluation_source", "side", "evidence", "provenance"].includes(key)
                    ? t(`inspection.${value}`, {}, String(value)) : String(value)}</span>)}
                {visual.subject_id && visual.access_scope !== "diagnostic_private" ? <details><summary>{t("inspection.individual_reference")}</summary>{visual.subject_id}</details> : null}
                {visual.related_subject_ids.length > 0 ? <details><summary>{t("inspection.group_members")}</summary>
                  <ul>{visual.related_subject_ids.map(identifier => <li key={identifier}>{identifier}</li>)}</ul>
                  {Number(visual.properties.member_count) > visual.related_subject_ids.length ? <span>{t("inspection.reduced")}</span> : null}
                </details> : null}
                {visual.segment_id ? <details><summary>{t(visual.layer === "groups" ? "inspection.group_reference" : visual.layer === "gestures" ? "inspection.gesture_episode" : "inspection.segment")}</summary>{visual.segment_id}
                  {visual.sample_sequences.length > 0 ? <span> · {t("inspection.samples")}: {visual.sample_sequences.join(", ")}</span> : null}
                </details> : null}
              </div>)}
            </article>;
          })}
        </details>
      </div>
      {snapshot && <details className="pipelineInspectionDetails"><summary>{t("inspection.deliveries")}</summary>
        <p>{t("inspection.delivery_limit")}</p>
        {snapshot.deliveries.map(delivery => <article className="pipelineInspectionNode" key={`${delivery.physical_edge_id}:${delivery.source_node_id}:${delivery.source_port}:${delivery.target_node_id}:${delivery.target_port}`}>
          <strong>{nodeLabel(delivery.source_node_id)} → {nodeLabel(delivery.target_node_id)}</strong>
          <span>{t("inspection.delivery_counts")}: {delivery.accepted_count} / {delivery.received_count} / {delivery.rejected_count}</span>
          <span>{t("inspection.latest_delivery")}: {t(`inspection.delivery_${delivery.expires_at === null || delivery.expires_at * 1000 <= now ? "expired" : delivery.state}`)}</span>
          <details><summary>{t("inspection.delivery_reference")}</summary><pre>{JSON.stringify({
            physical_edge_id: delivery.physical_edge_id, producer_epoch: delivery.producer_epoch,
            source: { node: delivery.source_node_id, port: delivery.source_port },
            target: { node: delivery.target_node_id, port: delivery.target_port },
            frame: delivery.expires_at !== null && delivery.expires_at * 1000 > now ? delivery.frame : null,
            revision: snapshot.revision,
          }, null, 2)}</pre></details>
        </article>)}
        {!snapshot.deliveries.length && <p>{t("inspection.delivery_empty")}</p>}
      </details>}
      {snapshot && snapshot.dropped > 0 ? <p>{t("inspection.reduced")}: {snapshot.dropped}</p> : null}
    {paired && publication && snapshot && target.current ? createPortal(
      <PairedInspectionVideo session={snapshot.session} nodeId={publication.id} frozen={frozen} boxes={boxes}
        layers={layers} selectedNodes={selectedNodes}
        onSnapshot={setSnapshot} onClosed={() => { void closePipelineInspection(snapshot.session).catch(() => {}); setSnapshot(null); setError(true); }} />,
      target.current) : null}
  </article>;
}
