import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n, SettingsPanel } from "@toposync/plugin-api";

import {
  discoverOnvifDevices,
  fetchCamerasIndex,
  fetchOnvifStreamUri,
  fetchRtspSnapshot,
  inspectOnvif,
} from "../api/camerasApi";
import { CAMERAS_EXTENSION_ID } from "../constants";
import { createUniqueId, parseCameras, serializeCameras } from "../parsing";
import type {
  CameraConfig,
  CameraOnvifConfig,
  OnvifDiscoverResponse,
  OnvifDiscoveredDeviceInfo,
  OnvifInspectResponse,
  OnvifProfileInfo,
} from "../types";
import { SubModal } from "../ui/SubModal";
import { CameraPipelineWizardModal } from "../wizard/CameraPipelineWizardModal";

export function createCamerasSettingsPanel(): SettingsPanel {
  return {
    id: CAMERAS_EXTENSION_ID,
    icon: "video",
    name: { key: "ext.cameras.settings.name", fallback: "Cameras" },
    description: { key: "ext.cameras.settings.desc" },
    render: ({ i18n, settings, updateSettings }) => (
      <CamerasSettingsPanelContent i18n={i18n} settings={settings} updateSettings={updateSettings} />
    ),
  };
}

function normalizeQuery(value: string): string {
  return value.trim().toLowerCase();
}

