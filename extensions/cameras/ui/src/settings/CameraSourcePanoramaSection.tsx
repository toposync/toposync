import React, { useCallback, useEffect, useRef, useState } from "react";
import type { HostI18n, HostUi } from "@toposync/plugin-api";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import { CameraPanoramaRequestError } from "../api/camerasApi";
import type { CameraSourcePanoramaArtifact, CameraSourcePanoramaCrop, CameraSourcePanoramaJob } from "../types";
import { exportPanoramaCrop, PanoramaCropEditor, PanoramaImage } from "./PanoramaCropEditor";
import { FULL_PANORAMA_CROP, panoramaPreviewCrop, samePanoramaCrop, validPanoramaCrop } from "./panoramaCrop";
import { isPanoramaJobRunning, useCameraSourcePanorama } from "./useCameraSourcePanorama";
import { cameraSourcePanoramaStyles } from "./cameraSourcePanoramaStyles";
import { CameraPanoramaTelemetry } from "./CameraPanoramaTelemetry";

function creationDate(value: string | number, locale: string): string {
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date) : "";
}

function panoramaQualityApproved(artifact: CameraSourcePanoramaArtifact | null | undefined): boolean {
  const quality = artifact?.quality;
  return artifact?.quality_approved !== false
    && Boolean(quality && typeof quality === "object" && !Array.isArray(quality)
      && quality.status === "ready" && Array.isArray(quality.reasons) && quality.reasons.length === 0);
}

function panoramaReviewReason(artifact: CameraSourcePanoramaArtifact | null | undefined, candidate = false): "alignment" | "review" | null {
  const quality = artifact?.quality ?? {};
  const reasons = Array.isArray(quality.reasons) ? quality.reasons.filter((reason): reason is string => typeof reason === "string") : [];
  if (reasons.includes("independent_alignment_error")) return "alignment";
  return quality.status === "review" || reasons.length > 0 || (candidate && !panoramaQualityApproved(artifact)) ? "review" : null;
}

function capturePhase(job: CameraSourcePanoramaJob): string {
  if (job.operation === "verify_control") return "verifying_control";
  const progress = job.coverage_progress;
  if (progress?.stage === "return_reference") return "recovering_reference";
  if (progress?.stage === "step") return "exploring_height";
  if (progress?.stage === "reference") return "finding_reference";
  if (job.capture_goal === "initial_region") return "capturing_region";
  if (progress?.stage === "pan") return progress.current_band === 0 ? "capturing_reference" : "capturing_horizontal";
  if (job.phase === "finding_reference") return "finding_reference";
  return ["settling", "stabilizing", "waiting_for_stability"].includes(job.phase) ? "stabilizing" : job.status;
}

function confirmedArtifactBands(artifact: CameraSourcePanoramaArtifact): number | null {
  const acquisition = artifact.coverage?.acquisition;
  const completed = acquisition?.progress?.bands_completed;
  if (typeof completed === "number" && Number.isInteger(completed) && completed >= 0) return completed;
  if (!acquisition?.bands || typeof acquisition.bands !== "object") return null;
  return Object.values(acquisition.bands).filter((band) => band?.complete === true).length;
}

function failureHelp(job: CameraSourcePanoramaJob): string {
  if (job.operation === "verify_control") return "control_unconfirmed_help";
  if (job.captures_accepted === 0) return job.status === "interrupted" ? "interrupted_help_empty" : "job_failed_empty";
  const singleImage = job.captures_accepted === 1;
  if (job.phase === "interrupted_processing") return singleImage ? "interrupted_processing_image" : "interrupted_processing_help";
  if (job.status === "interrupted") return singleImage ? "interrupted_image" : "interrupted_help";
  return singleImage ? "job_failed_image" : "job_failed";
}

