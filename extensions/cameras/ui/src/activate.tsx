import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

import cameraSvg from "@fortawesome/fontawesome-free/svgs/solid/camera.svg";

import type {
  CompositionElement,
  CompositionElementPatch,
  EditorTool,
  EditorToolPointerEvent,
  EditorToolSession,
  ElementType,
  HostI18n,
  PlanePoint,
  SettingsPanel,
  TopoSyncHost,
  Viewport2DContext,
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
  detections?: CameraDetection[];
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

type ControlPoint = {
  id: string;
  label: string;
  image?: { x: number; y: number } | null;
  world?: { x: number; z: number } | null;
};

const YOLO_V12_CATEGORIES = [
  "person",
  "bicycle",
  "car",
  "motorcycle",
  "airplane",
  "bus",
  "train",
  "truck",
  "boat",
  "traffic light",
  "fire hydrant",
  "stop sign",
  "parking meter",
  "bench",
  "bird",
  "cat",
  "dog",
  "horse",
  "sheep",
  "cow",
  "elephant",
  "bear",
  "zebra",
  "giraffe",
  "backpack",
  "umbrella",
  "handbag",
  "tie",
  "suitcase",
  "frisbee",
  "skis",
  "snowboard",
  "sports ball",
  "kite",
  "baseball bat",
  "baseball glove",
  "skateboard",
  "surfboard",
  "tennis racket",
  "bottle",
  "wine glass",
  "cup",
  "fork",
  "knife",
  "spoon",
  "bowl",
  "banana",
  "apple",
  "sandwich",
  "orange",
  "broccoli",
  "carrot",
  "hot dog",
  "pizza",
  "donut",
  "cake",
  "chair",
  "couch",
  "potted plant",
  "bed",
  "dining table",
  "toilet",
  "tv",
  "laptop",
  "mouse",
  "remote",
  "keyboard",
  "cell phone",
  "microwave",
  "oven",
  "toaster",
  "sink",
  "refrigerator",
  "book",
  "clock",
  "vase",
  "scissors",
  "teddy bear",
  "hair drier",
  "toothbrush",
] as const;

type YoloV12Category = (typeof YOLO_V12_CATEGORIES)[number];

const YOLO_LEGACY_CATEGORY_MAP: Record<string, YoloV12Category> = {
  motorbike: "motorcycle",
  aeroplane: "airplane",
  sofa: "couch",
  pottedplant: "potted plant",
  diningtable: "dining table",
  tvmonitor: "tv",
};

type DetectionCondition =
  | { kind: "motion" }
  | { kind: "ha_sensor"; entity_id: string }
  | { kind: "ha_state"; entity_id: string; state: string }
  | { kind: "object"; category: YoloV12Category };

type CameraDetection = {
  id: string;
  trigger: DetectionCondition;
  filters: DetectionCondition[];
};

const CONTROL_POINT_COLORS = [
  "#ef4444",
  "#f59e0b",
  "#22c55e",
  "#38bdf8",
  "#a855f7",
  "#f472b6",
  "#14b8a6",
  "#eab308",
];

function labelForIndex(index: number): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
  if (index >= 0 && index < alphabet.length) return alphabet[index];
  return String(index + 1);
}

function titleCaseWords(v: string): string {
  return v
    .split(" ")
    .map((part) => {
      const s = part.trim();
      if (!s) return "";
      return `${s.slice(0, 1).toUpperCase()}${s.slice(1)}`;
    })
    .join(" ")
    .trim();
}

function yoloCategoryLabel(category: YoloV12Category): string {
  if (category === "tv") return "TV";
  return titleCaseWords(category);
}

