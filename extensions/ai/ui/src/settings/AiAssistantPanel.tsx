import React, { useEffect, useState } from "react";
import { requestJson, resolveToposyncUrl, type HostI18n, type SettingsPanel } from "@toposync/plugin-api";

type Catalog = { pipelines: string[]; pipeline_labels?: Record<string, string>; profiles: { id: string; name: string; model: string }[] };
type Preview = { proposal_id: string; difference: string; changes: string[]; pipeline_name: string };
type SavedChange = { undo_id: string; pipeline_name: string; savedAt: number };

export function createAiAssistantPanel(): SettingsPanel {
  return {
    id: "toposync.ai.assistant", name: { key: "ext.ai.assistant.title" }, description: { key: "ext.ai.assistant.description" },
    icon: "brain", managesOwnPersistence: true, pipelineAssistant: true,
    render: ({ i18n }) => <Assistant i18n={i18n} />,
  };
}
function Assistant({ i18n }: { i18n: HostI18n }): React.ReactElement {
  const { t } = i18n.useI18n();
  const params = new URLSearchParams(window.location.search);
  const [mode, setMode] = useState(params.get("pipeline") ? "pipeline" : "create");
  return <div style={{ display: "grid", gap: 20, minWidth: 0 }}>
    <label className="field">{t("ext.ai.assistant.task")}<select className="input" value={mode} onChange={event => setMode(event.target.value)}>
      <option value="create">{t("ext.ai.assistant.create")}</option><option value="pipeline">{t("ext.ai.assistant.change")}</option>
    </select></label>
    <PipelineEditor key={mode} create={mode === "create"} i18n={i18n} />
  </div>;
}
function PipelineEditor({ create, i18n }: { create: boolean; i18n: HostI18n }): React.ReactElement {
  const { t: translate } = i18n.useI18n();
  const t = (key: string) => translate(`ext.ai.assistant.${key}`);
  const params = new URLSearchParams(window.location.search);
  const cameraId = create ? params.get("camera_id") : null;
  const sourceId = cameraId ? params.get("source_id") : null;
  const initialPipeline = create ? "" : params.get("pipeline") || "";
  const storageKey = `toposync.ai.last-change:${create ? cameraId || "create" : initialPipeline}`;
  const [saved, setSaved] = useState<SavedChange | null>(() => {
    try { const value = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      return value && typeof value.undo_id === "string" && typeof value.pipeline_name === "string" && Date.now() - value.savedAt < 3600000 ? value : null;
    } catch { return null; }
  });
  const [catalog, setCatalog] = useState<Catalog>({ pipelines: [], profiles: [] });
  const [pipeline, setPipeline] = useState(initialPipeline);
  const [profile, setProfile] = useState("");
  const [instruction, setInstruction] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    requestJson<Catalog>("/api/ai/assistant/pipelines", { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      setCatalog(value); setPipeline(initialPipeline || (value.pipelines.length === 1 ? value.pipelines[0] : "")); setProfile(value.profiles[0]?.id || "");
    }).catch(() => { if (!controller.signal.aborted) setError(t("unavailable")); });
    return () => controller.abort();
  }, []);
  async function run(action: "preview" | "apply" | "undo") {
    if (busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      if (action === "preview") {
        setPreview(null);
        const proposal = await requestJson<Preview & { question?: string }>(create ? "/api/ai/assistant/recipes/preview" : "/api/ai/assistant/preview", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ ...(create ? { camera_id: cameraId, source_id: sourceId } : { pipeline_name: pipeline }), profile_id: profile, instruction }),
        });
        if (proposal.question) setMessage(proposal.question); else setPreview(proposal);
      } else {
        const result = await requestJson<{ undo_id: string }>(`/api/ai/assistant/apply/${action === "undo" ? saved?.undo_id : preview?.proposal_id}`, { method: "POST" });
        const change = action === "apply" && preview ? { undo_id: result.undo_id, pipeline_name: preview.pipeline_name, savedAt: Date.now() } : null;
        setSaved(change); setPreview(null);
        try { if (change) sessionStorage.setItem(storageKey, JSON.stringify(change)); else sessionStorage.removeItem(storageKey); } catch { /* Undo remains available in this view. */ }
        setMessage(t(action === "undo" ? "undone" : "saved"));
      }
    } catch (reason) { setError((reason as { status?: number })?.status === 409 ? t("conflict") : t("unavailable")); }
    finally { setBusy(false); }
  }
  return <section aria-label={t("title")} style={{ display: "grid", gap: 16, maxWidth: 900, minWidth: 0, overflowWrap: "anywhere" }}>
    <p style={{ margin: 0 }}>{t(create ? "createHelp" : "changeHelp")}</p>
    {cameraId && <p style={{ margin: 0 }}>{t("cameraContext")}: <strong>{params.get("camera_name") || cameraId}</strong>{sourceId ? ` · ${params.get("source_name") || sourceId}` : ""}</p>}
    {!create && !catalog.pipelines.length && <p style={{ margin: 0 }}>{t("noPipelines")}</p>}
    {!catalog.profiles.length && <p style={{ margin: 0 }}>{t("noProfiles")} <a href={resolveToposyncUrl("/settings?panel=com.toposync.ai")}>{t("configure")}</a></p>}
    {!create && <label className="field">{t("pipeline")}<select className="input" style={{ minWidth: 0, width: "100%" }} value={pipeline} disabled={busy} onChange={event => { setPipeline(event.target.value); setPreview(null); }}>
      <option value="">{t("choose")}</option>
      {pipeline && !catalog.pipelines.includes(pipeline) && <option value={pipeline}>{t("pipelineUnavailable")}</option>}
      {catalog.pipelines.map(name => <option key={name} value={name}>{catalog.pipeline_labels?.[name] || name}</option>)}
    </select></label>}
    <details><summary>{t("localModel")}</summary><label className="field">{t("profile")}<select className="input" value={profile} disabled={busy} onChange={event => { setProfile(event.target.value); setPreview(null); }}>
      {catalog.profiles.map(item => <option key={item.id} value={item.id}>{item.name} — {item.model}</option>)}
    </select></label></details>
    <label className="field">{t(create ? "observe" : "instruction")}<textarea className="input" style={{ height: "auto", padding: 12, resize: "vertical" }} rows={4} maxLength={4000} value={instruction} disabled={busy} onChange={event => { setInstruction(event.target.value); setPreview(null); setMessage(""); }} placeholder={t(create ? "createExample" : "changeExample")} /></label>
    {error && <p role="alert" style={{ margin: 0 }}>{error}</p>}
    <div role="status">{busy ? t("busy") : message}</div>
    {preview ? <>
      <h3 style={{ margin: 0 }}>{t("review")}: {catalog.pipeline_labels?.[preview.pipeline_name] || preview.pipeline_name}</h3>
      <p style={{ margin: 0 }}>{t("reviewHelp")}</p>
      <ul>{preview.changes.map((change, index) => <li key={index}>{change}</li>)}</ul>
      <details><summary>{t("technical")}</summary><pre tabIndex={0} aria-label={t("difference")} style={{ overflow: "auto", maxHeight: 360, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{preview.difference}</pre></details>
      <button className="primaryButton" disabled={busy} onClick={() => void run("apply")}>{t(create ? "saveRecipe" : "apply")}</button>
      <button className="chipButton" disabled={busy} onClick={() => setPreview(null)}>{t("discard")}</button>
    </> : <button className={saved ? "chipButton" : "primaryButton"} disabled={busy || (!create && !catalog.pipelines.includes(pipeline)) || !profile || !instruction.trim()} onClick={() => void run("preview")}>{t("prepare")}</button>}
    {saved && <>
      <a className={preview ? "chipButton" : "primaryButton"} href={resolveToposyncUrl(`/settings/pipelines/${encodeURIComponent(saved.pipeline_name)}`)}>{t("reviewSaved")}: {saved.pipeline_name}</a>
      <button className="chipButton" disabled={busy} onClick={() => void run("undo")}>{t("undo")}</button><small>{t("undoHelp")}</small>
    </>}
  </section>;
}
