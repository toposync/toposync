import React, { useEffect, useMemo, useState } from "react";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  ElementType,
  HostI18n,
  PlanePoint,
  SettingsPanel,
  TopoSyncHost,
} from "@toposync/plugin-api";

const EXTENSION_ID = "com.toposync.cameras";
const ELEMENT_TYPE_ID = "com.toposync.cameras.camera";
const TOOL_ID_ADD = "com.toposync.cameras.tool.add";

type ProcessingServer = {
  id: string;
  name: string;
  url: string;
  username?: string;
  password?: string;
};

type CameraConfig = {
  id: string;
  name: string;
  connection_type: "rtsp";
  rtsp_url: string;
  username?: string;
  password?: string;
  processing_server_id?: string;
};

type CamerasIndex = {
  processing_servers: Array<{ id: string; name: string; url: string }>;
  cameras: Array<{ id: string; name: string; connection_type: string; processing_server_id?: string }>;
};

function roundRectPath(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const anyCtx = ctx as unknown as { roundRect?: (x: number, y: number, w: number, h: number, r: number) => void };
  if (typeof anyCtx.roundRect === "function") {
    anyCtx.roundRect(x, y, w, h, r);
    return;
  }

  const radius = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
}

function asString(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function newId(): string {
  const cryptoAny = crypto as unknown as { randomUUID?: () => string };
  return cryptoAny.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readProcessingServers(settings: Record<string, unknown>): ProcessingServer[] {
  const raw = settings.processing_servers;
  if (!Array.isArray(raw)) return [];
  const out: ProcessingServer[] = [];
  for (const item of raw) {
    const rec = asRecord(item);
    const id = asString(rec.id).trim();
    if (!id) continue;
    out.push({
      id,
      name: asString(rec.name).trim(),
      url: asString(rec.url).trim(),
      username: asString(rec.username).trim(),
      password: asString(rec.password).trim(),
    });
  }
  return out;
}

function readCameras(settings: Record<string, unknown>): CameraConfig[] {
  const raw = settings.cameras;
  if (!Array.isArray(raw)) return [];
  const out: CameraConfig[] = [];
  for (const item of raw) {
    const rec = asRecord(item);
    const id = asString(rec.id).trim();
    if (!id) continue;
    out.push({
      id,
      name: asString(rec.name).trim(),
      connection_type: "rtsp",
      rtsp_url: asString(rec.rtsp_url).trim(),
      username: asString(rec.username).trim(),
      password: asString(rec.password).trim(),
      processing_server_id: asString(rec.processing_server_id).trim(),
    });
  }
  return out;
}

async function fetchIndex(): Promise<CamerasIndex> {
  const res = await fetch("/api/cameras/index");
  if (!res.ok) throw new Error(`Failed to load cameras index: ${res.status}`);
  const data = await res.json();
  const rec = asRecord(data);
  return {
    processing_servers: Array.isArray(rec.processing_servers)
      ? (rec.processing_servers as any[]).filter(Boolean)
      : [],
    cameras: Array.isArray(rec.cameras) ? (rec.cameras as any[]).filter(Boolean) : [],
  };
}

async function fetchRtspSnapshot(opts: { url: string; username?: string; password?: string }): Promise<Blob> {
  const res = await fetch("/api/cameras/rtsp/snapshot", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url: opts.url, username: opts.username ?? "", password: opts.password ?? "" }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${res.status}`);
  }
  return res.blob();
}

async function fetchCameraSnapshot(cameraId: string): Promise<Blob> {
  const res = await fetch(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/snapshot`);
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || `Snapshot failed: ${res.status}`);
  }
  return res.blob();
}