function readFiniteNumber(v: unknown, fallback: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

function readNormalizedPoint(v: unknown): { x: number; y: number } | null {
  const rec = asRecord(v);
  const x = readFiniteNumber(rec.x, NaN);
  const y = readFiniteNumber(rec.y, NaN);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
}

function readWorldPoint(v: unknown): { x: number; z: number } | null {
  const rec = asRecord(v);
  const x = readFiniteNumber(rec.x, NaN);
  const z = readFiniteNumber(rec.z, NaN);
  if (!Number.isFinite(x) || !Number.isFinite(z)) return null;
  return { x, z };
}

function readControlPoints(v: unknown): ControlPoint[] {
  if (!Array.isArray(v)) return [];
  const out: ControlPoint[] = [];
  for (let i = 0; i < v.length; i += 1) {
    const rec = asRecord(v[i]);
    const id = asString(rec.id).trim();
    if (!id) continue;
    const label = asString(rec.label).trim() || labelForIndex(i);
    out.push({
      id,
      label,
      image: readNormalizedPoint(rec.image),
      world: readWorldPoint(rec.world),
    });
  }
  return out;
}

function readDetectionCondition(v: unknown): DetectionCondition | null {
  const rec = asRecord(v);
  const kind = asString(rec.kind).trim();
  if (kind === "motion") return { kind: "motion" };
  if (kind === "ha_sensor") return { kind: "ha_sensor", entity_id: asString(rec.entity_id).trim() };
  if (kind === "ha_state")
    return { kind: "ha_state", entity_id: asString(rec.entity_id).trim(), state: asString(rec.state).trim() };
  if (kind === "object") {
    const rawCategory = asString(rec.category).trim();
    const normalized = YOLO_LEGACY_CATEGORY_MAP[rawCategory] ?? rawCategory;
    const category = YOLO_V12_CATEGORIES.find((c) => c === normalized);
    if (!category) return null;
    return { kind: "object", category };
  }
  return null;
}

function readCameraDetections(v: unknown): CameraDetection[] {
  if (!Array.isArray(v)) return [];
  const out: CameraDetection[] = [];
  for (const item of v) {
    const rec = asRecord(item);
    const id = asString(rec.id).trim();
    if (!id) continue;
    const trigger = readDetectionCondition(rec.trigger) ?? { kind: "motion" };
    const filters = Array.isArray(rec.filters) ? rec.filters.map(readDetectionCondition).filter(Boolean) : [];
    out.push({ id, trigger, filters: filters as DetectionCondition[] });
  }
  return out;
}

function defaultControlPoints(count = 4): ControlPoint[] {
  const n = Math.max(1, Math.min(12, Math.floor(count)));
  return Array.from({ length: n }, (_, i) => ({
    id: newId(),
    label: labelForIndex(i),
    image: null,
    world: null,
  }));
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
      detections: readCameraDetections(rec.detections),
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
    "ext.cameras.settings.detections": "Detections",
    "ext.cameras.element.name": "Camera",
    "ext.cameras.element.desc": "Camera placed in the scene; click to see a snapshot.",
    "ext.cameras.tool.add": "Camera",
    "ext.cameras.tool.add_desc": "Click to place a camera and configure it.",
    "ext.cameras.editor.camera": "Camera",
    "ext.cameras.editor.no_cameras": "Add a camera in Settings first.",
    "ext.cameras.editor.select_placeholder": "Select…",
    "ext.cameras.editor.control_points": "Control points",
    "ext.cameras.editor.control_points_none": "No control points yet.",
    "ext.cameras.editor.control_points_some": "{{complete}}/{{total}} points",
    "ext.cameras.editor.control_points_open": "Place control points",
    "ext.cameras.control.title": "Control points",
    "ext.cameras.control.help": "Select a point, then click on the image and on the canvas.",
    "ext.cameras.control.loading": "Loading…",
    "ext.cameras.control.min_points": "Use at least 4 points.",
    "ext.cameras.control.image": "Camera snapshot",
    "ext.cameras.control.canvas": "Composition canvas",
    "ext.cameras.action.no_camera": "No camera selected.",
    "ext.cameras.action.refresh": "Refresh snapshot",
    "ext.cameras.action.loading": "Loading…",
    "ext.cameras.theme.neon_name": "Neon (blue)",
    "ext.cameras.theme.neon_desc": "A cool neon-blue variant for the whole UI.",
    "ext.cameras.detections.title": "Detections",
    "ext.cameras.detections.help": "Each rule has a trigger and optional filters. Filters help avoid expensive object detections when they are not needed (future).",
    "ext.cameras.detections.list": "Rules",
    "ext.cameras.detections.empty": "No detections yet.",
    "ext.cameras.detections.item": "Rule {{n}}",
    "ext.cameras.detections.details": "Details",
    "ext.cameras.detections.trigger": "Trigger",
    "ext.cameras.detections.filters": "Filters",
    "ext.cameras.detections.add_filter": "Add filter",
    "ext.cameras.detections.filters_empty": "No filters.",
    "ext.cameras.detections.select_prompt": "Select or add a rule.",
    "ext.cameras.detections.cond.motion": "Motion",
    "ext.cameras.detections.cond.object": "Object (YOLOv12)",
    "ext.cameras.detections.cond.ha_sensor": "HA sensor (soon)",
    "ext.cameras.detections.cond.ha_state": "HA entity state (soon)",
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
    "ext.cameras.settings.detections": "Detecções",
    "ext.cameras.element.name": "Câmera",
    "ext.cameras.element.desc": "Câmera na cena; clique para ver um snapshot.",
    "ext.cameras.tool.add": "Câmera",
    "ext.cameras.tool.add_desc": "Clique para posicionar uma câmera e configurar.",
    "ext.cameras.editor.camera": "Câmera",
    "ext.cameras.editor.no_cameras": "Adicione uma câmera nas Configurações primeiro.",
    "ext.cameras.editor.select_placeholder": "Selecionar…",
    "ext.cameras.editor.control_points": "Pontos de controle",
    "ext.cameras.editor.control_points_none": "Nenhum ponto definido.",
    "ext.cameras.editor.control_points_some": "{{complete}}/{{total}} pontos",
    "ext.cameras.editor.control_points_open": "Posicionar pontos de controle",
    "ext.cameras.control.title": "Pontos de controle",
    "ext.cameras.control.help": "Selecione um ponto e clique na imagem e no canvas.",
    "ext.cameras.control.loading": "Carregando…",
    "ext.cameras.control.min_points": "Use ao menos 4 pontos.",
    "ext.cameras.control.image": "Imagem da câmera",
    "ext.cameras.control.canvas": "Canvas da composição",
    "ext.cameras.action.no_camera": "Nenhuma câmera selecionada.",
    "ext.cameras.action.refresh": "Atualizar snapshot",
    "ext.cameras.action.loading": "Carregando...",
    "ext.cameras.theme.neon_name": "Neon (azul)",
    "ext.cameras.theme.neon_desc": "Uma variação neon azul para toda a interface.",
    "ext.cameras.detections.title": "Detecções",
    "ext.cameras.detections.help": "Cada regra tem um gatilho e filtros opcionais. Filtros ajudam a evitar detecções pesadas de objetos quando não são necessárias (futuro).",
    "ext.cameras.detections.list": "Regras",
    "ext.cameras.detections.empty": "Nenhuma detecção por enquanto.",
    "ext.cameras.detections.item": "Regra {{n}}",
    "ext.cameras.detections.details": "Detalhes",
    "ext.cameras.detections.trigger": "Gatilho",
    "ext.cameras.detections.filters": "Filtros",
    "ext.cameras.detections.add_filter": "Adicionar filtro",
    "ext.cameras.detections.filters_empty": "Nenhum filtro.",
    "ext.cameras.detections.select_prompt": "Selecione ou adicione uma regra.",
    "ext.cameras.detections.cond.motion": "Movimento",
    "ext.cameras.detections.cond.object": "Objeto (YOLOv12)",
    "ext.cameras.detections.cond.ha_sensor": "Sensor HA (em breve)",
    "ext.cameras.detections.cond.ha_state": "Estado de entidade HA (em breve)",
  },
} as const;

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(translations);
  host.registerTheme({
    id: "com.toposync.theme.neon_blue",
    name: { key: "ext.cameras.theme.neon_name", fallback: "Neon (blue)" },
    description: { key: "ext.cameras.theme.neon_desc" },
    vars: {
      "--bg": "#050814",
      "--panel": "rgba(8, 14, 30, 0.74)",
      "--panelSolid": "#080e1e",
      "--panel2": "rgba(10, 18, 40, 0.90)",
      "--accent": "#38bdf8",
      "--glassBlur": "18px",
      "--glassSaturate": "1.25",
    },
  });
  host.registerSettingsPanel(settingsPanel());
  host.registerElementType(cameraElementType(host));
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
  panelStyle,
  bodyStyle,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  panelStyle?: React.CSSProperties;
  bodyStyle?: React.CSSProperties;
}): React.ReactElement | null {
  if (!open) return null;

  return createPortal(
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
        style={{ width: "min(980px, calc(100vw - 28px))", ...(panelStyle ?? {}) }}
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
        <div className="modalBody" style={bodyStyle}>
          {children}
        </div>
      </div>
    </div>,
    document.body,
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

  const [detectionsModalOpen, setDetectionsModalOpen] = useState(false);
  const [detectionsCameraId, setDetectionsCameraId] = useState<string | null>(null);

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

  function openDetectionsModal(cameraId: string) {
    setDetectionsCameraId(cameraId);
    setDetectionsModalOpen(true);
  }

  function closeDetectionsModal() {
    setDetectionsModalOpen(false);
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
                    detections: [],
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
                      className={[
                        "iconButton",
                        (cam.detections?.length ?? 0) > 0 ? "iconButtonPrimary" : "",
                      ].join(" ")}
                      type="button"
                      onClick={() => openDetectionsModal(cam.id)}
                      aria-label={t("ext.cameras.settings.detections")}
                      title={
                        (cam.detections?.length ?? 0) > 0
                          ? `${t("ext.cameras.settings.detections")} (${cam.detections?.length ?? 0})`
                          : t("ext.cameras.settings.detections")
                      }
                    >
                      <i className="fa-solid fa-bullseye" aria-hidden="true" />
                    </button>
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

  const detectionsCamera = detectionsCameraId ? draftCameras.find((c) => c.id === detectionsCameraId) ?? null : null;

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

      <CameraDetectionsModal
        open={detectionsModalOpen}
        onClose={closeDetectionsModal}
        i18n={i18n}
        cameraLabel={detectionsCamera?.name || detectionsCamera?.id || ""}
        initialDetections={detectionsCamera?.detections ?? []}
        onSave={(next) => {
          if (!detectionsCameraId) return;
          setDraftCameras((prev) => prev.map((c) => (c.id === detectionsCameraId ? { ...c, detections: next } : c)));
          setDirtyCameras(true);
        }}
      />
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
          props: { camera_id: "", camera_name: "", view_mode: "ceiling" },
        });
        if (id) openEditor(id);
      },
    }),
  };
}

