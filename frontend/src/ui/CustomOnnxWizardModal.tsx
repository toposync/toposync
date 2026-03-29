import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  ProcessingServerVisionCustomOnnxInspectResponse,
  ProcessingServerVisionCustomOnnxPreviewResponse,
  ProcessingServerVisionCustomOnnxRequest,
  ProcessingServerVisionManifestImportResponse,
} from "../util/api";
import {
  importProcessingServerCustomOnnx,
  inspectProcessingServerCustomOnnx,
  previewProcessingServerCustomOnnx,
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

function formatBytes(bytes: number): string {
  const size = Number(bytes || 0);
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  if (size < 1024) return `${Math.round(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

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

function formatShape(shape: Array<number | string | null> | undefined): string {
  const items = Array.isArray(shape) ? shape : [];
  return items.map((item) => (item == null || item === "" ? "?" : String(item))).join(" × ");
}

export function CustomOnnxWizardModal({
  open,
  serverId,
  task,
  onClose,
  onSaved,
}: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const onnxInputRef = useRef<HTMLInputElement | null>(null);
  const previewInputRef = useRef<HTMLInputElement | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [inspectLoading, setInspectLoading] = useState(false);
  const [inspectError, setInspectError] = useState<string | null>(null);
  const [inspectResult, setInspectResult] = useState<ProcessingServerVisionCustomOnnxInspectResponse | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
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

  const applySuggestion = useCallback(
    (result: ProcessingServerVisionCustomOnnxInspectResponse) => {
      const suggestion =
        result.task_suggestions.find((item) => item.task === task) ??
        result.task_suggestions[0] ??
        null;
      setDisplayName(result.suggested_display_name || "");
      setTensorName(String(suggestion?.defaults?.tensor_name || ""));
      setOutputName(String(suggestion?.defaults?.output_name || ""));
      setWidth(String(suggestion?.defaults?.width || 640));
      setHeight(String(suggestion?.defaults?.height || 640));
      setLayout(String(suggestion?.defaults?.layout || "nchw"));
      setColorOrder(String(suggestion?.defaults?.color_order || "rgb"));
      setResizeMode(String(suggestion?.defaults?.resize_mode || "stretch"));
      setRescaleFactor(String(suggestion?.defaults?.rescale_factor ?? 1));
      setNormalizationMean((suggestion?.defaults?.normalization_mean || [0, 0, 0]).join(", "));
      setNormalizationStd((suggestion?.defaults?.normalization_std || [1, 1, 1]).join(", "));
      setClassLabels("");
      setPreviewFile(null);
      setPreviewResult(null);
      setPreviewError(null);
      setSaveError(null);
    },
    [task],
  );

  useEffect(() => {
    if (!open) return;
    setDragActive(false);
    setInspectLoading(false);
    setInspectError(null);
    setInspectResult(null);
    setDisplayName("");
    setSourceUrl("");
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
  }, [open, task]);

  const suggestion = useMemo(
    () => inspectResult?.task_suggestions.find((item) => item.task === task) ?? null,
    [inspectResult, task],
  );

  const payload = useMemo<ProcessingServerVisionCustomOnnxRequest | null>(() => {
    if (!inspectResult) return null;
    const cleanName = String(displayName || "").trim();
    if (!cleanName) return null;
    return {
      artifact_path: inspectResult.artifact_path,
      uploaded_filename: inspectResult.uploaded_filename,
      display_name: cleanName,
      task,
      adapter_family: suggestion?.adapter_family || (task === "classification" ? "image_classification_logits" : "generic_boxes"),
      tensor_name: String(tensorName || "").trim(),
      output_name: String(outputName || "").trim(),
      width: Math.max(1, Number(width || 0) || 640),
      height: Math.max(1, Number(height || 0) || 640),
      layout: String(layout || "nchw").trim().toLowerCase() || "nchw",
      color_order: String(colorOrder || "rgb").trim().toLowerCase() || "rgb",
      resize_mode: String(resizeMode || "stretch").trim().toLowerCase() || "stretch",
      rescale_factor: Number(rescaleFactor || 1) || 1,
      normalization_mean: parseNumberList(normalizationMean),
      normalization_std: parseNumberList(normalizationStd),
      box_format: "xyxy01",
      class_labels: parseLabelList(classLabels),
      source_url: String(sourceUrl || "").trim(),
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
    sourceUrl,
    suggestion?.adapter_family,
    task,
    tensorName,
    width,
  ]);

  const handleInspectFile = useCallback(
    async (file: File | null) => {
      if (!file) return;
      if (!String(file.name || "").toLowerCase().endsWith(".onnx")) {
        setInspectError(
          t(
            "core.ui.vision.custom_onnx_modal.inspect_extension_error",
            {},
            "This wizard only accepts .onnx files.",
          ),
        );
        return;
      }
      setInspectLoading(true);
      setInspectError(null);
      setInspectResult(null);
      setPreviewFile(null);
      setPreviewResult(null);
      setPreviewError(null);
      setSaveError(null);
      try {
        const result = await inspectProcessingServerCustomOnnx(serverId, file);
        setInspectResult(result);
        applySuggestion(result);
      } catch (error: any) {
        setInspectError(String(error?.message ?? error));
      } finally {
        setInspectLoading(false);
      }
    },
    [applySuggestion, serverId, t],
  );

  const handlePreview = useCallback(async () => {
    if (!payload || !previewFile) return;
    setPreviewLoading(true);
    setPreviewError(null);
    setPreviewResult(null);
    try {
      const result = await previewProcessingServerCustomOnnx(serverId, payload, previewFile);
      setPreviewResult(result);
    } catch (error: any) {
      setPreviewError(String(error?.message ?? error));
    } finally {
      setPreviewLoading(false);
    }
  }, [payload, previewFile, serverId]);

  const handleSave = useCallback(async () => {
    if (!payload) return;
    setSaveLoading(true);
    setSaveError(null);
    try {
      const result = await importProcessingServerCustomOnnx(serverId, payload);
      await onSaved(result);
    } catch (error: any) {
      setSaveError(String(error?.message ?? error));
    } finally {
      setSaveLoading(false);
    }
  }, [onSaved, payload, serverId]);

  if (!open) return null;

  return (
    <Modal
      open={open}
      title={t(
        task === "classification"
          ? "core.ui.vision.custom_onnx_modal.title_classification"
          : "core.ui.vision.custom_onnx_modal.title_detection",
      )}
      onClose={() => {
        if (inspectLoading || previewLoading || saveLoading) return;
        onClose();
      }}
      panelClassName="customOnnxWizardModal"
    >
      <div className="pipelinesHint">
        {t(
          task === "classification"
            ? "core.ui.vision.custom_onnx_modal.intro_classification"
            : "core.ui.vision.custom_onnx_modal.intro_detection",
        )}
      </div>

      {!inspectResult ? (
        <div
          className={["pipelinesArtifactDropzone", dragActive ? "isActive" : ""].filter(Boolean).join(" ")}
          style={{ marginTop: 12 }}
          role="button"
          tabIndex={0}
          onClick={() => onnxInputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onnxInputRef.current?.click();
            }
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragEnter={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={(event) => {
            if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
            setDragActive(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setDragActive(false);
            const nextFile = event.dataTransfer.files?.[0] ?? null;
            void handleInspectFile(nextFile);
          }}
        >
          <input
            ref={onnxInputRef}
            type="file"
            accept=".onnx"
            hidden
            onChange={(event) => {
              const nextFile = event.target.files?.[0] ?? null;
              void handleInspectFile(nextFile);
              event.currentTarget.value = "";
            }}
          />
          <div className="pipelinesArtifactDropzoneTitle">
            {inspectLoading
              ? t("core.ui.vision.custom_onnx_modal.inspecting", {}, "Inspecting ONNX…")
              : t("core.ui.vision.custom_onnx_modal.dropzone_title", {}, "Drop an ONNX file here")}
          </div>
          <div className="pipelinesStepHint">
            {t(
              "core.ui.vision.custom_onnx_modal.dropzone_hint",
              {},
              "TopoSync inspects the graph locally on the selected processing server and suggests the right adapter.",
            )}
          </div>
        </div>
      ) : (
        <>
          <div className="card" style={{ marginTop: 12 }}>
            <div className="cardBody">
              <div className="cardHeaderRow">
                <div className="cardTitle">{inspectResult.suggested_display_name}</div>
                <div className="cardMeta">{formatBytes(inspectResult.file_size_bytes)}</div>
              </div>
              <div className="pipelinesStepHint">
                {t(
                  "core.ui.vision.custom_onnx_modal.inspect_summary",
                  {
                    inputs: inspectResult.input_tensors.length,
                    outputs: inspectResult.output_tensors.length,
                    task:
                      task === "classification"
                        ? t("core.ui.vision.custom_onnx_modal.task_classification")
                        : t("core.ui.vision.custom_onnx_modal.task_detection"),
                  },
                  `${inspectResult.input_tensors.length} inputs • ${inspectResult.output_tensors.length} outputs`,
                )}
              </div>
              {suggestion ? <div className="pipelinesStepHint">{suggestion.reason}</div> : null}
            </div>
          </div>

          <label className="pipelinesLabel" style={{ marginTop: 12 }}>
            <span>{t("core.ui.vision.custom_onnx_modal.display_name")}</span>
            <input className="pipelinesInput" type="text" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
          </label>

          <label className="pipelinesLabel">
            <span>{t("core.ui.vision.custom_onnx_modal.source_url")}</span>
            <input
              className="pipelinesInput"
              type="text"
              value={sourceUrl}
              placeholder="https://example.com/model"
              onChange={(event) => setSourceUrl(event.target.value)}
            />
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
          <div className="pipelinesStepHint">
            {t(
              "core.ui.vision.custom_onnx_modal.class_labels_hint",
              {},
              "Optional. Use commas or new lines. Leave empty when the parser can infer labels elsewhere.",
            )}
          </div>

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
                <input
                  className="pipelinesInput"
                  type="number"
                  step="0.0001"
                  value={rescaleFactor}
                  onChange={(event) => setRescaleFactor(event.target.value)}
                />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.mean")}</span>
                <input
                  className="pipelinesInput"
                  type="text"
                  value={normalizationMean}
                  onChange={(event) => setNormalizationMean(event.target.value)}
                />
              </label>
              <label className="pipelinesLabel">
                <span>{t("core.ui.vision.custom_onnx_modal.std")}</span>
                <input
                  className="pipelinesInput"
                  type="text"
                  value={normalizationStd}
                  onChange={(event) => setNormalizationStd(event.target.value)}
                />
              </label>
            </div>
            <div className="pipelinesStepHint">{t("core.ui.vision.custom_onnx_modal.advanced_hint")}</div>
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
              <div className="pipelinesStepHint">{t("core.ui.vision.custom_onnx_modal.preview_hint")}</div>
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
                  disabled={!previewFile || previewLoading || !payload}
                >
                  {previewLoading
                    ? t("core.ui.vision.custom_onnx_modal.preview_running")
                    : t("core.ui.vision.custom_onnx_modal.preview_run")}
                </button>
              </div>
              {previewFile ? (
                <div className="pipelinesStepHint">
                  {previewFile.name} • {formatBytes(previewFile.size)}
                </div>
              ) : null}
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
      )}

      {inspectError ? <div className="errorText">{inspectError}</div> : null}
      {saveError ? <div className="errorText">{saveError}</div> : null}

      <div className="modalFooter">
        <button
          className="pillButton"
          type="button"
          onClick={onClose}
          disabled={inspectLoading || previewLoading || saveLoading}
        >
          {t("core.actions.cancel")}
        </button>
        {inspectResult ? (
          <button
            className="pillButton pillButtonPrimary"
            type="button"
            onClick={() => void handleSave()}
            disabled={!payload || inspectLoading || previewLoading || saveLoading}
          >
            {saveLoading
              ? t("core.ui.vision.custom_onnx_modal.saving")
              : t("core.ui.vision.custom_onnx_modal.save_and_use")}
          </button>
        ) : null}
      </div>
    </Modal>
  );
}
