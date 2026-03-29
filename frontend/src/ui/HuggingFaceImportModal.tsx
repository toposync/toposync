import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  ProcessingServerVisionCustomOnnxPreviewResponse,
  ProcessingServerVisionCustomOnnxRequest,
  ProcessingServerVisionHuggingFaceImportRequest,
  ProcessingServerVisionHuggingFaceInspectResponse,
  ProcessingServerVisionHuggingFaceProbeResponse,
  ProcessingServerVisionManifestImportResponse,
} from "../util/api";
import {
  importProcessingServerVisionHuggingFace,
  inspectProcessingServerVisionHuggingFace,
  previewProcessingServerCustomOnnx,
  probeProcessingServerVisionHuggingFace,
} from "../util/api";
import { i18n } from "../util/i18n";
import { Modal } from "./Modal";

type Props = {
  open: boolean;
  serverId: string;
  task: "classification" | "detection";
  onClose: () => void;
  onSaved: (result: ProcessingServerVisionManifestImportResponse) => void | Promise<void>;
};

function parseNumberList(value: string): number[] {
  return String(value || "")
    .split(/[\n,;]+/g)
    .map((item) => Number(item.trim()))
    .filter((item) => Number.isFinite(item));
}

function parseLabelList(value: string): string[] {
  return String(value || "")
    .split(/[\n,;]+/g)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatBytes(bytes: number): string {
  const size = Number(bytes || 0);
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  if (size < 1024) return `${Math.round(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatShape(shape: Array<number | string | null> | undefined): string {
  const items = Array.isArray(shape) ? shape : [];
  return items.map((item) => (item == null || item === "" ? "?" : String(item))).join(" × ");
}

function downloadReasonLabel(reason: string, t: (key: string, vars?: Record<string, any>, fallback?: string) => string): string {
  if (reason === "onnx_missing") return t("core.ui.vision.huggingface_modal.reason.onnx_missing");
  if (reason === "task_unsupported") return t("core.ui.vision.huggingface_modal.reason.task_unsupported");
  if (reason === "task_unknown") return t("core.ui.vision.huggingface_modal.reason.task_unknown");
  if (reason === "onnx_ready") return t("core.ui.vision.huggingface_modal.reason.onnx_ready");
  return reason || t("core.ui.vision.huggingface_modal.reason.unknown");
}

export function HuggingFaceImportModal({
  open,
  serverId,
  task,
  onClose,
  onSaved,
}: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const previewInputRef = useRef<HTMLInputElement | null>(null);
  const [repo, setRepo] = useState("");
  const [revision, setRevision] = useState("");
  const [probeLoading, setProbeLoading] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<ProcessingServerVisionHuggingFaceProbeResponse | null>(null);
  const [selectedOnnxFile, setSelectedOnnxFile] = useState("");
  const [inspectLoading, setInspectLoading] = useState(false);
  const [inspectError, setInspectError] = useState<string | null>(null);
  const [inspectResult, setInspectResult] = useState<ProcessingServerVisionHuggingFaceInspectResponse | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [tensorName, setTensorName] = useState("");
  const [outputName, setOutputName] = useState("");
  const [width, setWidth] = useState("640");
  const [height, setHeight] = useState("640");
  const [layout, setLayout] = useState("nchw");
  const [colorOrder, setColorOrder] = useState("rgb");
  const [resizeMode, setResizeMode] = useState("stretch");
  const [rescaleFactor, setRescaleFactor] = useState("1");
  const [normalizationMean, setNormalizationMean] = useState("0, 0, 0");
  const [normalizationStd, setNormalizationStd] = useState("1, 1, 1");
  const [classLabels, setClassLabels] = useState("");
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [previewFile, setPreviewFile] = useState<File | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewResult, setPreviewResult] = useState<ProcessingServerVisionCustomOnnxPreviewResponse | null>(null);
  const [saveLoading, setSaveLoading] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const applyInspectDefaults = useCallback((result: ProcessingServerVisionHuggingFaceInspectResponse) => {
    const suggestion = result.task_suggestions.find((item) => item.task === task) ?? result.task_suggestions[0] ?? null;
    const preprocess = (result.preprocess_defaults || {}) as Record<string, unknown>;
    setDisplayName(result.suggested_display_name || result.repo_id || "");
    setTensorName(String(suggestion?.defaults?.tensor_name || ""));
    setOutputName(String(suggestion?.defaults?.output_name || ""));
    setWidth(String((preprocess.width as number) || suggestion?.defaults?.width || 640));
    setHeight(String((preprocess.height as number) || suggestion?.defaults?.height || 640));
    setLayout(String(suggestion?.defaults?.layout || "nchw"));
    setColorOrder(String((preprocess.color_order as string) || suggestion?.defaults?.color_order || "rgb"));
    setResizeMode(String((preprocess.resize_mode as string) || suggestion?.defaults?.resize_mode || "stretch"));
    setRescaleFactor(String((preprocess.rescale_factor as number) ?? suggestion?.defaults?.rescale_factor ?? 1));
    setNormalizationMean(
      ((preprocess.normalization_mean as number[]) || suggestion?.defaults?.normalization_mean || [0, 0, 0]).join(", "),
    );
    setNormalizationStd(
      ((preprocess.normalization_std as number[]) || suggestion?.defaults?.normalization_std || [1, 1, 1]).join(", "),
    );
    setClassLabels((result.labels || []).join(", "));
  }, [task]);

  useEffect(() => {
    if (!open) return;
    setRepo("");
    setRevision("");
    setProbeLoading(false);
    setProbeError(null);
    setProbeResult(null);
    setSelectedOnnxFile("");
    setInspectLoading(false);
    setInspectError(null);
    setInspectResult(null);
    setDisplayName("");
    setTensorName("");
    setOutputName("");
    setWidth("640");
    setHeight("640");
    setLayout("nchw");
    setColorOrder("rgb");
    setResizeMode("stretch");
    setRescaleFactor("1");
    setNormalizationMean("0, 0, 0");
    setNormalizationStd("1, 1, 1");
    setClassLabels("");
    setReplaceExisting(false);
    setPreviewFile(null);
    setPreviewLoading(false);
    setPreviewError(null);
    setPreviewResult(null);
    setSaveLoading(false);
    setSaveError(null);
  }, [open]);

  const taskMismatch = !!probeResult?.detected_task && probeResult.detected_task !== task;

  const customPreviewPayload = useMemo<ProcessingServerVisionCustomOnnxRequest | null>(() => {
    if (!inspectResult) return null;
    return {
      artifact_path: inspectResult.artifact_path,
      uploaded_filename: inspectResult.uploaded_filename,
      display_name: displayName,
      task,
      adapter_family:
        inspectResult.task_suggestions.find((item) => item.task === task)?.adapter_family ||
        (task === "classification" ? "image_classification_logits" : "generic_boxes"),
      tensor_name: tensorName,
      output_name: outputName,
      width: Math.max(1, Number(width || 0) || 640),
      height: Math.max(1, Number(height || 0) || 640),
      layout,
      color_order: colorOrder,
      resize_mode: resizeMode,
      rescale_factor: Number(rescaleFactor || 1) || 1,
      normalization_mean: parseNumberList(normalizationMean),
      normalization_std: parseNumberList(normalizationStd),
      box_format: "xyxy01",
      class_labels: parseLabelList(classLabels),
      source_url: inspectResult.source_url,
      replace_existing: replaceExisting,
    };
  }, [
    classLabels,
    colorOrder,
    displayName,
    height,
    inspectResult,
    layout,
    normalizationMean,
    normalizationStd,
    outputName,
    replaceExisting,
    rescaleFactor,
    resizeMode,
    task,
    tensorName,
    width,
  ]);

  const importPayload = useMemo<ProcessingServerVisionHuggingFaceImportRequest | null>(() => {
    if (!inspectResult || !probeResult) return null;
    return {
      artifact_path: inspectResult.artifact_path,
      repo_id: inspectResult.repo_id,
      resolved_revision: inspectResult.resolved_revision,
      onnx_filename: selectedOnnxFile,
      uploaded_filename: inspectResult.uploaded_filename,
      display_name: displayName,
      task,
      adapter_family:
        inspectResult.task_suggestions.find((item) => item.task === task)?.adapter_family ||
        (task === "classification" ? "image_classification_logits" : "generic_boxes"),
      tensor_name: tensorName,
      output_name: outputName,
      width: Math.max(1, Number(width || 0) || 640),
      height: Math.max(1, Number(height || 0) || 640),
      layout,
      color_order: colorOrder,
      resize_mode: resizeMode,
      rescale_factor: Number(rescaleFactor || 1) || 1,
      normalization_mean: parseNumberList(normalizationMean),
      normalization_std: parseNumberList(normalizationStd),
      box_format: "xyxy01",
      class_labels: parseLabelList(classLabels),
      replace_existing: replaceExisting,
    };
  }, [
    classLabels,
    colorOrder,
    displayName,
    height,
    inspectResult,
    layout,
    normalizationMean,
    normalizationStd,
    outputName,
    probeResult,
    replaceExisting,
    rescaleFactor,
    resizeMode,
    selectedOnnxFile,
    task,
    tensorName,
    width,
  ]);

  const handleProbe = useCallback(async () => {
    const cleanRepo = String(repo || "").trim();
    if (!cleanRepo) return;
    setProbeLoading(true);
    setProbeError(null);
    setProbeResult(null);
    setSelectedOnnxFile("");
    setInspectError(null);
    setInspectResult(null);
    setSaveError(null);
    try {
      const result = await probeProcessingServerVisionHuggingFace(serverId, {
        repo: cleanRepo,
        revision: revision.trim(),
      });
      setProbeResult(result);
      setSelectedOnnxFile(result.onnx_candidates[0]?.path || "");
    } catch (error: any) {
      setProbeError(String(error?.message ?? error));
    } finally {
      setProbeLoading(false);
    }
  }, [repo, revision, serverId]);

  const handleInspect = useCallback(async () => {
    if (!probeResult || !selectedOnnxFile) return;
    setInspectLoading(true);
    setInspectError(null);
    setInspectResult(null);
    setPreviewFile(null);
    setPreviewResult(null);
    setPreviewError(null);
    setSaveError(null);
    try {
      const result = await inspectProcessingServerVisionHuggingFace(serverId, {
        repo_id: probeResult.repo_id,
        revision: probeResult.resolved_revision,
        onnx_filename: selectedOnnxFile,
        task,
      });
      setInspectResult(result);
      applyInspectDefaults(result);
    } catch (error: any) {
      setInspectError(String(error?.message ?? error));
    } finally {
      setInspectLoading(false);
    }
  }, [applyInspectDefaults, probeResult, selectedOnnxFile, serverId, task]);

  const handlePreview = useCallback(async () => {
    if (!customPreviewPayload || !previewFile) return;
    setPreviewLoading(true);
    setPreviewError(null);
    setPreviewResult(null);
    try {
      const result = await previewProcessingServerCustomOnnx(serverId, customPreviewPayload, previewFile);
      setPreviewResult(result);
    } catch (error: any) {
      setPreviewError(String(error?.message ?? error));
    } finally {
      setPreviewLoading(false);
    }
  }, [customPreviewPayload, previewFile, serverId]);

  const handleSave = useCallback(async () => {
    if (!importPayload) return;
    setSaveLoading(true);
    setSaveError(null);
    try {
      const result = await importProcessingServerVisionHuggingFace(serverId, importPayload);
      await onSaved(result);
    } catch (error: any) {
      setSaveError(String(error?.message ?? error));
    } finally {
      setSaveLoading(false);
    }
  }, [importPayload, onSaved, serverId]);

  if (!open) return null;

  return (
    <Modal
      open={open}
      title={t(
        task === "classification"
          ? "core.ui.vision.huggingface_modal.title_classification"
          : "core.ui.vision.huggingface_modal.title_detection",
      )}
      onClose={() => {
        if (probeLoading || inspectLoading || previewLoading || saveLoading) return;
        onClose();
      }}
      panelClassName="customOnnxWizardModal"
    >
      <div className="pipelinesHint">
        {t(
          task === "classification"
            ? "core.ui.vision.huggingface_modal.intro_classification"
            : "core.ui.vision.huggingface_modal.intro_detection",
        )}
      </div>

      <div className="customOnnxWizardGrid" style={{ marginTop: 12 }}>
        <label className="pipelinesLabel">
          <span>{t("core.ui.vision.huggingface_modal.repo")}</span>
          <input
            className="pipelinesInput"
            type="text"
            value={repo}
            placeholder="Falconsai/nsfw_image_detection"
            onChange={(event) => setRepo(event.target.value)}
          />
        </label>
        <label className="pipelinesLabel">
          <span>{t("core.ui.vision.huggingface_modal.revision")}</span>
          <input
            className="pipelinesInput"
            type="text"
            value={revision}
            placeholder="main"
            onChange={(event) => setRevision(event.target.value)}
          />
        </label>
      </div>

      <div className="pipelinesProvisionActions">
        <button className="pillButton pillButtonPrimary" type="button" onClick={() => void handleProbe()} disabled={probeLoading || !repo.trim()}>
          {probeLoading ? t("core.ui.vision.huggingface_modal.probing") : t("core.ui.vision.huggingface_modal.probe")}
        </button>
      </div>

      {probeError ? <div className="errorText">{probeError}</div> : null}

      {probeResult ? (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="cardBody">
            <div className="cardHeaderRow">
              <div className="cardTitle">{probeResult.repo_id}</div>
              <div className="cardMeta">{probeResult.resolved_revision || probeResult.requested_revision || "HEAD"}</div>
            </div>
            <div className="pipelinesStepHint">
              {t("core.ui.vision.huggingface_modal.summary", {
                pipelineTag: probeResult.pipeline_tag || "unknown",
                license: probeResult.declared_license || "n/a",
              })}
            </div>
            <div className="pipelinesStepHint">
              {t("core.ui.vision.huggingface_modal.download_reason", {
                reason: downloadReasonLabel(probeResult.download_reason, t),
              })}
            </div>
            {taskMismatch ? (
              <div className="errorText">
                {t("core.ui.vision.huggingface_modal.task_mismatch", {
                  repoTask: probeResult.detected_task,
                  currentTask:
                    task === "classification"
                      ? t("core.ui.vision.custom_onnx_modal.task_classification")
                      : t("core.ui.vision.custom_onnx_modal.task_detection"),
                })}
              </div>
            ) : null}
            {probeResult.onnx_candidates.length > 0 ? (
              <label className="pipelinesLabel" style={{ marginTop: 12 }}>
                <span>{t("core.ui.vision.huggingface_modal.onnx_file")}</span>
                <select className="pipelinesInput" value={selectedOnnxFile} onChange={(event) => setSelectedOnnxFile(event.target.value)}>
                  {probeResult.onnx_candidates.map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.label} {item.size_bytes > 0 ? `• ${formatBytes(item.size_bytes)}` : ""}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {probeResult.labels.length > 0 ? (
              <div className="pipelinesStepHint">
                {t("core.ui.vision.huggingface_modal.labels_found", { count: probeResult.labels.length })}
              </div>
            ) : null}
            {probeResult.source_url ? (
              <div className="pipelinesProvisionLinks">
                <a className="pillButton" href={probeResult.source_url} target="_blank" rel="noreferrer">
                  {t("core.ui.vision.huggingface_modal.open_repo")}
                </a>
              </div>
            ) : null}
            {probeResult.download_supported && !taskMismatch && selectedOnnxFile ? (
              <div className="pipelinesProvisionActions">
                <button className="pillButton" type="button" onClick={() => void handleInspect()} disabled={inspectLoading}>
                  {inspectLoading
                    ? t("core.ui.vision.huggingface_modal.inspecting")
                    : t("core.ui.vision.huggingface_modal.inspect_selected")}
                </button>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {inspectError ? <div className="errorText">{inspectError}</div> : null}

      {inspectResult ? (
        <>
          <label className="pipelinesLabel" style={{ marginTop: 12 }}>
            <span>{t("core.ui.vision.custom_onnx_modal.display_name")}</span>
            <input className="pipelinesInput" type="text" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.vision.custom_onnx_modal.class_labels")}</span>
            <textarea
              className="pipelinesTextArea"
              rows={3}
              value={classLabels}
              placeholder={task === "classification" ? "normal, nsfw" : "person, car, truck"}
              onChange={(event) => setClassLabels(event.target.value)}
            />
          </label>

          <details className="customOnnxWizardDetails">
            <summary>{t("core.ui.vision.custom_onnx_modal.advanced_mapping")}</summary>
            <div className="customOnnxWizardGrid">
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.input_tensor")}</span>
                <input className="pipelinesInput" type="text" value={tensorName} onChange={(event) => setTensorName(event.target.value)} />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.output_tensor")}</span>
                <input className="pipelinesInput" type="text" value={outputName} onChange={(event) => setOutputName(event.target.value)} />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.width")}</span>
                <input className="pipelinesInput" type="number" min={1} value={width} onChange={(event) => setWidth(event.target.value)} />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.height")}</span>
                <input className="pipelinesInput" type="number" min={1} value={height} onChange={(event) => setHeight(event.target.value)} />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.layout")}</span>
                <select className="pipelinesInput" value={layout} onChange={(event) => setLayout(event.target.value)}>
                  <option value="nchw">NCHW</option>
                  <option value="nhwc">NHWC</option>
                </select>
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.color_order")}</span>
                <select className="pipelinesInput" value={colorOrder} onChange={(event) => setColorOrder(event.target.value)}>
                  <option value="rgb">RGB</option>
                  <option value="bgr">BGR</option>
                </select>
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.resize_mode")}</span>
                <select className="pipelinesInput" value={resizeMode} onChange={(event) => setResizeMode(event.target.value)}>
                  <option value="stretch">stretch</option>
                  <option value="letterbox">letterbox</option>
                </select>
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.rescale_factor")}</span>
                <input className="pipelinesInput" type="number" step="0.0001" value={rescaleFactor} onChange={(event) => setRescaleFactor(event.target.value)} />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.mean")}</span>
                <input className="pipelinesInput" type="text" value={normalizationMean} onChange={(event) => setNormalizationMean(event.target.value)} />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.std")}</span>
                <input className="pipelinesInput" type="text" value={normalizationStd} onChange={(event) => setNormalizationStd(event.target.value)} />
              </label>
            </div>
            <div className="customOnnxWizardTensorList">
              <div>
                <div className="cardMeta">{t("core.ui.vision.custom_onnx_modal.inputs_detected")}</div>
                {inspectResult.input_tensors.map((tensor) => (
                  <div key={`in-${tensor.name}`} className="pipelinesStepHint">
                    <strong>{tensor.name || "input"}</strong> • {tensor.dtype} • {formatShape(tensor.shape)}
                  </div>
                ))}
              </div>
              <div>
                <div className="cardMeta">{t("core.ui.vision.custom_onnx_modal.outputs_detected")}</div>
                {inspectResult.output_tensors.map((tensor) => (
                  <div key={`out-${tensor.name}`} className="pipelinesStepHint">
                    <strong>{tensor.name || "output"}</strong> • {tensor.dtype} • {formatShape(tensor.shape)}
                  </div>
                ))}
              </div>
            </div>
            <label className="pipelinesCheckboxLabel">
              <input type="checkbox" checked={replaceExisting} onChange={(event) => setReplaceExisting(event.target.checked)} />
              <span>{t("core.ui.vision.custom_onnx_modal.replace_existing")}</span>
            </label>
          </details>

          <div className="card" style={{ marginTop: 12 }}>
            <div className="cardBody">
              <div className="cardTitle">{t("core.ui.vision.custom_onnx_modal.preview_title")}</div>
              <div className="pipelinesProvisionActions">
                <input
                  ref={previewInputRef}
                  type="file"
                  accept="image/*"
                  hidden
                  onChange={(event) => {
                    const nextFile = event.target.files?.[0] ?? null;
                    setPreviewFile(nextFile);
                    setPreviewResult(null);
                    setPreviewError(null);
                    event.currentTarget.value = "";
                  }}
                />
                <button className="pillButton" type="button" onClick={() => previewInputRef.current?.click()}>
                  {previewFile
                    ? t("core.ui.vision.custom_onnx_modal.preview_change_image")
                    : t("core.ui.vision.custom_onnx_modal.preview_choose_image")}
                </button>
                <button
                  className="pillButton pillButtonPrimary"
                  type="button"
                  onClick={() => void handlePreview()}
                  disabled={!previewFile || previewLoading || !customPreviewPayload}
                >
                  {previewLoading
                    ? t("core.ui.vision.custom_onnx_modal.preview_running")
                    : t("core.ui.vision.custom_onnx_modal.preview_run")}
                </button>
              </div>
              {previewFile ? <div className="pipelinesStepHint">{previewFile.name}</div> : null}
              {previewError ? <div className="errorText">{previewError}</div> : null}
              {previewResult?.task === "classification" ? (
                <div className="customOnnxWizardPreviewList">
                  <div className="pipelinesStepHint">
                    {t("core.ui.vision.custom_onnx_modal.preview_top_label", {
                      label: String(previewResult.summary.top_label || "n/a"),
                      score: Number(previewResult.summary.top_score || 0).toFixed(3),
                    })}
                  </div>
                  {Array.isArray(previewResult.summary.labels)
                    ? previewResult.summary.labels.map((item: any, index: number) => (
                        <div key={`${item?.label || index}`} className="pipelinesStepHint">
                          {String(item?.label || `label_${index}`)} • {Number(item?.score || 0).toFixed(3)}
                        </div>
                      ))
                    : null}
                </div>
              ) : null}
              {previewResult?.task === "detection" ? (
                <div className="customOnnxWizardPreviewList">
                  <div className="pipelinesStepHint">
                    {t("core.ui.vision.custom_onnx_modal.preview_detection_count", {
                      count: Number(previewResult.summary.count || 0),
                    })}
                  </div>
                  {Array.isArray(previewResult.summary.detections)
                    ? previewResult.summary.detections.map((item: any, index: number) => (
                        <div key={`${item?.label || index}-${index}`} className="pipelinesStepHint">
                          {String(item?.label || `detection_${index}`)} • {Number(item?.score || 0).toFixed(3)}
                        </div>
                      ))
                    : null}
                </div>
              ) : null}
            </div>
          </div>
        </>
      ) : null}

      {saveError ? <div className="errorText">{saveError}</div> : null}

      <div className="modalFooter">
        <button className="pillButton" type="button" onClick={onClose} disabled={probeLoading || inspectLoading || previewLoading || saveLoading}>
          {t("core.actions.cancel")}
        </button>
        {inspectResult ? (
          <button
            className="pillButton pillButtonPrimary"
            type="button"
            onClick={() => void handleSave()}
            disabled={!importPayload || saveLoading || probeLoading || inspectLoading || previewLoading}
          >
            {saveLoading
              ? t("core.ui.vision.huggingface_modal.importing")
              : t("core.ui.vision.huggingface_modal.save_and_use")}
          </button>
        ) : null}
      </div>
    </Modal>
  );
}
