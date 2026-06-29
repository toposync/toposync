import React, { useEffect, useMemo, useState } from "react";

import type { HostApi, HostI18n, SettingsPanel } from "@toposync/plugin-api";

import {
  createCinematicPipeline,
  fetchCamerasIndex,
  fetchCinematicDiagnostics,
  fetchCinematicStatus,
  fetchTransmissions
} from "../api";
import type {
  CameraIndexItem,
  CameraMode,
  CinematicDiagnosticsResponse,
  CinematicStatusItem,
  CinematicStatusResponse,
  CinematicWizardCreatePipelineResponse,
  Priority,
  ResizeMode,
  SourceRole,
  Transmission
} from "../types";
import { SubModal } from "./SubModal";

const CINEMATIC_EXTENSION_ID = "com.toposync.cinematic";
const PRIORITIES: Priority[] = ["high", "medium", "low"];
const CAMERA_MODES: CameraMode[] = ["all", "include", "exclude"];

type TranslateFn = ReturnType<HostI18n["useI18n"]>["t"];

export function createCinematicSettingsPanel(): SettingsPanel {
  return {
    id: CINEMATIC_EXTENSION_ID,
    icon: "film",
    name: { key: "ext.cinematic.settings.name", fallback: "Cinematic" },
    description: { key: "ext.cinematic.settings.desc" },
    render: ({ i18n, api }) => <CinematicSettingsPanelContent i18n={i18n} api={api} />
  };
}