function cameraElementType(host: TopoSyncHost): ElementType {
  const i18n = host.i18n;
  const iconGeometryCache = new Map<string, { geometry: any; scale: number }>();
  const ICON_TARGET_SIZE = 0.14;

  const BUTTON_RADIUS = 0.18;
  const BUTTON_THETA_TOP_CUT = 1.05;
  const CEILING_TOP_MARGIN = 0.0;

  return {
    type: ELEMENT_TYPE_ID,
    name: { key: "ext.cameras.element.name", fallback: "Camera" },
    description: { key: "ext.cameras.element.desc" },
    placeable: false,
    defaultProps: { camera_id: "", camera_name: "", view_mode: "ceiling" },
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
    create3D: ({ THREE, view }, element) => {
      function getIconGeometry(): { geometry: any; scale: number } {
        const cached = iconGeometryCache.get("camera");
        if (cached) return cached;

        const data = new SVGLoader().parse(cameraSvg);
        const shapes: any[] = [];
        for (const path of data.paths) shapes.push(...SVGLoader.createShapes(path));

        const geometry = new THREE.ShapeGeometry(shapes);
        geometry.computeBoundingBox();
        const bbox = geometry.boundingBox;
        if (bbox) {
          const cx = (bbox.min.x + bbox.max.x) / 2;
          const cy = (bbox.min.y + bbox.max.y) / 2;
          geometry.translate(-cx, -cy, 0);
        }

        geometry.scale(1, -1, 1);
        geometry.rotateX(-Math.PI / 2);

        geometry.computeBoundingBox();
        const bbox3 = geometry.boundingBox;
        const sizeX = bbox3 ? bbox3.max.x - bbox3.min.x : 1;
        const sizeZ = bbox3 ? bbox3.max.z - bbox3.min.z : 1;
        const maxXZ = Math.max(sizeX, sizeZ, 1e-9);
        const scale = ICON_TARGET_SIZE / maxXZ;

        const entry = { geometry, scale };
        iconGeometryCache.set("camera", entry);
        return entry;
      }

      const NEON = 0x38bdf8;

      const group = new THREE.Group();
      const mountGroup = new THREE.Group();
      group.add(mountGroup);

      const topY = BUTTON_RADIUS * Math.cos(BUTTON_THETA_TOP_CUT);
      const topRadius = BUTTON_RADIUS * Math.sin(BUTTON_THETA_TOP_CUT);

      const domeCeilingGeom = new THREE.SphereGeometry(
        BUTTON_RADIUS,
        56,
        34,
        0,
        Math.PI * 2,
        BUTTON_THETA_TOP_CUT,
        Math.PI - BUTTON_THETA_TOP_CUT,
      );

      const sphereMat = new THREE.MeshStandardMaterial({
        color: 0x0b1220,
        emissive: new THREE.Color(NEON),
        emissiveIntensity: 0.36,
        roughness: 0.32,
        metalness: 0.0,
      });
      const cutMat = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      const iconMat = new THREE.MeshBasicMaterial({ color: NEON, side: THREE.DoubleSide });
      iconMat.depthWrite = false;
      iconMat.polygonOffset = true;
      iconMat.polygonOffsetFactor = -1;
      iconMat.polygonOffsetUnits = -1;

      const dome = new THREE.Mesh(domeCeilingGeom, sphereMat);
      mountGroup.add(dome);

      const topCapGeom = new THREE.CircleGeometry(topRadius, 48);
      const topCap = new THREE.Mesh(topCapGeom, cutMat);
      topCap.rotation.x = -Math.PI / 2;
      topCap.position.set(0, topY, 0);
      mountGroup.add(topCap);

      const topIconGeo = getIconGeometry();
      const topIcon = new THREE.Mesh(topIconGeo.geometry, iconMat);
      topIcon.scale.setScalar(topIconGeo.scale);
      topIcon.position.set(0, topY + 0.002, 0);
      topIcon.renderOrder = 10;
      mountGroup.add(topIcon);

      // Dome camera lens "window" on the underside, slightly angled.
      const lensCutMat = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      lensCutMat.depthWrite = false;
      lensCutMat.polygonOffset = true;
      lensCutMat.polygonOffsetFactor = -1;
      lensCutMat.polygonOffsetUnits = -1;

      const lensRadius = 0.055;
      const lensCutGeom = new THREE.CircleGeometry(lensRadius, 42);
      const lensCut = new THREE.Mesh(lensCutGeom, lensCutMat);
      lensCut.renderOrder = 9;
      mountGroup.add(lensCut);

      const light = new THREE.PointLight(NEON, 0.18, 0.9, 2.2);
      light.position.set(0, BUTTON_RADIUS * 0.45, 0);
      mountGroup.add(light);

      function apply(el: CompositionElement) {
        // Ceiling-only for now.
        mountGroup.rotation.set(0, 0, 0);
        mountGroup.position.set(0, 0, 0);

        // Hang from ceiling: top cut flush at wallHeight.
        mountGroup.position.y = view.wallHeight - topY - CEILING_TOP_MARGIN;

        const lensDir = new THREE.Vector3(0.12, -0.72, 1).normalize();
        const lensPos = lensDir.clone().multiplyScalar(BUTTON_RADIUS * 0.92);
        lensCut.position.copy(lensPos);
        lensCut.lookAt(lensPos.clone().add(lensDir));
        lensCut.rotateZ(0.55);
        lensCut.position.add(lensDir.clone().multiplyScalar(0.002));
      }

      apply(element);

      return {
        object: group,
        update: apply,
        dispose: () => {
          domeCeilingGeom.dispose();
          topCapGeom.dispose();
          lensCutGeom.dispose();
          sphereMat.dispose();
          cutMat.dispose();
          iconMat.dispose();
          lensCutMat.dispose();
        },
      };
    },
    renderEditorModal: ({ element, update, remove, close }) => (
      <CameraEditor element={element} update={update} remove={remove} close={close} i18n={i18n} host={host} />
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
  host,
}: {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
  host: TopoSyncHost;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = asRecord(element.props);
  const selectedId = asString(props.camera_id).trim();
  const existingControlPoints = useMemo(() => readControlPoints(props.control_points), [props.control_points]);
  const controlPointPairs = existingControlPoints.filter((p) => Boolean(p.image) && Boolean(p.world)).length;
  const totalControlPoints = existingControlPoints.length;
  const [isControlPointsOpen, setIsControlPointsOpen] = useState(false);

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

      <div className="field">
        <label className="label">{t("ext.cameras.editor.control_points")}</label>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="cardMeta">
            {totalControlPoints > 0
              ? t("ext.cameras.editor.control_points_some", { complete: controlPointPairs, total: totalControlPoints })
              : t("ext.cameras.editor.control_points_none")}
          </div>

          <button
            className="chipButton"
            type="button"
            disabled={!selectedId}
            onClick={() => setIsControlPointsOpen(true)}
          >
            {t("ext.cameras.editor.control_points_open")}
          </button>
        </div>
        {totalControlPoints > 0 && controlPointPairs < 4 ? (
          <div className="cardMeta" style={{ marginTop: 6 }}>
            {t("ext.cameras.control.min_points")}
          </div>
        ) : null}
      </div>

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

      <ControlPointsModal
        open={isControlPointsOpen}
        onClose={() => setIsControlPointsOpen(false)}
        host={host}
        i18n={i18n}
        cameraId={selectedId}
        initialPoints={existingControlPoints}
        onSave={(points) => update({ props: { control_points: points } })}
      />
    </div>
  );
}

function ControlPointsModal({
  open,
  onClose,
  host,
  i18n,
  cameraId,
  initialPoints,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  host: TopoSyncHost;
  i18n: HostI18n;
  cameraId: string;
  initialPoints: ControlPoint[];
  onSave: (points: ControlPoint[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [points, setPoints] = useState<ControlPoint[]>([]);
  const [selectedPointId, setSelectedPointId] = useState<string | null>(null);

  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotErr, setSnapshotErr] = useState<string | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);

  useEffect(() => {
    return () => {
      if (snapshotUrl) URL.revokeObjectURL(snapshotUrl);
    };
  }, [snapshotUrl]);

  useEffect(() => {
    if (!open) return;
    const base = initialPoints.length ? initialPoints : defaultControlPoints(4);
    const padded: ControlPoint[] = base.map((p) => ({ ...p, image: p.image ?? null, world: p.world ?? null }));
    while (padded.length < 4) {
      padded.push({ id: newId(), label: labelForIndex(padded.length), image: null, world: null });
    }
    setPoints(padded);
    setSelectedPointId(padded[0]?.id ?? null);
  }, [open, initialPoints]);

  useEffect(() => {
    if (!open) {
      setSnapshotErr(null);
      setSnapshotBusy(false);
      setSnapshotUrl(null);
      return;
    }
    if (!cameraId) return;

    let cancelled = false;
    setSnapshotBusy(true);
    setSnapshotErr(null);
    fetchCameraSnapshot(cameraId)
      .then((blob) => {
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        setSnapshotUrl(url);
      })
      .catch((e) => {
        if (cancelled) return;
        setSnapshotErr(e instanceof Error ? e.message : String(e));
        setSnapshotUrl(null);
      })
      .finally(() => {
        if (!cancelled) setSnapshotBusy(false);
      });

    return () => {
      cancelled = true;
    };
  }, [open, cameraId]);

  const completePairs = useMemo(() => points.filter((p) => Boolean(p.image) && Boolean(p.world)).length, [points]);

  const toolSession = useMemo<EditorToolSession>(() => {
    return {
      onPointerEvent: (evt: EditorToolPointerEvent) => {
        if (evt.kind !== "down") return;
        if (!selectedPointId) return;
        setPoints((prev) =>
          prev.map((p) => (p.id === selectedPointId ? { ...p, world: { x: evt.world.x, z: evt.world.z } } : p)),
        );
      },
      renderOverlay2D: ({ ctx, viewport }: { ctx: CanvasRenderingContext2D; viewport: Viewport2DContext }) => {
        ctx.save();
        ctx.font = "700 12px system-ui, -apple-system, Segoe UI, Roboto, Arial";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        for (let i = 0; i < points.length; i += 1) {
          const p = points[i];
          if (!p.world) continue;
          const color = CONTROL_POINT_COLORS[i % CONTROL_POINT_COLORS.length];
          const screen = viewport.worldToScreen(p.world);
          const isSelected = selectedPointId === p.id;
          const r = isSelected ? 10 : 8;

          ctx.beginPath();
          ctx.arc(screen.x, screen.y, r, 0, Math.PI * 2);
          ctx.fillStyle = color;
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = isSelected ? "rgba(255,255,255,0.92)" : "rgba(0,0,0,0.65)";
          ctx.stroke();

          ctx.fillStyle = "rgba(0,0,0,0.82)";
          ctx.fillText(p.label || labelForIndex(i), screen.x, screen.y + 0.5);
        }

        ctx.restore();
      },
      getCursor: () => "crosshair",
    };
  }, [points, selectedPointId]);

  function addPoint() {
    const id = newId();
    setPoints((prev) => [...prev, { id, label: labelForIndex(prev.length), image: null, world: null }]);
    setSelectedPointId(id);
  }

  function setImagePointFromEvent(e: React.MouseEvent<HTMLImageElement>) {
    if (!selectedPointId) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const nx = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
    const ny = Math.max(0, Math.min(1, (e.clientY - rect.top) / Math.max(1, rect.height)));
    setPoints((prev) => prev.map((p) => (p.id === selectedPointId ? { ...p, image: { x: nx, y: ny } } : p)));
  }

  return (
    <SubModal
      open={open}
      onClose={onClose}
      title={t("ext.cameras.control.title")}
      panelStyle={{
        width: "min(1440px, calc(100vw - 28px))",
        height: "calc(100vh - 28px)",
        maxHeight: "calc(100vh - 28px)",
      }}
      bodyStyle={{
        padding: 0,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 12, flex: 1, minHeight: 0 }}>
        <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div className="rowWrap" style={{ gap: 8 }}>
            {points.map((p, i) => {
              const isSelected = selectedPointId === p.id;
              const color = CONTROL_POINT_COLORS[i % CONTROL_POINT_COLORS.length];
              const hasImg = Boolean(p.image);
              const hasWorld = Boolean(p.world);
              return (
                <button
                  key={p.id}
                  type="button"
                  className="chipButton"
                  onClick={() => setSelectedPointId(p.id)}
                  style={{
                    minWidth: 46,
                    justifyContent: "center",
                    borderColor: isSelected ? "rgba(56,189,248,0.55)" : "rgba(255,255,255,0.14)",
                    background: isSelected ? "rgba(56,189,248,0.10)" : undefined,
                  }}
                  aria-label={`Point ${p.label || labelForIndex(i)}`}
                >
                  <span
                    aria-hidden="true"
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: 999,
                      background: color,
                      boxShadow: "0 0 0 2px rgba(0,0,0,0.25)",
                      opacity: hasImg && hasWorld ? 1 : 0.4,
                    }}
                  />
                  <span>{p.label || labelForIndex(i)}</span>
                </button>
              );
            })}

            <button className="iconButton" type="button" onClick={addPoint} aria-label={t("core.actions.add")}>
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          <div className="cardMeta" style={{ textAlign: "right" }}>
            {t("ext.cameras.control.help")}
            {completePairs > 0 && completePairs < 4 ? ` ${t("ext.cameras.control.min_points")}` : ""}
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="label">{t("ext.cameras.control.image")}</div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.14)",
                background: "rgba(0,0,0,0.30)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 10,
                overflow: "hidden",
              }}
            >
              {snapshotErr ? (
                <div className="card">
                  <div className="cardBody">{snapshotErr}</div>
                </div>
              ) : snapshotUrl ? (
                <div style={{ position: "relative", display: "inline-block", maxWidth: "100%", maxHeight: "100%" }}>
                  <img
                    src={snapshotUrl}
                    alt={t("ext.cameras.control.image")}
                    style={{
                      display: "block",
                      maxWidth: "100%",
                      maxHeight: "100%",
                      borderRadius: 14,
                      border: "1px solid rgba(255,255,255,0.10)",
                    }}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      setImagePointFromEvent(e);
                    }}
                  />

                  {points.map((p, i) => {
                    if (!p.image) return null;
                    const isSelected = selectedPointId === p.id;
                    const color = CONTROL_POINT_COLORS[i % CONTROL_POINT_COLORS.length];
                    return (
                      <div
                        key={p.id}
                        style={{
                          position: "absolute",
                          left: `${p.image.x * 100}%`,
                          top: `${p.image.y * 100}%`,
                          transform: "translate(-50%,-50%)",
                          width: isSelected ? 22 : 20,
                          height: isSelected ? 22 : 20,
                          borderRadius: 999,
                          background: color,
                          border: isSelected ? "2px solid rgba(255,255,255,0.92)" : "2px solid rgba(0,0,0,0.65)",
                          boxShadow: "0 8px 18px rgba(0,0,0,0.28)",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontSize: 12,
                          fontWeight: 800,
                          color: "rgba(0,0,0,0.82)",
                          pointerEvents: "none",
                        }}
                      >
                        {p.label || labelForIndex(i)}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="card">
                  <div className="cardBody">{snapshotBusy ? t("ext.cameras.control.loading") : t("ext.cameras.control.image")}</div>
                </div>
              )}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="label">{t("ext.cameras.control.canvas")}</div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                borderRadius: 16,
                border: "1px solid rgba(255,255,255,0.14)",
                background: "rgba(0,0,0,0.30)",
                overflow: "hidden",
              }}
            >
              <host.ui.Viewport2DReplica session={toolSession} style={{ width: "100%", height: "100%" }} />
            </div>
          </div>
        </div>

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <button className="chipButton" type="button" onClick={onClose}>
            {t("core.actions.cancel")}
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              onSave(points);
              onClose();
            }}
          >
            {t("core.actions.save")}
          </button>
        </div>
      </div>
    </SubModal>
  );
}

