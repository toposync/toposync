import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { InspectionSnapshot } from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";
import { listPipelines, openPipelineInspection, getPipelineInspection, closePipelineInspection, type Pipeline } from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
import { PairedInspectionVideo } from "./PairedInspectionVideo";
import "./pipelineInspection.css";

export function PipelineInspectionPanel({ transmissionId, playerElement, onPairedChange }: {
  transmissionId: string; playerElement: React.RefObject<HTMLDivElement>; onPairedChange: (active: boolean) => void;
}) {
  const { t } = i18n.useI18n();
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [selected, setSelected] = useState("");
  const [snapshot, setSnapshot] = useState<InspectionSnapshot | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingPipelines, setLoadingPipelines] = useState(false);
  const [visible, setVisible] = useState(!document.hidden);
  const [now, setNow] = useState(Date.now());
  const [loadedImage, setLoadedImage] = useState("");
  const [frozen, setFrozen] = useState(false);
  const [boxes, setBoxes] = useState(true);
  const [layers, setLayers] = useState<ReadonlySet<string>>(() => new Set(["pose"]));
  const [selectedNodes, setSelectedNodes] = useState<ReadonlySet<string>>();
  const selectedPipeline = pipelines.find(pipeline => pipeline.name === selected);
  const graph = selectedPipeline?.graph as { nodes?: Array<{ id: string; operator: string; config?: { transmission_id?: string } }> } | undefined;
  const publication = graph?.nodes?.find(node => node.operator === "stream.publish_video" && node.config?.transmission_id === transmissionId);
  const paired = Boolean(open && visible && publication && snapshot);
  useEffect(() => { onPairedChange(paired); return () => onPairedChange(false); }, [paired, onPairedChange]);
  useEffect(() => { setFrozen(false); setSelectedNodes(undefined); }, [selected, open]);
  useEffect(() => {
    const changed = () => { setVisible(!document.hidden); setSnapshot(null); };
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!open || !visible) { setLoadingPipelines(false); return; }
    let canceled = false;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 2500);
    setLoadingPipelines(true); setError(false); setPipelines([]);
    listPipelines({ signal: controller.signal }).then(items => { if (!canceled && !controller.signal.aborted) setPipelines(items); })
      .catch(() => { if (!canceled) setError(true); })
      .finally(() => { window.clearTimeout(timeout); if (!canceled) setLoadingPipelines(false); });
    return () => { canceled = true; controller.abort(); window.clearTimeout(timeout); };
  }, [open, visible]);
  useEffect(() => {
    if (!open || !visible) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [open, visible]);
  useEffect(() => {
    setSnapshot(null); setLoadedImage(""); setLoading(false); setError(false);
    if (!open || !visible || !selected) return;
    let canceled = false, session = "", released = false, timer = 0, timeout = 0;
    const controller = new AbortController();
    const release = () => {
      if (session && !released) { released = true; void closePipelineInspection(session).catch(() => {}); }
    };
    setLoading(true);
    async function poll() {
      timeout = window.setTimeout(() => controller.abort(), 2500);
      try {
        const options = { signal: controller.signal };
        const value = session ? await getPipelineInspection(session, options) : await openPipelineInspection(selected, options);
        session = value.session;
        if (canceled) { release(); return; }
        if (controller.signal.aborted) throw new Error("inspection_timeout");
        setSnapshot(value); setLoading(false);
        if (!publication) timer = window.setTimeout(poll, 400);
      } catch {
        release();
        if (!canceled) { setSnapshot(null); setLoadedImage(""); setError(true); setLoading(false); }
      } finally {
        window.clearTimeout(timeout);
      }
    }
    void poll();
    return () => { canceled = true; controller.abort(); window.clearTimeout(timer); window.clearTimeout(timeout); release(); };
  }, [open, visible, selected, publication?.id]);
  const current = snapshot?.nodes.find(node => (!selectedNodes || selectedNodes.has(node.node_id)) && node.image_available && node.expires_at !== null && node.expires_at * 1000 > now);
  const image = current && snapshot ? resolveToposyncUrl(`/api/pipeline-inspection/${encodeURIComponent(snapshot.session)}/images/${encodeURIComponent(current.node_id)}/${current.image_sequence}`) : "";
  const close = () => { setOpen(false); setSelected(""); setSnapshot(null); setError(false); setLoadedImage(""); trigger.current?.focus(); };
  return <>
    <button ref={trigger} type="button" className="iconButton streamsTileOverlayButton" title={t("inspection.open")} aria-label={t("inspection.open")} aria-expanded={open} onClick={() => setOpen(true)}><Icon name="eye" /></button>
    {open ? createPortal(<aside className="pipelineInspectionPanel" aria-label={t("inspection.open")} onKeyDown={event => { if (event.key === "Escape") close(); }}>
      <button type="button" autoFocus onClick={close}>{t("inspection.close")}</button>
      <h2>{t("inspection.open")}</h2>
      <p className="pipelineInspectionLimit">{t(paired ? "inspection.paired_limit" : "inspection.limit")}</p>
      <label className="pipelineInspectionSelect">{t("inspection.choose")}
        <select value={selected} onChange={event => setSelected(event.target.value)}>
          <option value="">{t("inspection.choose")}</option>
          {pipelines.map(pipeline => <option key={pipeline.name} value={pipeline.name}>{pipeline.display_name || pipeline.name}</option>)}
        </select>
      </label>
      {error ? <p role="alert">{t("inspection.failed")}</p> : loading || loadingPipelines ? <p role="status">{t("inspection.loading")}</p> : !pipelines.length ? <p>{t("inspection.empty")}</p> : !snapshot ? <p>{t("inspection.waiting")}</p> : null}
      {paired && <div className="pipelineInspectionControls">
        <label><input type="checkbox" checked={boxes} onChange={event => setBoxes(event.target.checked)} />{t("inspection.boxes")}</label>
        <label><input type="checkbox" checked={layers.has("pose")} onChange={event => setLayers(new Set(event.target.checked ? ["pose"] : []))} />{t("inspection.pose")}</label>
        <button type="button" aria-pressed={frozen} onClick={() => setFrozen(!frozen)}>{t(frozen ? "inspection.resume" : "inspection.freeze")}</button>
      </div>}
      <div className="pipelineInspectionGrid">
        {!paired && <section aria-label={t("inspection.frame")}>
          <h3>{t("inspection.frame")}</h3>
          <p>{t("inspection.raw")}</p>
          {image && current ? <div className="pipelineInspectionImage" style={{ aspectRatio: `${current.geometry?.width || 1} / ${current.geometry?.height || 1}` }}>
            <img src={image} alt={t("inspection.frame")} onLoad={() => setLoadedImage(image)} onError={() => setLoadedImage("")} />
            {loadedImage === image ? <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
              {current.detections.map((detection, index) => <rect key={index} x={detection.box[0]} y={detection.box[1]} width={detection.box[2] - detection.box[0]} height={detection.box[3] - detection.box[1]} />)}
            </svg> : <p>{t("inspection.expired")}</p>}
          </div> : <p>{t("inspection.expired")}</p>}
          {current && loadedImage === image ? <ul>{current.detections.map((detection, index) => <li key={index}>{detection.label} · {t("inspection.score")}: {detection.score.toFixed(3)}</li>)}</ul> : null}
          {current?.frame ? <details><summary>{current.node_id}</summary><pre>{JSON.stringify({ frame: current.frame, geometry: current.geometry, model: current.model_id, revision: snapshot?.revision }, null, 2)}</pre></details> : null}
        </section>}
        <section aria-label={t("inspection.open")}>
          {snapshot?.nodes.map(node => {
            const expired = node.expires_at !== null && node.expires_at * 1000 <= now;
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
              {state === "waiting" ? <small>{t("inspection.waiting")}</small> : null}
              {node.reason ? <small>{t(["category_rejected", "image_unavailable", "inference_interval", "sample_expired", "operator_error", "flow_limit", "inspection_capacity", "delivery_not_accepted", "execution_stopped"].includes(node.reason) ? `inspection.${node.reason}` : "inspection.reason")}</small> : null}
              {!expired && node.motion ? <small>{t("inspection.motion")}: {node.motion.current} / {node.motion.maximum} ({t("inspection.window", {seconds:node.motion.window_seconds,samples:node.motion.samples})})</small> : null}
              {!expired && node.visuals.map((visual, index) => <small key={index}>
                {t(`inspection.${visual.layer}`, {}, visual.layer)}{visual.subject_id ? ` · ${visual.subject_id}` : ""}
                {visual.score !== null ? ` · ${t(`inspection.${visual.score_kind}`, {}, visual.score_kind || "")} ${visual.score.toFixed(3)}` : ""}
              </small>)}
            </article>;
          })}
        </section>
      </div>
      {snapshot && snapshot.dropped > 0 ? <p>{t("inspection.reduced")}: {snapshot.dropped}</p> : null}
    </aside>, document.body) : null}
    {paired && publication && snapshot && playerElement.current ? createPortal(
      <PairedInspectionVideo session={snapshot.session} nodeId={publication.id} frozen={frozen} boxes={boxes}
        layers={layers} selectedNodes={selectedNodes}
        onSnapshot={setSnapshot} onClosed={() => { void closePipelineInspection(snapshot.session).catch(() => {}); setSnapshot(null); setError(true); }} />,
      playerElement.current) : null}
  </>;
}
