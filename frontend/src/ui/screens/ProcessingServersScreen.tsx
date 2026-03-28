import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { ProcessingServer, ProcessingServerStatus } from "../../util/api";
import {
  deleteProcessingServer,
  getProcessingServerStatus,
  installProcessingServerVisionModel,
  listProcessingServers,
  putProcessingServer,
} from "../../util/api";
import { i18n } from "../../util/i18n";
import { LocalBuildConsentModal } from "../LocalBuildConsentModal";
import { ProcessingServerModal } from "../ProcessingServerModal";

type Props = {
  onClose: () => void;
  canManageProvisioning?: boolean;
};

type VisionDetectionCatalogItem = {
  modelId: string;
  displayName: string;
  availability: string;
  badgeIds: string[];
  artifactExists: boolean;
  localBuildSupported: boolean;
  localBuildReason: string;
  localBuildRuntime: string;
  localBuildSourceLabel: string;
  explicitConsentRequired: boolean;
  installJob: { status: string; phase: string; progressPct: number; error: string } | null;
};

type LocalBuildConsentState = {
  serverId: string;
  item: VisionDetectionCatalogItem;
};

type VisionRuntimeUpgradeSuggestion = {
  id: string;
  label: string;
  packageName: string;
  installCommand: string;
  replaceCommand: string;
  replacementRequired: boolean;
  note: string;
};

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function formatBytes(bytes: number): string {
  const value = Number.isFinite(bytes) ? bytes : 0;
  const abs = Math.max(0, value);
  const gb = abs / (1024 * 1024 * 1024);
  if (gb >= 10) return `${gb.toFixed(0)} GB`;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  const mb = abs / (1024 * 1024);
  if (mb >= 10) return `${mb.toFixed(0)} MB`;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${abs.toFixed(0)} B`;
}

function shortDigest(value: string): string {
  const clean = String(value || "").trim().toLowerCase();
  if (!clean) return "";
  if (clean.length <= 16) return clean;
  return `${clean.slice(0, 12)}…`;
}

function formatExecutionProviderLabel(value: string): string {
  const clean = String(value || "").trim();
  if (!clean) return "";
  if (clean === "CPUExecutionProvider") return "CPU";
  if (clean === "CUDAExecutionProvider") return "CUDA";
  if (clean === "TensorrtExecutionProvider") return "TensorRT";
  if (clean === "OpenVINOExecutionProvider") return "OpenVINO";
  if (clean === "CoreMLExecutionProvider") return "CoreML";
  if (clean === "DmlExecutionProvider") return "DirectML";
  return clean.replace(/ExecutionProvider$/, "");
}

function buildDiagnosticsSummary(status: Record<string, unknown> | undefined): string | null {
  if (!status || !isRecord(status)) return null;
  const system = isRecord(status.system) ? status.system : null;
  const platform = system && isRecord(system.platform) ? system.platform : null;
  const python = system && isRecord(system.python) ? system.python : null;
  const memory = system && isRecord(system.memory) ? system.memory : null;

  const vision = isRecord(status.vision) ? status.vision : null;
  const backends = vision && Array.isArray(vision.backends) ? vision.backends : [];
  const preferredProviders =
    vision && Array.isArray(vision.preferred_execution_providers)
      ? vision.preferred_execution_providers
      : vision && Array.isArray(vision.execution_providers)
        ? vision.execution_providers
        : [];

  const cameras = isRecord(status.cameras) ? status.cameras : null;
  const opencv = cameras && isRecord(cameras.opencv) ? cameras.opencv : null;
  const ffmpeg = cameras && isRecord(cameras.ffmpeg) ? cameras.ffmpeg : null;
  const hub = cameras && isRecord(cameras.hub) ? cameras.hub : null;

  const parts: string[] = [];

  const os = [String(platform?.system || "").trim(), String(platform?.machine || "").trim()].filter(Boolean).join(" ");
  if (os) parts.push(os);

  const py = String(python?.version || "").trim();
  if (py) parts.push(`Python ${py}`);

  const ortBackend = backends.find((raw) => isRecord(raw) && String(raw.id || "").trim() === "onnxruntime");
  const ortVersion = ortBackend && isRecord(ortBackend) ? String(ortBackend.version || "").trim() : "";
  if (ortVersion) parts.push(`ONNX Runtime ${ortVersion}`);

  const preferredProvider = formatExecutionProviderLabel(String(preferredProviders[0] || "").trim());
  if (preferredProvider) {
    parts.push(`Vision ${preferredProvider}`);
  }

  const memTotal = Number(memory?.total_bytes ?? 0);
  if (Number.isFinite(memTotal) && memTotal > 0) parts.push(`RAM ${formatBytes(memTotal)}`);

  const cv = opencv && opencv.available ? String(opencv.version || "").trim() : "";
  if (cv) parts.push(`OpenCV ${cv}`);

  const ff = ffmpeg && ffmpeg.available ? String(ffmpeg.version || "").trim() : "";
  if (ff) parts.push(`ffmpeg ${ff}`);

  const active = Number(hub?.active_count ?? 0);
  if (Number.isFinite(active) && active > 0) parts.push(`cameras ${active}`);

  return parts.length ? parts.join(" • ") : null;
}

function readVisionDetectionCatalog(status: Record<string, unknown> | undefined): {
  profile: string;
  items: VisionDetectionCatalogItem[];
} | null {
  if (!status || !isRecord(status)) return null;
  const vision = isRecord(status.vision) ? status.vision : null;
  const taskCatalogs = vision && isRecord(vision.task_catalogs) ? vision.task_catalogs : null;
  const detection = taskCatalogs && isRecord(taskCatalogs.detection) ? taskCatalogs.detection : null;
  const itemsRaw = detection && Array.isArray(detection.items) ? detection.items : [];
  const items = itemsRaw
    .map((raw) => {
      if (!isRecord(raw)) return null;
      const modelId = String(raw.model_id || "").trim();
      if (!modelId.startsWith("rtmdet_det_")) return null;
      if (String(raw.source_kind || "").trim() === "custom") return null;
      const acquisition = isRecord(raw.acquisition) ? raw.acquisition : null;
      return {
        modelId,
        displayName: String(raw.display_name || raw.model_id || "").trim(),
        availability: String(raw.availability || "").trim(),
        badgeIds: Array.isArray(raw.badge_ids) ? raw.badge_ids.map((value) => String(value || "").trim()).filter(Boolean) : [],
        artifactExists: !!raw.artifact_exists,
        localBuildSupported: !!raw.local_build_supported,
        localBuildReason: String(raw.local_build_reason || "").trim(),
        localBuildRuntime: String(raw.local_build_runtime || "").trim(),
        localBuildSourceLabel: String(raw.local_build_source_label || acquisition?.checkpoint_url || "").trim(),
        explicitConsentRequired: !!acquisition?.explicit_consent_required,
        installJob: isRecord(raw.install_job)
          ? {
              status: String(raw.install_job.status || "").trim(),
              phase: String(raw.install_job.phase || "").trim(),
              progressPct: Number(raw.install_job.progress_pct ?? 0),
              error: String(raw.install_job.error || "").trim(),
            }
          : null,
      };
    })
    .filter(
      (
        item,
      ): item is VisionDetectionCatalogItem => !!item && !!item.modelId,
    );
  if (!items.length) return null;
  return {
    profile: String(detection?.profile || "").trim(),
    items: items.slice(0, 3),
  };
}

function readVisionLocalBuilder(status: Record<string, unknown> | undefined): {
  supported: boolean;
  reason: string;
  backend: string;
  runtime: string;
  supportedModels: string[];
  lastJob: {
    modelId: string;
    displayName: string;
    status: string;
    phase: string;
    error: string;
    sourceLabel: string;
    outputSha256: string;
    requestedBy: {
      username: string;
      displayName: string;
      role: string;
    } | null;
  } | null;
} | null {
  if (!status || !isRecord(status)) return null;
  const vision = isRecord(status.vision) ? status.vision : null;
  const localBuilder = vision && isRecord(vision.local_builder) ? vision.local_builder : null;
  if (!localBuilder) return null;
  const lastJob = isRecord(localBuilder.last_job) ? localBuilder.last_job : null;
  return {
    supported: !!localBuilder.supported,
    reason: String(localBuilder.reason || "").trim(),
    backend: String(localBuilder.backend || "").trim(),
    runtime: String(localBuilder.runtime || "").trim(),
    supportedModels: Array.isArray(localBuilder.supported_models)
      ? localBuilder.supported_models.map((value) => String(value || "").trim()).filter(Boolean)
      : [],
    lastJob: lastJob
      ? {
          modelId: String(lastJob.model_id || "").trim(),
          displayName: String(lastJob.display_name || lastJob.model_id || "").trim(),
          status: String(lastJob.status || "").trim(),
          phase: String(lastJob.phase || "").trim(),
          error: String(lastJob.error || "").trim(),
          sourceLabel: String(lastJob.source_label || "").trim(),
          outputSha256: String(lastJob.output_sha256 || "").trim(),
          requestedBy: isRecord(lastJob.requested_by)
            ? {
                username: String(lastJob.requested_by.username || "").trim(),
                displayName: String(lastJob.requested_by.display_name || "").trim(),
                role: String(lastJob.requested_by.role || "").trim(),
              }
            : null,
        }
      : null,
  };
}

function readVisionRuntimeUpgradeSuggestions(
  status: Record<string, unknown> | undefined,
): VisionRuntimeUpgradeSuggestion[] {
  if (!status || !isRecord(status)) return [];
  const vision = isRecord(status.vision) ? status.vision : null;
  const runtimeUpgrades = vision && isRecord(vision.runtime_upgrades) ? vision.runtime_upgrades : null;
  const suggestionsRaw = runtimeUpgrades && Array.isArray(runtimeUpgrades.suggestions) ? runtimeUpgrades.suggestions : [];
  return suggestionsRaw
    .map((raw) => {
      if (!isRecord(raw)) return null;
      const packageName = String(raw.package_name || "").trim();
      if (!packageName) return null;
      return {
        id: String(raw.id || packageName).trim(),
        label: String(raw.label || packageName).trim(),
        packageName,
        installCommand: String(raw.install_command || "").trim(),
        replaceCommand: String(raw.replace_command || raw.install_command || "").trim(),
        replacementRequired: !!raw.replacement_required,
        note: String(raw.note || "").trim(),
      };
    })
    .filter((item): item is VisionRuntimeUpgradeSuggestion => !!item);
}

function installPhaseLabel(
  phase: string,
  t: (key: string, vars?: Record<string, unknown>, fallback?: string) => string,
): string {
  const clean = String(phase || "").trim() || "queued";
  return t(`core.ui.pipelines.panels.yolo.install_phase.${clean}`, {}, clean);
}

function localBuildReasonLabel(
  reason: string,
  t: (key: string, vars?: Record<string, unknown>, fallback?: string) => string,
): string {
  const clean = String(reason || "").trim() || "unsupported";
  return t(`core.ui.processing_servers.local_build.reason.${clean}`, {}, clean);
}

function sortServers(list: ProcessingServer[]): ProcessingServer[] {
  const local = list.find((s) => s.id === "local") ?? null;
  const rest = list.filter((s) => s.id !== "local").sort((a, b) => a.id.localeCompare(b.id));
  return local ? [local, ...rest] : rest;
}

export function ProcessingServersScreen({ onClose, canManageProvisioning = false }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [servers, setServers] = useState<ProcessingServer[]>([]);
  const [serverModalOpen, setServerModalOpen] = useState(false);
  const [serverModalTarget, setServerModalTarget] = useState<ProcessingServer | null>(null);
  const [serverStatusById, setServerStatusById] = useState<Record<string, ProcessingServerStatus>>({});
  const [serverTestingById, setServerTestingById] = useState<Record<string, boolean>>({});
  const [provisioningByKey, setProvisioningByKey] = useState<Record<string, boolean>>({});
  const [serverProvisionErrorById, setServerProvisionErrorById] = useState<Record<string, string>>({});
  const [localBuildConsent, setLocalBuildConsent] = useState<LocalBuildConsentState | null>(null);
  const [localBuildConsentChecked, setLocalBuildConsentChecked] = useState(false);
  const [localBuildConsentSubmitting, setLocalBuildConsentSubmitting] = useState(false);
  const [localBuildConsentError, setLocalBuildConsentError] = useState<string | null>(null);

  const renderRecommendationBadge = useCallback(
    (badgeId: string): string => {
      const clean = String(badgeId || "").trim();
      if (!clean) return "";
      const key = `core.ui.processing_servers.vision_recommendations.badge.${clean}`;
      return t(key, {}, clean);
    },
    [t],
  );

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await listProcessingServers();
      setServers(sortServers(list));
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const canAdd = useMemo(() => servers.length < 128, [servers.length]);

  const handleSaveServer = async (server: ProcessingServer) => {
    setError(null);
    try {
      await putProcessingServer(server);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleDeleteServer = async (serverId: string, confirmDelete = true) => {
    if (confirmDelete && !confirm(t("core.ui.processing_servers.confirm_delete", { id: serverId }))) return;
    setError(null);
    try {
      await deleteProcessingServer(serverId);
      await reload();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    }
  };

  const handleTestServer = useCallback(async (serverId: string): Promise<ProcessingServerStatus> => {
    const sid = String(serverId || "").trim().toLowerCase();
    if (!sid) return { ok: false, error: "Missing server id" };
    setServerTestingById((prev) => ({ ...prev, [sid]: true }));
    try {
      const status = await getProcessingServerStatus(sid);
      setServerStatusById((prev) => ({ ...prev, [sid]: status }));
      return status;
    } catch (err: any) {
      const failed = { ok: false, error: String(err?.message ?? err) };
      setServerStatusById((prev) => ({ ...prev, [sid]: failed }));
      return failed;
    } finally {
      setServerTestingById((prev) => ({ ...prev, [sid]: false }));
    }
  }, []);

  const handleStartLocalBuild = useCallback(
    async (serverId: string, item: VisionDetectionCatalogItem) => {
      const sid = String(serverId || "").trim().toLowerCase();
      const key = `${sid}:${item.modelId}`;
      if (!sid || provisioningByKey[key]) return;
      setProvisioningByKey((prev) => ({ ...prev, [key]: true }));
      setServerProvisionErrorById((prev) => ({ ...prev, [sid]: "" }));
      try {
        await installProcessingServerVisionModel(sid, item.modelId, {
          mode: "local_build",
          acknowledge_upstream_terms: true,
        });
        await handleTestServer(sid);
      } catch (err: any) {
        const message = String(err?.message ?? err);
        setServerProvisionErrorById((prev) => ({ ...prev, [sid]: message }));
        throw new Error(message);
      } finally {
        setProvisioningByKey((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
      }
    },
    [handleTestServer, provisioningByKey],
  );

  const openLocalBuildConsent = useCallback((serverId: string, item: VisionDetectionCatalogItem) => {
    const sid = String(serverId || "").trim().toLowerCase();
    if (!sid) return;
    setLocalBuildConsentError(null);
    if (!item.explicitConsentRequired) {
      void handleStartLocalBuild(sid, item);
      return;
    }
    setLocalBuildConsent({ serverId: sid, item });
    setLocalBuildConsentChecked(false);
  }, [handleStartLocalBuild]);

  const closeLocalBuildConsent = useCallback(() => {
    if (localBuildConsentSubmitting) return;
    setLocalBuildConsent(null);
    setLocalBuildConsentChecked(false);
    setLocalBuildConsentError(null);
  }, [localBuildConsentSubmitting]);

  const confirmLocalBuildConsent = useCallback(async () => {
    if (!localBuildConsent) return;
    if (localBuildConsent.item.explicitConsentRequired && !localBuildConsentChecked) return;
    setLocalBuildConsentSubmitting(true);
    setLocalBuildConsentError(null);
    try {
      await handleStartLocalBuild(localBuildConsent.serverId, localBuildConsent.item);
      setLocalBuildConsent(null);
      setLocalBuildConsentChecked(false);
    } catch (err: any) {
      setLocalBuildConsentError(String(err?.message ?? err));
    } finally {
      setLocalBuildConsentSubmitting(false);
    }
  }, [handleStartLocalBuild, localBuildConsent, localBuildConsentChecked]);

  useEffect(() => {
    const activeIds = Object.entries(serverStatusById)
      .filter(([, probe]) => {
        if (!probe?.ok || !isRecord(probe.status)) return false;
        const recommendation = readVisionDetectionCatalog(probe.status);
        if (!recommendation) return false;
        return recommendation.items.some((item) => {
          const status = String(item.installJob?.status || "").trim();
          return ["queued", "downloading", "verifying", "installing"].includes(status);
        });
      })
      .map(([serverId]) => serverId);
    if (!activeIds.length) return undefined;
    const timer = window.setInterval(() => {
      activeIds.forEach((serverId) => {
        void handleTestServer(serverId);
      });
    }, 1500);
    return () => window.clearInterval(timer);
  }, [handleTestServer, serverStatusById]);

  return (
    <div className="pipelinesRoot screenRoot">
      <div className="pipelinesTopbar">
        <button className="iconButton" type="button" onClick={onClose} aria-label={t("core.actions.back", {}, "Back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="pipelinesTitle">{t("core.ui.processing_servers.title")}</div>
        <div className="pipelinesTopbarRight">
          <button
            className="pillButton pillButtonPrimary"
            type="button"
            disabled={!canAdd}
            onClick={() => {
              setServerModalTarget(null);
              setServerModalOpen(true);
            }}
          >
            <i className="fa-solid fa-plus" aria-hidden="true" />
            {t("core.ui.processing_servers.add_server")}
          </button>
        </div>
      </div>

      <div className="processingServersBody">
        <div className="processingServersContent">
          <div className="processingServersIntro">{t("core.ui.processing_servers.description")}</div>

          {loading ? <div className="settingsStatusMuted">{t("core.ui.loading")}</div> : null}

          {error ? <div className="errorText">{error}</div> : null}

          <div className="processingServersList">
            {servers.map((server) => {
              const probe = serverStatusById[server.id] ?? null;
              const testing = !!serverTestingById[server.id];
              const serverProvisionError = String(serverProvisionErrorById[server.id] || "").trim();
              const statusLabel = testing
                ? ` • ${t("core.ui.processing_servers.status.testing")}`
                : probe
                  ? probe.ok
                    ? ` • ${t("core.ui.processing_servers.status.online")}`
                    : ` • ${t("core.ui.processing_servers.status.offline")}`
                  : "";
              const statusTitle =
                testing ? t("core.ui.processing_servers.status.testing") : probe && !probe.ok ? String(probe.error || "") : "";
              const diagnosticsLine = probe && probe.ok ? buildDiagnosticsSummary(probe.status) : null;
              const runtimeUpgrades = probe && probe.ok ? readVisionRuntimeUpgradeSuggestions(probe.status) : [];
              const visionCatalog = probe && probe.ok ? readVisionDetectionCatalog(probe.status) : null;
              const localBuilder = probe && probe.ok ? readVisionLocalBuilder(probe.status) : null;
              const localBuilderLastPhase = localBuilder?.lastJob
                ? installPhaseLabel(localBuilder.lastJob.phase || localBuilder.lastJob.status || "queued", t)
                : "";
              const localBuilderActor =
                localBuilder?.lastJob?.requestedBy?.displayName ||
                localBuilder?.lastJob?.requestedBy?.username ||
                "";
              return (
                <div key={server.id} className={["pipelinesServerRow", server.id === "local" ? "isBuiltIn" : ""].filter(Boolean).join(" ")}>
                  <div
                    className="pipelinesServerMain"
                    role="button"
                    tabIndex={server.id === "local" ? -1 : 0}
                    aria-disabled={server.id === "local"}
                    onClick={() => {
                      if (server.id === "local") return;
                      setServerModalTarget(server);
                      setServerModalOpen(true);
                    }}
                    onKeyDown={(event) => {
                      if (server.id === "local") return;
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setServerModalTarget(server);
                        setServerModalOpen(true);
                      }
                    }}
                  >
                    <div className="pipelinesServerId">
                      {server.id}
                      {server.id === "local" ? ` ${t("core.ui.processing_servers.built_in")}` : ""}
                    </div>
                    <div className="pipelinesServerMeta" title={statusTitle}>
                      {server.kind}
                      {server.url ? ` • ${server.url}` : ""}
                      {statusLabel}
                    </div>
                    {diagnosticsLine ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag" title={diagnosticsLine}>
                        {diagnosticsLine}
                      </div>
                    ) : null}
                    {runtimeUpgrades.map((item) => (
                      <div key={item.id} className="pipelinesServerMeta pipelinesServerMetaDiag">
                        <div>
                          {t(
                            "core.ui.processing_servers.runtime_upgrade.title",
                            { label: item.label },
                            `Suggested runtime upgrade: ${item.label}`,
                          )}
                        </div>
                        <div style={{ fontFamily: "monospace", overflowWrap: "anywhere" }}>
                          {item.replacementRequired ? item.replaceCommand : item.installCommand}
                        </div>
                        {item.note ? <div>{item.note}</div> : null}
                      </div>
                    ))}
                    {visionCatalog ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {t("core.ui.processing_servers.vision_recommendations.title")}{" "}
                        {t("core.ui.processing_servers.vision_recommendations.profile", {
                          profile: t(
                            `core.ui.processing_servers.vision_recommendations.profile_label.${visionCatalog.profile}`,
                            {},
                            visionCatalog.profile,
                          ),
                        })}
                      </div>
                    ) : null}
                    {localBuilder ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {localBuilder.supported
                          ? t("core.ui.processing_servers.local_build.ready", {
                              runtime: localBuilder.runtime || localBuilder.backend || "local",
                              count: localBuilder.supportedModels.length || 0,
                            })
                          : t("core.ui.processing_servers.local_build.unavailable", {
                              reason: localBuildReasonLabel(localBuilder.reason, t),
                            })}
                      </div>
                    ) : null}
                    {localBuilder?.lastJob ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {t("core.ui.processing_servers.local_build.last_job", {
                          model: localBuilder.lastJob.displayName || localBuilder.lastJob.modelId,
                          phase: localBuilderLastPhase || localBuilder.lastJob.phase || localBuilder.lastJob.status || "queued",
                        })}
                      </div>
                    ) : null}
                    {localBuilderActor ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {t("core.ui.processing_servers.local_build.last_job_actor", {
                          actor: localBuilderActor,
                        })}
                      </div>
                    ) : null}
                    {localBuilder?.lastJob?.sourceLabel ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {t("core.ui.processing_servers.local_build.last_job_source", {
                          source: localBuilder.lastJob.sourceLabel,
                        })}
                      </div>
                    ) : null}
                    {localBuilder?.lastJob?.outputSha256 ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {t("core.ui.processing_servers.local_build.last_job_hash", {
                          sha: shortDigest(localBuilder.lastJob.outputSha256),
                        })}
                      </div>
                    ) : null}
                    {localBuilder?.lastJob?.error ? (
                      <div className="pipelinesServerMeta pipelinesServerMetaDiag">
                        {t("core.ui.processing_servers.local_build.last_job_error", {
                          error: localBuilder.lastJob.error,
                        })}
                      </div>
                    ) : null}
                    {visionCatalog
                      ? visionCatalog.items.map((item) => {
                          const badges = item.badgeIds.map(renderRecommendationBadge).filter(Boolean);
                          const suffix = badges.length ? ` • ${badges.join(" • ")}` : "";
                          const installedSuffix = item.artifactExists
                            ? ` • ${t("core.ui.processing_servers.vision_recommendations.installed")}`
                            : ` • ${t("core.ui.processing_servers.vision_recommendations.manifest_only")}`;
                          const jobPhase = item.installJob?.phase ? installPhaseLabel(item.installJob.phase, t) : "";
                          const key = `${server.id}:${item.modelId}`;
                          const canPrepareLocally =
                            canManageProvisioning &&
                            !item.artifactExists &&
                            item.localBuildSupported &&
                            !item.installJob;
                          return (
                            <div key={item.modelId} className="pipelinesServerMeta pipelinesServerMetaDiag">
                              <div>
                                {item.displayName}
                                {suffix}
                                {installedSuffix}
                              </div>
                              {item.installJob ? (
                                <div>
                                  {t("core.ui.pipelines.panels.yolo.local_build.job_progress", {
                                    phase: jobPhase || item.installJob.phase || "queued",
                                    progress: Math.max(0, Math.min(100, Math.round(item.installJob.progressPct || 0))),
                                  })}
                                </div>
                              ) : null}
                              {!item.artifactExists && !item.localBuildSupported && item.localBuildReason ? (
                                <div>
                                  {t("core.ui.processing_servers.local_build.reason", {
                                    reason: localBuildReasonLabel(item.localBuildReason, t),
                                  })}
                                </div>
                              ) : null}
                              {canPrepareLocally ? (
                                <div style={{ marginTop: 6 }}>
                                  <button
                                    className="pillButton"
                                    type="button"
                                    disabled={!!provisioningByKey[key]}
                                    onClick={(event) => {
                                      event.preventDefault();
                                      event.stopPropagation();
                                      void openLocalBuildConsent(server.id, item);
                                    }}
                                  >
                                    {provisioningByKey[key]
                                      ? t("core.ui.pipelines.panels.yolo.local_build.starting")
                                      : t("core.ui.pipelines.panels.yolo.local_build.start")}
                                  </button>
                                </div>
                              ) : null}
                            </div>
                          );
                        })
                      : null}
                    {serverProvisionError ? <div className="errorText" style={{ marginTop: 8 }}>{serverProvisionError}</div> : null}
                  </div>

                  <button
                    className="iconButton iconButtonPrimary"
                    type="button"
                    disabled={testing}
                    onClick={() => void handleTestServer(server.id)}
                    title={t("core.ui.processing_servers.actions.test_connection")}
                  >
                    <i className="fa-solid fa-plug" aria-hidden="true" />
                  </button>

                  {server.id !== "local" ? (
                    <>
                      <button
                        className="iconButton"
                        type="button"
                        onClick={() => {
                          setServerModalTarget(server);
                          setServerModalOpen(true);
                        }}
                        title={t("core.ui.processing_servers.actions.edit_server")}
                      >
                        <i className="fa-solid fa-pen-to-square" aria-hidden="true" />
                      </button>

                      <button
                        className="iconButton iconButtonDanger"
                        type="button"
                        onClick={() => void handleDeleteServer(server.id)}
                        title={t("core.ui.processing_servers.actions.delete_server")}
                      >
                        <i className="fa-solid fa-trash" aria-hidden="true" />
                      </button>
                    </>
                  ) : null}
                </div>
              );
            })}

            {!loading && servers.length === 0 ? (
              <div className="processingServersEmpty">{t("core.ui.processing_servers.none")}</div>
            ) : null}
          </div>
        </div>
      </div>

      <ProcessingServerModal
        open={serverModalOpen}
        server={serverModalTarget}
        onClose={() => {
          setServerModalOpen(false);
          setServerModalTarget(null);
        }}
        onSave={handleSaveServer}
        onDelete={(serverId) => handleDeleteServer(serverId, false)}
        onTest={handleTestServer}
      />

      <LocalBuildConsentModal
        open={!!localBuildConsent}
        action="prepare"
        serverId={localBuildConsent?.serverId || ""}
        modelName={localBuildConsent?.item.displayName || ""}
        runtimeLabel={localBuildConsent?.item.localBuildRuntime || "docker / podman"}
        sourceLabel={localBuildConsent?.item.localBuildSourceLabel || ""}
        checked={localBuildConsentChecked}
        submitting={localBuildConsentSubmitting}
        error={localBuildConsentError}
        onToggleChecked={setLocalBuildConsentChecked}
        onClose={closeLocalBuildConsent}
        onConfirm={() => void confirmLocalBuildConsent()}
      />
    </div>
  );
}