function describeDetectionCondition(
  cond: DetectionCondition,
  t: (key: string, vars?: Record<string, unknown>) => string,
): string {
  if (cond.kind === "motion") return t("ext.cameras.detections.cond.motion");
  if (cond.kind === "ha_sensor")
    return cond.entity_id
      ? `${t("ext.cameras.detections.cond.ha_sensor")}: ${cond.entity_id}`
      : t("ext.cameras.detections.cond.ha_sensor");
  if (cond.kind === "ha_state") {
    const base = cond.entity_id
      ? `${t("ext.cameras.detections.cond.ha_state")}: ${cond.entity_id}`
      : t("ext.cameras.detections.cond.ha_state");
    return cond.state ? `${base} = ${cond.state}` : base;
  }
  return `${t("ext.cameras.detections.cond.object")}: ${yoloCategoryLabel(cond.category)}`;
}

function DetectionConditionEditor({
  value,
  onChange,
  i18n,
}: {
  value: DetectionCondition;
  onChange: (next: DetectionCondition) => void;
  i18n: HostI18n;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  return (
    <div className="rowWrap" style={{ gap: 8, alignItems: "center" }}>
      <select
        className="input"
        value={value.kind}
        onChange={(e) => {
          const nextKind = e.target.value as DetectionCondition["kind"];
          if (nextKind === "motion") onChange({ kind: "motion" });
          else if (nextKind === "object") onChange({ kind: "object", category: "person" });
          else if (nextKind === "ha_sensor") onChange({ kind: "ha_sensor", entity_id: "" });
          else if (nextKind === "ha_state") onChange({ kind: "ha_state", entity_id: "", state: "" });
        }}
        style={{ minWidth: 220 }}
      >
        <option value="motion">{t("ext.cameras.detections.cond.motion")}</option>
        <option value="object">{t("ext.cameras.detections.cond.object")}</option>
        <option value="ha_sensor">{t("ext.cameras.detections.cond.ha_sensor")}</option>
        <option value="ha_state">{t("ext.cameras.detections.cond.ha_state")}</option>
      </select>

      {value.kind === "object" ? (
        <select
          className="input"
          value={value.category}
          onChange={(e) => {
            const raw = e.target.value;
            const category = YOLO_V12_CATEGORIES.find((c) => c === raw);
            if (!category) return;
            onChange({ kind: "object", category });
          }}
          style={{ minWidth: 240, flex: 1 }}
        >
          {YOLO_V12_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {yoloCategoryLabel(c)}
            </option>
          ))}
        </select>
      ) : null}

      {value.kind === "ha_sensor" || value.kind === "ha_state" ? (
        <input
          className="input"
          value={value.entity_id}
          onChange={(e) => {
            const next = e.target.value;
            onChange(value.kind === "ha_sensor" ? { ...value, entity_id: next } : { ...value, entity_id: next });
          }}
          placeholder="sensor.some_entity"
          style={{ minWidth: 240, flex: 1 }}
        />
      ) : null}

      {value.kind === "ha_state" ? (
        <input
          className="input"
          value={value.state}
          onChange={(e) => onChange({ ...value, state: e.target.value })}
          placeholder="on"
          style={{ width: 120 }}
        />
      ) : null}
    </div>
  );
}