const translations = {
  en: {
    "ext.cameras.settings.name": "Cameras",
    "ext.cameras.settings.desc": "Configure cameras and processing servers (local-first).",
    "ext.cameras.settings.processing": "Processing servers",
    "ext.cameras.settings.cameras": "Cameras",
    "ext.cameras.settings.notice": "Credentials are stored locally in Toposync configuration.",
    "ext.cameras.settings.add_server": "Add processing server",
    "ext.cameras.settings.add_camera": "Add camera",
    "ext.cameras.settings.empty_servers": "No processing servers yet.",
    "ext.cameras.settings.empty_cameras": "No cameras yet.",
    "ext.cameras.settings.unsaved": "Unsaved changes",
    "ext.cameras.settings.server_name": "Nickname",
    "ext.cameras.settings.server_url": "URL",
    "ext.cameras.settings.username": "Username (optional)",
    "ext.cameras.settings.password": "Password (optional)",
    "ext.cameras.settings.camera_name": "Nickname",
    "ext.cameras.settings.camera_type": "Connection type",
    "ext.cameras.settings.camera_type_rtsp": "RTSP",
    "ext.cameras.settings.camera_url": "RTSP URL",
    "ext.cameras.settings.processing_server": "Processing server (optional)",
    "ext.cameras.settings.none": "None",
    "ext.cameras.settings.test": "Test connection",
    "ext.cameras.settings.testing": "Testing…",
    "ext.cameras.settings.snapshot": "Snapshot",
    "ext.cameras.settings.snapshot_loading": "Loading snapshot…",
    "ext.cameras.element.name": "Camera",
    "ext.cameras.element.desc": "Camera placed in the scene; click to see a snapshot.",
    "ext.cameras.tool.add": "Camera",
    "ext.cameras.tool.add_desc": "Click to place a camera and configure it.",
    "ext.cameras.editor.camera": "Camera",
    "ext.cameras.editor.no_cameras": "Add a camera in Settings first.",
    "ext.cameras.editor.select_placeholder": "Select…",
    "ext.cameras.action.no_camera": "No camera selected.",
    "ext.cameras.action.refresh": "Refresh snapshot",
    "ext.cameras.action.loading": "Loading…",
  },
  "pt-BR": {
    "ext.cameras.settings.name": "Câmeras",
    "ext.cameras.settings.desc": "Configure câmeras e servidores de processamento (local-first).",
    "ext.cameras.settings.processing": "Servidores de processamento",
    "ext.cameras.settings.cameras": "Câmeras",
    "ext.cameras.settings.notice": "Credenciais ficam armazenadas localmente no config do Toposync.",
    "ext.cameras.settings.add_server": "Adicionar servidor de processamento",
    "ext.cameras.settings.add_camera": "Adicionar câmera",
    "ext.cameras.settings.empty_servers": "Nenhum servidor de processamento por enquanto.",
    "ext.cameras.settings.empty_cameras": "Nenhuma câmera por enquanto.",
    "ext.cameras.settings.unsaved": "Alterações não salvas",
    "ext.cameras.settings.server_name": "Apelido",
    "ext.cameras.settings.server_url": "URL",
    "ext.cameras.settings.username": "Usuário (opcional)",
    "ext.cameras.settings.password": "Senha (opcional)",
    "ext.cameras.settings.camera_name": "Apelido",
    "ext.cameras.settings.camera_type": "Tipo de conexão",
    "ext.cameras.settings.camera_type_rtsp": "RTSP",
    "ext.cameras.settings.camera_url": "URL RTSP",
    "ext.cameras.settings.processing_server": "Servidor de processamento (opcional)",
    "ext.cameras.settings.none": "Nenhum",
    "ext.cameras.settings.test": "Testar conexão",
    "ext.cameras.settings.testing": "Testando…",
    "ext.cameras.settings.snapshot": "Snapshot",
    "ext.cameras.settings.snapshot_loading": "Carregando snapshot…",
    "ext.cameras.element.name": "Câmera",
    "ext.cameras.element.desc": "Câmera na cena; clique para ver um snapshot.",
    "ext.cameras.tool.add": "Câmera",
    "ext.cameras.tool.add_desc": "Clique para posicionar uma câmera e configurar.",
    "ext.cameras.editor.camera": "Câmera",
    "ext.cameras.editor.no_cameras": "Adicione uma câmera nas Configurações primeiro.",
    "ext.cameras.editor.select_placeholder": "Selecionar…",
    "ext.cameras.action.no_camera": "Nenhuma câmera selecionada.",
    "ext.cameras.action.refresh": "Atualizar snapshot",
    "ext.cameras.action.loading": "Carregando...",
  },
} as const;

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(translations);
  host.registerSettingsPanel(settingsPanel());
  host.registerElementType(cameraElementType(host.i18n));
  host.registerEditorTool(addCameraTool(host.i18n));
}