function includesQuery(value: string, query: string): boolean {
  const normalized = normalizeQuery(value);
  if (!normalized) return false;
  return normalized.includes(query);
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function CamerasSettingsPanelContent({
  i18n,
  settings,
  updateSettings,
}: {
  i18n: HostI18n;
  settings: Record<string, unknown>;
  updateSettings: (patch: Record<string, unknown>) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();

  const cameras = useMemo(() => parseCameras(settings), [settings]);
  const camerasRef = React.useRef<CameraConfig[]>([]);

  useEffect(() => {
    camerasRef.current = cameras;
  }, [cameras]);

  const [cameraQuery, setCameraQuery] = useState("");

  const [activeCameraId, setActiveCameraId] = useState<string | null>(null);

  const [confirmDeleteCameraId, setConfirmDeleteCameraId] = useState<string | null>(null);

  const [snapshotModalOpen, setSnapshotModalOpen] = useState(false);
  const [snapshotTitle, setSnapshotTitle] = useState("");
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const snapshotAbortRef = React.useRef<AbortController | null>(null);

  const [suggestionsLoading, setSuggestionsLoading] = useState(false);
  const [suggestionsErrorMessage, setSuggestionsErrorMessage] = useState<string | null>(null);
  const [suggestionsResult, setSuggestionsResult] = useState<OnvifDiscoverResponse | null>(null);
  const suggestionsAbortRef = React.useRef<AbortController | null>(null);
  const suggestionsAutoScanRef = React.useRef(false);

  const [onvifInspectResult, setOnvifInspectResult] = useState<OnvifInspectResponse | null>(null);
  const [onvifErrorMessage, setOnvifErrorMessage] = useState<string | null>(null);
  const [onvifLoading, setOnvifLoading] = useState(false);
  const [onvifStreamLoading, setOnvifStreamLoading] = useState(false);
  const onvifAbortRef = React.useRef<AbortController | null>(null);

  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  useEffect(() => {
    return () => {
      snapshotAbortRef.current?.abort();
      snapshotAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    return () => {
      suggestionsAbortRef.current?.abort();
      suggestionsAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    return () => {
      onvifAbortRef.current?.abort();
      onvifAbortRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (activeCameraId && cameras.some((camera) => camera.id === activeCameraId)) return;
    setActiveCameraId(cameras[0]?.id ?? null);
  }, [activeCameraId, cameras]);

  useEffect(() => {
    setOnvifInspectResult(null);
    setOnvifErrorMessage(null);
    setOnvifLoading(false);
    setOnvifStreamLoading(false);
    onvifAbortRef.current?.abort();
    onvifAbortRef.current = null;
  }, [activeCameraId]);

  const filteredCameras = useMemo(() => {
    const q = normalizeQuery(cameraQuery);
    if (!q) return cameras;
    return cameras.filter((camera) => {
      const onvifXaddr = camera.onvif?.xaddr ?? "";
      return (
        includesQuery(camera.name || "", q) ||
        includesQuery(camera.id, q) ||
        includesQuery(camera.rtsp_url, q) ||
        includesQuery(onvifXaddr, q)
      );
    });
  }, [cameraQuery, cameras]);

  function hostForUrl(value: string): string {
    const raw = String(value ?? "").trim();
    if (!raw) return "";
    try {
      const parsed = new URL(raw);
      return parsed.hostname.trim().toLowerCase();
    } catch (_error) {
      return "";
    }
  }

  const suggestedDevices = useMemo(() => {
    const devices = suggestionsResult?.devices ?? [];

    const knownDeviceIds = new Set(
      cameras
        .map((camera) => camera.onvif?.device_id)
        .filter((value): value is string => typeof value === "string" && Boolean(value.trim()))
        .map((value) => value.trim()),
    );

    const knownHosts = new Set(
      cameras
        .flatMap((camera) => [camera.rtsp_url, camera.onvif?.xaddr ?? ""])
        .map((url) => hostForUrl(url))
        .filter(Boolean),
    );

    return devices.filter((device) => {
      const deviceId = String(device.device_id ?? "").trim();
      if (deviceId && knownDeviceIds.has(deviceId)) return false;
      const xaddr = String(device.xaddr ?? device.xaddrs?.[0] ?? "").trim();
      const host = hostForUrl(xaddr) || String(device.source_ip ?? "").trim().toLowerCase();
      if (host && knownHosts.has(host)) return false;
      return Boolean(xaddr || device.source_ip);
    });
  }, [cameras, suggestionsResult]);

  const suggestionsWarnings = suggestionsResult?.warnings ?? [];
  const suggestionsTargets = suggestionsResult?.targets ?? [];

  function closeSnapshotModal(): void {
    snapshotAbortRef.current?.abort();
    snapshotAbortRef.current = null;
    setSnapshotModalOpen(false);
    setSnapshotTitle("");
    setSnapshotErrorMessage(null);
    setSnapshotLoading(false);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
  }

  function openSnapshotModal(title: string): void {
    setSnapshotTitle(title);
    setSnapshotErrorMessage(null);
    setSnapshotLoading(false);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    setSnapshotModalOpen(true);
  }

  async function testCameraConnection(camera: CameraConfig): Promise<void> {
    snapshotAbortRef.current?.abort();
    const controller = new AbortController();
    snapshotAbortRef.current = controller;
    setSnapshotLoading(true);
    setSnapshotErrorMessage(null);
    try {
      const blob = await fetchRtspSnapshot(
        { url: camera.rtsp_url, username: camera.username, password: camera.password },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return url;
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    } finally {
      if (!controller.signal.aborted) setSnapshotLoading(false);
    }
  }

  function commitCameras(next: CameraConfig[]): void {
    camerasRef.current = next;
    updateSettings(serializeCameras(next));
  }

  function updateCamera(cameraId: string, patch: Partial<CameraConfig>): void {
    const next = camerasRef.current.map((camera) => (camera.id === cameraId ? { ...camera, ...patch } : camera));
    commitCameras(next);
  }

  function updateCameraOnvif(cameraId: string, patch: Partial<CameraOnvifConfig>): void {
    const current = camerasRef.current.find((camera) => camera.id === cameraId)?.onvif ?? null;
    const nextBase = current && typeof current === "object" ? current : { xaddr: "" };
    updateCamera(cameraId, { onvif: { ...nextBase, ...patch } });
  }

  function addCamera(): void {
    const id = createUniqueId();
    const next: CameraConfig = {
      id,
      name: "",
      connection_type: "onvif",
      channel_id: "video_main",
      rtsp_url: "",
      username: "",
      password: "",
      fps: 5,
      onvif: { xaddr: "" },
    };
    commitCameras([next, ...camerasRef.current]);
    setActiveCameraId(id);
    setConfirmDeleteCameraId(null);
  }

  function addSuggestedCamera(device: OnvifDiscoveredDeviceInfo): void {
    const id = createUniqueId();
    const xaddrCandidate = (device.xaddr || device.xaddrs?.[0] || device.source_ip || "").trim();
    const name = String(device.name || device.hardware || device.source_ip || "").trim();
    const next: CameraConfig = {
      id,
      name,
      connection_type: "onvif",
      channel_id: "video_main",
      rtsp_url: "",
      username: "",
      password: "",
      fps: 5,
      onvif: {
        xaddr: xaddrCandidate,
        device_id: String(device.device_id || "").trim() || undefined,
        hardware: String(device.hardware || "").trim() || undefined,
      },
    };
    commitCameras([next, ...camerasRef.current]);
    setActiveCameraId(id);
    setConfirmDeleteCameraId(null);
  }

  function deleteCamera(cameraId: string): void {
    commitCameras(camerasRef.current.filter((camera) => camera.id !== cameraId));
    setConfirmDeleteCameraId(null);
    if (activeCameraId === cameraId) setActiveCameraId(null);
  }

  async function scanOnvifSuggestions({ force }: { force?: boolean } = {}): Promise<void> {
    suggestionsAbortRef.current?.abort();
    const controller = new AbortController();
    suggestionsAbortRef.current = controller;
    setSuggestionsLoading(true);
    setSuggestionsErrorMessage(null);
    try {
      const result = await discoverOnvifDevices(
        {
          timeout_ms: 1600,
          force: Boolean(force),
          exclude_known: true,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setSuggestionsResult(result);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setSuggestionsErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setSuggestionsLoading(false);
    }
  }

  useEffect(() => {
    if (suggestionsAutoScanRef.current) return;
    suggestionsAutoScanRef.current = true;
    // Avoid eager WS-Discovery scans while settings are still loading. We first ask the backend
    // if the user has any cameras configured and only auto-scan on a truly empty setup.
    let cancelled = false;
    void (async () => {
      try {
        const index = await fetchCamerasIndex();
        if (cancelled) return;
        if ((index.cameras ?? []).length !== 0) return;
        await scanOnvifSuggestions({ force: false });
      } catch {
        // Ignore auto-scan errors; user can always click "Scan network".
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const activeCamera = activeCameraId ? cameras.find((camera) => camera.id === activeCameraId) ?? null : null;

  async function discoverOnvifProfiles(camera: CameraConfig): Promise<void> {
    const xaddr = camera.onvif?.xaddr?.trim() ?? "";
    if (!xaddr) return;

    onvifAbortRef.current?.abort();
    const controller = new AbortController();
    onvifAbortRef.current = controller;

    setOnvifLoading(true);
    setOnvifStreamLoading(false);
    setOnvifErrorMessage(null);
    setOnvifInspectResult(null);

    try {
      const result = await inspectOnvif(
        {
          xaddr,
          username: camera.username ?? "",
          password: camera.password ?? "",
          timeout_ms: 3500,
          auth: "auto",
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setOnvifInspectResult(result);
      if (result.xaddr && result.xaddr !== xaddr) {
        updateCameraOnvif(camera.id, { xaddr: result.xaddr });
      }
      if (typeof result.media_xaddr === "string" && result.media_xaddr.trim()) {
        updateCameraOnvif(camera.id, { media_xaddr: result.media_xaddr.trim() });
      }
      if (typeof result.ptz_xaddr === "string" && result.ptz_xaddr.trim()) {
        updateCameraOnvif(camera.id, { ptz_xaddr: result.ptz_xaddr.trim() });
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setOnvifErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setOnvifLoading(false);
    }
  }

  async function applyOnvifProfile(camera: CameraConfig, profile: OnvifProfileInfo): Promise<void> {
    const xaddr = camera.onvif?.xaddr?.trim() ?? "";
    if (!xaddr) return;
    const token = String(profile.token ?? "").trim();
    if (!token) return;

    onvifAbortRef.current?.abort();
    const controller = new AbortController();
    onvifAbortRef.current = controller;
    setOnvifStreamLoading(true);
    setOnvifErrorMessage(null);

    try {
      const result = await fetchOnvifStreamUri(
        {
          xaddr,
          media_xaddr: camera.onvif?.media_xaddr ?? onvifInspectResult?.media_xaddr ?? "",
          profile_token: token,
          username: camera.username ?? "",
          password: camera.password ?? "",
          timeout_ms: 4500,
          auth: "auto",
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      updateCamera(camera.id, { rtsp_url: result.rtsp_url });
      updateCameraOnvif(camera.id, { profile_token: token, profile_name: profile.name?.trim() ?? "" });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setOnvifErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (!controller.signal.aborted) setOnvifStreamLoading(false);
    }
  }

  return (
    <div>
      <div className="card">
        <div className="cardBody">
          <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center", gap: 12 }}>
            <div>
              <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                {t("ext.cameras.settings.suggestions.title", {}, "Suggested cameras")}
              </div>
              <div className="cardMeta">{t("ext.cameras.settings.suggestions.desc")}</div>
            </div>

            <button
              className="chipButton"
              type="button"
              disabled={suggestionsLoading}
              onClick={() => void scanOnvifSuggestions({ force: true })}
            >
              {suggestionsLoading
                ? t("ext.cameras.settings.suggestions.scanning", {}, "Scanning…")
                : t("ext.cameras.settings.suggestions.scan", {}, "Scan network")}
            </button>
          </div>

          {suggestionsErrorMessage ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">{suggestionsErrorMessage}</div>
            </div>
          ) : null}

          {suggestionsWarnings.length > 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                {suggestionsWarnings.map((warning, index) => (
                  <div key={`${warning}-${index}`}>{warning}</div>
                ))}
              </div>
            </div>
          ) : null}

          {suggestionsResult && !suggestionsLoading && suggestedDevices.length === 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div>{t("ext.cameras.settings.suggestions.none", {}, "No new cameras found.")}</div>
                {suggestionsTargets.length > 0 ? (
                  <div className="cardMeta" style={{ marginTop: 6 }}>
                    {t("ext.cameras.settings.suggestions.targets", {}, "Discovery targets")}: {suggestionsTargets.join(", ")}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {suggestedDevices.length > 0 ? (
            <div className="settingsList" style={{ marginTop: 10 }}>
              {suggestedDevices.map((device) => {
                const xaddr = String(device.xaddr ?? device.xaddrs?.[0] ?? "").trim();
                const title =
                  String(device.name ?? "").trim() ||
                  String(device.hardware ?? "").trim() ||
                  String(device.source_ip ?? "").trim() ||
                  String(device.device_id ?? "").trim() ||
                  xaddr;
                const meta =
                  xaddr ||
                  String(device.source_ip ?? "").trim() ||
                  String(device.device_id ?? "").trim();
                return (
                  <div key={device.device_id || xaddr || String(device.source_ip ?? "") || title} className="choiceItem" style={{ cursor: "default" }}>
                    <div className="settingsListItemRow">
                      <div className="settingsListItemMain">
                        <div className="settingsListItemTitle" title={title}>
                          {title}
                        </div>
                        <div className="settingsListItemMeta" title={meta}>
                          {meta}
                        </div>
                      </div>
                      <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
                        <button
                          className="chipButton"
                          type="button"
                          onClick={() => addSuggestedCamera(device)}
                        >
                          {t("ext.cameras.settings.suggestions.add", {}, "Add")}
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>

      <div className="sectionDivider" />

      <div className="settingsSplit">
        <div className="settingsSplitSidebar">
          <div className="settingsSplitToolbar">
            <input
              className="input"
              placeholder={t("ext.cameras.settings.search_cameras", {}, "Search cameras…")}
              value={cameraQuery}
              onChange={(event) => setCameraQuery(event.target.value)}
            />
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.cameras.settings.add_camera")}
              onClick={addCamera}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          {filteredCameras.length === 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>{t("ext.cameras.settings.empty_cameras")}</div>
                <button className="primaryButton" type="button" onClick={addCamera}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.cameras.settings.add_camera")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsList">
              {filteredCameras.map((camera) => {
                const selected = camera.id === activeCameraId;
                const name = camera.name.trim() || t("ext.cameras.settings.unnamed_camera", {}, "Untitled camera");
                const meta = camera.rtsp_url.trim() || t("ext.cameras.settings.missing_rtsp_url", {}, "Stream URL missing");
                return (
                  <button
                    key={camera.id}
                    type="button"
                    className={["choiceItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                    onClick={() => {
                      setActiveCameraId(camera.id);
                      setConfirmDeleteCameraId(null);
                    }}
                  >
                    <div className="settingsListItemRow">
                      <div className="settingsListItemMain">
                        <div className="settingsListItemTitle" title={name}>
                          {name}
                        </div>
                        <div className="settingsListItemMeta" title={meta}>
                          {meta}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="settingsSplitMain">
          {!activeCamera ? (
            <div className="card">
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>{t("ext.cameras.settings.select_camera", {}, "Select a camera to edit.")}</div>
                <button className="primaryButton" type="button" onClick={addCamera}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.cameras.settings.add_camera")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsDetail">
              <div className="settingsDetailHeader">
                <div>
                  <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                    {activeCamera.name.trim() || t("ext.cameras.settings.unnamed_camera", {}, "Untitled camera")}
                  </div>
                  <div className="cardMeta">ID: {activeCamera.id}</div>
                </div>

                <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                  <button className="chipButton" type="button" onClick={() => setWizardOpen(true)}>
                    {t("ext.cameras.wizard.open", {}, "Create pipeline")}
                  </button>

                  <button
                    className="chipButton"
                    type="button"
                    disabled={snapshotLoading || !activeCamera.rtsp_url.trim()}
                    onClick={() => {
                      openSnapshotModal(
                        activeCamera.name
                          ? `${t("ext.cameras.settings.snapshot")}: ${activeCamera.name}`
                          : t("ext.cameras.settings.snapshot"),
                      );
                      void testCameraConnection(activeCamera);
                    }}
                  >
                    {snapshotLoading ? t("ext.cameras.settings.testing") : t("ext.cameras.settings.test")}
                  </button>

                  <button
                    className={confirmDeleteCameraId === activeCamera.id ? "dangerButton" : "iconButton iconButtonDanger"}
                    type="button"
                    aria-label={t("core.actions.delete")}
                    title={t("core.actions.delete")}
                    onClick={() => {
                      if (confirmDeleteCameraId === activeCamera.id) {
                        deleteCamera(activeCamera.id);
                        return;
                      }
                      setConfirmDeleteCameraId(activeCamera.id);
                    }}
                  >
                    {confirmDeleteCameraId === activeCamera.id ? (
                      t("core.actions.delete")
                    ) : (
                      <i className="fa-solid fa-trash" aria-hidden="true" />
                    )}
                  </button>
                </div>
              </div>

              <div className="sectionDivider" />

              <div className="card">
                <div className="cardBody">
                  <div className="field">
                    <label className="label">{t("ext.cameras.settings.camera_name")}</label>
                    <input
                      className="input"
                      value={activeCamera.name}
                      onChange={(event) => updateCamera(activeCamera.id, { name: event.target.value })}
                    />
                  </div>

                  <div className="field">
                    <label className="label">{t("ext.cameras.settings.camera_type")}</label>
                    <select
                      className="input"
                      value={activeCamera.connection_type}
                      onChange={(event) => {
                        const next = event.target.value === "onvif" ? "onvif" : "rtsp";
                        const patch: Partial<CameraConfig> = { connection_type: next };
                        if (next === "onvif" && !activeCamera.onvif) patch.onvif = { xaddr: "" };
                        updateCamera(activeCamera.id, patch);
                        setOnvifInspectResult(null);
                        setOnvifErrorMessage(null);
                      }}
                    >
                      <option value="onvif">{t("ext.cameras.settings.camera_type_onvif")}</option>
                      <option value="rtsp">{t("ext.cameras.settings.camera_type_rtsp")}</option>
                    </select>
                  </div>

                  {activeCamera.connection_type === "onvif" ? (
                    <>
                      <div className="field">
                        <label className="label">{t("ext.cameras.settings.onvif_xaddr")}</label>
                        <input
                          className="input"
                          value={activeCamera.onvif?.xaddr ?? ""}
                          onChange={(event) => {
                            updateCameraOnvif(activeCamera.id, { xaddr: event.target.value });
                            setOnvifInspectResult(null);
                            setOnvifErrorMessage(null);
                          }}
                          placeholder="192.168.0.10"
                        />
                        <div className="label">{t("ext.cameras.settings.onvif_xaddr_hint")}</div>
                      </div>

                      <div className="rowWrap" style={{ gap: 10 }}>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.username")}</label>
                          <input
                            className="input"
                            value={activeCamera.username ?? ""}
                            onChange={(event) => updateCamera(activeCamera.id, { username: event.target.value })}
                          />
                        </div>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.password")}</label>
                          <input
                            className="input"
                            type="password"
                            value={activeCamera.password ?? ""}
                            onChange={(event) => updateCamera(activeCamera.id, { password: event.target.value })}
                          />
                        </div>
                      </div>

                      <div className="rowWrap" style={{ gap: 10, alignItems: "center" }}>
                        <button
                          className="chipButton"
                          type="button"
                          disabled={onvifLoading || !(activeCamera.onvif?.xaddr ?? "").trim()}
                          onClick={() => void discoverOnvifProfiles(activeCamera)}
                        >
                          {onvifLoading
                            ? t("ext.cameras.settings.onvif_discovering")
                            : t("ext.cameras.settings.onvif_discover")}
                        </button>

                        {onvifStreamLoading ? (
                          <div className="cardMeta">{t("ext.cameras.settings.onvif_discovering")}</div>
                        ) : null}
                      </div>

                      {onvifErrorMessage ? (
                        <div className="card" style={{ marginTop: 10 }}>
                          <div className="cardBody">{onvifErrorMessage}</div>
                        </div>
                      ) : null}

                      {onvifInspectResult?.warnings?.length ? (
                        <div className="card" style={{ marginTop: 10 }}>
                          <div className="cardBody">
                            {onvifInspectResult.warnings.map((warning, index) => (
                              <div key={index}>{warning}</div>
                            ))}
                          </div>
                        </div>
                      ) : null}

                      {onvifInspectResult?.profiles?.length ? (
                        <div className="field" style={{ marginTop: 12 }}>
                          <label className="label">{t("ext.cameras.settings.onvif_profile")}</label>
                          <select
                            className="input"
                            value={activeCamera.onvif?.profile_token ?? ""}
                            onChange={(event) => {
                              const token = event.target.value;
                              const profile =
                                (onvifInspectResult?.profiles ?? []).find((item) => item.token === token) ?? null;
                              if (!profile) return;
                              updateCameraOnvif(activeCamera.id, {
                                profile_token: profile.token,
                                profile_name: profile.name?.trim() ?? "",
                              });
                              void applyOnvifProfile(activeCamera, profile);
                            }}
                          >
                            <option value="">{t("ext.cameras.editor.select_placeholder", {}, "Select…")}</option>
                            {(onvifInspectResult?.profiles ?? []).map((profile) => {
                              const parts = [];
                              if (profile.name) parts.push(profile.name);
                              if (profile.width && profile.height) parts.push(`${profile.width}×${profile.height}`);
                              if (profile.encoding) parts.push(profile.encoding);
                              const label = parts.join(" • ") || profile.token;
                              return (
                                <option key={profile.token} value={profile.token}>
                                  {label}
                                </option>
                              );
                            })}
                          </select>
                          <div className="label">{t("ext.cameras.settings.onvif_profile_hint")}</div>
                        </div>
                      ) : null}

                      <div className="field">
                        <label className="label">{t("ext.cameras.settings.onvif_rtsp_from_onvif")}</label>
                        <input
                          className="input"
                          value={activeCamera.rtsp_url}
                          onChange={(event) => updateCamera(activeCamera.id, { rtsp_url: event.target.value })}
                          placeholder="rtsp://..."
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="field">
                        <label className="label">{t("ext.cameras.settings.camera_url")}</label>
                        <input
                          className="input"
                          value={activeCamera.rtsp_url}
                          onChange={(event) => updateCamera(activeCamera.id, { rtsp_url: event.target.value })}
                          placeholder="rtsp://..."
                        />
                      </div>

                      <div className="rowWrap" style={{ gap: 10 }}>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.username")}</label>
                          <input
                            className="input"
                            value={activeCamera.username ?? ""}
                            onChange={(event) => updateCamera(activeCamera.id, { username: event.target.value })}
                          />
                        </div>
                        <div className="field" style={{ flex: 1, minWidth: 220 }}>
                          <label className="label">{t("ext.cameras.settings.password")}</label>
                          <input
                            className="input"
                            type="password"
                            value={activeCamera.password ?? ""}
                            onChange={(event) => updateCamera(activeCamera.id, { password: event.target.value })}
                          />
                        </div>
                      </div>
                    </>
                  )}

                  <div className="rowWrap" style={{ gap: 10 }}>
                    <div className="field" style={{ flex: 1, minWidth: 220 }}>
                      <label className="label">{t("ext.cameras.settings.camera_fps")}</label>
                      <input
                        className="input"
                        type="number"
                        min={1}
                        max={60}
                        step={1}
                        value={Number.isFinite(activeCamera.fps) ? activeCamera.fps : 5}
                        onChange={(event) => {
                          const parsed = event.target.value ? Number(event.target.value) : NaN;
                          const nextFps = Number.isFinite(parsed) ? clamp(parsed, 1, 60) : 5;
                          updateCamera(activeCamera.id, { fps: nextFps });
                        }}
                      />
                      <div className="label">{t("ext.cameras.settings.camera_fps_hint")}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <SubModal open={snapshotModalOpen} title={snapshotTitle || t("ext.cameras.settings.snapshot")} onClose={closeSnapshotModal}>
        {snapshotErrorMessage ? (
          <div className="card">
            <div className="cardBody">{snapshotErrorMessage}</div>
          </div>
        ) : snapshotUrl ? (
          <img
            src={snapshotUrl}
            alt={snapshotTitle}
            style={{
              width: "100%",
              borderRadius: 14,
              border: "1px solid rgba(255,255,255,0.14)",
              background: "rgba(0,0,0,0.35)",
            }}
          />
        ) : (
          <div className="card">
            <div className="cardBody">{snapshotLoading ? t("ext.cameras.settings.snapshot_loading") : t("ext.cameras.settings.snapshot")}</div>
          </div>
        )}
      </SubModal>

      {activeCamera ? (
        <CameraPipelineWizardModal open={wizardOpen} camera={activeCamera} i18n={i18n} onClose={() => setWizardOpen(false)} />
      ) : null}
    </div>
  );
}
