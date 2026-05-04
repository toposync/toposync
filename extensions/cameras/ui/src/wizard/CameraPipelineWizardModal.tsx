import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n } from "@toposync/plugin-api";

import { createCameraPipelineFromWizard, fetchCameraContexts } from "../api/camerasApi";
import type { CameraConfig, CameraContextsResponse, CameraPipelineWizardPreset } from "../types";
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
  const raw = String(value ?? "");
  if (!raw.trim()) return "";
  const cleaned = raw.replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+/, "");
  let out = cleaned || "fluxo";
  if (!/^[A-Za-z_]/.test(out)) out = `_${out}`;
  if (PYTHON_KEYWORDS.has(out)) out = `${out}_`;
  return out.slice(0, 120);
}

type WizardStep = "preset" | "configure" | "done";

function presetSuffix(preset: CameraPipelineWizardPreset): string {
  if (preset === "people") return "people";
  if (preset === "vehicles_stopped") return "vehicles_stopped";
  return "pets";
}

export function CameraPipelineWizardModal({
  open,
  camera,
  i18n,
  onClose,
}: {
  open: boolean;
  camera: CameraConfig;
  i18n: HostI18n;
  onClose: () => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [step, setStep] = useState<WizardStep>("preset");
  const [preset, setPreset] = useState<CameraPipelineWizardPreset | null>(null);

  const [pipelineName, setPipelineName] = useState("");
  const [enabled, setEnabled] = useState(true);

  const [contexts, setContexts] = useState<CameraContextsResponse | null>(null);
  const [contextsLoading, setContextsLoading] = useState(false);
  const [contextsError, setContextsError] = useState<string | null>(null);

  const [compositionId, setCompositionId] = useState("");
  const [areaId, setAreaId] = useState("");

  const [creating, setCreating] = useState(false);
  const [createdPipelineName, setCreatedPipelineName] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);

  const title = t("ext.cameras.wizard.title", {}, "Create camera pipeline");

  useEffect(() => {
    if (!open) return;
    setStep("preset");
    setPreset(null);
    setPipelineName("");
    setEnabled(Boolean(camera.rtsp_url.trim()));
    setContexts(null);
    setContextsLoading(false);
    setContextsError(null);
    setCompositionId("");
    setAreaId("");
    setCreating(false);
    setCreatedPipelineName(null);
    setCreateError(null);
  }, [camera.rtsp_url, open]);

  const vehiclesCompositions = useMemo(() => {
    const compositions = contexts?.compositions ?? [];
    return compositions
      .map((composition) => {
        const hasMapping = (composition.camera_elements ?? []).some((el) => Boolean(el.has_mapping));
        return { composition, hasMapping };
      })
      .filter((item) => item.hasMapping)
      .map((item) => item.composition);
  }, [contexts]);

  const selectedComposition = useMemo(() => {
    if (!compositionId) return null;
    return (vehiclesCompositions ?? []).find((c) => c.id === compositionId) ?? null;
  }, [compositionId, vehiclesCompositions]);

  useEffect(() => {
    if (preset !== "vehicles_stopped") return;
    if (!open) return;

    const controller = new AbortController();
    setContextsLoading(true);
    setContextsError(null);
    setContexts(null);

    void (async () => {
      try {
        const data = await fetchCameraContexts(camera.id, controller.signal);
        if (controller.signal.aborted) return;
        setContexts(data);

        const first = data.compositions
          .map((composition) => ({
            composition,
            hasMapping: (composition.camera_elements ?? []).some((el) => Boolean(el.has_mapping)),
          }))
          .filter((item) => item.hasMapping)
          .map((item) => item.composition)[0];
        if (first) setCompositionId(first.id);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setContextsError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!controller.signal.aborted) setContextsLoading(false);
      }
    })();

    return () => controller.abort();
  }, [camera.id, open, preset]);

  useEffect(() => {
    setAreaId("");
  }, [compositionId]);

  function selectPreset(next: CameraPipelineWizardPreset): void {
    setPreset(next);
    setPipelineName(safePipelineName(`camera_${camera.id}__${presetSuffix(next)}`));
    setStep("configure");
  }

  const defaultPipelineName = useMemo(() => {
    if (!preset) return "";
    return safePipelineName(`camera_${camera.id}__${presetSuffix(preset)}`);
  }, [camera.id, preset]);

  const cameraPlaceholder = "{{camera_name}}";
  const areaPlaceholder = "{{area_label}}";
  const categoryPlaceholder = "{{object_category_label}}";

  const notifyTitle = useMemo(() => {
    if (!preset) return "";
    if (preset === "people") return `${cameraPlaceholder}: ${t("ext.cameras.wizard.notify.people", {}, "Person detected")}`;
    if (preset === "pets") return `${cameraPlaceholder}: ${t("ext.cameras.wizard.notify.pets", {}, "Pet detected")}`;
    return `${cameraPlaceholder}: ${t("ext.cameras.wizard.notify.vehicles_stopped", {}, "Vehicle stopped")}`;
  }, [preset, t]);

  const notifyDescription = useMemo(() => {
    if (!preset) return "";
    if (preset !== "vehicles_stopped") return cameraPlaceholder;
    if (areaId) return `${categoryPlaceholder} — ${areaPlaceholder} — ${cameraPlaceholder}`;
    return `${categoryPlaceholder} — ${cameraPlaceholder}`;
  }, [areaId, preset]);

  const vehiclesBlocked = preset === "vehicles_stopped" && !contextsLoading && vehiclesCompositions.length === 0;

  async function createPipeline(): Promise<void> {
    if (!preset) return;
    setCreating(true);
    setCreateError(null);
    try {
      const response = await createCameraPipelineFromWizard(camera.id, {
        preset,
        pipeline_name: pipelineName.trim() && pipelineName.trim() !== defaultPipelineName ? pipelineName.trim() : "",
        enabled,
        composition_id: preset === "vehicles_stopped" ? compositionId : "",
        area_id: preset === "vehicles_stopped" ? areaId : "",
        notification_title: notifyTitle,
        notification_description: notifyDescription,
      });
      setCreatedPipelineName(response.pipeline_name);
      setStep("done");
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreating(false);
    }
  }

  return (
    <SubModal
      open={open}
      title={title}
      onClose={() => {
        if (creating) return;
        onClose();
      }}
      panelStyle={{ width: "min(900px, calc(100vw - 28px))" }}
    >
      {step === "preset" ? (
        <div>
          <div className="card" style={{ marginBottom: 10 }}>
            <div className="cardBody">
              {t("ext.cameras.wizard.subtitle", {}, "Choose a preset and Toposync will create a ready-to-edit pipeline.")}
            </div>
          </div>

          <div className="rowWrap" style={{ gap: 12, marginTop: 10 }}>
            <button className="card choiceItem" type="button" onClick={() => selectPreset("people")} style={{ flex: 1, minWidth: 240 }}>
              <div className="cardBody">
                <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                  {t("ext.cameras.wizard.preset.people.title", {}, "People")}
                </div>
                <div className="cardMeta">
                  {t("ext.cameras.wizard.preset.people.desc", {}, "Motion gate + person tracking + 5s throttle + segmentation + notification.")}
                </div>
              </div>
            </button>

            <button
              className="card choiceItem"
              type="button"
              onClick={() => selectPreset("vehicles_stopped")}
              style={{ flex: 1, minWidth: 240 }}
            >
              <div className="cardBody">
                <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                  {t("ext.cameras.wizard.preset.vehicles.title", {}, "Stopped vehicles")}
                </div>
                <div className="cardMeta">
                  {t(
                    "ext.cameras.wizard.preset.vehicles.desc",
                    {},
                    "Motion gate + vehicle tracking + mapping + stopped-speed detection + optional area restriction + notification.",
                  )}
                </div>
              </div>
            </button>

            <button className="card choiceItem" type="button" onClick={() => selectPreset("pets")} style={{ flex: 1, minWidth: 240 }}>
              <div className="cardBody">
                <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                  {t("ext.cameras.wizard.preset.pets.title", {}, "Pets")}
                </div>
                <div className="cardMeta">
                  {t("ext.cameras.wizard.preset.pets.desc", {}, "Motion gate + cat/dog tracking + throttle + segmentation + notification.")}
                </div>
              </div>
            </button>
          </div>

          <div className="rowWrap" style={{ marginTop: 14, justifyContent: "flex-end" }}>
            <button className="secondaryButton" type="button" onClick={onClose}>
              {t("core.actions.cancel")}
            </button>
          </div>
        </div>
      ) : null}

      {step === "configure" && preset ? (
        <div>
          {!camera.rtsp_url.trim() && !(camera.connection_type === "onvif" && camera.onvif?.xaddr?.trim()) ? (
            <div className="card" style={{ marginBottom: 10 }}>
              <div className="cardBody">
                {t(
                  "ext.cameras.wizard.missing_rtsp_url",
                  {},
                  "This camera has no RTSP URL configured yet. Create the pipeline disabled, then enable it after setting the URL.",
                )}
              </div>
            </div>
          ) : null}

          {createError ? (
            <div className="card" style={{ marginBottom: 10 }}>
              <div className="cardBody">{createError}</div>
            </div>
          ) : null}

          {preset === "vehicles_stopped" ? (
            <div className="card" style={{ marginBottom: 10 }}>
              <div className="cardBody">
                <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                  {t("ext.cameras.wizard.vehicles.mapping_title", {}, "Mapping & area")}
                </div>

                {contextsLoading ? (
                  <div className="cardMeta">{t("ext.cameras.wizard.loading_contexts", {}, "Loading camera contexts…")}</div>
                ) : contextsError ? (
                  <div className="cardMeta">{contextsError}</div>
                ) : vehiclesBlocked ? (
                  <div className="cardMeta">
                    {t(
                      "ext.cameras.wizard.vehicles.no_mapping",
                      {},
                      "No camera mapping found. Add at least 4 control points in a composition before using this preset.",
                    )}
                  </div>
                ) : (
                  <div className="rowWrap" style={{ gap: 10 }}>
                    <div className="field" style={{ flex: 1, minWidth: 260 }}>
                      <label className="label">{t("ext.cameras.wizard.vehicles.composition", {}, "Composition")}</label>
                      <select className="input" value={compositionId} onChange={(e) => setCompositionId(e.target.value)}>
                        {vehiclesCompositions.map((composition) => (
                          <option key={composition.id} value={composition.id}>
                            {composition.name || composition.id}
                          </option>
                        ))}
                      </select>
                      <div className="label">{t("ext.cameras.wizard.vehicles.composition_hint", {}, "Used for mapping and area restriction.")}</div>
                    </div>

                    <div className="field" style={{ flex: 1, minWidth: 260 }}>
                      <label className="label">{t("ext.cameras.wizard.vehicles.area", {}, "Area restriction (optional)")}</label>
                      <select className="input" value={areaId} onChange={(e) => setAreaId(e.target.value)} disabled={!selectedComposition}>
                        <option value="">{t("ext.cameras.wizard.vehicles.area_none", {}, "No restriction")}</option>
                        {(selectedComposition?.areas ?? []).map((area) => (
                          <option key={area.id} value={area.id}>
                            {area.name || area.id}
                          </option>
                        ))}
                      </select>
                      <div className="label">
                        {t("ext.cameras.wizard.vehicles.area_hint", {}, "Only notify when the vehicle stops inside the selected area.")}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          ) : null}

          <div className="card">
            <div className="cardBody">
              <div className="field">
                <label className="label">{t("ext.cameras.wizard.pipeline_name", {}, "Pipeline name")}</label>
                <input className="input" value={pipelineName} onChange={(e) => setPipelineName(safePipelineName(e.target.value))} />
                <div className="label">
                  {t("ext.cameras.wizard.pipeline_name_hint", {}, "Must be a valid Python identifier (letters, numbers, underscore).")}
                </div>
              </div>

              <div className="field">
                <label className="label">{t("ext.cameras.wizard.enabled", {}, "Enabled")}</label>
                <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                  <span className="cardMeta">{t("ext.cameras.wizard.enabled_hint", {}, "Start this pipeline automatically after creation.")}</span>
                </label>
              </div>

              <div className="field">
                <label className="label">{t("ext.cameras.wizard.summary", {}, "Summary")}</label>
                <div className="cardMeta" style={{ lineHeight: 1.5 }}>
                  {preset === "people" ? t("ext.cameras.wizard.summary.people", {}, "Detect people and notify (5s throttle, main image).") : null}
                  {preset === "pets" ? t("ext.cameras.wizard.summary.pets", {}, "Detect pets (cat/dog) and notify (main image).") : null}
                  {preset === "vehicles_stopped"
                    ? t("ext.cameras.wizard.summary.vehicles", {}, "Detect vehicles that stop (mapping required), optionally restrict by area, and notify (high priority).")
                    : null}
                </div>
              </div>
            </div>
          </div>

          <div className="rowWrap" style={{ marginTop: 14, justifyContent: "space-between" }}>
            <button
              className="secondaryButton"
              type="button"
              onClick={() => {
                if (creating) return;
                setStep("preset");
              }}
            >
              {t("core.actions.back", {}, "Back")}
            </button>

            <div className="rowWrap" style={{ gap: 10 }}>
              <button className="secondaryButton" type="button" onClick={onClose} disabled={creating}>
                {t("core.actions.cancel")}
              </button>
              <button className="primaryButton" type="button" onClick={() => void createPipeline()} disabled={creating || vehiclesBlocked}>
                {creating ? t("ext.cameras.wizard.creating", {}, "Creating…") : t("ext.cameras.wizard.create", {}, "Create pipeline")}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {step === "done" ? (
        <div>
          <div className="card">
            <div className="cardBody">
              <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
                {t("ext.cameras.wizard.done_title", {}, "Pipeline created")}
              </div>
              <div className="cardMeta">
                {t("ext.cameras.wizard.done_name", { name: createdPipelineName ?? "" }, "Name: {{name}}")}
              </div>
              <div className="cardMeta" style={{ marginTop: 8 }}>
                {t("ext.cameras.wizard.done_hint", {}, "You can edit it later in the Pipelines screen.")}
              </div>
            </div>
          </div>

          <div className="rowWrap" style={{ marginTop: 14, justifyContent: "flex-end" }}>
            <button className="primaryButton" type="button" onClick={onClose}>
              {t("core.actions.close", {}, "Close")}
            </button>
          </div>
        </div>
      ) : null}
    </SubModal>
  );
}
