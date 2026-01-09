import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n, SettingsPanel } from "@toposync/plugin-api";

import { fetchRtspSnapshot } from "../api/camerasApi";
import { CAMERAS_EXTENSION_ID } from "../constants";
import { createUniqueId, parseCameras, parseProcessingServers } from "../parsing";
import type { CameraConfig, ProcessingServer } from "../types";
import { SubModal } from "../ui/SubModal";

import { CameraDetectionsModal } from "./CameraDetectionsModal";

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

  const processingServersFromSettings = useMemo(() => parseProcessingServers(settings), [settings]);
  const camerasFromSettings = useMemo(() => parseCameras(settings), [settings]);

  const [activeSection, setActiveSection] = useState<"servers" | "cameras">("cameras");
  const [draftProcessingServers, setDraftProcessingServers] = useState<ProcessingServer[]>(processingServersFromSettings);
  const [draftCameras, setDraftCameras] = useState<CameraConfig[]>(camerasFromSettings);
  const [processingServersDirty, setProcessingServersDirty] = useState(false);
  const [camerasDirty, setCamerasDirty] = useState(false);

  const [snapshotModalOpen, setSnapshotModalOpen] = useState(false);
  const [snapshotTitle, setSnapshotTitle] = useState("");
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);

  const [detectionsModalOpen, setDetectionsModalOpen] = useState(false);
  const [detectionsCameraId, setDetectionsCameraId] = useState<string | null>(null);

  useEffect(() => {
    if (!processingServersDirty) setDraftProcessingServers(processingServersFromSettings);
  }, [processingServersDirty, processingServersFromSettings]);

  useEffect(() => {
    if (!camerasDirty) setDraftCameras(camerasFromSettings);
  }, [camerasDirty, camerasFromSettings]);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  function openSnapshotModal(title: string) {
    setSnapshotTitle(title);
    setSnapshotErrorMessage(null);
    setSnapshotLoading(false);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
    setSnapshotModalOpen(true);
  }

  function closeSnapshotModal() {
    setSnapshotModalOpen(false);
    setSnapshotTitle("");
    setSnapshotErrorMessage(null);
    setSnapshotLoading(false);
    setSnapshotUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return null;
    });
  }

  function openDetectionsModal(cameraId: string) {
    setDetectionsCameraId(cameraId);
    setDetectionsModalOpen(true);
  }

  function closeDetectionsModal() {
    setDetectionsModalOpen(false);
  }

  async function testCameraConnection(camera: CameraConfig) {
    setSnapshotLoading(true);
    setSnapshotErrorMessage(null);
    try {
      const blob = await fetchRtspSnapshot({ url: camera.rtsp_url, username: camera.username, password: camera.password });
      const url = URL.createObjectURL(blob);
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return url;
      });
    } catch (error) {
      setSnapshotErrorMessage(error instanceof Error ? error.message : String(error));
      setSnapshotUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return null;
      });
    } finally {
      setSnapshotLoading(false);
    }
  }

  function renderProcessingServers(): React.ReactElement {
    return (
      <div>
        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
          <div>
            <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
              {t("ext.cameras.settings.processing")}
            </div>
            {processingServersDirty ? <div className="label">{t("ext.cameras.settings.unsaved")}</div> : null}
          </div>

          <div className="row" style={{ gap: 10 }}>
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.cameras.settings.add_server")}
              onClick={() => {
                setDraftProcessingServers((previous) => [
                  { id: createUniqueId(), name: "", url: "", username: "", password: "" },
                  ...previous,
                ]);
                setProcessingServersDirty(true);
              }}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>

            <button
              className="primaryButton"
              type="button"
              disabled={!processingServersDirty}
              onClick={() => {
                updateSettings({ processing_servers: draftProcessingServers });
                setProcessingServersDirty(false);
              }}
            >
              {t("core.actions.save")}
            </button>

            <button
              className="chipButton"
              type="button"
              disabled={!processingServersDirty}
              onClick={() => {
                setDraftProcessingServers(processingServersFromSettings);
                setProcessingServersDirty(false);
              }}
            >
              {t("core.actions.cancel")}
            </button>
          </div>
        </div>

        <div className="sectionDivider" />

        {draftProcessingServers.length === 0 ? (
          <div className="card">
            <div className="cardBody">{t("ext.cameras.settings.empty_servers")}</div>
          </div>
        ) : (
          <div className="choiceList">
            {draftProcessingServers.map((processingServer) => (
              <div className="card" key={processingServer.id}>
                <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
                  <div className="label" style={{ margin: 0 }}>
                    ID: {processingServer.id}
                  </div>
                  <button
                    className="iconButton iconButtonDanger"
                    type="button"
                    onClick={() => {
                      setDraftProcessingServers((previous) => previous.filter((server) => server.id !== processingServer.id));
                      setDraftCameras((previous) =>
                        previous.map((camera) =>
                          camera.processing_server_id === processingServer.id ? { ...camera, processing_server_id: "" } : camera,
                        ),
                      );
                      setProcessingServersDirty(true);
                      setCamerasDirty(true);
                    }}
                    aria-label={t("core.actions.delete")}
                  >
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                  </button>
                </div>

                <div className="sectionDivider" />

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.server_name")}</label>
                  <input
                    className="input"
                    value={processingServer.name}
                    onChange={(event) => {
                      const nextName = event.target.value;
                      setDraftProcessingServers((previous) =>
                        previous.map((server) => (server.id === processingServer.id ? { ...server, name: nextName } : server)),
                      );
                      setProcessingServersDirty(true);
                    }}
                  />
                </div>

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.server_url")}</label>
                  <input
                    className="input"
                    value={processingServer.url}
                    onChange={(event) => {
                      const nextUrl = event.target.value;
                      setDraftProcessingServers((previous) =>
                        previous.map((server) => (server.id === processingServer.id ? { ...server, url: nextUrl } : server)),
                      );
                      setProcessingServersDirty(true);
                    }}
                  />
                </div>

                <div className="rowWrap" style={{ gap: 10 }}>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.username")}</label>
                    <input
                      className="input"
                      value={processingServer.username ?? ""}
                      onChange={(event) => {
                        const nextUsername = event.target.value;
                        setDraftProcessingServers((previous) =>
                          previous.map((server) =>
                            server.id === processingServer.id ? { ...server, username: nextUsername } : server,
                          ),
                        );
                        setProcessingServersDirty(true);
                      }}
                    />
                  </div>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.password")}</label>
                    <input
                      className="input"
                      type="password"
                      value={processingServer.password ?? ""}
                      onChange={(event) => {
                        const nextPassword = event.target.value;
                        setDraftProcessingServers((previous) =>
                          previous.map((server) =>
                            server.id === processingServer.id ? { ...server, password: nextPassword } : server,
                          ),
                        );
                        setProcessingServersDirty(true);
                      }}
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  function renderCameras(): React.ReactElement {
    const servers = draftProcessingServers;
    return (
      <div>
        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
          <div>
            <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
              {t("ext.cameras.settings.cameras")}
            </div>
            {camerasDirty ? <div className="label">{t("ext.cameras.settings.unsaved")}</div> : null}
          </div>

          <div className="row" style={{ gap: 10 }}>
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.cameras.settings.add_camera")}
              onClick={() => {
                setDraftCameras((previous) => [
                  {
                    id: createUniqueId(),
                    name: "",
                    connection_type: "rtsp",
                    rtsp_url: "",
                    username: "",
                    password: "",
                    fps: 5,
                    processing_server_id: "",
                    detections: [],
                  },
                  ...previous,
                ]);
                setCamerasDirty(true);
              }}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>

            <button
              className="primaryButton"
              type="button"
              disabled={!camerasDirty}
              onClick={() => {
                updateSettings({ cameras: draftCameras });
                setCamerasDirty(false);
              }}
            >
              {t("core.actions.save")}
            </button>

            <button
              className="chipButton"
              type="button"
              disabled={!camerasDirty}
              onClick={() => {
                setDraftCameras(camerasFromSettings);
                setCamerasDirty(false);
              }}
            >
              {t("core.actions.cancel")}
            </button>
          </div>
        </div>

        <div className="sectionDivider" />

        {draftCameras.length === 0 ? (
          <div className="card">
            <div className="cardBody">{t("ext.cameras.settings.empty_cameras")}</div>
          </div>
        ) : (
          <div className="choiceList">
            {draftCameras.map((camera) => (
              <div className="card" key={camera.id}>
                <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
                  <div className="label" style={{ margin: 0 }}>
                    ID: {camera.id}
                  </div>
                  <div className="row" style={{ gap: 10 }}>
                    <button
                      className={["iconButton", (camera.detections?.length ?? 0) > 0 ? "iconButtonPrimary" : ""].join(" ")}
                      type="button"
                      onClick={() => openDetectionsModal(camera.id)}
                      aria-label={t("ext.cameras.settings.detections")}
                      title={
                        (camera.detections?.length ?? 0) > 0
                          ? `${t("ext.cameras.settings.detections")} (${camera.detections?.length ?? 0})`
                          : t("ext.cameras.settings.detections")
                      }
                    >
                      <i className="fa-solid fa-bullseye" aria-hidden="true" />
                    </button>
                    <button
                      className="chipButton"
                      type="button"
                      disabled={snapshotLoading || !camera.rtsp_url.trim()}
                      onClick={() => {
                        openSnapshotModal(
                          camera.name ? `${t("ext.cameras.settings.snapshot")}: ${camera.name}` : t("ext.cameras.settings.snapshot"),
                        );
                        void testCameraConnection(camera);
                      }}
                    >
                      {snapshotLoading ? t("ext.cameras.settings.testing") : t("ext.cameras.settings.test")}
                    </button>
                    <button
                      className="iconButton iconButtonDanger"
                      type="button"
                      onClick={() => {
                        setDraftCameras((previous) => previous.filter((existing) => existing.id !== camera.id));
                        setCamerasDirty(true);
                      }}
                      aria-label={t("core.actions.delete")}
                    >
                      <i className="fa-solid fa-trash" aria-hidden="true" />
                    </button>
                  </div>
                </div>

                <div className="sectionDivider" />

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.camera_name")}</label>
                  <input
                    className="input"
                    value={camera.name}
                    onChange={(event) => {
                      const nextName = event.target.value;
                      setDraftCameras((previous) =>
                        previous.map((existing) => (existing.id === camera.id ? { ...existing, name: nextName } : existing)),
                      );
                      setCamerasDirty(true);
                    }}
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
                    value={camera.rtsp_url}
                    onChange={(event) => {
                      const nextUrl = event.target.value;
                      setDraftCameras((previous) =>
                        previous.map((existing) => (existing.id === camera.id ? { ...existing, rtsp_url: nextUrl } : existing)),
                      );
                      setCamerasDirty(true);
                    }}
                    placeholder="rtsp://..."
                  />
                </div>

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.camera_fps")}</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={60}
                    step={1}
                    value={Number.isFinite(camera.fps) ? camera.fps : 5}
                    onChange={(event) => {
                      const rawValue = event.target.value;
                      const parsed = rawValue ? Number(rawValue) : NaN;
                      const nextFps = Number.isFinite(parsed) ? Math.max(1, Math.min(60, parsed)) : 5;
                      setDraftCameras((previous) =>
                        previous.map((existing) => (existing.id === camera.id ? { ...existing, fps: nextFps } : existing)),
                      );
                      setCamerasDirty(true);
                    }}
                  />
                  <div className="label">{t("ext.cameras.settings.camera_fps_hint")}</div>
                </div>

                <div className="rowWrap" style={{ gap: 10 }}>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.username")}</label>
                    <input
                      className="input"
                      value={camera.username ?? ""}
                      onChange={(event) => {
                        const nextUsername = event.target.value;
                        setDraftCameras((previous) =>
                          previous.map((existing) =>
                            existing.id === camera.id ? { ...existing, username: nextUsername } : existing,
                          ),
                        );
                        setCamerasDirty(true);
                      }}
                    />
                  </div>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.password")}</label>
                    <input
                      className="input"
                      type="password"
                      value={camera.password ?? ""}
                      onChange={(event) => {
                        const nextPassword = event.target.value;
                        setDraftCameras((previous) =>
                          previous.map((existing) =>
                            existing.id === camera.id ? { ...existing, password: nextPassword } : existing,
                          ),
                        );
                        setCamerasDirty(true);
                      }}
                    />
                  </div>
                </div>

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.processing_server")}</label>
                  <select
                    className="input"
                    value={camera.processing_server_id ?? ""}
                    onChange={(event) => {
                      const nextProcessingServerId = event.target.value;
                      setDraftCameras((previous) =>
                        previous.map((existing) =>
                          existing.id === camera.id ? { ...existing, processing_server_id: nextProcessingServerId } : existing,
                        ),
                      );
                      setCamerasDirty(true);
                    }}
                  >
                    <option value="">{t("ext.cameras.settings.none")}</option>
                    {servers.map((processingServer) => (
                      <option key={processingServer.id} value={processingServer.id}>
                        {processingServer.name || processingServer.url || processingServer.id}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  const detectionsCamera = detectionsCameraId ? draftCameras.find((camera) => camera.id === detectionsCameraId) ?? null : null;

  return (
    <div>
      <div className="card">
        <div className="cardBody">{t("ext.cameras.settings.notice")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap" style={{ gap: 12, alignItems: "stretch" }}>
        <div style={{ width: 220, minWidth: 220 }}>
          <div className="choiceList">
            <button
              type="button"
              className={["choiceItem", activeSection === "cameras" ? "isSelected" : ""].join(" ")}
              onClick={() => setActiveSection("cameras")}
            >
              <span className="row" style={{ gap: 10 }}>
                <i className="fa-solid fa-video" aria-hidden="true" />
                <span>{t("ext.cameras.settings.cameras")}</span>
              </span>
            </button>
            <button
              type="button"
              className={["choiceItem", activeSection === "servers" ? "isSelected" : ""].join(" ")}
              onClick={() => setActiveSection("servers")}
            >
              <span className="row" style={{ gap: 10 }}>
                <i className="fa-solid fa-server" aria-hidden="true" />
                <span>{t("ext.cameras.settings.processing")}</span>
              </span>
            </button>
          </div>
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>{activeSection === "servers" ? renderProcessingServers() : renderCameras()}</div>
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
            <div className="cardBody">
              {snapshotLoading ? t("ext.cameras.settings.snapshot_loading") : t("ext.cameras.settings.snapshot")}
            </div>
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
          setDraftCameras((previous) =>
            previous.map((camera) => (camera.id === detectionsCameraId ? { ...camera, detections: next } : camera)),
          );
          setCamerasDirty(true);
        }}
      />
    </div>
  );
}
