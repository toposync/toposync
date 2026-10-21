import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { activateModalFocus, type HostI18n, type HostUi, type PipelineReferenceCapture, type PipelineReferenceFrame } from "@toposync/plugin-api";
import { captureVisualFrame, captureVisualReference, createVisualDraft, readVisualDraft, testVisualState, updateVisualDraft, visualReferenceUrl } from "./api";
import { bindVisualConfiguration, newVisualClass, newVisualConfiguration, replaceVisualRegion, restoreVisualReferences, visualClassesValid, visualSource, type VisualClass, type VisualConfiguration, type VisualDraft } from "./visualState";
import { styles } from "./styles";
import { VisualStateModelSettings } from "./VisualStateModelSettings";
import { VisualStateDecisionSettings } from "./VisualStateDecisionSettings";
import { VisualStatePreviewResult } from "./VisualStatePreviewResult";
import { VisualStateRegionSettings } from "./VisualStateRegionSettings";
import type { VisualPreview } from "./visualState";

type Props = { i18n: HostI18n; config: Record<string, unknown>; replaceConfig: (config: Record<string, unknown>) => void;
  referenceCapture?: PipelineReferenceCapture; showAdvanced?: boolean; ui?: Pick<HostUi, "ImageMaskEditor"> };
type PendingCapture = { draft: VisualDraft; classId: string; operationId: string; frame: PipelineReferenceFrame };

function PhotoDialog({ url, label, close, t }: { url: string; label: string; close: () => void; t: (key: string) => string }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => panel.current ? activateModalFocus(panel.current, close) : undefined, [close]);
  return <div className="modalBackdrop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div ref={panel} className="modalPanel visualReferenceDialog" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}>
      <div className="modalHeader"><h3>{label}</h3><button type="button" className="chipButton" onClick={close}>{t("close")}</button></div>
      <img src={url} alt={label} />
    </div>
  </div>;
}

