import React, { useEffect, useMemo, useState } from "react";

import type { HostI18n, SettingsPanel } from "@toposync/plugin-api";

import { fetchRtspSnapshot } from "../api/camerasApi";
import { CAMERAS_EXTENSION_ID } from "../constants";
import { createUniqueId, parseCameras } from "../parsing";
import type { CameraConfig } from "../types";
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

  const [cameraQuery, setCameraQuery] = useState("");

  const [activeCameraId, setActiveCameraId] = useState<string | null>(null);

  const [confirmDeleteCameraId, setConfirmDeleteCameraId] = useState<string | null>(null);

  const [snapshotModalOpen, setSnapshotModalOpen] = useState(false);
  const [snapshotTitle, setSnapshotTitle] = useState("");
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErrorMessage, setSnapshotErrorMessage] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const snapshotAbortRef = React.useRef<AbortController | null>(null);

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
    if (activeCameraId && cameras.some((camera) => camera.id === activeCameraId)) return;
    setActiveCameraId(cameras[0]?.id ?? null);
  }, [activeCameraId, cameras]);

  const filteredCameras = useMemo(() => {
    const q = normalizeQuery(cameraQuery);
    if (!q) return cameras;
    return cameras.filter((camera) => includesQuery(camera.name || "", q) || includesQuery(camera.id, q) || includesQuery(camera.rtsp_url, q));
  }, [cameraQuery, cameras]);

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

  const activeCamera = activeCameraId ? cameras.find((camera) => camera.id === activeCameraId) ?? null : null;

  return (
    <div>
      <div className="card">
        <div className="cardBody">{t("ext.cameras.settings.notice")}</div>
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
                const meta = camera.rtsp_url.trim() || t("ext.cameras.settings.missing_rtsp_url", {}, "RTSP URL missing");
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
