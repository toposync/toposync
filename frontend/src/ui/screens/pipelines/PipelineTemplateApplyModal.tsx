import React, { useEffect, useMemo, useState } from "react";
import Select, { type MultiValue } from "react-select";

import type {
  CameraSummary,
  Pipeline,
  PipelineTemplateApplyCamerasRequest,
  PipelineTemplateApplyCamerasResponse,
  ProcessingServer,
} from "../../../util/api";
import { Modal } from "../../Modal";
import { pipelinesReactSelectStyles } from "./constants";
import type { SelectOption } from "./types";

type Props = {
  open: boolean;
  template: Pipeline | null;
  cameras: CameraSummary[];
  servers: ProcessingServer[];
  onClose: () => void;
  onApply: (payload: PipelineTemplateApplyCamerasRequest) => Promise<PipelineTemplateApplyCamerasResponse>;
};

export function PipelineTemplateApplyModal({
  open,
  template,
  cameras,
  servers,
  onClose,
  onApply,
}: Props): React.ReactElement | null {
  const [enabled, setEnabled] = useState(false);
  const [processingServerId, setProcessingServerId] = useState("local");
  const [conflict, setConflict] = useState<"skip" | "replace" | "error">("skip");
  const [selectedCameras, setSelectedCameras] = useState<SelectOption[]>([]);
  const [applying, setApplying] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [result, setResult] = useState<PipelineTemplateApplyCamerasResponse | null>(null);

  const cameraOptions = useMemo<SelectOption[]>(() => {
    const options = (cameras ?? [])
      .map((camera) => {
        const id = String(camera.id || "").trim();
        const name = String(camera.name || "").trim();
        const label = name ? `${name} (${id})` : id;
        return { value: id, label };
      })
      .filter((opt) => opt.value.length > 0);
    options.sort((a, b) => a.label.localeCompare(b.label));
    return options;
  }, [cameras]);

  useEffect(() => {
    if (!open) return;
    setLocalError(null);
    setResult(null);
    setApplying(false);
    setEnabled(false);
    setConflict("skip");
    setSelectedCameras([]);
    setProcessingServerId(String(template?.processing_server_id ?? "local") || "local");
  }, [open, template?.processing_server_id]);

  const canApply = useMemo(() => {
    if (!template) return false;
    if (template.type !== "reuse") return false;
    if (selectedCameras.length === 0) return false;
    return true;
  }, [template, selectedCameras.length]);

  const applyNow = async () => {
    if (!template) return;
    setLocalError(null);
    setResult(null);
    if (!canApply) return;
    setApplying(true);
    try {
      const payload: PipelineTemplateApplyCamerasRequest = {
        template_pipeline_name: template.name,
        camera_ids: selectedCameras.map((opt) => opt.value),
        instance_type: "final",
        enabled,
        processing_server_id: processingServerId,
        conflict,
      };
      const response = await onApply(payload);
      setResult(response);
    } catch (err: any) {
      setLocalError(String(err?.message ?? err));
    } finally {
      setApplying(false);
    }
  };

  const title = useMemo(() => {
    if (!template) return "Apply template";
    return `Apply template: ${template.name}`;
  }, [template]);

  if (!open) return null;

  return (
    <Modal open={open} title={title} onClose={onClose}>
      {localError ? (
        <div className="card cardDanger">
          <div className="cardBody">{localError}</div>
        </div>
      ) : null}

      {!template ? <div className="pipelinesHint">Select a template pipeline first.</div> : null}

      {template && template.type !== "reuse" ? (
        <div className="pipelinesInlineError">Only reuse pipelines can be applied as templates.</div>
      ) : null}

      <div className="pipelinesOperatorConfigCard">
        <label className="pipelinesLabel">
          <span>Cameras</span>
          <Select<SelectOption, true>
            isMulti
            styles={pipelinesReactSelectStyles}
            options={cameraOptions}
            value={selectedCameras}
            isDisabled={!template || template.type !== "reuse"}
            placeholder={cameraOptions.length ? "Select cameras…" : "No cameras found…"}
            onChange={(value: MultiValue<SelectOption>) => setSelectedCameras(value as SelectOption[])}
          />
        </label>
        <div className="pipelinesStepHint">Creates one final pipeline per camera (same graph, only camera_id changes).</div>

        <label className="pipelinesLabel">
          <span>Processing server</span>
          <select
            className="pipelinesSelect"
            value={processingServerId}
            onChange={(event) => setProcessingServerId(String(event.target.value || "local"))}
            disabled={!template || template.type !== "reuse"}
          >
            {servers.map((server) => (
              <option key={server.id} value={server.id}>
                {server.id}
              </option>
            ))}
          </select>
        </label>

        <label className="pipelinesLabel">
          <span>Enable created pipelines</span>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            disabled={!template || template.type !== "reuse"}
          />
        </label>

        <label className="pipelinesLabel">
          <span>When name exists</span>
          <select
            className="pipelinesSelect"
            value={conflict}
            onChange={(event) => setConflict(event.target.value as any)}
            disabled={!template || template.type !== "reuse"}
          >
            <option value="skip">Skip</option>
            <option value="replace">Replace</option>
            <option value="error">Error</option>
          </select>
        </label>

        <button className="pillButton pillButtonPrimary" type="button" disabled={!canApply || applying} onClick={() => void applyNow()}>
          <i className="fa-solid fa-wand-magic-sparkles" aria-hidden="true" />
          {applying ? "Applying…" : "Apply"}
        </button>
      </div>

      {result ? (
        <div className="card">
          <div className="cardTitle">Result</div>
          <div className="cardBody">
            <div className="pipelinesHint">
              Created: {result.created?.length ?? 0} • Updated: {result.updated?.length ?? 0} • Skipped: {result.skipped?.length ?? 0}
            </div>
            <pre className="pipelinesPre">{JSON.stringify(result, null, 2)}</pre>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