export function VisualStatePanel({ i18n, config, replaceConfig, referenceCapture, showAdvanced, ui }: Props) {
  const { t: translate } = i18n.useI18n();
  const t = (key: string, parameters?: Record<string, unknown>) => translate(`ext.vision.visualState.${key}`, parameters);
  const [initial] = useState(() => newVisualConfiguration(index => t("state", { index })));
  const unconfigured = !config.sensor_id || config.sensor_id === "unconfigured";
  const value = { ...initial, ...config, ...(unconfigured ? {
    sensor_id: initial.sensor_id, reference_revision: initial.reference_revision, classes: initial.classes,
  } : {}) } as VisualConfiguration;
  const [draft, setDraft] = useState<VisualDraft | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [undo, setUndo] = useState<VisualConfiguration | null>(null);
  const [pending, setPending] = useState<PendingCapture | null>(null);
  const [photo, setPhoto] = useState<{ id: string; label: string } | null>(null);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [preview, setPreview] = useState<{ result: VisualPreview; classes: VisualClass[]; context: string; pipeline: string } | null>(null);
  const active = useRef<AbortController | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const readonly = referenceCapture?.available === false && referenceCapture.reason === "read_only";
  const captureCallback = referenceCapture?.available ? referenceCapture.capture : undefined;
  const inputPipeline = referenceCapture?.available ? referenceCapture.inputPipeline : undefined;
  const pipelineContext = JSON.stringify(inputPipeline) ?? "";
  const source = visualSource(inputPipeline);
  const regionCompatible = !value.canonical_region || (source?.camera_id === value.canonical_region.camera_id && source?.source_id === value.canonical_region.source_id);
  const canCapture = Boolean(captureCallback && regionCompatible && (!value.canonical_region || inputPipeline));
  const context = JSON.stringify(config);
  useLayoutEffect(() => {
    setBusy(null); setPending(null);
    return () => { active.current?.abort(); active.current = null; };
  }, [context, captureCallback, pipelineContext]);
  useEffect(() => {
    if (!readonly && unconfigured) replaceConfig(value);
  }, [config.sensor_id, readonly]);
  useEffect(() => { setDraft(null); setUndo(null); setPhoto(null); }, [value.sensor_id]);

  const classes = Array.isArray(value.classes) ? value.classes : [];
  const valid = classes.every(item => item && typeof item === "object") && visualClassesValid(classes);
  const disabled = readonly || busy !== null;
  const changed = (next: VisualConfiguration) => { setError(null); replaceConfig(next); };
  const updateClass = (classId: string, patch: Partial<VisualClass>) => changed({ ...value,
    classes: classes.map(item => item.class_id === classId ? { ...item, ...patch } : item) });

  async function run(name: string, work: (signal: AbortSignal) => Promise<void>) {
    if (readonly || active.current) return;
    const controller = new AbortController(); active.current = controller; setBusy(name); setError(null);
    const timeout = name === "test" ? setTimeout(() => {
      controller.abort();
      if (active.current === controller) { active.current = null; setBusy(null); setError("visual_preview_timeout"); }
    }, 30_000) : undefined;
    try { await work(controller.signal); }
    catch (cause) {
      if (!controller.signal.aborted) {
        const message = String((cause as Error)?.message ?? "");
        const status = (cause as { status?: number }).status;
        setError(["reference_context_changed", "reference_region_required", "reference_capture_receipt_invalid", "reference_limit", "reference_conflict",
          "visual_preview_busy", "visual_preview_timeout", "visual_preview_configuration_invalid", "visual_preview_input_invalid", "visual_preview_failed", "region_unavailable"].find(key => message.includes(key))
          ?? (status === 401 || status === 403 ? "forbidden" : status === 409 ? "reference_conflict"
            : name === "test" ? "visual_preview_failed" : status === 413 ? "reference_limit" : status === 422 ? "invalid_classes" : "request_failed"));
      }
    } finally { clearTimeout(timeout); if (active.current === controller) { active.current = null; setBusy(null); } }
  }
  async function synchronize(configuration: VisualConfiguration, signal: AbortSignal): Promise<VisualDraft> {
    const previous = draftRef.current;
    const result = previous && previous.expires_at * 1000 > Date.now()
      ? await updateVisualDraft(previous, configuration, signal) : await createVisualDraft(configuration, signal);
    signal.throwIfAborted(); draftRef.current = result; setDraft(result); return result;
  }
  function mutate(next: VisualConfiguration, remember = true) {
    void run("save", async signal => {
      const result = await synchronize(next, signal);
      setUndo(remember ? value : null);
      changed(result.configuration);
    });
  }
  async function upload(candidate: PendingCapture, signal: AbortSignal) {
    const result = await captureVisualReference(candidate.draft, candidate.classId, candidate.operationId, candidate.frame, signal);
    signal.throwIfAborted(); draftRef.current = result.draft; setDraft(result.draft); setPending(null); setUndo(null);
    changed(result.draft.configuration);
  }
  function capture(classId: string) {
    if (!captureCallback || !canCapture || !valid) return;
    void run(classId, async signal => {
      const frame = await (value.canonical_region ? captureVisualFrame(value, inputPipeline, signal) : captureCallback(signal)); signal.throwIfAborted();
      const configuration = bindVisualConfiguration(value, frame.evidence);
      const current = await synchronize(configuration, signal);
      const candidate = { draft: current, classId, operationId: crypto.randomUUID(), frame };
      setPending(candidate); await upload(candidate, signal);
    });
  }
  const closePhoto = React.useCallback(() => setPhoto(null), []);
  const unavailable = !referenceCapture ? "host_unavailable" : referenceCapture.available ? null : referenceCapture.reason;
  const hasReferences = classes.some(item => Array.isArray(item?.reference_ids) && item.reference_ids.length > 0);
  return <section className="identityGallery visualStatePanel" aria-label={t("title")}>
    <style>{styles}</style>
    <p>{t("help")}</p>
    {!value.model_id && <p className="identityMuted">{t("model_missing")}</p>}
    {unavailable && <p role="status">{t(unavailable)}</p>}
    {error && <div role="alert"><p>{t(error)}</p>
      {pending && <button className="chipButton" type="button" disabled={disabled} onClick={() => void run("retry", signal => upload(pending, signal))}>{t("retry_capture")}</button>}
      {draft && error !== "region_unavailable" && !error.startsWith("visual_preview_") && <button className="chipButton" type="button" disabled={disabled} onClick={() => void run("reload", async signal => {
        const current = await readVisualDraft(draft.draft_id, signal); signal.throwIfAborted();
        setUndo(value); draftRef.current = current; setDraft(current); changed(current.configuration);
      })}>{t("reload")}</button>}
      {error === "reference_conflict" && <button className="chipButton" type="button" disabled={disabled || !valid} onClick={() => void run("reopen", async signal => {
        const current = await createVisualDraft(value, signal); signal.throwIfAborted();
        draftRef.current = current; setDraft(current); setPending(null); changed(current.configuration);
      })}>{t("reopen")}</button>}
    </div>}
    {busy && <div role="status">{t(busy === "test" ? "testing" : "working")} <button type="button" className="chipButton" onClick={() => { active.current?.abort(); active.current = null; setBusy(null); setPending(null); }}>{t("cancel")}</button></div>}
    {!valid && <p role="alert">{t("invalid_classes")}</p>}
    <fieldset disabled={disabled}>
      <legend>{t("region")}</legend>
      <p className="identityMuted">{t("region_help")}</p>
      <VisualStateRegionSettings ui={ui} value={value} pipeline={inputPipeline} disabled={disabled} advanced={showAdvanced} change={changed} t={t} />
      <label><input type="checkbox" checked={value.use_full_image} disabled={hasReferences}
        onChange={event => changed(replaceVisualRegion(value, null, event.target.checked))} />{t("full_image")}</label>
      {hasReferences && <button type="button" className="chipButton" onClick={() => mutate({ ...value, source_identity: null,
        preparation_revision: null, region_id: null, use_full_image: false, classes: classes.map(item => ({ ...item, reference_ids: [] })) })}>{t("reset_region")}</button>}
    </fieldset>
    {classes.map((category, index) => category && typeof category === "object" && <fieldset key={category.class_id || index} disabled={disabled} className="visualStateClass">
      <legend>{t("class_number", { index: index + 1 })}</legend>
      <div className="identityToolbar">
        <label className="identityField visualStateName"><span>{t("name")}</span><input value={category.label ?? ""} maxLength={80} required
          onChange={event => updateClass(category.class_id, { label: event.target.value })} /></label>
        <button type="button" className="chipButton" disabled={!valid || !canCapture || category.reference_ids?.length >= 30}
          onClick={() => capture(category.class_id)}>{t("capture")}</button>
        <button type="button" className="chipButton" disabled={classes.length <= 2} aria-label={`${t("remove_class")} ${category.label}`}
          onClick={() => mutate({ ...value, classes: classes.filter(item => item.class_id !== category.class_id) })}>{t("remove_class")}</button>
      </div>
      <label className="identityField"><span>{t("description")}</span><textarea className="input" rows={2} maxLength={1000}
        value={category.description ?? ""} onChange={event => updateClass(category.class_id, { description: event.target.value })} placeholder={t("description_hint")} /></label>
      <div className="visualStatePhotos" aria-label={`${t("references")} ${category.label}`}>
        {(Array.isArray(category.reference_ids) ? category.reference_ids : []).map((reference, photoIndex) => <div className="visualStatePhoto" key={reference}>
          <button type="button" className="identityPhotoButton" disabled={!draft} aria-label={`${t("enlarge")} ${category.label}, ${photoIndex + 1}`}
            onClick={() => setPhoto({ id: reference, label: `${category.label}, ${photoIndex + 1}` })}>
            {draft ? <img src={visualReferenceUrl(draft.draft_id, reference)} alt={`${category.label}, ${photoIndex + 1}`} loading="lazy"
              onError={() => setError("reference_conflict")} /> : <span>{t("load_references")}</span>}
          </button>
          <button type="button" className="chipButton" aria-label={`${t("remove_capture")} ${category.label}, ${photoIndex + 1}`}
            onClick={() => mutate({ ...value, classes: classes.map(item => item.class_id === category.class_id
              ? { ...item, reference_ids: item.reference_ids.filter(id => id !== reference) } : item) })}>{t("remove")}</button>
        </div>)}
      </div>
      {!category.reference_ids?.length && <p className="identityMuted">{t("empty_references")}</p>}
      {(showAdvanced || category.enabled === false) && <><label className="identityField"><span>{t("key")}</span><input value={category.key ?? ""} maxLength={64}
        pattern="[a-z][a-z0-9_]{0,63}" onChange={event => updateClass(category.class_id, { key: event.target.value })} /></label>
        <label><input type="checkbox" checked={category.enabled} onChange={event => updateClass(category.class_id, { enabled: event.target.checked })} />{t("enabled")}</label></>}
    </fieldset>)}
    <div className="identityToolbar">
      <button type="button" className="chipButton" disabled={disabled || classes.length >= 32} onClick={() => changed({ ...value,
        classes: [...classes, newVisualClass(classes, t("state", { index: classes.length + 1 }))] })}>{t("add_class")}</button>
      {hasReferences && !draft && <button type="button" className="chipButton" disabled={disabled || !valid} onClick={() => void run("load", async signal => { await synchronize(value, signal); })}>{t("load_references")}</button>}
      {undo && <button type="button" className="chipButton" disabled={disabled} onClick={() => { mutate(restoreVisualReferences(value, undo), false); }}>{t("undo")}</button>}
    </div>
    <VisualStateModelSettings value={value} change={changed} disabled={disabled} t={t} />
    <VisualStateDecisionSettings value={value} change={changed} disabled={disabled} editing={setSettingsDirty} t={t} />
    <fieldset disabled={disabled}>
      <legend>{t("preview_title")}</legend><p className="identityMuted">{t("preview_help")}</p>
      {!inputPipeline && <p role="status">{t("preview_host_unavailable")}</p>}
      <button type="button" className="chipButton" disabled={!inputPipeline || !regionCompatible || !valid || settingsDirty || !value.model_id || !value.comparison
        || !classes.filter(item => item.enabled).every(item => item.reference_ids.length > 0)}
        onClick={() => void run("test", async signal => {
          const result = await testVisualState(value, inputPipeline, signal); signal.throwIfAborted();
          setPreview({ result, classes: structuredClone(classes), context, pipeline: pipelineContext });
        })}>{t("test_now")}</button>
      {preview && <VisualStatePreviewResult result={preview.result} classes={preview.classes}
        stale={preview.context !== context || preview.pipeline !== pipelineContext || settingsDirty} t={t} />}
    </fieldset>
    {showAdvanced && <label className="identityField"><span>{t("scene_description")}</span><textarea className="input" rows={3} maxLength={2000}
      disabled={disabled} value={value.scene_description} onChange={event => changed({ ...value, scene_description: event.target.value })} /></label>}
    {photo && draft && <PhotoDialog url={visualReferenceUrl(draft.draft_id, photo.id, false)} label={photo.label} close={closePhoto} t={t} />}
  </section>;
}