function CinematicSettingsPanelContent({ i18n, api }: { i18n: HostI18n; api: HostApi }): React.ReactElement {
  const { t } = i18n.useI18n();
  const [status, setStatus] = useState<CinematicStatusResponse | null>(null);
  const [diagnostics, setDiagnostics] = useState<CinematicDiagnosticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function load(): Promise<void> {
      try {
        const [nextStatus, nextDiagnostics] = await Promise.all([
          fetchCinematicStatus(api, controller.signal),
          fetchCinematicDiagnostics(api, controller.signal)
        ]);
        if (cancelled) return;
        setStatus(nextStatus);
        setDiagnostics(nextDiagnostics);
        setError(null);
      } catch (err) {
        if (cancelled || (err instanceof DOMException && err.name === "AbortError")) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    const intervalId = window.setInterval(() => void load(), 2500);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(intervalId);
    };
  }, [api, refreshNonce]);

  const items = Array.isArray(status?.items) ? status.items : [];
  const sortedItems = useMemo(() => [...items].sort(compareStatusItems), [items]);
  const issues = Array.isArray(diagnostics?.issues) ? diagnostics.issues : [];

  return (
    <div className="settingsPanel">
      <div className="card">
        <div className="cardBody">
          <div className="rowWrap" style={{ alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
            <div>
              <div className="cardTitle">{t("ext.cinematic.settings.title", {}, "Diretor cinemático")}</div>
              <div className="cardMeta" style={{ marginTop: 4 }}>
                {t(
                  "ext.cinematic.settings.subtitle",
                  {},
                  "Uma saída de vídeo sob demanda que corta entre câmeras a partir das notificações."
                )}
              </div>
            </div>
            <div className="rowWrap" style={{ gap: 8 }}>
              <button className="chipButton" type="button" onClick={() => setRefreshNonce((value) => value + 1)}>
                <i className="fa-solid fa-rotate" aria-hidden="true" />
                <span>{t("ext.cinematic.settings.refresh", {}, "Atualizar")}</span>
              </button>
              <button className="chipButton primary" type="button" onClick={() => setWizardOpen(true)}>
                <i className="fa-solid fa-plus" aria-hidden="true" />
                <span>{t("ext.cinematic.settings.create", {}, "Criar pipeline")}</span>
              </button>
            </div>
          </div>
          {error ? <div className="errorText" style={{ marginTop: 10 }}>{error}</div> : null}
          {loading ? <div className="settingsStatusMuted" style={{ marginTop: 10 }}>{t("ext.cinematic.common.loading", {}, "Carregando...")}</div> : null}
        </div>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <div className="cardBody">
          <div className="modalSectionTitle">{t("ext.cinematic.status.title", {}, "Status")}</div>
          {sortedItems.length > 0 ? (
            <div style={{ display: "grid", gap: 12 }}>
              {sortedItems.map((item) => (
                <div key={item.key} style={{ display: "grid", gap: 8 }}>
                  <div className="cardMeta" style={{ wordBreak: "break-word" }}>{statusItemLabel(item)}</div>
                  <StatusPreview item={item} t={t} />
                </div>
              ))}
            </div>
          ) : (
            <div className="cardMeta">{t("ext.cinematic.status.empty", {}, "Nenhum diretor cinemático ativo ainda.")}</div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <div className="cardBody">
          <div className="modalSectionTitle">{t("ext.cinematic.diagnostics.title", {}, "Diagnósticos")}</div>
          {issues.length === 0 ? (
            <div className="cardMeta">{t("ext.cinematic.diagnostics.ok", {}, "Sem bloqueios.")}</div>
          ) : (
            <div style={{ display: "grid", gap: 8 }}>
              {issues.map((issue) => (
                <div key={issue.code} className="settingsInlinePanel">
                  <div className="rowWrap" style={{ gap: 8, justifyContent: "space-between" }}>
                    <div className="cardTitle">{issue.code}</div>
                    <span className="cardMeta">{issue.severity}</span>
                  </div>
                  <div className="cardMeta" style={{ marginTop: 4 }}>{issue.message}</div>
                </div>
              ))}
            </div>
          )}
          {diagnostics?.counts ? (
            <div className="cardMeta" style={{ marginTop: 10 }}>
              {Object.entries(diagnostics.counts).map(([key, value]) => `${key}: ${value}`).join(" | ")}
            </div>
          ) : null}
        </div>
      </div>

      <CinematicWizard
        open={wizardOpen}
        i18n={i18n}
        api={api}
        onClose={() => setWizardOpen(false)}
        onCreated={() => setRefreshNonce((value) => value + 1)}
      />
    </div>
  );
}

function compareStatusItems(left: CinematicStatusItem, right: CinematicStatusItem): number {
  const leftScore = statusItemScore(left);
  const rightScore = statusItemScore(right);
  if (leftScore !== rightScore) return rightScore - leftScore;
  return Number(right.updated_at || 0) - Number(left.updated_at || 0);
}

function statusItemScore(item: CinematicStatusItem): number {
  return (item.demand_active ? 8 : 0) + (item.stream_open ? 4 : 0) + (item.frame_ts ? 2 : 0);
}

function statusItemLabel(item: CinematicStatusItem): string {
  const raw = String(item.node_id || item.pipeline_name || item.key || "").trim();
  return raw.replace(/^isolated_/, "").replace(/__director$/, "") || "-";
}

function StatusPreview({ item, t }: { item: CinematicStatusItem; t: TranslateFn }): React.ReactElement {
  const camera = String(item.active_camera_id || "").trim() || "-";
  const source = String(item.active_source_id || "").trim();
  const frameLabel = item.frame_width && item.frame_height
    ? `${item.frame_width}x${item.frame_height}${typeof item.frame_age_seconds === "number" ? ` | ${item.frame_age_seconds.toFixed(1)}s` : ""}`
    : "-";
  return (
    <div style={{ display: "grid", gap: 10 }}>
      <div className="settingsInlinePanel">
        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 8 }}>
          <div>
            <div className="cardTitle">{t("ext.cinematic.status.cut_reason", {}, "Motivo do corte")}</div>
            <div className="cardMeta" style={{ marginTop: 4 }}>{item.cut_reason || "-"}</div>
          </div>
          <span className="cardMeta">{item.demand_active ? t("ext.cinematic.status.demand_on", {}, "Demanda ativa") : t("ext.cinematic.status.demand_off", {}, "Sem demanda")}</span>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
        <Metric title={t("ext.cinematic.status.camera", {}, "Câmera")} value={source ? `${camera} / ${source}` : camera} />
        <Metric title={t("ext.cinematic.status.mode", {}, "Modo")} value={item.mode || "-"} />
        <Metric title={t("ext.cinematic.status.frame", {}, "Frame")} value={frameLabel} />
        <Metric title={t("ext.cinematic.status.event", {}, "Evento")} value={item.active_event_key || "-"} />
      </div>
      {item.last_error ? <div className="errorText">{item.last_error}</div> : null}
    </div>
  );
}

function Metric({ title, value }: { title: string; value: string }): React.ReactElement {
  return (
    <div className="settingsInlinePanel">
      <div className="label">{title}</div>
      <div style={{ marginTop: 4, wordBreak: "break-word" }}>{value}</div>
    </div>
  );
}

function CinematicWizard({
  open,
  i18n,
  api,
  onClose,
  onCreated
}: {
  open: boolean;
  i18n: HostI18n;
  api: HostApi;
  onClose: () => void;
  onCreated: (payload: CinematicWizardCreatePipelineResponse) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [transmissions, setTransmissions] = useState<Transmission[]>([]);
  const [cameras, setCameras] = useState<CameraIndexItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [created, setCreated] = useState<CinematicWizardCreatePipelineResponse | null>(null);

  const [transmissionId, setTransmissionId] = useState("");
  const [pipelineName, setPipelineName] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [cameraMode, setCameraMode] = useState<CameraMode>("all");
  const [cameraIds, setCameraIds] = useState<string[]>([]);
  const [priorities, setPriorities] = useState<Priority[]>([...PRIORITIES]);
  const [fps, setFps] = useState("8");
  const [width, setWidth] = useState("1280");
  const [height, setHeight] = useState("720");
  const [resizeMode, setResizeMode] = useState<ResizeMode>("contain");
  const [writerPriority, setWriterPriority] = useState("0");
  const [sourceRole, setSourceRole] = useState<SourceRole>("auto");
  const [idleDwellSeconds, setIdleDwellSeconds] = useState("8");
  const [eventMinSeconds, setEventMinSeconds] = useState("10");
  const [cutCooldownSeconds, setCutCooldownSeconds] = useState("1.5");
  const [closeHoldSeconds, setCloseHoldSeconds] = useState("3");
  const [currentStickySeconds, setCurrentStickySeconds] = useState("4");
  const [pipelineMap, setPipelineMap] = useState("");

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    setCreateError(null);
    setCreated(null);
    setTransmissionId("");
    setPipelineName("");
    setEnabled(true);
    setCameraMode("all");
    setCameraIds([]);
    setPriorities([...PRIORITIES]);

    void (async () => {
      try {
        const [nextTransmissions, nextCameras] = await Promise.all([
          fetchTransmissions(api, controller.signal),
          fetchCamerasIndex(api, controller.signal)
        ]);
        if (controller.signal.aborted) return;
        setTransmissions(nextTransmissions);
        setCameras(nextCameras);
        setTransmissionId(String(nextTransmissions[0]?.id || ""));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setLoadError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();

    return () => controller.abort();
  }, [api, open]);

  const selectedTransmission = useMemo(
    () => transmissions.find((item) => String(item.id || "") === transmissionId) ?? null,
    [transmissionId, transmissions]
  );
  const canPickCameras = cameraMode !== "all";

  async function submit(): Promise<void> {
    if (!transmissionId.trim()) {
      setCreateError(t("ext.cinematic.wizard.select_transmission", {}, "Selecione uma transmissão."));
      return;
    }
    if (cameraMode !== "all" && cameraIds.length === 0) {
      setCreateError(t("ext.cinematic.wizard.select_camera", {}, "Selecione pelo menos uma câmera."));
      return;
    }

    setCreateBusy(true);
    setCreateError(null);
    try {
      const priority_filter = priorities.length === PRIORITIES.length ? [] : priorities;
      const response = await createCinematicPipeline(api, {
        transmission_id: transmissionId.trim(),
        optional_parameters: {
          pipeline_name: pipelineName.trim() || undefined,
          enabled,
          processing_server_id: selectedTransmission?.host_server_id || "local",
          cameras_mode: cameraMode,
          camera_ids: cameraMode === "all" ? [] : cameraIds,
          priority_filter,
          pipeline_camera_map: parsePipelineMap(pipelineMap),
          preferred_source_role: sourceRole,
          idle_dwell_seconds: toNumber(idleDwellSeconds, 8),
          event_min_seconds: toNumber(eventMinSeconds, 10),
          cut_cooldown_seconds: toNumber(cutCooldownSeconds, 1.5),
          close_hold_seconds: toNumber(closeHoldSeconds, 3),
          current_camera_sticky_seconds: toNumber(currentStickySeconds, 4),
          fps: toNumber(fps, 8),
          width: toInteger(width, 1280),
          height: toInteger(height, 720),
          resize_mode: resizeMode,
          writer_priority: toInteger(writerPriority, 0)
        }
      });
      setCreated(response);
      onCreated(response);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreateBusy(false);
    }
  }

  return (
    <SubModal
      open={open}
      title={t("ext.cinematic.wizard.title", {}, "Criar pipeline cinemático")}
      closeAriaLabel={t("ext.cinematic.common.close", {}, "Fechar")}
      onClose={() => {
        if (!createBusy) onClose();
      }}
    >
      {created ? (
        <div style={{ display: "grid", gap: 12 }}>
          <div className="settingsInlinePanel">
            <div className="cardTitle">{t("ext.cinematic.wizard.created", {}, "Pipeline criado.")}</div>
            <div className="cardMeta" style={{ marginTop: 4 }}>{created.pipeline_name}</div>
          </div>
          {created.warnings?.map((warning) => <div key={warning} className="cardMeta">{warning}</div>)}
          <div className="rowWrap" style={{ justifyContent: "flex-end", gap: 8 }}>
            <button className="chipButton" type="button" onClick={onClose}>
              {t("ext.cinematic.common.close", {}, "Fechar")}
            </button>
            <button className="chipButton primary" type="button" onClick={() => openPipelineScreen(created.pipeline_name)}>
              <i className="fa-solid fa-diagram-project" aria-hidden="true" />
              <span>{t("ext.cinematic.wizard.open_pipeline", {}, "Abrir pipeline")}</span>
            </button>
          </div>
        </div>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {loading ? <div className="settingsStatusMuted">{t("ext.cinematic.common.loading", {}, "Carregando...")}</div> : null}
          {loadError ? <div className="errorText">{loadError}</div> : null}
          {createError ? <div className="errorText">{createError}</div> : null}
          {!loading && transmissions.length === 0 ? <div className="cardMeta">{t("ext.cinematic.wizard.no_transmissions", {}, "Nenhuma transmissão configurada em Streaming.")}</div> : null}
          {!loading && cameras.length === 0 ? <div className="cardMeta">{t("ext.cinematic.wizard.no_cameras", {}, "Nenhuma câmera configurada.")}</div> : null}

          <div className="field">
            <label className="label">{t("ext.cinematic.wizard.transmission", {}, "Transmissão")}</label>
            <select className="input" value={transmissionId} onChange={(event) => setTransmissionId(event.target.value)} disabled={loading || transmissions.length === 0}>
              {transmissions.map((item) => (
                <option key={item.id} value={item.id}>{item.name || item.path || item.id}</option>
              ))}
            </select>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: 10, alignItems: "end" }}>
            <div className="field">
              <label className="label">{t("ext.cinematic.wizard.pipeline_name", {}, "Nome do pipeline")}</label>
              <input
                className="input"
                value={pipelineName}
                onChange={(event) => setPipelineName(event.target.value)}
                placeholder={t("ext.cinematic.wizard.pipeline_name_placeholder", {}, "Opcional")}
              />
            </div>
            <label className="chipButton" style={{ alignSelf: "end" }}>
              <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
              <span>{t("ext.cinematic.wizard.enabled", {}, "Habilitado")}</span>
            </label>
          </div>

          <div className="field">
            <label className="label">{t("ext.cinematic.wizard.camera_mode", {}, "Câmeras")}</label>
            <div className="rowWrap" style={{ gap: 8 }}>
              {CAMERA_MODES.map((mode) => (
                <button
                  key={mode}
                  className={`chipButton${cameraMode === mode ? " isActive" : ""}`}
                  type="button"
                  aria-pressed={cameraMode === mode}
                  onClick={() => setCameraMode(mode)}
                >
                  {t(`ext.cinematic.wizard.camera_mode.${mode}`, {}, mode)}
                </button>
              ))}
            </div>
          </div>

          {canPickCameras ? (
            <div style={{ display: "grid", gap: 6, maxHeight: 170, overflow: "auto" }}>
              {cameras.map((camera) => {
                const selected = cameraIds.includes(camera.id);
                return (
                  <label key={camera.id} className="chipButton" style={{ justifyContent: "flex-start" }}>
                    <input
                      type="checkbox"
                      checked={selected}
                      onChange={(event) => {
                        setCameraIds((current) =>
                          event.target.checked ? [...current, camera.id] : current.filter((item) => item !== camera.id)
                        );
                      }}
                    />
                    <span>{camera.name || camera.id}</span>
                    <span className="cardMeta">{camera.id}</span>
                  </label>
                );
              })}
            </div>
          ) : null}

          <div className="field">
            <label className="label">{t("ext.cinematic.wizard.priorities", {}, "Prioridades de notificação")}</label>
            <div className="rowWrap" style={{ gap: 8 }}>
              {PRIORITIES.map((priority) => (
                <label key={priority} className="chipButton">
                  <input
                    type="checkbox"
                    checked={priorities.includes(priority)}
                    onChange={(event) => {
                      setPriorities((current) =>
                        event.target.checked ? [...current, priority] : current.filter((item) => item !== priority)
                      );
                    }}
                  />
                  <span>{priority}</span>
                </label>
              ))}
            </div>
          </div>

          <details>
            <summary className="cardTitle">{t("ext.cinematic.wizard.advanced", {}, "Configuração avançada")}</summary>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10, marginTop: 10 }}>
              <TextField label={t("ext.cinematic.wizard.fps", {}, "FPS")} value={fps} onChange={setFps} />
              <TextField label={t("ext.cinematic.wizard.width", {}, "Largura")} value={width} onChange={setWidth} />
              <TextField label={t("ext.cinematic.wizard.height", {}, "Altura")} value={height} onChange={setHeight} />
              <TextField label={t("ext.cinematic.wizard.writer_priority", {}, "Prioridade de escrita")} value={writerPriority} onChange={setWriterPriority} />
              <TextField label="Idle dwell" value={idleDwellSeconds} onChange={setIdleDwellSeconds} />
              <TextField label="Event min" value={eventMinSeconds} onChange={setEventMinSeconds} />
              <TextField label="Cut cooldown" value={cutCooldownSeconds} onChange={setCutCooldownSeconds} />
              <TextField label="Close hold" value={closeHoldSeconds} onChange={setCloseHoldSeconds} />
              <TextField label="Sticky current" value={currentStickySeconds} onChange={setCurrentStickySeconds} />
              <div className="field">
                <label className="label">{t("ext.cinematic.wizard.resize", {}, "Redimensionar")}</label>
                <select className="input" value={resizeMode} onChange={(event) => setResizeMode(event.target.value as ResizeMode)}>
                  <option value="contain">contain</option>
                  <option value="none">none</option>
                </select>
              </div>
              <div className="field">
                <label className="label">{t("ext.cinematic.wizard.source_role", {}, "Papel da fonte")}</label>
                <select className="input" value={sourceRole} onChange={(event) => setSourceRole(event.target.value as SourceRole)}>
                  <option value="auto">auto</option>
                  <option value="main">main</option>
                  <option value="sub">sub</option>
                  <option value="zoom">zoom</option>
                </select>
              </div>
            </div>
            <div className="field" style={{ marginTop: 10 }}>
              <label className="label">{t("ext.cinematic.wizard.pipeline_map", {}, "Mapa pipeline para câmera")}</label>
              <textarea
                className="input"
                style={{ minHeight: 74, resize: "vertical" }}
                value={pipelineMap}
                onChange={(event) => setPipelineMap(event.target.value)}
                placeholder={t("ext.cinematic.wizard.pipeline_map_placeholder", {}, "nome_do_pipeline=id_da_camera")}
              />
            </div>
          </details>

          <div className="rowWrap" style={{ justifyContent: "flex-end", gap: 8 }}>
            <button className="chipButton" type="button" onClick={onClose} disabled={createBusy}>
              {t("ext.cinematic.common.close", {}, "Fechar")}
            </button>
            <button className="chipButton primary" type="button" onClick={() => void submit()} disabled={createBusy || loading || !transmissionId}>
              <i className="fa-solid fa-wand-magic-sparkles" aria-hidden="true" />
              <span>{t("ext.cinematic.wizard.create", {}, "Criar")}</span>
            </button>
          </div>
        </div>
      )}
    </SubModal>
  );
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }): React.ReactElement {
  return (
    <div className="field">
      <label className="label">{label}</label>
      <input className="input" value={value} onChange={(event) => onChange(event.target.value)} />
    </div>
  );
}

function toNumber(value: string, fallback: number): number {
  const parsed = Number(String(value || "").trim());
  return Number.isFinite(parsed) ? parsed : fallback;
}

function toInteger(value: string, fallback: number): number {
  const parsed = Number.parseInt(String(value || "").trim(), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parsePipelineMap(value: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of String(value || "").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const separatorIndex = line.includes("=") ? line.indexOf("=") : line.indexOf(":");
    if (separatorIndex <= 0) continue;
    const key = line.slice(0, separatorIndex).trim();
    const cameraId = line.slice(separatorIndex + 1).trim();
    if (key && cameraId) out[key] = cameraId;
  }
  return out;
}

function openPipelineScreen(pipelineName: string): void {
  if (typeof window === "undefined") return;
  const name = String(pipelineName || "").trim();
  if (!name) return;
  const target = `/settings/pipelines/${encodeURIComponent(name)}`;
  if (window.location.pathname === target) return;
  window.history.pushState(null, "", target);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