function CameraDetectionsModal({
  open,
  onClose,
  i18n,
  cameraLabel,
  initialDetections,
  onSave,
}: {
  open: boolean;
  onClose: () => void;
  i18n: HostI18n;
  cameraLabel: string;
  initialDetections: CameraDetection[];
  onSave: (detections: CameraDetection[]) => void;
}): React.ReactElement | null {
  const { t } = i18n.useI18n();

  const [detections, setDetections] = useState<CameraDetection[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const normalized = (initialDetections ?? []).map((d) => ({
      id: d.id || newId(),
      trigger: d.trigger ?? { kind: "motion" },
      filters: Array.isArray(d.filters) ? d.filters : [],
    }));
    setDetections(normalized);
    setSelectedId(normalized[0]?.id ?? null);
  }, [open, initialDetections]);

  const selected = useMemo(
    () => (selectedId ? detections.find((d) => d.id === selectedId) ?? null : null),
    [detections, selectedId],
  );

  function addDetection() {
    const id = newId();
    setDetections((prev) => [{ id, trigger: { kind: "motion" }, filters: [] }, ...prev]);
    setSelectedId(id);
  }

  function updateDetection(id: string, patch: Partial<CameraDetection>) {
    setDetections((prev) => prev.map((d) => (d.id === id ? { ...d, ...patch } : d)));
  }

  function deleteDetection(id: string) {
    setDetections((prev) => prev.filter((d) => d.id !== id));
    setSelectedId((prev) => (prev === id ? null : prev));
  }

  return (
    <SubModal
      open={open}
      onClose={onClose}
      title={cameraLabel ? `${t("ext.cameras.detections.title")}: ${cameraLabel}` : t("ext.cameras.detections.title")}
      panelStyle={{ width: "min(1100px, calc(100vw - 28px))" }}
      bodyStyle={{
        padding: 0,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 12, flex: 1, minHeight: 0 }}>
        <div className="card">
          <div className="cardBody">{t("ext.cameras.detections.help")}</div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <div className="label" style={{ margin: 0 }}>
                {t("ext.cameras.detections.list")}
              </div>
              <button
                className="iconButton iconButtonPrimary"
                type="button"
                onClick={addDetection}
                aria-label={t("core.actions.add")}
              >
                <i className="fa-solid fa-plus" aria-hidden="true" />
              </button>
            </div>

            <div className="sectionDivider" />

            <div style={{ overflow: "auto", minHeight: 0, paddingRight: 2 }}>
              {detections.length === 0 ? (
                <div className="card">
                  <div className="cardBody">{t("ext.cameras.detections.empty")}</div>
                </div>
              ) : (
                <div className="choiceList">
                  {detections.map((d, i) => {
                    const isSelected = selectedId === d.id;
                    const summary = describeDetectionCondition(d.trigger, t);
                    const filtersCount = d.filters.length;
                    return (
                      <button
                        key={d.id}
                        type="button"
                        className={["choiceItem", isSelected ? "isSelected" : ""].join(" ")}
                        onClick={() => setSelectedId(d.id)}
                      >
                        <div style={{ display: "flex", flexDirection: "column", gap: 4, width: "100%" }}>
                          <div className="row" style={{ justifyContent: "space-between", gap: 10 }}>
                            <span style={{ fontWeight: 700 }}>{t("ext.cameras.detections.item", { n: i + 1 })}</span>
                            {filtersCount ? (
                              <span
                                style={{
                                  minWidth: 24,
                                  height: 20,
                                  padding: "0 8px",
                                  borderRadius: 999,
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  fontSize: 12,
                                  fontWeight: 700,
                                  border: "1px solid rgba(255,255,255,0.14)",
                                  background: "rgba(255,255,255,0.06)",
                                  color: "rgba(230,232,242,0.92)",
                                }}
                              >
                                {filtersCount}
                              </span>
                            ) : null}
                          </div>
                          <div className="cardMeta" style={{ margin: 0 }}>
                            {summary}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
            {selected ? (
              <div className="card" style={{ overflow: "hidden", display: "flex", flexDirection: "column", minHeight: 0 }}>
                <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
                  <div className="label" style={{ margin: 0 }}>
                    {t("ext.cameras.detections.details")}
                  </div>
                  <button
                    className="iconButton iconButtonDanger"
                    type="button"
                    onClick={() => deleteDetection(selected.id)}
                    aria-label={t("core.actions.delete")}
                  >
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                  </button>
                </div>

                <div className="sectionDivider" />

                <div style={{ display: "flex", flexDirection: "column", gap: 12, minHeight: 0 }}>
                  <div className="field">
                    <label className="label">{t("ext.cameras.detections.trigger")}</label>
                    <DetectionConditionEditor
                      value={selected.trigger}
                      i18n={i18n}
                      onChange={(next) => updateDetection(selected.id, { trigger: next })}
                    />
                  </div>

                  <div className="field">
                    <div className="rowWrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
                      <label className="label" style={{ margin: 0 }}>
                        {t("ext.cameras.detections.filters")}
                      </label>
                      <button
                        className="iconButton"
                        type="button"
                        onClick={() => {
                          updateDetection(selected.id, { filters: [...selected.filters, { kind: "motion" }] });
                        }}
                        aria-label={t("ext.cameras.detections.add_filter")}
                      >
                        <i className="fa-solid fa-plus" aria-hidden="true" />
                      </button>
                    </div>

                    {selected.filters.length === 0 ? (
                      <div className="card">
                        <div className="cardBody">{t("ext.cameras.detections.filters_empty")}</div>
                      </div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                        {selected.filters.map((f, idx) => (
                          <div className="rowWrap" key={idx} style={{ gap: 8, alignItems: "center" }}>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <DetectionConditionEditor
                                value={f}
                                i18n={i18n}
                                onChange={(next) => {
                                  const nextFilters = selected.filters.map((prev, j) => (j === idx ? next : prev));
                                  updateDetection(selected.id, { filters: nextFilters });
                                }}
                              />
                            </div>
                            <button
                              className="iconButton iconButtonDanger"
                              type="button"
                              onClick={() => {
                                const nextFilters = selected.filters.filter((_, j) => j !== idx);
                                updateDetection(selected.id, { filters: nextFilters });
                              }}
                              aria-label={t("core.actions.delete")}
                            >
                              <i className="fa-solid fa-trash" aria-hidden="true" />
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <div className="card">
                <div className="cardBody">{t("ext.cameras.detections.select_prompt")}</div>
              </div>
            )}
          </div>
        </div>

        <div className="rowWrap" style={{ justifyContent: "space-between" }}>
          <button className="chipButton" type="button" onClick={onClose}>
            {t("core.actions.cancel")}
          </button>
          <button
            className="primaryButton"
            type="button"
            onClick={() => {
              onSave(detections);
              onClose();
            }}
          >
            {t("core.actions.save")}
          </button>
        </div>
      </div>
    </SubModal>
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
