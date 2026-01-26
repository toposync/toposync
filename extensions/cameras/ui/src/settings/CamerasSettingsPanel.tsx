import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n, SettingsPanel } from "@toposync/plugin-api";

import { fetchRtspSnapshot } from "../api/camerasApi";
import { CAMERAS_EXTENSION_ID } from "../constants";
import { createUniqueId, parseCameras, parseProcessingServers } from "../parsing";
import type { CameraConfig, CameraDetection, ProcessingServer } from "../types";
import { SubModal } from "../ui/SubModal";

import { CameraDetectionsModal } from "./CameraDetectionsModal";

type SettingsTab = "cameras" | "servers";

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

  const processingServers = useMemo(() => parseProcessingServers(settings), [settings]);
  const cameras = useMemo(() => parseCameras(settings), [settings]);

  const [activeTab, setActiveTab] = useState<SettingsTab>("cameras");
  const [cameraQuery, setCameraQuery] = useState("");
  const [serverQuery, setServerQuery] = useState("");

  const [activeCameraId, setActiveCameraId] = useState<string | null>(null);
  const [activeServerId, setActiveServerId] = useState<string | null>(null);

  const [confirmDeleteCameraId, setConfirmDeleteCameraId] = useState<string | null>(null);
  const [confirmDeleteServerId, setConfirmDeleteServerId] = useState<string | null>(null);

  const [snapshotModalOpen, setSnapshotModalOpen] = useState(false);
  const [snapshotTitle, setSnapshotTitle] = useState("");
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const snapshotAbortRef = React.useRef<AbortController | null>(null);

  const [detectionsModalOpen, setDetectionsModalOpen] = useState(false);
  const [detectionsCameraId, setDetectionsCameraId] = useState<string | null>(null);

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
    if (activeCameraId && cameras.some((camera) => camera.id === activeCameraId)) return;
    setActiveCameraId(cameras[0]?.id ?? null);
  }, [activeCameraId, cameras]);

  useEffect(() => {
    if (activeServerId && processingServers.some((server) => server.id === activeServerId)) return;
    setActiveServerId(processingServers[0]?.id ?? null);
  }, [activeServerId, processingServers]);

  const filteredCameras = useMemo(() => {
    const q = normalizeQuery(cameraQuery);
    if (!q) return cameras;
    return cameras.filter((camera) => includesQuery(camera.name || "", q) || includesQuery(camera.id, q) || includesQuery(camera.rtsp_url, q));
  }, [cameraQuery, cameras]);

  const filteredServers = useMemo(() => {
    const q = normalizeQuery(serverQuery);
    if (!q) return processingServers;
    return processingServers.filter(
      (server) => includesQuery(server.name || "", q) || includesQuery(server.id, q) || includesQuery(server.url, q),
    );
  }, [processingServers, serverQuery]);

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

  function updateCamera(cameraId: string, patch: Partial<CameraConfig>): void {
    updateSettings({ cameras: cameras.map((camera) => (camera.id === cameraId ? { ...camera, ...patch } : camera)) });
  }

  function updateCameraDetections(cameraId: string, detections: CameraDetection[]): void {
    updateCamera(cameraId, { detections });
  }

  function addCamera(): void {
    const id = createUniqueId();
    const next: CameraConfig = {
      id,
      name: "",
      connection_type: "rtsp",
      rtsp_url: "",
      username: "",
      password: "",
      fps: 5,
      processing_server_id: "",
      detections: [],
    };
    updateSettings({ cameras: [next, ...cameras] });
    setActiveCameraId(id);
    setConfirmDeleteCameraId(null);
  }

  function deleteCamera(cameraId: string): void {
    updateSettings({ cameras: cameras.filter((camera) => camera.id !== cameraId) });
    setConfirmDeleteCameraId(null);
    if (activeCameraId === cameraId) setActiveCameraId(null);
  }

  function updateProcessingServer(serverId: string, patch: Partial<ProcessingServer>): void {
    updateSettings({
      processing_servers: processingServers.map((server) => (server.id === serverId ? { ...server, ...patch } : server)),
    });
  }

  function addProcessingServer(): void {
    const id = createUniqueId();
    const next: ProcessingServer = { id, name: "", url: "", username: "", password: "" };
    updateSettings({ processing_servers: [next, ...processingServers] });
    setActiveServerId(id);
    setConfirmDeleteServerId(null);
  }

  function deleteProcessingServer(serverId: string): void {
    updateSettings({
      processing_servers: processingServers.filter((server) => server.id !== serverId),
      cameras: cameras.map((camera) =>
        camera.processing_server_id === serverId ? { ...camera, processing_server_id: "" } : camera,
      ),
    });
    setConfirmDeleteServerId(null);
    if (activeServerId === serverId) setActiveServerId(null);
  }

  function openDetectionsModal(cameraId: string): void {
    setDetectionsCameraId(cameraId);
    setDetectionsModalOpen(true);
  }

  function closeDetectionsModal(): void {
    setDetectionsModalOpen(false);
  }

  const activeCamera = activeCameraId ? cameras.find((camera) => camera.id === activeCameraId) ?? null : null;
  const activeServer = activeServerId ? processingServers.find((server) => server.id === activeServerId) ?? null : null;

  const detectionsCamera =
    detectionsCameraId ? cameras.find((camera) => camera.id === detectionsCameraId) ?? null : null;

  return (
    <div>
      <div className="card">
        <div className="cardBody">{t("ext.cameras.settings.notice")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="settingsTabBar">
        <button
          type="button"
          className={["settingsTab", activeTab === "cameras" ? "isSelected" : ""].filter(Boolean).join(" ")}
          onClick={() => setActiveTab("cameras")}
        >
          <i className="fa-solid fa-video" aria-hidden="true" />
          <span>{t("ext.cameras.settings.cameras")}</span>
        </button>
        <button
          type="button"
          className={["settingsTab", activeTab === "servers" ? "isSelected" : ""].filter(Boolean).join(" ")}
          onClick={() => setActiveTab("servers")}
        >
          <i className="fa-solid fa-server" aria-hidden="true" />
          <span>{t("ext.cameras.settings.processing")}</span>
        </button>
      </div>

      <div className="sectionDivider" />

      {activeTab === "cameras" ? (
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
                  const meta = camera.rtsp_url.trim() || t("ext.cameras.settings.missing_rtsp_url", {}, "RTSP URL missing");
                  const rulesCount = camera.detections?.length ?? 0;
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
                        {rulesCount > 0 ? <span className="pillBadge">{rulesCount}</span> : null}
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
                  <div style={{ marginBottom: 10 }}>
                    {t("ext.cameras.settings.select_camera", {}, "Select a camera to edit.")}
                  </div>
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
                      <select className="input" value="rtsp" disabled>
                        <option value="rtsp">{t("ext.cameras.settings.camera_type_rtsp")}</option>
                      </select>
                    </div>

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

                      <div className="field" style={{ flex: 1, minWidth: 220 }}>
                        <label className="label">{t("ext.cameras.settings.processing_server")}</label>
                        <select
                          className="input"
                          value={activeCamera.processing_server_id ?? ""}
                          onChange={(event) => updateCamera(activeCamera.id, { processing_server_id: event.target.value })}
                        >
                          <option value="">{t("ext.cameras.settings.none")}</option>
                          {processingServers.map((processingServer) => (
                            <option key={processingServer.id} value={processingServer.id}>
                              {processingServer.name || processingServer.url || processingServer.id}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="sectionDivider" />

                <div className="card">
                  <div className="cardBody">
                    <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center", gap: 10 }}>
                      <div>
                        <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                          {t("ext.cameras.settings.detections")}
                        </div>
                        <div className="label">
                          {t(
                            "ext.cameras.settings.detections_hint",
                            { count: activeCamera.detections?.length ?? 0 },
                            "Object detection and motion rules for this camera.",
                          )}
                        </div>
                      </div>
                      <button className="primaryButton" type="button" onClick={() => openDetectionsModal(activeCamera.id)}>
                        {t("ext.cameras.settings.edit_detections", {}, "Edit rules")}
                        {(activeCamera.detections?.length ?? 0) > 0 ? (
                          <span style={{ marginLeft: 8, opacity: 0.85 }}>
                            ({activeCamera.detections?.length ?? 0})
                          </span>
                        ) : null}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="settingsSplit">
          <div className="settingsSplitSidebar">
            <div className="settingsSplitToolbar">
              <input
                className="input"
                placeholder={t("ext.cameras.settings.search_servers", {}, "Search servers…")}
                value={serverQuery}
                onChange={(event) => setServerQuery(event.target.value)}
              />
              <button
                className="iconButton iconButtonPrimary"
                type="button"
                aria-label={t("ext.cameras.settings.add_server")}
                onClick={addProcessingServer}
              >
                <i className="fa-solid fa-plus" aria-hidden="true" />
              </button>
            </div>

            {filteredServers.length === 0 ? (
              <div className="card" style={{ marginTop: 10 }}>
                <div className="cardBody">
                  <div style={{ marginBottom: 10 }}>{t("ext.cameras.settings.empty_servers")}</div>
                  <button className="primaryButton" type="button" onClick={addProcessingServer}>
                    <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.cameras.settings.add_server")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="settingsList">
                {filteredServers.map((server) => {
                  const selected = server.id === activeServerId;
                  const name = server.name.trim() || t("ext.cameras.settings.unnamed_server", {}, "Untitled server");
                  const meta = server.url.trim() || t("ext.cameras.settings.missing_server_url", {}, "URL missing");
                  return (
                    <button
                      key={server.id}
                      type="button"
                      className={["choiceItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                      onClick={() => {
                        setActiveServerId(server.id);
                        setConfirmDeleteServerId(null);
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
            {!activeServer ? (
              <div className="card">
                <div className="cardBody">
                  <div style={{ marginBottom: 10 }}>
                    {t("ext.cameras.settings.select_server", {}, "Select a server to edit.")}
                  </div>
                  <button className="primaryButton" type="button" onClick={addProcessingServer}>
                    <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.cameras.settings.add_server")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="settingsDetail">
                <div className="settingsDetailHeader">
                  <div>
                    <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                      {activeServer.name.trim() || t("ext.cameras.settings.unnamed_server", {}, "Untitled server")}
                    </div>
                    <div className="cardMeta">ID: {activeServer.id}</div>
                  </div>

                  <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                    <button
                      className={confirmDeleteServerId === activeServer.id ? "dangerButton" : "iconButton iconButtonDanger"}
                      type="button"
                      aria-label={t("core.actions.delete")}
                      title={t("core.actions.delete")}
                      onClick={() => {
                        if (confirmDeleteServerId === activeServer.id) {
                          deleteProcessingServer(activeServer.id);
                          return;
                        }
                        setConfirmDeleteServerId(activeServer.id);
                      }}
                    >
                      {confirmDeleteServerId === activeServer.id ? (
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
                      <label className="label">{t("ext.cameras.settings.server_name")}</label>
                      <input
                        className="input"
                        value={activeServer.name}
                        onChange={(event) => updateProcessingServer(activeServer.id, { name: event.target.value })}
                      />
                    </div>

                    <div className="field">
                      <label className="label">{t("ext.cameras.settings.server_url")}</label>
                      <input
                        className="input"
                        value={activeServer.url}
                        onChange={(event) => updateProcessingServer(activeServer.id, { url: event.target.value })}
                      />
                    </div>

                    <div className="rowWrap" style={{ gap: 10 }}>
                      <div className="field" style={{ flex: 1, minWidth: 220 }}>
                        <label className="label">{t("ext.cameras.settings.username")}</label>
                        <input
                          className="input"
                          value={activeServer.username ?? ""}
                          onChange={(event) => updateProcessingServer(activeServer.id, { username: event.target.value })}
                        />
                      </div>
                      <div className="field" style={{ flex: 1, minWidth: 220 }}>
                        <label className="label">{t("ext.cameras.settings.password")}</label>
                        <input
                          className="input"
                          type="password"
                          value={activeServer.password ?? ""}
                          onChange={(event) => updateProcessingServer(activeServer.id, { password: event.target.value })}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

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

      <CameraDetectionsModal
        open={detectionsModalOpen}
        onClose={closeDetectionsModal}
        i18n={i18n}
        cameraLabel={detectionsCamera?.name || detectionsCamera?.id || ""}
        initialDetections={detectionsCamera?.detections ?? []}
        onSave={(next) => {
          if (!detectionsCameraId) return;
          updateCameraDetections(detectionsCameraId, next);
        }}
      />
    </div>
  );
}
