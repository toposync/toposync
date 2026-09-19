import React, { useEffect, useRef, useState } from "react";
import { requestJson, type HostI18n } from "@toposync/plugin-api";
import { styles } from "./styles";

type Model = {
  model_id: string;
  display_name: string;
  availability: string;
  acquisition_supported: boolean;
  acquisition?: { guide_url?: string; source_url?: string };
  license?: { weights_license?: string; dataset_notes?: string };
  install_job?: { status: string; progress_pct?: number } | null;
};
type Status = { ok: boolean; status?: { vision?: { task_catalogs?: Record<string, { items: Model[] }> } } };
const activeStatuses = new Set(["queued", "downloading", "verifying", "installing", "canceling"]);

function publicSource(value?: string): string | undefined {
  try { const url = new URL(value || ""); return url.protocol === "https:" ? url.href : undefined; }
  catch { return undefined; }
}

export function ModelSetup({ i18n, processingServerId, config }: {
  i18n: HostI18n;
  processingServerId?: string;
  config: Record<string, unknown>;
}) {
  const { t } = i18n.useI18n();
  const text = (key: string) => t(`ext.vision.identity.${key}`);
  const [models, setModels] = useState<Model[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [consent, setConsent] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const locked = useRef(false);
  const operation = useRef<AbortController>();
  const prefix = `/api/processing-servers/${encodeURIComponent(processingServerId || "")}`;
  const ids = [String(config.person_model_id || "opencv_sface_2021dec"), String(config.face_detector_model_id || "opencv_yunet_2023mar"), String(config.pet_model_id || "open_noodle_pet_small")];

  useEffect(() => {
    setConsent({}); setModels([]); setBusy(null); locked.current = false;
    return () => operation.current?.abort();
  }, [processingServerId]);
  useEffect(() => {
    if (!processingServerId) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setError(false);
    void requestJson<Status>(`${prefix}/status`, { signal: controller.signal }).then((response) => {
      if (controller.signal.aborted) return;
      if (!response.ok) throw new Error("unavailable");
      const catalog = response.status?.vision?.task_catalogs;
      setModels([...(catalog?.embedding?.items || []), ...(catalog?.face_detection?.items || [])]);
      setLoading(false);
    }).catch(() => { if (!controller.signal.aborted) { setError(true); setLoading(false); } });
    return () => controller.abort();
  }, [prefix, processingServerId, refresh]);
  const installing = models.some((model) => activeStatuses.has(model.install_job?.status || ""));
  useEffect(() => {
    if (!installing) return;
    const timer = window.setTimeout(() => setRefresh((value) => value + 1), 2000);
    return () => window.clearTimeout(timer);
  }, [installing, refresh]);

  async function prepare(model: Model) {
    if (locked.current || !processingServerId || !consent[model.model_id]) return;
    locked.current = true; setBusy(model.model_id); setError(false);
    const controller = new AbortController(); operation.current = controller;
    try {
      await requestJson(`${prefix}/vision/models/${encodeURIComponent(model.model_id)}/install`, {
        method: "POST", signal: controller.signal, headers: { "content-type": "application/json" },
        body: JSON.stringify({ acknowledge_upstream_terms: true }),
      });
      if (!controller.signal.aborted) setRefresh((value) => value + 1);
    } catch { if (!controller.signal.aborted) setError(true); }
    finally { if (operation.current === controller) { locked.current = false; setBusy(null); } }
  }

  if (!processingServerId) return <p>{text("modelHostUnknown")}</p>;
  return <section className="identityGallery" aria-label={text("models")}>
    <style>{styles}</style><h3>{text("models")}</h3>
    <p className="identityMuted">{t("ext.vision.identity.modelHost", { name: processingServerId })}</p>
    {error && <p role="alert">{text("error")}</p>}
    {loading && !models.length ? <p role="status">{text("loading")}</p> : ids.map((id) => {
      const model = models.find((item) => item.model_id === id);
      if (!model) return <p key={id}>{text("modelNoCatalog")}</p>;
      const active = activeStatuses.has(model.install_job?.status || "");
      const ready = model.availability === "available";
      const source = publicSource(model.acquisition?.guide_url || model.acquisition?.source_url);
      return <div key={id} className="identityForm">
        <strong>{model.display_name}</strong>
        <p role="status">{text(active ? "modelWorking" : ready ? "modelReady" : model.availability === "incompatible" ? "modelUnavailable" : "modelMissing")}</p>
        {model.install_job?.status === "failed" && <p role="alert">{text("modelFailed")}</p>}
        <details><summary>{text("modelSource")}</summary>
          <p>{model.license?.weights_license}</p>
          {source && <p><a href={source} target="_blank" rel="noreferrer">{text("modelSource")}</a></p>}
          {model.license?.dataset_notes && <p className="identityMuted">{text("modelDataNotes")}</p>}
        </details>
        {!ready && !active && model.acquisition_supported && <>
          <label><input type="checkbox" checked={!!consent[id]} onChange={(event) => setConsent((previous) => ({ ...previous, [id]: event.target.checked }))} />{text("modelTerms")}</label>
          <button className="chipButton" type="button" disabled={!!busy || !consent[id]} onClick={() => { void prepare(model); }}>{text("modelPrepare")}</button>
        </>}
      </div>;
    })}
    <button className="chipButton" type="button" disabled={loading || !!busy} onClick={() => setRefresh((value) => value + 1)}>{text("retry")}</button>
  </section>;
}