function settingsPanel(): SettingsPanel {
  return {
    id: EXTENSION_ID,
    icon: "video",
    name: { key: "ext.cameras.settings.name", fallback: "Cameras" },
    description: { key: "ext.cameras.settings.desc" },
    render: ({ i18n, settings, updateSettings }) => (
      <CamerasSettings i18n={i18n} settings={settings} updateSettings={updateSettings} />
    ),
  };
}

function SubModal({
  title,
  open,
  onClose,
  children,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}): React.ReactElement | null {
  if (!open) return null;
  return (
    <div
      className="modalBackdrop"
      style={{ zIndex: 70 }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div
        className="modalPanel"
        style={{ width: "min(980px, calc(100vw - 28px))" }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modalHeader">
          <div className="modalTitle">{title}</div>
          <button className="iconButton" type="button" onClick={onClose} aria-label="Close">
            <i className="fa-solid fa-xmark" aria-hidden="true" />
          </button>
        </div>
        <div className="modalBody">{children}</div>
      </div>
    </div>
  );
}

function CamerasSettings({
  i18n,
  settings,
  updateSettings,
}: {
  i18n: HostI18n;
  settings: Record<string, unknown>;
  updateSettings: (patch: Record<string, unknown>) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();

  const serversFromSettings = useMemo(() => readProcessingServers(settings), [settings]);
  const camerasFromSettings = useMemo(() => readCameras(settings), [settings]);

  const [activeSection, setActiveSection] = useState<"servers" | "cameras">("cameras");
  const [draftServers, setDraftServers] = useState<ProcessingServer[]>(serversFromSettings);
  const [draftCameras, setDraftCameras] = useState<CameraConfig[]>(camerasFromSettings);
  const [dirtyServers, setDirtyServers] = useState(false);
  const [dirtyCameras, setDirtyCameras] = useState(false);

  const [snapshotModalOpen, setSnapshotModalOpen] = useState(false);
  const [snapshotTitle, setSnapshotTitle] = useState("");
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErr, setSnapshotErr] = useState<string | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);

  useEffect(() => {
    if (!dirtyServers) setDraftServers(serversFromSettings);
  }, [dirtyServers, serversFromSettings]);

  useEffect(() => {
    if (!dirtyCameras) setDraftCameras(camerasFromSettings);
  }, [dirtyCameras, camerasFromSettings]);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  function openSnapshotModal(title: string) {
    setSnapshotTitle(title);
    setSnapshotErr(null);
    setSnapshotBusy(false);
    setSnapshotUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return null;
    });
    setSnapshotModalOpen(true);
  }

  function closeSnapshotModal() {
    setSnapshotModalOpen(false);
    setSnapshotTitle("");
    setSnapshotErr(null);
    setSnapshotBusy(false);
    setSnapshotUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return null;
    });
  }

  async function testCamera(cam: CameraConfig) {
    setSnapshotBusy(true);
    setSnapshotErr(null);
    try {
      const blob = await fetchRtspSnapshot({ url: cam.rtsp_url, username: cam.username, password: cam.password });
      const url = URL.createObjectURL(blob);
      setSnapshotUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return url;
      });
    } catch (err) {
      setSnapshotErr(err instanceof Error ? err.message : String(err));
      setSnapshotUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return null;
      });
    } finally {
      setSnapshotBusy(false);
    }
  }

  function renderServers(): React.ReactElement {
    return (
      <div>
        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
          <div>
            <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
              {t("ext.cameras.settings.processing")}
            </div>
            {dirtyServers ? <div className="label">{t("ext.cameras.settings.unsaved")}</div> : null}
          </div>

          <div className="row" style={{ gap: 10 }}>
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.cameras.settings.add_server")}
              onClick={() => {
                setDraftServers((prev) => [{ id: newId(), name: "", url: "", username: "", password: "" }, ...prev]);
                setDirtyServers(true);
              }}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>

            <button
              className="primaryButton"
              type="button"
              disabled={!dirtyServers}
              onClick={() => {
                updateSettings({ processing_servers: draftServers });
                setDirtyServers(false);
              }}
            >
              {t("core.actions.save")}
            </button>

            <button
              className="chipButton"
              type="button"
              disabled={!dirtyServers}
              onClick={() => {
                setDraftServers(serversFromSettings);
                setDirtyServers(false);
              }}
            >
              {t("core.actions.cancel")}
            </button>
          </div>
        </div>

        <div className="sectionDivider" />

        {draftServers.length === 0 ? (
          <div className="card">
            <div className="cardBody">{t("ext.cameras.settings.empty_servers")}</div>
          </div>
        ) : (
          <div className="choiceList">
            {draftServers.map((srv) => (
              <div className="card" key={srv.id}>
                <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
                  <div className="label" style={{ margin: 0 }}>
                    ID: {srv.id}
                  </div>
                  <button
                    className="iconButton iconButtonDanger"
                    type="button"
                    onClick={() => {
                      setDraftServers((prev) => prev.filter((s) => s.id !== srv.id));
                      setDraftCameras((prev) =>
                        prev.map((c) =>
                          c.processing_server_id === srv.id ? { ...c, processing_server_id: "" } : c,
                        ),
                      );
                      setDirtyServers(true);
                      setDirtyCameras(true);
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
                    value={srv.name}
                    onChange={(e) => {
                      const next = e.target.value;
                      setDraftServers((prev) => prev.map((s) => (s.id === srv.id ? { ...s, name: next } : s)));
                      setDirtyServers(true);
                    }}
                  />
                </div>

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.server_url")}</label>
                  <input
                    className="input"
                    value={srv.url}
                    onChange={(e) => {
                      const next = e.target.value;
                      setDraftServers((prev) => prev.map((s) => (s.id === srv.id ? { ...s, url: next } : s)));
                      setDirtyServers(true);
                    }}
                  />
                </div>

                <div className="rowWrap" style={{ gap: 10 }}>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.username")}</label>
                    <input
                      className="input"
                      value={srv.username ?? ""}
                      onChange={(e) => {
                        const next = e.target.value;
                        setDraftServers((prev) =>
                          prev.map((s) => (s.id === srv.id ? { ...s, username: next } : s)),
                        );
                        setDirtyServers(true);
                      }}
                    />
                  </div>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.password")}</label>
                    <input
                      className="input"
                      type="password"
                      value={srv.password ?? ""}
                      onChange={(e) => {
                        const next = e.target.value;
                        setDraftServers((prev) =>
                          prev.map((s) => (s.id === srv.id ? { ...s, password: next } : s)),
                        );
                        setDirtyServers(true);
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
    const servers = draftServers;
    return (
      <div>
        <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
          <div>
            <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
              {t("ext.cameras.settings.cameras")}
            </div>
            {dirtyCameras ? <div className="label">{t("ext.cameras.settings.unsaved")}</div> : null}
          </div>

          <div className="row" style={{ gap: 10 }}>
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.cameras.settings.add_camera")}
              onClick={() => {
                setDraftCameras((prev) => [
                  {
                    id: newId(),
                    name: "",
                    connection_type: "rtsp",
                    rtsp_url: "",
                    username: "",
                    password: "",
                    processing_server_id: "",
                  },
                  ...prev,
                ]);
                setDirtyCameras(true);
              }}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>

            <button
              className="primaryButton"
              type="button"
              disabled={!dirtyCameras}
              onClick={() => {
                updateSettings({ cameras: draftCameras });
                setDirtyCameras(false);
              }}
            >
              {t("core.actions.save")}
            </button>

            <button
              className="chipButton"
              type="button"
              disabled={!dirtyCameras}
              onClick={() => {
                setDraftCameras(camerasFromSettings);
                setDirtyCameras(false);
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
            {draftCameras.map((cam) => (
              <div className="card" key={cam.id}>
                <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
                  <div className="label" style={{ margin: 0 }}>
                    ID: {cam.id}
                  </div>
                  <div className="row" style={{ gap: 10 }}>
                    <button
                      className="chipButton"
                      type="button"
                      disabled={snapshotBusy || !cam.rtsp_url.trim()}
                      onClick={() => {
                        openSnapshotModal(cam.name ? `${t("ext.cameras.settings.snapshot")}: ${cam.name}` : t("ext.cameras.settings.snapshot"));
                        void testCamera(cam);
                      }}
                    >
                      {snapshotBusy ? t("ext.cameras.settings.testing") : t("ext.cameras.settings.test")}
                    </button>
                    <button
                      className="iconButton iconButtonDanger"
                      type="button"
                      onClick={() => {
                        setDraftCameras((prev) => prev.filter((c) => c.id !== cam.id));
                        setDirtyCameras(true);
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
                    value={cam.name}
                    onChange={(e) => {
                      const next = e.target.value;
                      setDraftCameras((prev) => prev.map((c) => (c.id === cam.id ? { ...c, name: next } : c)));
                      setDirtyCameras(true);
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
                    value={cam.rtsp_url}
                    onChange={(e) => {
                      const next = e.target.value;
                      setDraftCameras((prev) =>
                        prev.map((c) => (c.id === cam.id ? { ...c, rtsp_url: next } : c)),
                      );
                      setDirtyCameras(true);
                    }}
                    placeholder="rtsp://..."
                  />
                </div>

                <div className="rowWrap" style={{ gap: 10 }}>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.username")}</label>
                    <input
                      className="input"
                      value={cam.username ?? ""}
                      onChange={(e) => {
                        const next = e.target.value;
                        setDraftCameras((prev) =>
                          prev.map((c) => (c.id === cam.id ? { ...c, username: next } : c)),
                        );
                        setDirtyCameras(true);
                      }}
                    />
                  </div>
                  <div className="field" style={{ flex: 1, minWidth: 220 }}>
                    <label className="label">{t("ext.cameras.settings.password")}</label>
                    <input
                      className="input"
                      type="password"
                      value={cam.password ?? ""}
                      onChange={(e) => {
                        const next = e.target.value;
                        setDraftCameras((prev) =>
                          prev.map((c) => (c.id === cam.id ? { ...c, password: next } : c)),
                        );
                        setDirtyCameras(true);
                      }}
                    />
                  </div>
                </div>

                <div className="field">
                  <label className="label">{t("ext.cameras.settings.processing_server")}</label>
                  <select
                    className="input"
                    value={cam.processing_server_id ?? ""}
                    onChange={(e) => {
                      const next = e.target.value;
                      setDraftCameras((prev) =>
                        prev.map((c) =>
                          c.id === cam.id ? { ...c, processing_server_id: next } : c,
                        ),
                      );
                      setDirtyCameras(true);
                    }}
                  >
                    <option value="">{t("ext.cameras.settings.none")}</option>
                    {servers.map((srv) => (
                      <option key={srv.id} value={srv.id}>
                        {srv.name || srv.url || srv.id}
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

        <div style={{ flex: 1, minWidth: 0 }}>
          {activeSection === "servers" ? renderServers() : renderCameras()}
        </div>
      </div>

      <SubModal
        open={snapshotModalOpen}
        title={snapshotTitle || t("ext.cameras.settings.snapshot")}
        onClose={closeSnapshotModal}
      >
        {snapshotErr ? (
          <div className="card">
            <div className="cardBody">{snapshotErr}</div>
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
            <div className="cardBody">{snapshotBusy ? t("ext.cameras.settings.snapshot_loading") : t("ext.cameras.settings.snapshot")}</div>
          </div>
        )}
      </SubModal>
    </div>
  );
}

function addCameraTool(i18n: HostI18n): EditorTool {
  return {
    id: TOOL_ID_ADD,
    name: { key: "ext.cameras.tool.add", fallback: "Camera" },
    description: { key: "ext.cameras.tool.add_desc" },
    icon: "video",
    createSession: ({ createElement, openEditor }) => ({
      onPointerEvent: (evt) => {
        if (evt.kind !== "down") return;
        if (evt.button !== 0) return;
        const id = createElement(ELEMENT_TYPE_ID, {
          position: { x: evt.world.x, y: 0, z: evt.world.z },
          props: { camera_id: "", camera_name: "" },
        });
        if (id) openEditor(id);
      },
    }),
  };
}

function cameraElementType(i18n: HostI18n): ElementType {
  return {
    type: ELEMENT_TYPE_ID,
    name: { key: "ext.cameras.element.name", fallback: "Camera" },
    description: { key: "ext.cameras.element.desc" },
    placeable: false,
    defaultProps: { camera_id: "", camera_name: "" },
    render2D: ({ ctx, element, viewport }) => {
      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const rot = typeof element.rotation?.y === "number" ? element.rotation.y : 0;
      const px = viewport.scale;
      const w = Math.max(14, Math.min(32, 0.28 * px));
      const h = Math.max(10, Math.min(26, 0.18 * px));

      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.rotate(rot);
      ctx.fillStyle = "rgba(56,189,248,0.12)";
      ctx.strokeStyle = "rgba(230,232,242,0.24)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      roundRectPath(ctx, -w / 2, -h / 2, w, h, Math.min(10, h / 2));
      ctx.fill();
      ctx.stroke();

      // Direction marker (forward = +z in 3D, maps to +y on canvas after rotation).
      ctx.fillStyle = "rgba(251,191,36,0.92)";
      ctx.beginPath();
      ctx.moveTo(0, h / 2 + 6);
      ctx.lineTo(-5, h / 2 - 4);
      ctx.lineTo(5, h / 2 - 4);
      ctx.closePath();
      ctx.fill();

      ctx.restore();
    },
    hitTest2D: ({ element, world }) => {
      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      return dx * dx + dz * dz <= 0.32 * 0.32;
    },
    translate2D: ({ element, delta }) => ({
      position: { x: element.position.x + delta.x, z: element.position.z + delta.z },
    }),
    create3D: ({ THREE }, element) => {
      const group = new THREE.Group();

      const bodyMat = new THREE.MeshStandardMaterial({
        color: 0x0b1220,
        roughness: 0.38,
        metalness: 0.08,
      });
      const accentMat = new THREE.MeshStandardMaterial({
        color: 0x111827,
        roughness: 0.42,
        metalness: 0.1,
      });
      const lensMat = new THREE.MeshStandardMaterial({
        color: 0x020617,
        emissive: new THREE.Color(0x38bdf8),
        emissiveIntensity: 0.15,
        roughness: 0.22,
        metalness: 0.2,
      });

      const base = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.05, 18), accentMat);
      base.position.y = 0.025;
      group.add(base);

      const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.022, 0.16, 14), bodyMat);
      neck.position.y = 0.12;
      group.add(neck);

      const head = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.07, 0.18), bodyMat);
      head.position.set(0, 0.19, 0.06);
      group.add(head);

      const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.025, 18), lensMat);
      lens.rotation.x = Math.PI / 2;
      lens.position.set(0, 0.19, 0.16);
      group.add(lens);

      const led = new THREE.PointLight(0x38bdf8, 0.15, 0.65, 2.0);
      led.position.set(0, 0.2, 0.165);
      group.add(led);

      led.intensity = Boolean(asString(asRecord(element.props).camera_id).trim()) ? 0.15 : 0.05;

      return {
        object: group,
        update: (el) => {
          const props = asRecord(el.props);
          led.intensity = Boolean(asString(props.camera_id).trim()) ? 0.15 : 0.05;
        },
        dispose: () => {
          (base.geometry as any).dispose?.();
          (neck.geometry as any).dispose?.();
          (head.geometry as any).dispose?.();
          (lens.geometry as any).dispose?.();
          bodyMat.dispose();
          accentMat.dispose();
          lensMat.dispose();
        },
      };
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <CameraEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
    renderActionModal: ({ element }) => <CameraAction element={element} i18n={i18n} />,
  };
}

function CameraEditor({
  element,
  update,
  remove,
  close,
  i18n,
}: {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = asRecord(element.props);
  const selectedId = asString(props.camera_id).trim();

  const [index, setIndex] = useState<CamerasIndex | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    fetchIndex()
      .then((data) => {
        if (!cancelled) setIndex(data);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const cameraOptions = useMemo(() => {
    const cams = index?.cameras ?? [];
    return cams
      .map((c) => ({ id: asString((c as any).id), name: asString((c as any).name) }))
      .filter((c) => Boolean(c.id));
  }, [index]);

  return (
    <div>
      {err ? (
        <div className="card">
          <div className="cardBody">{err}</div>
        </div>
      ) : null}

      {cameraOptions.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.cameras.editor.no_cameras")}</div>
        </div>
      ) : (
        <div className="field">
          <label className="label">{t("ext.cameras.editor.camera")}</label>
          <select
            className="input"
            value={selectedId}
            onChange={(e) => {
              const nextId = e.target.value;
              const selected = cameraOptions.find((c) => c.id === nextId) ?? null;
              update({
                name: selected?.name ?? "",
                props: { camera_id: nextId, camera_name: selected?.name ?? "" },
              });
            }}
          >
            <option value="">{t("ext.cameras.editor.select_placeholder")}</option>
            {cameraOptions.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name || c.id}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="sectionDivider" />

      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <button
          className="dangerButton"
          type="button"
          onClick={() => {
            remove();
            close();
          }}
        >
          {t("core.actions.delete")}
        </button>

        <button className="primaryButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}

function CameraAction({ element, i18n }: { element: CompositionElement; i18n: HostI18n }): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = asRecord(element.props);
  const cameraId = asString(props.camera_id).trim();

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [imgUrl, setImgUrl] = useState<string | null>(null);

  const refresh = () => {
    setBusy(true);
    setErr(null);
    fetchCameraSnapshot(cameraId)
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        setImgUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
      })
      .catch((e) => {
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  useEffect(() => {
    if (!cameraId) return;
    refresh();
    return () => {
      setImgUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return null;
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraId]);

  if (!cameraId) {
    return <div className="cardBody">{t("ext.cameras.action.no_camera")}</div>;
  }

  return (
    <div>
      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <div className="label">{asString(props.camera_name) || cameraId}</div>
        <button className="chipButton" type="button" onClick={refresh} disabled={busy}>
          {busy ? t("ext.cameras.action.loading") : t("ext.cameras.action.refresh")}
        </button>
      </div>

      <div className="sectionDivider" />

      {err ? (
        <div className="card">
          <div className="cardBody">{err}</div>
        </div>
      ) : imgUrl ? (
        <img
          src={imgUrl}
          alt={asString(props.camera_name) || cameraId}
          style={{
            width: "100%",
            borderRadius: 14,
            border: "1px solid rgba(255,255,255,0.14)",
            background: "rgba(0,0,0,0.35)",
          }}
        />
      ) : (
        <div className="card">
          <div className="cardBody">{t("ext.cameras.action.loading")}</div>
        </div>
      )}
    </div>
  );
}
