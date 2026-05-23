import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n } from "@toposync/plugin-api";

import { createCameraPipelinePreset } from "../api/camerasApi";
import type {
  CameraConfig,
  CameraContextComposition,
  CameraPipelinePreset,
  CameraPipelinesResponse,
  CameraSourceConfig,
  ProcessingServer,
} from "../types";
import { SubModal } from "../ui/SubModal";

const PYTHON_KEYWORDS = new Set([
  "False",
  "None",
  "True",
  "and",
  "as",
  "assert",
  "async",
  "await",
  "break",
  "class",
  "continue",
  "def",
  "del",
  "elif",
  "else",
  "except",
  "finally",
  "for",
  "from",
  "global",
  "if",
  "import",
  "in",
  "is",
  "lambda",
  "nonlocal",
  "not",
  "or",
  "pass",
  "raise",
  "return",
  "try",
  "while",
  "with",
  "yield",
]);

function safePipelineName(value: string): string {
  const cleaned = String(value || "")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
  let out = cleaned || "pipeline";
  if (!/^[A-Za-z_]/.test(out)) out = `_${out}`;
  if (PYTHON_KEYWORDS.has(out)) out = `${out}_`;
  return out.slice(0, 120);
}

function presetSuffix(preset: CameraPipelinePreset): string {
  return preset === "people_mapping" ? "people_mapping" : "people_detection";
}

function defaultPipelineName(cameraId: string, sourceId: string, preset: CameraPipelinePreset): string {
  return safePipelineName(`camera_${cameraId}__${sourceId || "source"}__${presetSuffix(preset)}`);
}

function sourceHasVideoOrigin(camera: CameraConfig, source: CameraSourceConfig | null): boolean {
  if (!source) return false;
  if (String(source.origin.rtsp_url || "").trim()) return true;
  return source.origin.type === "onvif_profile" && Boolean(String(camera.onvif?.xaddr || "").trim());
}

function normalizeServerId(value: string | null | undefined): string {
  return String(value || "local").trim().toLowerCase() || "local";
}

function serverLabel(server: ProcessingServer): string {
  const id = normalizeServerId(server.id);
  const name = String(server.name || "").trim();
  return name ? `${name} (${id})` : id;
}