export function CameraSourcePanoramaSection({ ui, cameraId, sourceId, cameraName, sourceName, enabled, persisted, i18n, onActiveArtifactChange }: {
  ui: HostUi; cameraId: string; sourceId: string; cameraName: string; sourceName: string; enabled: boolean; persisted: boolean; i18n: HostI18n;
  onActiveArtifactChange?: (artifact: CameraSourcePanoramaArtifact, changed: boolean) => void;
}): React.ReactElement {
  const { t, locale } = i18n.useI18n();
  const text = useCallback((key: string, parameters?: Record<string, unknown>) => t(`ext.cameras.source_panorama.${key}`, parameters), [t]);
  const diagnostic = (code: string) => t(`ext.cameras.source_panorama.diagnostic_${code}`, {}, text("diagnostic_unknown"));
  const number = new Intl.NumberFormat(locale).format;
  const { data, loading, connectionError, actionError, busy: actionBusy, refresh, start, operate, saveCrop,
    nativeReferences, nativeConnectionError, nativeAttemptId, operateReference, clearActionError } = useCameraSourcePanorama(cameraId, sourceId, persisted);
  const activeReference = nativeReferences?.find((reference) => ["queued", "preparing", "stopping"].includes(reference.status));
  const busy = actionBusy ?? (activeReference ? "prepare_reference" : null);
  const [editingArtifact, setEditingArtifact] = useState<CameraSourcePanoramaArtifact | null>(null);
  const [showWhole, setShowWhole] = useState(false);
  const [notice, setNotice] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState(false);
  const [restoreEditorFocus, setRestoreEditorFocus] = useState(false);
  const editButton = useRef<HTMLButtonElement>(null);
  const editorContainer = useRef<HTMLDivElement>(null);
  const detailsSummary = useRef<HTMLElement>(null);
  const job = data?.job;
  const initialArtifact = useRef<string | null>(null);
  const notifyArtifact = useRef(onActiveArtifactChange);
  notifyArtifact.current = onActiveArtifactChange;
  useEffect(() => {
    if (!data) return;
    const identity = data.active ? `${data.active.id}:${data.active.revision}` : "none";
    if (initialArtifact.current !== identity && data.active) notifyArtifact.current?.(data.active, initialArtifact.current !== null);
    initialArtifact.current = identity;
  }, [data?.active?.id, data?.active?.revision, Boolean(data)]);
  const running = isPanoramaJobRunning(job);
  const artifact = data?.active ?? data?.candidate ?? null;
  const jobArtifact = [data?.active, data?.candidate].find((item) => item && item.id === job?.artifact_id);
  const isCandidate = (item: CameraSourcePanoramaArtifact | null | undefined) => Boolean(item && data?.candidate?.id === item.id && data?.active?.id !== item.id);
  const jobReview = panoramaReviewReason(jobArtifact, isCandidate(jobArtifact));
  const imageReview = panoramaReviewReason(artifact);
  const editingReview = panoramaReviewReason(editingArtifact);
  const savedCrop = validPanoramaCrop(artifact?.crop) ? artifact.crop : FULL_PANORAMA_CROP;
  const cropped = !samePanoramaCrop(savedCrop, FULL_PANORAMA_CROP);
  const fittedCrop = artifact ? panoramaPreviewCrop(artifact) : FULL_PANORAMA_CROP;
  const fitted = !samePanoramaCrop(fittedCrop, FULL_PANORAMA_CROP);
  const physicalUnconfirmed = job?.physical_state === "stop_unconfirmed";
  const ownershipLost = job?.physical_state === "ownership_lost";
  const processingOnly = job?.status === "processing" || ["queued_processing", "reconstructing", "stopping_processing", "interrupted_processing"].includes(job?.phase ?? "");
  const canStop = job && running && (processingOnly ? job.status !== "stopping" : (job.status !== "stopping" || physicalUnconfirmed) && !ownershipLost);
  const canReconstruct = job && !running && job.can_reconstruct === true && ["failed", "interrupted", "partial"].includes(job.status);
  const canStart = enabled && Boolean(data) && !running && !editingArtifact && !busy;
  const canReturn = job && !running && job.can_return === true && !ownershipLost;
  const canCleanup = job && !running && job.can_cleanup === true;
  const cleanupNotice = notice === "cleanup_completed" || notice === "cleanup_pending";
  const currentEditingArtifact = editingArtifact
    ? [data?.active, data?.active ? null : data?.candidate].find((item) => item?.id === editingArtifact.id) ?? null
    : null;
  const editingStale = Boolean(editingArtifact && (!currentEditingArtifact || currentEditingArtifact.crop_revision !== editingArtifact.crop_revision));
  const capturing = job?.status === "exploring" || job?.status === "capturing";
  const initialRegion = job?.capture_goal === "initial_region";
  const regionalViews = job?.coverage_progress?.policy_version === 4
    ? text("region_coverage_views", { count: number(job.coverage_progress.qualified_views ?? 0) })
    : [2, 3].includes(job?.coverage_progress?.policy_version ?? 0)
    ? text("region_views_adaptive", { count: number(job?.coverage_progress?.qualified_views ?? 0), rows: number(job?.coverage_progress?.region_rows_completed ?? 0) })
    : text("region_views", { count: number(job?.coverage_progress?.qualified_views ?? 0), total: number(job?.coverage_progress?.required_views ?? 6) });
  const regionReady = jobArtifact?.region_status === "ready" && !jobReview;
  const phaseKey = job?.operation === "verify_control" && job.status === "failed" ? "control_unconfirmed" : jobReview && ["ready", "partial"].includes(job?.status ?? "") ? "review" : regionReady && job?.status === "partial" ? "region_ready" : ["queued_processing", "stopping_processing", "interrupted_processing"].includes(job?.phase ?? "") ? job?.phase : capturing && job ? capturePhase(job) : job?.status;
  const continuingAroundPendingRegion = capturing && (job?.coverage_progress?.regions_pending ?? 0) > 0
    && job?.coverage_progress?.continued_after_recovery === true && ["pan", "step"].includes(job?.coverage_progress?.stage ?? "");
  const cameraFact = physicalUnconfirmed || ownershipLost ? "camera_stop_unconfirmed"
    : running && !processingOnly ? job?.status === "returning" ? "camera_returning" : "camera_capture_active"
      : job?.physical_state === "restored" ? "returned" : job?.physical_state === "stopped" ? "camera_stopped" : "camera_stop_unconfirmed";
  const completedArtifactBands = artifact && artifact.capture_goal !== "initial_region" ? confirmedArtifactBands(artifact) : null;
  const coverageFact = artifact?.capture_goal === "initial_region" ? "coverage_initial_region" : artifact?.coverage?.acquisition_complete === true ? "coverage_confirmed"
    : artifact?.coverage?.acquisition_complete === false ? "coverage_pending" : "coverage_unknown";
  const presentationFact = artifact?.presentation?.status === "verified" ? "presentation_verified"
    : artifact?.presentation?.status === "unverified" ? "presentation_unverified" : "presentation_unknown";
  const duration = job?.estimated_remaining_seconds;
  const minutes = duration != null && Number.isFinite(duration) && duration > 0 ? Math.max(1, Math.ceil(duration / 60)) : null;
  const requestError = actionError instanceof CameraPanoramaRequestError ? actionError : null;
  const actionErrorKey = requestError?.code === "panorama_revision_conflict" ? "crop_conflict" : requestError?.code === "source_changed" ? "source_changed" : "action_failed";
  const actionErrorMessage = actionErrorKey !== "action_failed" || !requestError ? text(actionErrorKey) : diagnostic(requestError.code);
  const issueCodes = job?.issue_codes?.filter((code) => typeof code === "string") ?? [];
  const resumeUnavailableCode = job && job.outcomes?.acquisition !== "completed" && !job.coverage_progress?.region_complete && job.operation !== "verify_control" && !running && !job.can_resume && ["failed", "interrupted", "partial"].includes(job.status)
    ? job.resume_unavailable_code : null;
  const previewUrl = job?.preview_url ? `${job.preview_url}${job.preview_url.includes("?") ? "&" : "?"}version=${encodeURIComponent(String(job.updated_at))}` : null;
  const showJob = Boolean(job && (
    running || physicalUnconfirmed || ownershipLost || job.error || jobReview || job.can_resume
  ));
  const updated = Boolean(job && !showJob && artifact?.id === job.artifact_id && ["ready", "partial"].includes(job.status));

  useEffect(() => { setShowWhole(false); }, [artifact?.id]);
  useEffect(() => {
    if (!restoreEditorFocus || editingArtifact) return;
    const frame = window.requestAnimationFrame(() => {
      editButton.current?.focus();
      setRestoreEditorFocus(false);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [editingArtifact, restoreEditorFocus]);

  function closeEditor() {
    setEditingArtifact(null);
    clearActionError();
    setRestoreEditorFocus(true);
  }

  function openEditor(next: CameraSourcePanoramaArtifact) {
    setEditingArtifact(structuredClone(next));
    setNotice("");
    clearActionError();
    window.requestAnimationFrame(() => editorContainer.current?.querySelector<HTMLButtonElement>("button")?.focus());
  }

  async function saveSelection(crop: CameraSourcePanoramaCrop): Promise<boolean> {
    if (!editingArtifact) return false;
    const result = await saveCrop(editingArtifact, crop);
    if (!result) return false;
    closeEditor();
    setShowWhole(false);
    setNotice("crop_saved");
    return true;
  }

  async function downloadCrop() {
    if (!artifact || exporting) return;
    setExporting(true);
    setExportError(false);
    try {
      const blob = await exportPanoramaCrop(artifact, savedCrop);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `${cameraName.replace(/[^\p{L}\p{N}_-]+/gu, "-") || "panorama"}-${text("download_crop_suffix")}.png`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
    } catch { setExportError(true); }
    finally { setExporting(false); }
  }

  async function cleanupTemporaryPositions() {
    if (!job) return;
    setNotice("");
    const result = await operate(job.id, "cleanup");
    if (!result) return;
    setNotice(result.can_cleanup ? "cleanup_pending" : "cleanup_completed");
    window.requestAnimationFrame(() => detailsSummary.current?.focus());
  }

  return <section className="card sourcePanorama" data-testid="camera-source-panorama" aria-label={text("title")}>
    <style>{cameraSourcePanoramaStyles}</style>
    <div className="cardBody">
      <div className="sourcePanoramaHeading"><div><h2>{text("title")}</h2><p>{cameraName} · {sourceName}</p></div>{artifact ? <span className="cardMeta">{creationDate(artifact.created_at, locale)}</span> : null}</div>
      {!enabled ? <p className="sourcePanoramaNotice">{text("source_unavailable")}</p> : null}
      {loading ? <p role="status">{text("loading")}</p> : null}
      {connectionError ? <div className="sourcePanoramaNotice" role="status"><p>{text(data ? running ? "connection_lost" : "source_refresh_failed" : "load_failed")}</p><button className="chipButton" type="button" onClick={refresh}>{text("retry")}</button></div> : null}
      {physicalUnconfirmed || ownershipLost ? <div className="sourcePanoramaError" role="alert"><p>{text(ownershipLost ? "ownership_lost" : "stop_unconfirmed")}</p>{physicalUnconfirmed && canStop && !processingOnly ? <button className="dangerButton" type="button" disabled={busy === "stop"} onClick={() => void operate(job!.id, "stop")}>{text("retry_stop")}</button> : null}</div> : null}
      {actionError ? <div className="sourcePanoramaError" role="alert"><p>{actionErrorMessage}</p>{requestError?.message ? <details><summary>{text("original_diagnostic")}</summary><p>{requestError.message}</p></details> : null}<button type="button" className="chipButton" onClick={refresh}>{text("refresh")}</button></div> : null}
      {showJob && job ? <div className="sourcePanoramaJob" data-state={job.status}>
        {job.operation !== "verify_control" ? <ol className="sourcePanoramaMilestones" aria-label={text("milestones")}>
          {["capture", "assemble", "result"].map((step, index) => {
            const current = processingOnly ? 1 : ["ready", "partial"].includes(job.status) ? 2 : 0;
            const complete = (job.status === "ready" || regionReady) && !jobReview && index < current;
            return <li key={step} aria-current={index === current ? "step" : undefined} data-complete={complete ? "true" : undefined}><span aria-hidden="true">{complete ? "✓" : index + 1}</span>{text(`milestone_${step}`)}</li>;
          })}
        </ol> : null}
        <div className="sourcePanoramaJobHeading"><strong role="status" aria-live="polite">{t(`ext.cameras.source_panorama.status_${phaseKey}`, {}, text("status_unknown"))}</strong>{canStop && (!physicalUnconfirmed || processingOnly) ? <button type="button" className="dangerButton" disabled={busy === "stop"} onClick={() => void operate(job.id, "stop")}>{text(processingOnly ? "stop_processing" : "stop")}</button> : null}</div>
        {job.operation === "verify_control" ? <p>{text("control_checks_passed", { count: number(job.control_checks_passed ?? 0) })}</p> : <p>{text(job.captures_accepted === 1 ? "accepted_image" : "accepted_images", { count: number(job.captures_accepted) })}{initialRegion ? ` · ${regionalViews}` : job.coverage_progress ? ` · ${text(job.coverage_progress.bands_completed === 1 ? "completed_band" : "completed_bands", { count: number(job.coverage_progress.bands_completed) })}` : ""}{running && !processingOnly && minutes ? ` · ${text(minutes === 1 ? "remaining_one" : "remaining", { count: number(minutes) })}` : ""}</p>}
        {job.outcomes ? <div data-testid="panorama-independent-outcomes">{(["acquisition", "reconstruction", "return"] as const).map((stage) => <p key={stage}><strong>{text(`outcome_${stage}`)}: </strong>{text(`outcome_${job.outcomes![stage]}`)}</p>)}</div> : null}
        {job.coverage_progress?.primary_complete === true ? <p className="sourcePanoramaSuccess" role="status">{text("reference_captured")}</p> : null}
        {continuingAroundPendingRegion ? <p className="sourcePanoramaNotice" role="status" data-testid="panorama-continuing-with-gaps">{text("continuing_with_gaps")}</p> : null}
        {running ? <>
          <p className="cardMeta">{text(processingOnly ? "processing_background" : "capture_background")}</p>
          {previewUrl ? <figure className="sourcePanoramaPreview"><img src={resolveToposyncUrl(previewUrl)} alt={text("preview_alt")} /><figcaption>{text("preview_label")}</figcaption></figure> : null}
        </> : null}
        {job.error && !physicalUnconfirmed ? <div className="sourcePanoramaNotice"><p>{text(failureHelp(job))}</p>{job.error.code !== resumeUnavailableCode ? <p>{diagnostic(job.error.code)}</p> : null}{job.error.message ? <details><summary>{text("original_diagnostic")}</summary><p>{job.error.message}</p></details> : null}</div> : null}
        {jobReview && ["ready", "partial"].includes(job.status) ? <p className="sourcePanoramaNotice" data-testid="panorama-job-quality-warning">{text(`quality_${jobReview}_job`)}</p> : job.status === "partial" ? <p className={regionReady ? "sourcePanoramaSuccess" : "sourcePanoramaNotice"}>{text(regionReady ? "region_ready_help" : "partial_help")}</p> : null}
        {!artifact ? <p className="cardMeta" data-testid="panorama-camera-state"><strong>{text("fact_camera")}: </strong><span>{text(cameraFact)}</span></p> : null}
        {resumeUnavailableCode ? <p className="sourcePanoramaNotice" role="status" data-testid="panorama-resume-unavailable">{diagnostic(resumeUnavailableCode)}</p> : null}
        <div className="sourcePanoramaActions">
          {canReconstruct ? <div className="sourcePanoramaReconstruct"><button className={job.status === "failed" ? "primaryButton" : "chipButton"} type="button" disabled={Boolean(busy || editingArtifact)} aria-describedby={`panorama-reconstruct-${job.id}`} onClick={() => void operate(job.id, "reconstruct")}>{text(busy === "reconstruct" ? "reconstruct_starting" : "reconstruct")}</button><p className="cardMeta" id={`panorama-reconstruct-${job.id}`}>{text("reconstruct_hint")}</p></div> : null}
          {!running && job.can_resume && !physicalUnconfirmed && !ownershipLost ? <button className={canReconstruct && job.status === "failed" ? "chipButton" : "primaryButton"} type="button" disabled={Boolean(busy || editingArtifact)} onClick={() => void operate(job.id, "resume")}>{text(job.status === "partial" ? "complete_capture" : "resume")}</button> : null}
          {canReturn ? <button className="chipButton" type="button" disabled={Boolean(busy)} onClick={() => void operate(job.id, "return")}>{text("return_camera")}</button> : null}
        </div>
      </div> : null}
      {updated ? <p className={artifact?.coverage?.acquisition_complete === false ? "sourcePanoramaNotice" : "sourcePanoramaSuccess"} role="status">{text(artifact?.coverage?.acquisition_complete === false ? "panorama_updated_partial" : "panorama_updated")}</p> : null}
      {!showJob && canReturn ? <div className="sourcePanoramaNotice"><p>{text(cameraFact)}</p><button className="chipButton" type="button" disabled={Boolean(busy)} onClick={() => void operate(job!.id, "return")}>{text("return_camera")}</button></div> : null}
      {notice && !cleanupNotice ? <p className="sourcePanoramaSuccess" role="status">{text(notice)}</p> : null}
      {editingArtifact ? <div ref={editorContainer}>
        <PanoramaCropEditor ui={ui} key={`${editingArtifact.id}:${editingArtifact.crop_revision}`} artifact={editingArtifact} busy={Boolean(busy)} saveDisabled={editingStale} contextLabel={`${cameraName} · ${sourceName}`} feedback={<>
          {editingReview ? <p className="sourcePanoramaNotice" role="status" data-testid="panorama-crop-quality-warning">{text(`quality_${editingReview}_warning`)}</p> : null}
          {actionError ? <div className="sourcePanoramaError" role="alert"><p>{actionErrorMessage}</p>{requestError?.message ? <details><summary>{text("original_diagnostic")}</summary><p>{requestError.message}</p></details> : null}</div> : null}
          {editingStale ? <div className="sourcePanoramaNotice" role="status"><p>{text("crop_changed")}</p><button type="button" className="chipButton" onClick={() => artifact ? openEditor(artifact) : closeEditor()}>{text("reload_selection")}</button></div> : null}
        </>} text={text} locale={locale} onSave={saveSelection} onClose={closeEditor} />
      </div> : artifact ? <>
        {artifact.stale ? <p className="sourcePanoramaNotice" role="status">{text(artifact.stale_reason === "source_unavailable" ? "source_missing" : "source_changed")}</p> : null}
        {artifact.presentation?.status === "unverified" ? <p className="sourcePanoramaNotice" role="status">{text("presentation_warning")}</p> : null}
        {imageReview ? <p className="sourcePanoramaNotice" role="status" data-testid="panorama-quality-warning">{text(`quality_${imageReview}_warning`)}</p> : !updated && artifact.coverage?.acquisition_complete === false ? <p className="sourcePanoramaNotice">{text(artifact.capture_goal === "initial_region" ? "region_scope" : "partial_artifact")}</p> : null}
        {fitted ? <div className="sourcePanoramaActions" role="group" aria-label={text("image_view")}><button type="button" className="chipButton" aria-pressed={!showWhole} onClick={() => setShowWhole(false)}>{text(cropped ? "useful_area" : "photographed_area")}</button><button type="button" className="chipButton" aria-pressed={showWhole} onClick={() => setShowWhole(true)}>{text("full_panorama")}</button></div> : null}
        <PanoramaImage ui={ui} text={text} key={artifact.id} artifact={artifact} crop={showWhole ? FULL_PANORAMA_CROP : fittedCrop} label={text(showWhole || !fitted ? "panorama_alt" : cropped ? "crop_alt" : "photographed_alt")} errorLabel={text("image_failed")} />
        <div className="sourcePanoramaActions"><button className="primaryButton" type="button" ref={editButton} disabled={Boolean(busy) || running} onClick={() => openEditor(artifact)}>{text(cropped ? "edit_crop" : "select_crop")}</button></div>
      </> : !running && !loading ? <div className="sourcePanoramaEmpty"><i className="fa-solid fa-panorama" aria-hidden="true" /><h3>{text("empty_title")}</h3><p>{text("empty_help")}</p></div> : null}
      {data?.active && !editingArtifact ? <section className="sourcePanoramaJob" aria-label={text("native_title")} data-testid="panorama-native-references">
        <h3>{text("native_title")}</h3>
        <p className="cardMeta">{text("native_help")}</p>
        {nativeConnectionError ? <p role="status">{text("native_connection_error")}</p> : null}
        <div className="sourcePanoramaActions">
          <button className="chipButton" type="button" disabled={!enabled || Boolean(busy) || running || nativeReferences === null || nativeConnectionError || nativeReferences.some((reference) => reference.status === "unverified" && !reference.cleanup_confirmed)} onClick={() => void operateReference(data.active!, "prepare")}>{text("native_prepare")}</button>
          {(activeReference || (actionBusy === "prepare_reference" && nativeAttemptId)) ? <button className="chipButton" type="button" disabled={actionBusy === "stop" || activeReference?.status === "stopping"} onClick={() => void operateReference(data.active!, "stop", activeReference?.id ?? nativeAttemptId!)}>{text("native_stop")}</button> : null}
        </div>
        {nativeReferences?.map((reference, index) => <div key={reference.id} className="sourcePanoramaActions">
          <span role="status">{text("native_point", { count: number(index + 1) })} · {text(reference.status === "unverified" && reference.cleanup_confirmed ? "native_not_saved" : `native_${reference.status}`)}</span>
          {!["queued", "preparing", "stopping"].includes(reference.status) ? <button className="chipButton" type="button" disabled={Boolean(busy) || running || nativeConnectionError} onClick={() => void operateReference(data.active!, "remove", reference.id)}>{text(reference.cleanup_confirmed ? "native_discard" : reference.status === "unverified" ? "native_cleanup" : "native_remove")}</button> : null}
        </div>)}
      </section> : null}
      {artifact || job?.telemetry || issueCodes.length || job?.issues?.length || canCleanup || busy === "cleanup" || cleanupNotice ? <details className="sourcePanoramaDetails" data-testid="panorama-diagnostics"><summary ref={detailsSummary}>{text("details")}</summary>
        {!showJob && job?.outcomes ? <div data-testid="panorama-independent-outcomes">{(["acquisition", "reconstruction", "return"] as const).map((stage) => <p key={stage}><strong>{text(`outcome_${stage}`)}: </strong>{text(`outcome_${job.outcomes![stage]}`)}</p>)}</div> : null}
        {!showJob && resumeUnavailableCode ? <p>{diagnostic(resumeUnavailableCode)}</p> : null}
        {!showJob && canReconstruct ? <div className="sourcePanoramaReconstruct"><button className="chipButton" type="button" disabled={Boolean(busy || editingArtifact)} onClick={() => void operate(job!.id, "reconstruct")}>{text(busy === "reconstruct" ? "reconstruct_starting" : "reconstruct")}</button><p className="cardMeta">{text("reconstruct_hint")}</p></div> : null}
        {artifact ? <dl className="sourcePanoramaFacts" aria-label={text("result_facts")} data-testid="panorama-result-facts">
          <div><dt>{text("fact_image")}</dt><dd data-testid="panorama-image-state">{text(imageReview ? "image_needs_review" : "image_available")}</dd></div>
          <div><dt>{text("fact_coverage")}</dt><dd data-testid="panorama-coverage-state">{completedArtifactBands != null ? `${text(completedArtifactBands === 1 ? "confirmed_band" : "confirmed_bands", { count: number(completedArtifactBands) })} · ` : ""}{text(coverageFact)}</dd></div>
          <div><dt>{text("fact_presentation")}</dt><dd data-testid="panorama-presentation-state">{text(presentationFact)}</dd></div>
          <div><dt>{text("fact_camera")}</dt><dd data-testid="panorama-camera-state">{text(cameraFact)}</dd></div>
        </dl> : null}
        {job?.telemetry ? <CameraPanoramaTelemetry telemetry={job.telemetry} text={text} locale={locale} /> : null}
        {artifact ? <><p>{text("complete_definition")}</p><p>{text("positioning_not_validated")}</p></> : null}
        {artifact ? <div className="sourcePanoramaActions"><a className="chipButton" href={resolveToposyncUrl(artifact.image_url)} download>{text("download_full")}</a>{cropped ? <button type="button" className="chipButton" disabled={exporting} onClick={() => void downloadCrop()}>{text(exporting ? "exporting" : "download_crop")}</button> : null}</div> : null}
        {exportError ? <p className="sourcePanoramaError" role="alert">{text("export_failed")}</p> : null}
        {issueCodes.length ? <ul>{issueCodes.map((code, index) => <li key={index}>{diagnostic(code)}</li>)}</ul> : job?.issues?.length ? <p>{text("diagnostic_unknown")}</p> : null}
        {job?.issues?.length ? <details><summary>{text("original_diagnostic")}</summary><ul>{job.issues.filter((issue) => typeof issue === "string").map((issue, index) => <li key={index}>{issue}</li>)}</ul></details> : null}
        {canCleanup || busy === "cleanup" ? <div className="sourcePanoramaActions"><button className="chipButton" type="button" disabled={Boolean(busy || editingArtifact)} aria-describedby={`panorama-cleanup-${job!.id}`} onClick={() => void cleanupTemporaryPositions()}>{text(busy === "cleanup" ? "cleanup_running" : "cleanup")}</button><p className="cardMeta" id={`panorama-cleanup-${job!.id}`}>{text("cleanup_hint")}</p></div> : null}
        {cleanupNotice ? <p className={notice === "cleanup_completed" ? "sourcePanoramaSuccess" : "sourcePanoramaNotice"} role="status">{text(notice)}</p> : null}
        {artifact ? <a className="chipButton" href={resolveToposyncUrl(`/api/cameras/panorama-artifacts/${encodeURIComponent(artifact.id)}`)} target="_blank" rel="noreferrer">{text("open_report")}</a> : null}
      </details> : null}
      {!running && !editingArtifact && busy !== "reconstruct" ? <div className="sourcePanoramaFooter">{!artifact || physicalUnconfirmed ? <p className="cardMeta">{text(physicalUnconfirmed ? "new_capture_after_uncertain_stop" : "movement_notice")}</p> : null}<button type="button" className={artifact ? "chipButton" : "primaryButton"} disabled={!canStart} onClick={() => { setNotice(""); void start(); }}>{text(busy === "start" ? "starting" : physicalUnconfirmed ? "generate_again" : artifact ? "update_panorama" : "generate")}</button></div> : null}
    </div>
  </section>;
}
