import React, { useEffect, useState } from "react";
import type { InspectionSnapshot } from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";
import { listPipelines, openPipelineInspection, getPipelineInspection, closePipelineInspection, type Pipeline } from "../../util/api";
import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
import { Modal } from "../Modal";
import "./pipelineInspection.css";

export function PipelineInspectionPanel() {
  const { t } = i18n.useI18n();
  const [open, setOpen] = useState(false);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [selected, setSelected] = useState("");
  const [snapshot, setSnapshot] = useState<InspectionSnapshot | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [visible, setVisible] = useState(!document.hidden);
  const [now, setNow] = useState(Date.now());
  const [loadedImage, setLoadedImage] = useState("");
  useEffect(() => {
    const changed = () => { setVisible(!document.hidden); setSnapshot(null); };
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!open) return;
    let canceled = false;
    setLoading(true); setError(false);
    listPipelines().then(items => { if (!canceled) setPipelines(items); })
      .catch(() => { if (!canceled) setError(true); })
      .finally(() => { if (!canceled) setLoading(false); });
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => { canceled = true; window.clearInterval(timer); };
  }, [open]);
  useEffect(() => {
    setSnapshot(null); setLoadedImage("");
    if (!open || !visible || !selected) return;
    let canceled = false, session = "", timer = 0;
    setError(false); setLoading(true);
    async function poll() {
      try {
        const value = session ? await getPipelineInspection(session) : await openPipelineInspection(selected);
        session = value.session;
        if (canceled) { void closePipelineInspection(session).catch(() => {}); return; }
        setSnapshot(value); setLoading(false);
        timer = window.setTimeout(poll, 400);
      } catch {
        if (!canceled) { setSnapshot(null); setLoadedImage(""); setError(true); setLoading(false); }
      }
    }
    void poll();
    return () => { canceled = true; window.clearTimeout(timer); if (session) void closePipelineInspection(session).catch(() => {}); };
  }, [open, visible, selected]);
  const current = snapshot?.nodes.find(node => node.image_available && node.expires_at !== null && node.expires_at * 1000 > now);
  const image = current && snapshot ? resolveToposyncUrl(`/api/pipeline-inspection/${encodeURIComponent(snapshot.session)}/images/${encodeURIComponent(current.node_id)}/${current.image_sequence}`) : "";
  const close = () => { setOpen(false); setSelected(""); setSnapshot(null); setError(false); setLoadedImage(""); };
  return <>
    <button type="button" className="iconButton streamsTileOverlayButton" title={t("inspection.open")} aria-label={t("inspection.open")} onClick={() => setOpen(true)}><Icon name="eye" /></button>
    <Modal open={open} title={t("inspection.open")} onClose={close} panelClassName="pipelineInspectionModal">
      <p className="pipelineInspectionLimit">{t("inspection.limit")}</p>
      <label className="pipelineInspectionSelect">{t("inspection.choose")}
        <select value={selected} onChange={event => setSelected(event.target.value)}>
          <option value="">{t("inspection.choose")}</option>
          {pipelines.map(pipeline => <option key={pipeline.name} value={pipeline.name}>{pipeline.display_name || pipeline.name}</option>)}
        </select>
      </label>
      {error ? <p role="alert">{t("inspection.failed")}</p> : loading ? <p role="status">{t("inspection.loading")}</p> : !pipelines.length ? <p>{t("inspection.empty")}</p> : !snapshot ? <p>{t("inspection.waiting")}</p> : null}
      <div className="pipelineInspectionGrid">
        <section aria-label={t("inspection.frame")}>
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
        </section>
        <section aria-label={t("inspection.open")}>
          {snapshot?.nodes.map(node => {
            const expired = node.expires_at !== null && node.expires_at * 1000 <= now;
            const state = expired ? "expired" : node.state;
            return <article className="pipelineInspectionNode" key={node.node_id}>
              <strong>{t(`core.ui.pipelines.operator_name.${node.operator}`, {}, node.node_id)}</strong><span>{node.node_id} · {node.operator}</span>
              <span>{t(state === "waiting" ? "inspection.waiting_state" : state === "expired" ? "inspection.expired" : `inspection.${state}`)}</span>
              <small>{t("inspection.counts")}: {node.received_count} / {node.produced_count} / {node.filtered_count}</small>
              {state === "waiting" ? <small>{t("inspection.waiting")}</small> : null}
              {node.reason ? <small>{t(["category_rejected", "image_unavailable", "inference_interval", "sample_expired", "operator_error", "flow_limit", "inspection_capacity", "delivery_not_accepted", "execution_stopped"].includes(node.reason) ? `inspection.${node.reason}` : "inspection.reason")}</small> : null}
              {!expired && node.motion ? <small>{t("inspection.motion")}: {node.motion.current} / {node.motion.maximum} ({t("inspection.window", {seconds:node.motion.window_seconds,samples:node.motion.samples})})</small> : null}
            </article>;
          })}
        </section>
      </div>
      {snapshot && snapshot.dropped > 0 ? <p>{t("inspection.reduced")}: {snapshot.dropped}</p> : null}
    </Modal>
  </>;
}