export function CameraPipelinePresetModal({
  open,
  preset,
  camera,
  activeSourceId,
  pipelineOverview,
  mappedCompositions,
  processingServers,
  i18n,
  onClose,
  onCreated,
}: {
  open: boolean;
  preset: CameraPipelinePreset | null;
  camera: CameraConfig;
  activeSourceId: string;
  pipelineOverview: CameraPipelinesResponse | null;
  mappedCompositions: CameraContextComposition[];
  processingServers: ProcessingServer[];
  i18n: HostI18n;
  onClose: () => void;
  onCreated: (pipelineName: string) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [sourceId, setSourceId] = useState("");
  const [pipelineName, setPipelineName] = useState("");
  const [suggestedName, setSuggestedName] = useState("");
  const [compositionId, setCompositionId] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [processingServerId, setProcessingServerId] = useState("local");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const videoSources = useMemo(
    () => camera.sources.filter((source) => source.kind === "video" && source.enabled),
    [camera.sources],
  );
  const selectedSource = videoSources.find((source) => source.id === sourceId) ?? videoSources[0] ?? null;

  useEffect(() => {
    if (!open || !preset) return;
    const nextSource =
      videoSources.find((source) => source.id === activeSourceId) ??
      videoSources.find((source) => source.is_default) ??
      videoSources[0] ??
      null;
    const nextSourceId = nextSource?.id ?? "";
    const nextSuggested =
      pipelineOverview?.suggested_pipeline_names?.[preset] ??
      defaultPipelineName(camera.id, nextSourceId, preset);
    setSourceId(nextSourceId);
    setSuggestedName(nextSuggested);
    setPipelineName(nextSuggested);
    setCompositionId(mappedCompositions[0]?.id ?? "");
    setEnabled(sourceHasVideoOrigin(camera, nextSource));
    setProcessingServerId("local");
    setCreating(false);
    setError(null);
  }, [activeSourceId, camera, mappedCompositions, open, pipelineOverview, preset, videoSources]);

  function updateSource(nextSourceId: string): void {
    const nextSource = videoSources.find((source) => source.id === nextSourceId) ?? null;
    const nextSuggested = preset ? defaultPipelineName(camera.id, nextSourceId, preset) : "";
    const previousSuggested = suggestedName;
    setSourceId(nextSourceId);
    setEnabled(sourceHasVideoOrigin(camera, nextSource));
    setSuggestedName(nextSuggested);
    if (!pipelineName.trim() || pipelineName === previousSuggested) setPipelineName(nextSuggested);
  }

  async function submit(): Promise<void> {
    if (!preset || creating) return;
    setCreating(true);
    setError(null);
    try {
      const response = await createCameraPipelinePreset(camera.id, {
        preset,
        source_id: sourceId,
        pipeline_name: pipelineName.trim() && pipelineName.trim() !== suggestedName ? pipelineName.trim() : "",
        enabled,
        processing_server_id: processingServerId,
        composition_id: preset === "people_mapping" ? compositionId : "",
      });
      onCreated(response.pipeline_name);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  if (!open || !preset) return null;

  const isMappingPreset = preset === "people_mapping";
  const title = isMappingPreset
    ? t("ext.cameras.pipeline_preset.people_mapping.title", {}, "People detection and mapping")
    : t("ext.cameras.pipeline_preset.people_detection.title", {}, "Simple people detection");
  const noSource = videoSources.length === 0;
  const noMapping = isMappingPreset && mappedCompositions.length === 0;

  return (
    <SubModal open={open} title={title} onClose={() => (creating ? undefined : onClose())} panelStyle={{ width: "min(760px, calc(100vw - 28px))" }}>
      <div className="settingsPanel">
        {error ? <div className="errorText">{error}</div> : null}
        {noSource ? <div className="settingsStatusMuted">{t("ext.cameras.pipelines.no_video_source", {}, "Add an active video source before creating a pipeline.")}</div> : null}
        {noMapping ? <div className="settingsStatusMuted">{t("ext.cameras.pipelines.mapping_required", {}, "Map this camera in a composition before using this preset.")}</div> : null}

        <div className="rowWrap">
          <div className="field">
            <label className="label">{t("ext.cameras.pipeline_preset.source", {}, "Camera source")}</label>
            <select className="input" value={sourceId} onChange={(event) => updateSource(event.target.value)} disabled={noSource || creating}>
              {videoSources.map((source) => (
                <option key={source.id} value={source.id}>
                  {source.name || source.id}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="label">{t("ext.cameras.pipeline_preset.processing_server", {}, "Processing server")}</label>
            <select className="input" value={processingServerId} onChange={(event) => setProcessingServerId(event.target.value)} disabled={creating}>
              <option value="local">{t("ext.cameras.settings.ingest.host.local", {}, "Main environment")}</option>
              {processingServers
                .filter((server) => normalizeServerId(server.id) !== "local")
                .map((server) => {
                  const id = normalizeServerId(server.id);
                  return (
                    <option key={id} value={id}>
                      {serverLabel(server)}
                    </option>
                  );
                })}
            </select>
          </div>
        </div>

        {isMappingPreset ? (
          <div className="field">
            <label className="label">{t("ext.cameras.pipeline_preset.composition", {}, "Mapped composition")}</label>
            <select className="input" value={compositionId} onChange={(event) => setCompositionId(event.target.value)} disabled={noMapping || creating}>
              {mappedCompositions.map((composition) => (
                <option key={composition.id} value={composition.id}>
                  {composition.name || composition.id}
                </option>
              ))}
            </select>
          </div>
        ) : null}

        <div className="field">
          <label className="label">{t("ext.cameras.pipeline_preset.pipeline_name", {}, "Pipeline name")}</label>
          <input className="input" value={pipelineName} onChange={(event) => setPipelineName(safePipelineName(event.target.value))} disabled={creating} />
        </div>

        <label className="chipButton" style={{ justifyContent: "flex-start" }}>
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={creating || !selectedSource} />
          {t("ext.cameras.pipeline_preset.enabled", {}, "Enable after creation")}
        </label>

        <div className="settingsStatusMuted">
          {isMappingPreset
            ? t(
                "ext.cameras.pipeline_preset.people_mapping.summary",
                {},
                "Uses the camera, motion, person detection, tracking, mapping, speed calculation, 10s throttling, object crops, storage and notifications.",
              )
            : t(
                "ext.cameras.pipeline_preset.people_detection.summary",
                {},
                "Uses the camera, motion, person detection, tracking, 10s throttling, object crops, storage and notifications.",
              )}
        </div>

        <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
          <button className="chipButton" type="button" onClick={onClose} disabled={creating}>
            {t("core.actions.cancel", {}, "Cancel")}
          </button>
          <button className="primaryButton" type="button" onClick={() => void submit()} disabled={creating || noSource || noMapping}>
            {creating ? t("ext.cameras.pipeline_preset.creating", {}, "Creating...") : t("ext.cameras.pipeline_preset.create", {}, "Create pipeline")}
          </button>
        </div>
      </div>
    </SubModal>
  );
}
