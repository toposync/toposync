import React, { useEffect, useMemo, useRef, useState } from "react";

import Select from "react-select";
import type { GroupBase, StylesConfig } from "react-select";

import { SVGLoader } from "three/examples/jsm/loaders/SVGLoader.js";

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

import houseSvg from "@fortawesome/fontawesome-free/svgs/solid/house.svg";
import lightbulbSvg from "@fortawesome/fontawesome-free/svgs/solid/lightbulb.svg";
import toggleOnSvg from "@fortawesome/fontawesome-free/svgs/solid/toggle-on.svg";
import fanSvg from "@fortawesome/fontawesome-free/svgs/solid/fan.svg";
import temperatureHalfSvg from "@fortawesome/fontawesome-free/svgs/solid/temperature-half.svg";
import lockSvg from "@fortawesome/fontawesome-free/svgs/solid/lock.svg";
import windowMaximizeSvg from "@fortawesome/fontawesome-free/svgs/solid/window-maximize.svg";
import videoSvg from "@fortawesome/fontawesome-free/svgs/solid/video.svg";
import tvSvg from "@fortawesome/fontawesome-free/svgs/solid/tv.svg";

const EXTENSION_ID = "com.toposync.home_assistant";
const ELEMENT_TYPE_ID = "com.toposync.home_assistant.item";
const TOOL_ID_ADD = "com.toposync.home_assistant.tool.add";

const PRIMARY_TOGGLE_DOMAINS = new Set([
  "light",
  "switch",
  "fan",
  "input_boolean",
  "lock",
  "cover",
  "climate",
  "humidifier",
]);

type HaViewMode = "floor" | "ceiling" | "wall";
type HaSpecialView = "none" | "lamp";

const LAMP_COMPAT_DOMAINS = new Set(["light", "switch", "fan", "input_boolean", "humidifier"]);
const DEFAULT_LAMP_COLOR = "#ffe8b0";
const DEFAULT_LAMP_INTENSITY = 1.0;

type HaServer = {
  id: string;
  name: string;
  host: string;
  apiKey: string;
};

type HaServerPublic = {
  id: string;
  name: string;
  host: string;
};

type RegistryEntity = {
  entity_id: string;
  name: string;
  icon?: string;
  domain?: string;
  device_id?: string;
};

type RegistryDevice = {
  id: string;
  name: string;
};

type RegistryResponse = {
  entities: RegistryEntity[];
  devices: RegistryDevice[];
  device_entities: Record<string, string[]>;
};

type HaItemRef = {
  kind: "entity" | "device";
  id: string;
  name?: string;
  domain?: string;
  icon?: string;
  device_id?: string;
};

type HaItemOption = {
  value: string;
  label: string;
  kind: "entity" | "device";
  id: string;
  meta?: { subLabel?: string; icon?: string; domain?: string; deviceId?: string };
};

function asString(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

function readFiniteNumber(v: unknown, fallback: number): number {
  const num = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isFinite(num) ? num : fallback;
}

function readLampIntensity(v: unknown): number {
  return clamp(readFiniteNumber(v, DEFAULT_LAMP_INTENSITY), 0.2, 3.0);
}

function readHexColor(v: unknown, fallback: string): string {
  const s = typeof v === "string" ? v.trim() : "";
  const m = /^#?([0-9a-fA-F]{6})$/.exec(s);
  if (!m) return fallback;
  return `#${m[1].toLowerCase()}`;
}

function newId(): string {
  const cryptoAny = crypto as unknown as { randomUUID?: () => string };
  return cryptoAny.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readServers(settings: Record<string, unknown>): HaServer[] {
  const raw = settings.servers;
  if (!Array.isArray(raw)) return [];
  const out: HaServer[] = [];
  for (const item of raw) {
    const rec = asRecord(item);
    const host = asString(rec.host).trim();
    const apiKey = asString(rec.apiKey).trim();
    if (!host && !apiKey) continue;
    out.push({
      id: asString(rec.id) || newId(),
      name: asString(rec.name).trim(),
      host,
      apiKey,
    });
  }
  return out;
}

function isValidUrl(value: string): boolean {
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function readItemRefs(v: unknown): HaItemRef[] {
  if (!Array.isArray(v)) return [];
  const out: HaItemRef[] = [];
  for (const item of v) {
    const rec = asRecord(item);
    const kind = asString(rec.kind);
    const id = asString(rec.id).trim();
    if ((kind !== "entity" && kind !== "device") || !id) continue;
    out.push({
      kind,
      id,
      name: asString(rec.name).trim(),
      domain: asString(rec.domain).trim(),
      icon: asString(rec.icon).trim(),
      device_id: asString(rec.device_id).trim(),
    });
  }
  return out;
}

function sanitizeFaIconName(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/^fa-/, "")
    .replace(/[^a-z0-9-]/g, "")
    .slice(0, 64);
}

const FA_SVG_BY_NAME: Record<string, string> = {
  house: houseSvg,
  lightbulb: lightbulbSvg,
  "toggle-on": toggleOnSvg,
  fan: fanSvg,
  "temperature-half": temperatureHalfSvg,
  lock: lockSvg,
  "window-maximize": windowMaximizeSvg,
  video: videoSvg,
  tv: tvSvg,
};

type FaIconSvg = {
  viewBox: number[];
  path: string;
};

type FaIconFamilies = Record<
  string,
  {
    label?: string;
    search?: { terms?: string[] };
    svgs?: { classic?: { solid?: FaIconSvg } };
  }
>;

let faIconFamilies: FaIconFamilies | null = null;
let faIconFamiliesPromise: Promise<FaIconFamilies> | null = null;

function loadFaIconFamilies(): Promise<FaIconFamilies> {
  if (faIconFamilies) return Promise.resolve(faIconFamilies);
  if (faIconFamiliesPromise) return faIconFamiliesPromise;

  faIconFamiliesPromise = import("@fortawesome/fontawesome-free/metadata/icon-families.json")
    .then((m: any) => (m.default ?? m) as FaIconFamilies)
    .then((data) => {
      faIconFamilies = data;
      return data;
    })
    .finally(() => {
      faIconFamiliesPromise = null;
    });

  return faIconFamiliesPromise;
}

function normalizeFaSvgName(value: string): string {
  const key = sanitizeFaIconName(value);
  if (key === "thermometer-half" || key === "thermometer") return "temperature-half";
  return key;
}

function getFaSolidSvgFromFamilies(name: string): FaIconSvg | null {
  const key = normalizeFaSvgName(name);
  const entry = faIconFamilies?.[key];
  const svg = entry?.svgs?.classic?.solid;
  if (!svg?.path || !svg?.viewBox?.length) return null;
  return svg;
}

function buildSvgFromFaSolid(svg: FaIconSvg): string {
  const vb = svg.viewBox.join(" ");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}"><path d="${svg.path}"/></svg>`;
}

function isFaSolidIconAvailable(name: string): boolean {
  const key = normalizeFaSvgName(name);
  if (FA_SVG_BY_NAME[key]) return true;
  return Boolean(getFaSolidSvgFromFamilies(key));
}

function resolveFaSvg(value: string): { key: string; svgText: string } {
  const key = normalizeFaSvgName(value) || "house";

  const direct = FA_SVG_BY_NAME[key];
  if (direct) return { key, svgText: direct };

  const metaSvg = getFaSolidSvgFromFamilies(key);
  if (metaSvg) return { key, svgText: buildSvgFromFaSolid(metaSvg) };

  if (!faIconFamilies && !faIconFamiliesPromise) void loadFaIconFamilies();

  return { key: "house", svgText: FA_SVG_BY_NAME.house };
}

function isHaViewMode(value: unknown): value is HaViewMode {
  return value === "floor" || value === "ceiling" || value === "wall";
}

function readHaViewMode(value: unknown): HaViewMode {
  return isHaViewMode(value) ? value : "floor";
}

function isHaSpecialView(value: unknown): value is HaSpecialView {
  return value === "none" || value === "lamp";
}

function readHaSpecialView(value: unknown): HaSpecialView {
  return isHaSpecialView(value) ? value : "none";
}

function domainFromEntityId(entityId: string): string {
  const idx = entityId.indexOf(".");
  if (idx <= 0) return "";
  return entityId.slice(0, idx);
}

function suggestIconForDomain(domain: string): string {
  const d = domain.toLowerCase();
  if (d === "light") return "lightbulb";
  if (d === "switch") return "toggle-on";
  if (d === "fan") return "fan";
  if (d === "climate") return "thermometer-half";
  if (d === "lock") return "lock";
  if (d === "cover") return "window-maximize";
  if (d === "camera") return "video";
  if (d === "media_player") return "tv";
  return "house";
}

function isToggleDomain(domain: string): boolean {
  return PRIMARY_TOGGLE_DOMAINS.has(domain.toLowerCase());
}

function boolStateForDomain(domain: string, rawState: string): boolean | null {
  const d = domain.toLowerCase();
  const s = rawState.trim().toLowerCase();
  if (!s || s === "unknown" || s === "unavailable") return null;

  if (d === "lock") return s === "locked";
  if (d === "cover") return s === "closed" || s === "closing";
  if (d === "climate") return s !== "off";

  return s === "on";
}

type HaLiveState = { entity_id?: string; state?: string; attributes?: Record<string, any> };

type HaLiveServerStream = {
  counts: Map<string, number>;
  wanted: Set<string>;
  states: Map<string, HaLiveState>;
  listeners: Set<() => void>;
  es: EventSource | null;
  refreshTimer: number | null;
  restRefreshTimer: number | null;
  reconnectTimer: number | null;
  reconnectDelayMs: number;
  lastUrl: string;
};

const haLiveServers = new Map<string, HaLiveServerStream>();
const HA_LIVE_DEBUG = (() => {
  try {
    return typeof window !== "undefined" && window.localStorage?.getItem("toposync:debug_ha") === "1";
  } catch {
    return false;
  }
})();

function getLiveServerStream(serverId: string): HaLiveServerStream {
  const existing = haLiveServers.get(serverId);
  if (existing) return existing;
  const created: HaLiveServerStream = {
    counts: new Map(),
    wanted: new Set(),
    states: new Map(),
    listeners: new Set(),
    es: null,
    refreshTimer: null,
    restRefreshTimer: null,
    reconnectTimer: null,
    reconnectDelayMs: 500,
    lastUrl: "",
  };
  haLiveServers.set(serverId, created);
  return created;
}

function notifyLiveServer(serverId: string): void {
  const s = haLiveServers.get(serverId);
  if (!s) return;
  for (const fn of s.listeners) {
    try {
      fn();
    } catch {
      // ignore
    }
  }
  try {
    window.dispatchEvent(new CustomEvent("toposync:invalidate"));
  } catch {
    // ignore
  }
}

function buildStreamUrl(serverId: string, entityIds: Set<string>): string {
  const ids = Array.from(entityIds).slice(0, 300);
  return `/api/home_assistant/${encodeURIComponent(serverId)}/stream?entity_ids=${encodeURIComponent(ids.join(","))}`;
}

function scheduleRestRefresh(serverId: string): void {
  const stream = haLiveServers.get(serverId);
  if (!stream) return;
  if (stream.restRefreshTimer) return;
  stream.restRefreshTimer = window.setTimeout(() => {
    stream.restRefreshTimer = null;
    const ids = Array.from(stream.wanted);
    if (ids.length === 0) return;
    fetchStates(serverId, ids)
      .then((data) => {
        let changed = false;
        for (const [entityId, st] of Object.entries(data)) {
          if (!st || typeof st !== "object") continue;
          stream.states.set(entityId, st as HaLiveState);
          changed = true;
        }
        if (changed) notifyLiveServer(serverId);
      })
      .catch(() => {
        // ignore
      });
  }, 60);
}

function scheduleLiveReconnect(serverId: string): void {
  const stream = haLiveServers.get(serverId);
  if (!stream) return;
  if (stream.reconnectTimer) return;
  const delay = stream.reconnectDelayMs;
  stream.reconnectTimer = window.setTimeout(() => {
    stream.reconnectTimer = null;
    openLiveStream(serverId);
  }, delay);
  stream.reconnectDelayMs = Math.min(stream.reconnectDelayMs * 2, 10_000);
}

function openLiveStream(serverId: string): void {
  const stream = haLiveServers.get(serverId);
  if (!stream) return;

  if (stream.wanted.size === 0) {
    if (HA_LIVE_DEBUG) console.log("[HA live] closing stream (no wanted entities)", serverId);
    try {
      stream.es?.close();
    } catch {
      // ignore
    }
    stream.es = null;
    stream.lastUrl = "";
    return;
  }

  const url = buildStreamUrl(serverId, stream.wanted);
  if (!url) return;
  if (url === stream.lastUrl && stream.es) return;
  stream.lastUrl = url;

  try {
    stream.es?.close();
  } catch {
    // ignore
  }

  scheduleRestRefresh(serverId);

  if (HA_LIVE_DEBUG) console.log("[HA live] opening stream", { serverId, url, wanted: Array.from(stream.wanted) });
  const es = new EventSource(url);
  stream.es = es;

  es.onopen = () => {
    if (HA_LIVE_DEBUG) console.log("[HA live] stream open", { serverId, url });
  };

  es.onerror = () => {
    if (stream.es !== es) return;
    if (HA_LIVE_DEBUG) console.log("[HA live] stream error; scheduling reconnect", { serverId, url });
    try {
      es.close();
    } catch {
      // ignore
    }
    stream.es = null;
    stream.lastUrl = "";
    scheduleRestRefresh(serverId);
    scheduleLiveReconnect(serverId);
  };

  es.addEventListener("snapshot", (evt) => {
    try {
      const data = JSON.parse((evt as MessageEvent).data);
      if (!data || typeof data !== "object") return;
      for (const [entityId, st] of Object.entries(data as Record<string, any>)) {
        if (st && typeof st === "object") stream.states.set(entityId, st as HaLiveState);
      }
      stream.reconnectDelayMs = 500;
      notifyLiveServer(serverId);
    } catch {
      // ignore
    }
  });

  es.addEventListener("state_changed", (evt) => {
    try {
      const data = JSON.parse((evt as MessageEvent).data);
      const entityId = typeof data?.entity_id === "string" ? data.entity_id : "";
      if (!entityId) return;
      const state = data?.state;
      if (state && typeof state === "object") stream.states.set(entityId, state as HaLiveState);
      notifyLiveServer(serverId);
    } catch {
      // ignore
    }
  });
}

function scheduleLiveStreamRefresh(serverId: string): void {
  const stream = haLiveServers.get(serverId);
  if (!stream) return;
  if (stream.refreshTimer) return;
  stream.refreshTimer = window.setTimeout(() => {
    stream.refreshTimer = null;
    openLiveStream(serverId);
  }, 180);
}

function watchLiveStates(serverId: string, entityIds: string[]): () => void {
  const ids = entityIds.map((s) => s.trim()).filter(Boolean);
  if (!serverId || ids.length === 0) return () => {};

  const stream = getLiveServerStream(serverId);
  for (const id of ids) {
    const nextCount = (stream.counts.get(id) ?? 0) + 1;
    stream.counts.set(id, nextCount);
    stream.wanted.add(id);
  }
  scheduleLiveStreamRefresh(serverId);
  scheduleRestRefresh(serverId);

  return () => {
    const current = haLiveServers.get(serverId);
    if (!current) return;
    for (const id of ids) {
      const next = (current.counts.get(id) ?? 0) - 1;
      if (next <= 0) {
        current.counts.delete(id);
        current.wanted.delete(id);
        current.states.delete(id);
      } else {
        current.counts.set(id, next);
      }
    }
    scheduleLiveStreamRefresh(serverId);
  };
}

function subscribeLive(serverId: string, listener: () => void): () => void {
  if (!serverId) return () => {};
  const stream = getLiveServerStream(serverId);
  stream.listeners.add(listener);
  return () => {
    stream.listeners.delete(listener);
  };
}

function getLiveState(serverId: string, entityId: string): HaLiveState | null {
  if (!serverId || !entityId) return null;
  const stream = haLiveServers.get(serverId);
  if (!stream) return null;
  return stream.states.get(entityId) ?? null;
}

async function fetchHaServers(): Promise<HaServerPublic[]> {
  const res = await fetch("/api/home_assistant/servers");
  if (!res.ok) throw new Error(`Failed to list Home Assistant servers: ${res.status}`);
  const data = await res.json();
  return Array.isArray(data) ? (data as HaServerPublic[]) : [];
}

async function fetchRegistry(serverId: string): Promise<RegistryResponse> {
  const res = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/registry`);
  if (!res.ok) throw new Error(`Failed to load Home Assistant registry: ${res.status}`);
  return res.json();
}

async function fetchStates(serverId: string, entityIds: string[]): Promise<Record<string, any>> {
  const ids = entityIds.map((s) => s.trim()).filter(Boolean);
  if (ids.length === 0) return {};
  const res = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/states`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ entity_ids: ids }),
  });
  if (!res.ok) throw new Error(`Failed to fetch entity states: ${res.status}`);
  const data = await res.json();
  return data && typeof data === "object" ? (data as Record<string, any>) : {};
}

function itemValue(kind: "entity" | "device", id: string): string {
  return `${kind}:${id}`;
}

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(translations);
  host.registerSettingsPanel(settingsPanel());
  host.registerElementType(homeAssistantElementType(host.i18n));
  host.registerEditorTool(addHomeAssistantTool(host.i18n));
}

const translations = {
  en: {
    "ext.home_assistant.element.name": "Home Assistant item",
    "ext.home_assistant.element.desc": "Place one or more entities/devices from Home Assistant on the scene.",
    "ext.home_assistant.tool.add": "Home Assistant",
    "ext.home_assistant.tool.add_desc": "Click to place a Home Assistant item and configure it.",
    "ext.home_assistant.settings.name": "Home Assistant",
    "ext.home_assistant.settings.desc": "Configure one or more Home Assistant servers to connect and integrate.",
    "ext.home_assistant.settings.notice":
      "Your API token is stored locally in Toposync configuration (local-first).",
    "ext.home_assistant.settings.servers": "Servers",
    "ext.home_assistant.settings.add": "Add server",
    "ext.home_assistant.settings.empty": "No servers yet.",
    "ext.home_assistant.settings.server_name": "Name (optional)",
    "ext.home_assistant.settings.host": "Host URL",
    "ext.home_assistant.settings.api_key": "API token",
    "ext.home_assistant.settings.show_key": "Show token",
    "ext.home_assistant.settings.hide_key": "Hide token",
    "ext.home_assistant.settings.invalid_host": "Use a full URL (http:// or https://).",
    "ext.home_assistant.settings.unsaved": "Unsaved changes",
    "ext.home_assistant.editor.no_servers": "Add a Home Assistant server in Settings first.",
    "ext.home_assistant.editor.server": "Server",
    "ext.home_assistant.editor.items": "Entities / devices",
    "ext.home_assistant.editor.items_placeholder": "Select entities and/or devices…",
    "ext.home_assistant.editor.group_entities": "Entities",
    "ext.home_assistant.editor.group_devices": "Devices",
    "ext.home_assistant.editor.icon": "Icon",
    "ext.home_assistant.editor.icon_hint": "Font Awesome Free (solid) icons. Some names may not exist in the free set.",
    "ext.home_assistant.editor.icon_search": "Search icons…",
    "ext.home_assistant.editor.icon_loading": "Loading icons…",
    "ext.home_assistant.editor.icon_suggested": "Suggested icons",
    "ext.home_assistant.editor.icon_results": "{{count}} results",
    "ext.home_assistant.editor.icon_no_results": "No icons found.",
    "ext.home_assistant.editor.icon_not_found": "Icon not found in Font Awesome Free.",
    "ext.home_assistant.editor.view_mode": "View mode",
    "ext.home_assistant.editor.view_mode.floor": "Floor",
    "ext.home_assistant.editor.view_mode.ceiling": "Ceiling",
    "ext.home_assistant.editor.view_mode.wall": "Wall",
    "ext.home_assistant.editor.special_view": "Special view",
    "ext.home_assistant.editor.special_view.none": "None",
    "ext.home_assistant.editor.special_view.lamp": "Lamp",
    "ext.home_assistant.editor.special_view.hint": "Available when a single on/off entity is selected.",
    "ext.home_assistant.editor.lamp_intensity": "Light intensity",
    "ext.home_assistant.editor.lamp_color": "Light color",
    "ext.home_assistant.action.toggle": "Toggle",
    "ext.home_assistant.action.loading": "Loading…",
    "ext.home_assistant.action.no_items": "No entities/devices selected.",
  },
  "pt-BR": {
    "ext.home_assistant.element.name": "Item Home Assistant",
    "ext.home_assistant.element.desc": "Coloque uma ou mais entidades/dispositivos do Home Assistant na cena.",
    "ext.home_assistant.tool.add": "Home Assistant",
    "ext.home_assistant.tool.add_desc": "Clique para posicionar um item do Home Assistant e configurá-lo.",
    "ext.home_assistant.settings.name": "Home Assistant",
    "ext.home_assistant.settings.desc":
      "Configure um ou mais servidores do Home Assistant para conectar e integrar.",
    "ext.home_assistant.settings.notice":
      "Seu token de API é armazenado localmente na configuração do Toposync (local-first).",
    "ext.home_assistant.settings.servers": "Servidores",
    "ext.home_assistant.settings.add": "Adicionar servidor",
    "ext.home_assistant.settings.empty": "Nenhum servidor ainda.",
    "ext.home_assistant.settings.server_name": "Nome (opcional)",
    "ext.home_assistant.settings.host": "URL do host",
    "ext.home_assistant.settings.api_key": "Token de API",
    "ext.home_assistant.settings.show_key": "Mostrar token",
    "ext.home_assistant.settings.hide_key": "Ocultar token",
    "ext.home_assistant.settings.invalid_host": "Use uma URL completa (http:// ou https://).",
    "ext.home_assistant.settings.unsaved": "Alterações não salvas",
    "ext.home_assistant.editor.no_servers": "Adicione um servidor do Home Assistant nas Configurações primeiro.",
    "ext.home_assistant.editor.server": "Servidor",
    "ext.home_assistant.editor.items": "Entidades / dispositivos",
    "ext.home_assistant.editor.items_placeholder": "Selecione entidades e/ou dispositivos…",
    "ext.home_assistant.editor.group_entities": "Entidades",
    "ext.home_assistant.editor.group_devices": "Dispositivos",
    "ext.home_assistant.editor.icon": "Ícone",
    "ext.home_assistant.editor.icon_hint": "Ícones Font Awesome Free (solid). Alguns nomes não existem no pacote free.",
    "ext.home_assistant.editor.icon_search": "Buscar ícones…",
    "ext.home_assistant.editor.icon_loading": "Carregando ícones…",
    "ext.home_assistant.editor.icon_suggested": "Ícones sugeridos",
    "ext.home_assistant.editor.icon_results": "{{count}} resultados",
    "ext.home_assistant.editor.icon_no_results": "Nenhum ícone encontrado.",
    "ext.home_assistant.editor.icon_not_found": "Ícone não encontrado no Font Awesome Free.",
    "ext.home_assistant.editor.view_mode": "Visualização",
    "ext.home_assistant.editor.view_mode.floor": "Chão",
    "ext.home_assistant.editor.view_mode.ceiling": "Teto",
    "ext.home_assistant.editor.view_mode.wall": "Parede",
    "ext.home_assistant.editor.special_view": "Visualização especial",
    "ext.home_assistant.editor.special_view.none": "Nenhuma",
    "ext.home_assistant.editor.special_view.lamp": "Luminária",
    "ext.home_assistant.editor.special_view.hint": "Disponível quando apenas um item liga/desliga estiver selecionado.",
    "ext.home_assistant.editor.lamp_intensity": "Intensidade da luz",
    "ext.home_assistant.editor.lamp_color": "Cor da luz",
    "ext.home_assistant.action.toggle": "Alternar",
    "ext.home_assistant.action.loading": "Carregando...",
    "ext.home_assistant.action.no_items": "Nenhuma entidade/dispositivo selecionado.",
  },
} as const;

function settingsPanel(): SettingsPanel {
  return {
    id: EXTENSION_ID,
    icon: "house",
    name: { key: "ext.home_assistant.settings.name", fallback: "Home Assistant" },
    description: { key: "ext.home_assistant.settings.desc" },
    render: ({ i18n, settings, updateSettings }) => (
      <HomeAssistantSettings i18n={i18n} settings={settings} updateSettings={updateSettings} />
    ),
  };
}

function HomeAssistantSettings({
  i18n,
  settings,
  updateSettings,
}: {
  i18n: TopoSyncHost["i18n"];
  settings: Record<string, unknown>;
  updateSettings: (patch: Record<string, unknown>) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const serversFromSettings = useMemo(() => readServers(settings), [settings]);

  const [draftServers, setDraftServers] = useState<HaServer[]>(serversFromSettings);
  const [dirty, setDirty] = useState(false);
  const [showKeysById, setShowKeysById] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (dirty) return;
    setDraftServers(serversFromSettings);
  }, [dirty, serversFromSettings]);

  const hasInvalidHosts = useMemo(
    () => draftServers.some((s) => s.host.trim() !== "" && !isValidUrl(s.host.trim())),
    [draftServers],
  );

  const canSave = useMemo(() => {
    if (draftServers.length === 0) return true;
    return draftServers.every((s) => Boolean(s.host.trim()) && Boolean(s.apiKey.trim()) && isValidUrl(s.host.trim()));
  }, [draftServers]);

  return (
    <div>
      <div className="card">
        <div className="cardBody">{t("ext.home_assistant.settings.notice")}</div>
      </div>

      <div className="sectionDivider" />

      <div className="rowWrap" style={{ justifyContent: "space-between", gap: 10 }}>
        <div>
          <div className="modalSectionTitle" style={{ marginBottom: 6 }}>
            {t("ext.home_assistant.settings.servers")}
          </div>
          {dirty ? <div className="label">{t("ext.home_assistant.settings.unsaved")}</div> : null}
        </div>

        <div className="row" style={{ gap: 10 }}>
          <button
            className="iconButton iconButtonPrimary"
            type="button"
            aria-label={t("ext.home_assistant.settings.add")}
            onClick={() => {
              setDraftServers((prev) => [
                { id: newId(), name: "", host: "", apiKey: "" },
                ...prev,
              ]);
              setDirty(true);
            }}
          >
            <i className="fa-solid fa-plus" aria-hidden="true" />
          </button>

          <button
            className="primaryButton"
            type="button"
            disabled={!canSave || !dirty}
            onClick={() => {
              updateSettings({ servers: draftServers });
              setDirty(false);
            }}
          >
            {t("core.actions.save")}
          </button>

          <button
            className="chipButton"
            type="button"
            disabled={!dirty}
            onClick={() => {
              setDraftServers(serversFromSettings);
              setDirty(false);
            }}
          >
            {t("core.actions.cancel")}
          </button>
        </div>
      </div>

      {hasInvalidHosts ? (
        <div className="card" style={{ marginTop: 10 }}>
          <div className="cardBody">{t("ext.home_assistant.settings.invalid_host")}</div>
        </div>
      ) : null}

      <div style={{ height: 10 }} />

      {draftServers.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.settings.empty")}</div>
        </div>
      ) : (
        <div className="choiceList">
          {draftServers.map((srv) => {
            const showKey = Boolean(showKeysById[srv.id]);
            const title = srv.name.trim() || srv.host.trim() || t("ext.home_assistant.settings.name");
            return (
              <div className="card" key={srv.id}>
                <div className="cardHeaderRow">
                  <div style={{ minWidth: 0 }}>
                    <div className="cardTitle" style={{ marginBottom: 2 }}>
                      {title}
                    </div>
                    <div className="cardMeta" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                      {srv.host || "—"}
                    </div>
                  </div>
                  <button
                    className="iconButton iconButtonDanger"
                    type="button"
                    aria-label={t("core.actions.delete")}
                    onClick={() => {
                      setDraftServers((prev) => prev.filter((s) => s.id !== srv.id));
                      setDirty(true);
                    }}
                  >
                    <i className="fa-solid fa-trash" aria-hidden="true" />
                  </button>
                </div>

                <div className="cardBody">
                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.server_name")}</div>
                    <input
                      className="input"
                      value={srv.name}
                      onChange={(e) => {
                        const value = e.target.value;
                        setDraftServers((prev) => prev.map((s) => (s.id === srv.id ? { ...s, name: value } : s)));
                        setDirty(true);
                      }}
                      placeholder="Home"
                    />
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.host")}</div>
                    <input
                      className="input"
                      value={srv.host}
                      onChange={(e) => {
                        const value = e.target.value;
                        setDraftServers((prev) => prev.map((s) => (s.id === srv.id ? { ...s, host: value } : s)));
                        setDirty(true);
                      }}
                      placeholder="http://homeassistant.local:8123"
                    />
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.api_key")}</div>
                    <div className="row" style={{ gap: 10 }}>
                      <input
                        className="input"
                        style={{ flex: 1, minWidth: 0 }}
                        type={showKey ? "text" : "password"}
                        value={srv.apiKey}
                        onChange={(e) => {
                          const value = e.target.value;
                          setDraftServers((prev) => prev.map((s) => (s.id === srv.id ? { ...s, apiKey: value } : s)));
                          setDirty(true);
                        }}
                        placeholder="••••••••••••••••"
                      />
                      <button
                        className="iconButton"
                        type="button"
                        aria-label={showKey ? t("ext.home_assistant.settings.hide_key") : t("ext.home_assistant.settings.show_key")}
                        onClick={() =>
                          setShowKeysById((prev) => ({
                            ...prev,
                            [srv.id]: !prev[srv.id],
                          }))
                        }
                      >
                        <i className={["fa-solid", showKey ? "fa-eye-slash" : "fa-eye"].join(" ")} aria-hidden="true" />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function homeAssistantElementType(i18n: HostI18n): ElementType {
  const iconGeometryCache = new Map<string, { geometry: any; scale: number }>();
  const ICON_TARGET_SIZE = 0.14;

  const BUTTON_RADIUS = 0.18;
  const BUTTON_THETA_TOP_CUT = 1.05;

  return {
    type: ELEMENT_TYPE_ID,
    name: { key: "ext.home_assistant.element.name", fallback: "Home Assistant item" },
    description: { key: "ext.home_assistant.element.desc" },
    placeable: false,
    defaultProps: {
      server_id: "",
      items: [],
      icon: "house",
      primary_entity_id: "",
      primary_state: "",
      view_mode: "floor",
      special_view: "none",
      lamp_intensity: DEFAULT_LAMP_INTENSITY,
      lamp_color: DEFAULT_LAMP_COLOR,
    },
    primaryAction: async ({ element, api, update }) => {
      const props = asRecord(element.props);
      const serverId = asString(props.server_id).trim();
      const entityId = asString(props.primary_entity_id).trim();
      if (!serverId || !entityId) return false;
      const domain = domainFromEntityId(entityId);
      if (!isToggleDomain(domain)) return false;

      const res = await api.emitEvent("home_assistant.primary_action_requested", {
        server_id: serverId,
        entity_id: entityId,
      });
      const state = (res as any)?.result?.state;
      if (typeof state === "string") {
        update({ props: { primary_state: state } });
        const stream = getLiveServerStream(serverId);
        const prev = stream.states.get(entityId) ?? { entity_id: entityId };
        stream.states.set(entityId, { ...prev, entity_id: entityId, state });
        notifyLiveServer(serverId);
      }
      return true;
    },
    create3D: ({ THREE, view }, element) => {
      function getIconGeometry(iconName: string): { geometry: any; scale: number; key: string } {
        const resolved = resolveFaSvg(iconName);
        const cached = iconGeometryCache.get(resolved.key);
        if (cached) return { ...cached, key: resolved.key };

        const data = new SVGLoader().parse(resolved.svgText);

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
        iconGeometryCache.set(resolved.key, entry);
        return { ...entry, key: resolved.key };
      }

      const NEON_DEFAULT = 0x38bdf8;
      const NEON_ON = 0x22c55e;
      const NEON_OFF = 0xef4444;

      const group = new THREE.Group();
      const mountGroup = new THREE.Group();
      group.add(mountGroup);

      const topY = BUTTON_RADIUS * Math.cos(BUTTON_THETA_TOP_CUT);
      const topRadius = BUTTON_RADIUS * Math.sin(BUTTON_THETA_TOP_CUT);

      const domeFloorGeom = new THREE.SphereGeometry(
        BUTTON_RADIUS,
        56,
        28,
        0,
        Math.PI * 2,
        BUTTON_THETA_TOP_CUT,
        Math.PI / 2 - BUTTON_THETA_TOP_CUT,
      );
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
        emissive: new THREE.Color(NEON_DEFAULT),
        emissiveIntensity: 0.85,
        roughness: 0.32,
        metalness: 0.0,
      });
      const cutMat = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide });
      const iconMat = new THREE.MeshBasicMaterial({ color: NEON_DEFAULT, side: THREE.DoubleSide });
      iconMat.depthWrite = false;
      iconMat.polygonOffset = true;
      iconMat.polygonOffsetFactor = -1;
      iconMat.polygonOffsetUnits = -1;

      const dome = new THREE.Mesh(domeFloorGeom, sphereMat);
      mountGroup.add(dome);

      const topCapGeom = new THREE.CircleGeometry(topRadius, 48);
      const topCap = new THREE.Mesh(topCapGeom, cutMat);
      topCap.rotation.x = -Math.PI / 2;
      topCap.position.set(0, topY, 0);
      mountGroup.add(topCap);

      const bottomCapGeom = new THREE.CircleGeometry(BUTTON_RADIUS, 48);
      const bottomCap = new THREE.Mesh(bottomCapGeom, cutMat);
      bottomCap.rotation.x = Math.PI / 2;
      bottomCap.position.set(0, 0, 0);
      mountGroup.add(bottomCap);

      const light = new THREE.PointLight(NEON_DEFAULT, 0.9, 1.15, 2.2);
      light.position.set(0, topY * 0.6, 0);
      mountGroup.add(light);

      const houseGeo = getIconGeometry("house");
      const iconMesh = new THREE.Mesh(houseGeo.geometry, iconMat);
      iconMesh.scale.setScalar(houseGeo.scale);
      iconMesh.position.set(0, topY + 0.002, 0);
      iconMesh.renderOrder = 10;
      mountGroup.add(iconMesh);

      let wantedIconKey = "house";
      let currentIconKey = houseGeo.key;
      let currentViewMode: HaViewMode = "floor";
      let currentEl = element;
      let currentSpecialView: HaSpecialView = "none";
      let currentItemCount = 0;
      const lampColor = new THREE.Color(DEFAULT_LAMP_COLOR);
      let lampIntensity = DEFAULT_LAMP_INTENSITY;

      let unwatch: (() => void) | null = null;
      let watchedServer = "";
      let watchedEntity = "";
      let watchedDomain = "";
      let watchedIsToggle = false;
      let lastState = "";

      function applyNeonFromState(stateRaw: string) {
        const s = stateRaw.trim().toLowerCase();
        const boolState = watchedEntity ? boolStateForDomain(watchedDomain, s) : null;
        const canLamp =
          currentSpecialView === "lamp" &&
          currentItemCount === 1 &&
          watchedDomain &&
          LAMP_COMPAT_DOMAINS.has(watchedDomain.toLowerCase());

        if (canLamp) {
          const on = boolState === true;
          const unknown = boolState == null;

          const neon = on ? lampColor : 0x000000;
          sphereMat.emissive.set(neon);
          iconMat.color.set(on ? lampColor : unknown ? 0x334155 : 0x111827);
          light.color.set(lampColor);

          if (on) {
            const amp = clamp(lampIntensity, 0.2, 3.0);
            sphereMat.emissiveIntensity = 0.55 + 0.75 * amp;
            light.intensity = 1.8 * amp;
            light.distance = 4.5 + 3.5 * amp;
          } else if (unknown) {
            sphereMat.emissiveIntensity = 0.22;
            light.intensity = 0.0;
            light.distance = 0.0;
          } else {
            sphereMat.emissiveIntensity = 0.08;
            light.intensity = 0.0;
            light.distance = 0.0;
          }
          return;
        }

        const neon = watchedIsToggle
          ? boolState === true
            ? NEON_ON
            : boolState === false
              ? NEON_OFF
              : NEON_DEFAULT
          : NEON_DEFAULT;

        sphereMat.emissive.set(neon);
        iconMat.color.set(neon);
        light.color.set(neon);

        // Subtle indicator glow (non-special).
        sphereMat.emissiveIntensity = watchedIsToggle
          ? boolState === true
            ? 0.55
            : boolState === false
              ? 0.35
              : 0.42
          : 0.42;
        light.intensity = watchedIsToggle
          ? boolState === true
            ? 0.25
            : boolState === false
              ? 0.12
              : 0.16
          : 0.16;
        light.distance = 1.6;
      }

      function applyViewMode(mode: HaViewMode) {
        if (mode !== currentViewMode) {
          currentViewMode = mode;
          if (mode === "ceiling") {
            dome.geometry = domeCeilingGeom;
            bottomCap.visible = false;
          } else {
            dome.geometry = domeFloorGeom;
            bottomCap.visible = true;
          }
        }

        mountGroup.rotation.set(0, 0, 0);
        mountGroup.position.set(0, 0, 0);

        if (mode === "ceiling") {
          mountGroup.position.y = view.wallHeight - topY;
        } else if (mode === "wall") {
          mountGroup.position.y = view.wallHeight / 2;
          mountGroup.rotation.x = Math.PI / 2;
        }
      }

      function apply(el: CompositionElement) {
        currentEl = el;
        const p = asRecord(el.props);
        const icon = sanitizeFaIconName(asString(p.icon, "house")) || "house";
        const viewMode = readHaViewMode(p.view_mode);
        const specialView = readHaSpecialView(p.special_view);
        const itemsRaw = p.items;
        const itemCount = Array.isArray(itemsRaw) ? itemsRaw.length : 0;
        const primaryEntityId = asString(p.primary_entity_id).trim();
        const serverId = asString(p.server_id).trim();
        if (serverId !== watchedServer || primaryEntityId !== watchedEntity) {
          unwatch?.();
          unwatch = null;
          watchedServer = serverId;
          watchedEntity = primaryEntityId;
          watchedDomain = primaryEntityId ? domainFromEntityId(primaryEntityId) : "";
          watchedIsToggle = watchedDomain ? isToggleDomain(watchedDomain) : false;
          lastState = "";
          if (serverId && primaryEntityId) unwatch = watchLiveStates(serverId, [primaryEntityId]);
        }

        const live = watchedServer && watchedEntity ? getLiveState(watchedServer, watchedEntity) : null;
        const primaryState = asString(live?.state ?? p.primary_state);

        applyViewMode(viewMode);

        currentItemCount = itemCount;
        currentSpecialView = specialView;
        lampColor.set(readHexColor(p.lamp_color, DEFAULT_LAMP_COLOR));
        lampIntensity = readLampIntensity(p.lamp_intensity);
        if (
          currentSpecialView === "lamp" &&
          !(itemCount === 1 && watchedDomain && LAMP_COMPAT_DOMAINS.has(watchedDomain.toLowerCase()))
        ) {
          currentSpecialView = "none";
        }

        wantedIconKey = normalizeFaSvgName(icon) || "house";
        const entry = getIconGeometry(wantedIconKey);
        if (entry.key !== currentIconKey) {
          currentIconKey = entry.key;
          iconMesh.geometry = entry.geometry;
          iconMesh.scale.setScalar(entry.scale);
        }

        lastState = primaryState.trim().toLowerCase();
        applyNeonFromState(primaryState);
      }

      apply(element);

      return {
        object: group,
        update: apply,
        tick: () => {
          if (watchedServer && watchedEntity) {
            const live = getLiveState(watchedServer, watchedEntity);
            const next = asString(live?.state).trim().toLowerCase();
            if (next && next !== lastState) {
              lastState = next;
              applyNeonFromState(next);
            }
          }

          if (wantedIconKey === currentIconKey) return;
          if (!isFaSolidIconAvailable(wantedIconKey)) return;
          const entry = getIconGeometry(wantedIconKey);
          if (entry.key === currentIconKey) return;
          currentIconKey = entry.key;
          iconMesh.geometry = entry.geometry;
          iconMesh.scale.setScalar(entry.scale);
        },
        dispose: () => {
          unwatch?.();
          domeFloorGeom.dispose();
          domeCeilingGeom.dispose();
          topCapGeom.dispose();
          bottomCapGeom.dispose();
          sphereMat.dispose();
          cutMat.dispose();
          iconMat.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const p = asRecord(element.props);
      const primaryEntityId = asString(p.primary_entity_id).trim();
      const serverId = asString(p.server_id).trim();
      const live = serverId && primaryEntityId ? getLiveState(serverId, primaryEntityId) : null;
      const primaryState = asString(live?.state ?? p.primary_state).trim().toLowerCase();
      const domain = primaryEntityId ? domainFromEntityId(primaryEntityId) : "";
      const isToggle = primaryEntityId ? isToggleDomain(domain) : false;
      const boolState = primaryEntityId ? boolStateForDomain(domain, primaryState) : null;

      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const r = 11;

      const fill = isToggle
        ? boolState === true
          ? "rgba(34,197,94,0.22)"
          : boolState === false
            ? "rgba(239,68,68,0.18)"
            : "rgba(56,189,248,0.14)"
        : "rgba(56,189,248,0.14)";
      const stroke = isToggle
        ? boolState === true
          ? "rgba(34,197,94,0.72)"
          : boolState === false
            ? "rgba(239,68,68,0.72)"
            : "rgba(230,232,242,0.24)"
        : "rgba(230,232,242,0.24)";

      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, Math.PI * 2);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = "rgba(230,232,242,0.92)";
      ctx.font = "700 11px system-ui, -apple-system, Segoe UI, Roboto, Arial";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("HA", 0, 0);
      ctx.restore();
    },
    hitTest2D: ({ element, world }) => {
      const dx = world.x - element.position.x;
      const dz = world.z - element.position.z;
      return dx * dx + dz * dz <= 0.25 * 0.25;
    },
    renderActionModal: ({ element, update, close, api }) => (
      <HomeAssistantAction element={element} update={update} close={close} api={api} i18n={i18n} />
    ),
    renderEditorModal: ({ element, update, remove, close }) => (
      <HomeAssistantEditor element={element} update={update} remove={remove} close={close} i18n={i18n} />
    ),
  };
}

function addHomeAssistantTool(i18n: HostI18n): EditorTool {
  return {
    id: TOOL_ID_ADD,
    name: { key: "ext.home_assistant.tool.add", fallback: "Home Assistant" },
    description: { key: "ext.home_assistant.tool.add_desc" },
    icon: "house",
    createSession: ({ createElement, openEditor }) => ({
      onPointerEvent: (evt) => {
        if (evt.kind !== "down") return;
        if (evt.button !== 0) return;
        const id = createElement(ELEMENT_TYPE_ID, {
          name: "",
          position: { x: evt.world.x, y: 0, z: evt.world.z },
          props: {
            server_id: "",
            items: [],
            icon: "house",
            primary_entity_id: "",
            primary_state: "",
            view_mode: "floor",
            special_view: "none",
            lamp_intensity: DEFAULT_LAMP_INTENSITY,
            lamp_color: DEFAULT_LAMP_COLOR,
          },
        });
        if (id) openEditor(id);
      },
    }),
  };
}

type ActionProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  close: () => void;
  api: TopoSyncHost["api"];
  i18n: HostI18n;
};

function HomeAssistantAction({ element, update, close, api, i18n }: ActionProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const props = asRecord(element.props);
  const serverId = asString(props.server_id).trim();
  const items = useMemo(() => readItemRefs(props.items), [props.items]);
  const primaryEntityId = asString(props.primary_entity_id).trim();

  const [registry, setRegistry] = useState<RegistryResponse | null>(null);
  const [states, setStates] = useState<Record<string, any>>({});
  const [busyEntity, setBusyEntity] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const selectedEntityIds = useMemo(() => {
    const out = new Set<string>();
    for (const it of items) {
      if (it.kind === "entity") out.add(it.id);
      if (it.kind === "device" && registry?.device_entities?.[it.id]) {
        for (const eid of registry.device_entities[it.id] ?? []) out.add(eid);
      }
    }
    return [...out];
  }, [items, registry]);

  useEffect(() => {
    if (!serverId) return;
    const hasDevices = items.some((i) => i.kind === "device");
    if (!hasDevices) {
      setRegistry(null);
      return;
    }
    let cancelled = false;
    fetchRegistry(serverId)
      .then((data) => {
        if (!cancelled) setRegistry(data);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [items, serverId]);

  useEffect(() => {
    if (!serverId) return;
    let cancelled = false;
    setErr(null);
    fetchStates(serverId, selectedEntityIds)
      .then((data) => {
        if (!cancelled) setStates(data);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [serverId, selectedEntityIds.join("|")]);

  useEffect(() => {
    if (!serverId || selectedEntityIds.length === 0) return;
    const unwatch = watchLiveStates(serverId, selectedEntityIds);
    const unsub = subscribeLive(serverId, () => {
      setStates((prev) => {
        const next = { ...prev };
        for (const eid of selectedEntityIds) {
          const live = getLiveState(serverId, eid);
          if (live?.state) next[eid] = { ...(next[eid] ?? {}), entity_id: eid, state: live.state, attributes: live.attributes };
        }
        return next;
      });
    });
    return () => {
      unwatch();
      unsub();
    };
  }, [serverId, selectedEntityIds.join("|")]);

  async function toggle(entityId: string) {
    if (!serverId) return;
    setBusyEntity(entityId);
    setErr(null);
    try {
      const res = await api.emitEvent("home_assistant.primary_action_requested", {
        server_id: serverId,
        entity_id: entityId,
      });
      const state = (res as any)?.result?.state;
      if (typeof state === "string") {
        setStates((prev) => ({
          ...prev,
          [entityId]: { ...(prev[entityId] ?? {}), entity_id: entityId, state },
        }));
        if (entityId === primaryEntityId) update({ props: { primary_state: state } });
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyEntity(null);
    }
  }

  const entityRows = useMemo(() => {
    return selectedEntityIds.map((entityId) => {
      const st = states[entityId] ?? null;
      const state = typeof st?.state === "string" ? st.state : null;
      const domain = domainFromEntityId(entityId);
      const canToggle = isToggleDomain(domain);
      const label =
        asString(st?.attributes?.friendly_name).trim() ||
        items.find((i) => i.kind === "entity" && i.id === entityId)?.name ||
        entityId;

      return { entityId, label, state, canToggle };
    });
  }, [items, selectedEntityIds, states]);

  return (
    <div>
      {!serverId ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.editor.no_servers")}</div>
        </div>
      ) : null}

      {items.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.action.no_items")}</div>
        </div>
      ) : (
        <div className="choiceList">
          {entityRows.map((row) => (
            <div className="card" key={row.entityId}>
              <div className="cardHeaderRow">
                <div style={{ minWidth: 0 }}>
                  <div className="cardTitle" style={{ marginBottom: 2 }}>
                    {row.label}
                  </div>
                  <div className="cardMeta" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                    {row.entityId}
                    {row.state ? ` • ${row.state}` : ""}
                  </div>
                </div>
                <button
                  className="iconButton iconButtonPrimary"
                  type="button"
                  disabled={!row.canToggle || busyEntity === row.entityId}
                  aria-label={t("ext.home_assistant.action.toggle")}
                  onClick={() => toggle(row.entityId)}
                >
                  <i className={["fa-solid", busyEntity === row.entityId ? "fa-spinner" : "fa-power-off"].join(" ")} aria-hidden="true" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {err ? (
        <>
          <div className="sectionDivider" />
          <div className="cardBody" style={{ color: "rgba(252,165,165,0.92)" }}>
            {err}
          </div>
        </>
      ) : null}

      <div className="sectionDivider" />
      <div className="rowWrap">
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}

type EditorProps = {
  element: CompositionElement;
  update: (patch: CompositionElementPatch) => void;
  remove: () => void;
  close: () => void;
  i18n: HostI18n;
};

function HomeAssistantEditor({ element, update, remove, close, i18n }: EditorProps): React.ReactElement {
  const { t } = i18n.useI18n();

  const props = asRecord(element.props);
  const serverId = asString(props.server_id).trim();
  const icon = sanitizeFaIconName(asString(props.icon, "house")) || "house";
  const viewMode = readHaViewMode(props.view_mode);
  const specialView = readHaSpecialView(props.special_view);
  const primaryEntityId = asString(props.primary_entity_id).trim();
  const lampIntensityValue = readLampIntensity(props.lamp_intensity);
  const lampColorValue = readHexColor(props.lamp_color, DEFAULT_LAMP_COLOR);
  const items = useMemo(() => readItemRefs(props.items), [props.items]);

  const [servers, setServers] = useState<HaServerPublic[]>([]);
  const [registry, setRegistry] = useState<RegistryResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [isIconPickerOpen, setIsIconPickerOpen] = useState(false);
  const [iconSearch, setIconSearch] = useState("");
  const [iconFamilies, setIconFamilies] = useState<FaIconFamilies | null>(faIconFamilies);
  const [iconLoadError, setIconLoadError] = useState<string | null>(null);
  const [iconLoading, setIconLoading] = useState(false);
  const iconSearchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchHaServers()
      .then((data) => {
        if (!cancelled) setServers(data);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!isIconPickerOpen) return;

    let cancelled = false;
    setIconLoadError(null);

    if (faIconFamilies) {
      setIconFamilies(faIconFamilies);
      return;
    }

    setIconLoading(true);
    loadFaIconFamilies()
      .then((data) => {
        if (!cancelled) setIconFamilies(data);
      })
      .catch((e) => {
        if (!cancelled) setIconLoadError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setIconLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [isIconPickerOpen]);

  useEffect(() => {
    if (!isIconPickerOpen) return;
    const id = window.setTimeout(() => iconSearchRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [isIconPickerOpen]);

  useEffect(() => {
    if (serverId) return;
    if (servers.length === 1) update({ props: { server_id: servers[0].id } });
  }, [serverId, servers, update]);

  const canLamp = useMemo(() => {
    if (items.length !== 1) return false;
    if (!primaryEntityId) return false;
    const d = domainFromEntityId(primaryEntityId).toLowerCase();
    return LAMP_COMPAT_DOMAINS.has(d);
  }, [items.length, primaryEntityId]);

  useEffect(() => {
    if (specialView === "lamp" && !canLamp) update({ props: { special_view: "none" } });
  }, [canLamp, specialView, update]);

  useEffect(() => {
    if (!serverId) {
      setRegistry(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetchRegistry(serverId)
      .then((data) => {
        if (!cancelled) setRegistry(data);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [serverId]);

  const options = useMemo(() => {
    const entities: HaItemOption[] = (registry?.entities ?? []).map((e) => ({
      value: itemValue("entity", e.entity_id),
      label: e.name || e.entity_id,
      kind: "entity",
      id: e.entity_id,
      meta: { subLabel: e.entity_id, icon: e.icon, domain: e.domain, deviceId: e.device_id },
    }));
    const devices: HaItemOption[] = (registry?.devices ?? []).map((d) => ({
      value: itemValue("device", d.id),
      label: d.name || d.id,
      kind: "device",
      id: d.id,
      meta: { subLabel: d.id },
    }));
    const groups: Array<GroupBase<HaItemOption>> = [];
    if (entities.length > 0) groups.push({ label: t("ext.home_assistant.editor.group_entities"), options: entities });
    if (devices.length > 0) groups.push({ label: t("ext.home_assistant.editor.group_devices"), options: devices });
    return groups;
  }, [registry, t]);

  const optionByValue = useMemo(() => {
    const out: Record<string, HaItemOption> = {};
    for (const group of options) for (const opt of group.options) out[opt.value] = opt;
    return out;
  }, [options]);

  const selectedOptions = useMemo(() => {
    return items.map((ref) => optionByValue[itemValue(ref.kind, ref.id)] ?? { value: itemValue(ref.kind, ref.id), label: ref.name || ref.id, kind: ref.kind, id: ref.id });
  }, [items, optionByValue]);

  const selectStyles: StylesConfig<HaItemOption, true, GroupBase<HaItemOption>> = useMemo(
    () => ({
      control: (base, state) => ({
        ...base,
        minHeight: 36,
        borderRadius: 12,
        borderColor: state.isFocused ? "rgba(251,191,36,0.45)" : "rgba(255,255,255,0.10)",
        backgroundColor: "rgba(0,0,0,0.20)",
        boxShadow: "none",
      }),
      input: (base) => ({ ...base, color: "rgba(230,232,242,0.92)" }),
      multiValue: (base) => ({
        ...base,
        borderRadius: 999,
        backgroundColor: "rgba(255,255,255,0.08)",
        border: "1px solid rgba(255,255,255,0.10)",
      }),
      multiValueLabel: (base) => ({ ...base, color: "rgba(230,232,242,0.92)", fontWeight: 650 }),
      multiValueRemove: (base) => ({ ...base, color: "rgba(230,232,242,0.78)" }),
      menu: (base) => ({
        ...base,
        backgroundColor: "rgba(14,18,30,0.96)",
        border: "1px solid rgba(255,255,255,0.10)",
        borderRadius: 12,
        overflow: "hidden",
      }),
      option: (base, state) => ({
        ...base,
        backgroundColor: state.isFocused ? "rgba(255,255,255,0.08)" : "transparent",
        color: "rgba(230,232,242,0.92)",
      }),
      groupHeading: (base) => ({
        ...base,
        color: "rgba(230,232,242,0.70)",
        fontSize: 12,
        letterSpacing: "0.12em",
        textTransform: "uppercase",
      }),
      singleValue: (base) => ({ ...base, color: "rgba(230,232,242,0.92)" }),
      placeholder: (base) => ({ ...base, color: "rgba(230,232,242,0.55)" }),
      menuPortal: (base) => ({ ...base, zIndex: 9999 }),
    }),
    [],
  );

  const portalTarget = typeof document !== "undefined" ? document.body : undefined;

  const iconPickerPreview = useMemo(() => {
    if (!iconFamilies) return icon;
    return isFaSolidIconAvailable(icon) ? icon : "house";
  }, [icon, iconFamilies]);

  const iconPickerResults = useMemo(() => {
    if (!iconFamilies) return [];
    const q = iconSearch.trim().toLowerCase();

    const suggested = [
      iconPickerPreview,
      "house",
      "bell",
      "lightbulb",
      "toggle-on",
      "fan",
      "temperature-half",
      "lock",
      "video",
      "tv",
      "wifi",
      "plug",
      "power-off",
      "snowflake",
      "sun",
      "door-open",
      "camera",
    ];

    if (!q) {
      const out: string[] = [];
      const seen = new Set<string>();
      for (const name of suggested) {
        const key = normalizeFaSvgName(name);
        if (!key || seen.has(key)) continue;
        if (!iconFamilies[key]?.svgs?.classic?.solid && !FA_SVG_BY_NAME[key]) continue;
        seen.add(key);
        out.push(key);
      }
      return out;
    }

    const matches: string[] = [];
    for (const [name, entry] of Object.entries(iconFamilies)) {
      if (!entry?.svgs?.classic?.solid) continue;
      if (name.includes(q)) {
        matches.push(name);
        continue;
      }
      const label = (entry.label ?? "").toLowerCase();
      if (label && label.includes(q)) {
        matches.push(name);
        continue;
      }
      const terms = entry.search?.terms ?? [];
      if (Array.isArray(terms) && terms.some((t) => String(t).toLowerCase().includes(q))) {
        matches.push(name);
      }
    }
    matches.sort();
    return matches.slice(0, 220);
  }, [iconFamilies, iconPickerPreview, iconSearch]);

  function setItemsFromOptions(next: readonly HaItemOption[]) {
    const refs: HaItemRef[] = next.map((opt) => ({
      kind: opt.kind,
      id: opt.id,
      name: opt.label,
      domain: opt.meta?.domain,
      icon: opt.meta?.icon,
      device_id: opt.meta?.deviceId,
    }));

    let primaryEntityId = "";
    if (refs.length === 1) {
      const one = refs[0];
      if (one.kind === "entity") {
        const domain = one.domain || domainFromEntityId(one.id);
        if (isToggleDomain(domain)) primaryEntityId = one.id;
      } else if (one.kind === "device" && registry?.device_entities?.[one.id]) {
        const candidates = registry.device_entities[one.id] ?? [];
        const best = candidates.find((eid) => isToggleDomain(domainFromEntityId(eid))) ?? "";
        if (best) primaryEntityId = best;
      }
    }

    const suggestedName =
      refs.length === 1 ? refs[0].name || refs[0].id : refs.length > 1 ? `Home Assistant (${refs.length})` : "";
    const suggestedIcon =
      refs.length === 1
        ? sanitizeFaIconName(suggestIconForDomain(refs[0].domain || domainFromEntityId(refs[0].id)))
        : "";

    const patch: CompositionElementPatch = {
      props: { items: refs, primary_entity_id: primaryEntityId, primary_state: "" },
    };

    update(patch);

    if (!element.name && suggestedName) update({ name: suggestedName });
    const currentIcon = sanitizeFaIconName(asString(asRecord(element.props).icon, "")) || "";
    if (!currentIcon && suggestedIcon) update({ props: { icon: suggestedIcon } });
  }

  return (
    <div>
      {err ? (
        <div className="card">
          <div className="cardBody" style={{ color: "rgba(252,165,165,0.92)" }}>
            {err}
          </div>
        </div>
      ) : null}

      {servers.length === 0 ? (
        <div className="card">
          <div className="cardBody">{t("ext.home_assistant.editor.no_servers")}</div>
        </div>
      ) : (
        <>
          <div className="field">
            <div className="label">{t("core.element_editor.name")}</div>
            <input className="input" value={element.name} onChange={(e) => update({ name: e.target.value })} />
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.server")}</div>
            <select
              className="input"
              value={serverId}
              onChange={(e) => update({ props: { server_id: e.target.value } })}
            >
              <option value="" />
              {servers.map((s) => (
                <option value={s.id} key={s.id}>
                  {s.name ? `${s.name} (${s.host})` : s.host}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.items")}</div>
            <Select<HaItemOption, true, GroupBase<HaItemOption>>
              isMulti
              isDisabled={!serverId || loading}
              options={options}
              value={selectedOptions}
              placeholder={t("ext.home_assistant.editor.items_placeholder")}
              styles={selectStyles}
              menuPortalTarget={portalTarget}
              menuPosition="fixed"
              onChange={(next) => setItemsFromOptions(next ?? [])}
              formatOptionLabel={(opt) => (
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, minWidth: 0 }}>
                  <div style={{ fontWeight: 650, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {opt.label}
                  </div>
                  {opt.meta?.subLabel ? (
                    <div style={{ opacity: 0.7, fontSize: 12, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {opt.meta.subLabel}
                    </div>
                  ) : null}
                </div>
              )}
            />
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.icon")}</div>
            <button
              className="chipButton"
              type="button"
              onClick={() => setIsIconPickerOpen((prev) => !prev)}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                <i
                  className={["fa-solid", `fa-${iconPickerPreview}`].join(" ")}
                  aria-hidden="true"
                  style={{ width: 18, textAlign: "center" }}
                />
                <span style={{ fontWeight: 650, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {iconPickerPreview}
                </span>
              </span>
              <i
                className={["fa-solid", isIconPickerOpen ? "fa-chevron-up" : "fa-chevron-down"].join(" ")}
                aria-hidden="true"
              />
            </button>

            {isIconPickerOpen ? (
              <div className="card" style={{ marginTop: 10 }}>
                <div className="cardBody">
                  <div className="row" style={{ gap: 10 }}>
                    <input
                      ref={iconSearchRef}
                      className="input"
                      style={{ flex: 1, minWidth: 0 }}
                      value={iconSearch}
                      onChange={(e) => setIconSearch(e.target.value.slice(0, 64))}
                      placeholder={t("ext.home_assistant.editor.icon_search")}
                    />
                    <button
                      className="iconButton"
                      type="button"
                      aria-label={t("core.actions.close")}
                      onClick={() => {
                        setIconSearch("");
                        setIsIconPickerOpen(false);
                      }}
                    >
                      <i className={["fa-solid", "fa-xmark"].join(" ")} aria-hidden="true" />
                    </button>
                  </div>

                  {iconLoading ? (
                    <div className="cardMeta" style={{ marginTop: 10 }}>
                      {t("ext.home_assistant.editor.icon_loading")}
                    </div>
                  ) : iconLoadError ? (
                    <div className="cardMeta" style={{ marginTop: 10, color: "rgba(252,165,165,0.92)" }}>
                      {iconLoadError}
                    </div>
                  ) : (
                    <>
                      <div className="cardMeta" style={{ marginTop: 10 }}>
                        {!iconSearch.trim()
                          ? t("ext.home_assistant.editor.icon_suggested")
                          : t("ext.home_assistant.editor.icon_results", { count: iconPickerResults.length })}
                      </div>

                      <div
                        style={{
                          marginTop: 10,
                          display: "grid",
                          gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
                          gap: 8,
                          maxHeight: 260,
                          overflow: "auto",
                          paddingRight: 4,
                        }}
                      >
                        {iconPickerResults.length === 0 ? (
                          <div className="cardMeta">{t("ext.home_assistant.editor.icon_no_results")}</div>
                        ) : (
                          iconPickerResults.map((name) => (
                            <button
                              key={name}
                              className="chipButton"
                              type="button"
                              onClick={() => {
                                update({ props: { icon: name } });
                                setIsIconPickerOpen(false);
                              }}
                              style={{
                                height: 34,
                                borderRadius: 12,
                                padding: "0 10px",
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "flex-start",
                                gap: 10,
                                borderColor:
                                  name === iconPickerPreview ? "rgba(251,191,36,0.60)" : "rgba(255,255,255,0.10)",
                              }}
                            >
                              <i
                                className={["fa-solid", `fa-${name}`].join(" ")}
                                aria-hidden="true"
                                style={{ width: 18, textAlign: "center" }}
                              />
                              <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{name}</span>
                            </button>
                          ))
                        )}
                      </div>
                    </>
                  )}
                </div>
              </div>
            ) : (
              <div className="label" style={{ marginTop: 6 }}>
                {t("ext.home_assistant.editor.icon_hint")}
              </div>
            )}

            {iconFamilies && !isFaSolidIconAvailable(icon) ? (
              <div className="cardMeta" style={{ marginTop: 6, color: "rgba(252,165,165,0.92)" }}>
                {t("ext.home_assistant.editor.icon_not_found")}
              </div>
            ) : null}
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.view_mode")}</div>
            <select
              className="input"
              value={viewMode}
              onChange={(e) => update({ props: { view_mode: e.target.value } })}
            >
              <option value="floor">{t("ext.home_assistant.editor.view_mode.floor")}</option>
              <option value="ceiling">{t("ext.home_assistant.editor.view_mode.ceiling")}</option>
              <option value="wall">{t("ext.home_assistant.editor.view_mode.wall")}</option>
            </select>
          </div>

          <div className="field">
            <div className="label">{t("ext.home_assistant.editor.special_view")}</div>
            <select
              className="input"
              value={specialView}
              onChange={(e) => {
                const next = readHaSpecialView(e.target.value);
                if (next === "lamp") {
                  update({
                    props: {
                      special_view: next,
                      lamp_intensity: lampIntensityValue,
                      lamp_color: lampColorValue,
                    },
                  });
                } else {
                  update({ props: { special_view: "none" } });
                }
              }}
            >
              <option value="none">{t("ext.home_assistant.editor.special_view.none")}</option>
              <option value="lamp" disabled={!canLamp}>
                {t("ext.home_assistant.editor.special_view.lamp")}
              </option>
            </select>
            {!canLamp ? <div className="label" style={{ marginTop: 6 }}>{t("ext.home_assistant.editor.special_view.hint")}</div> : null}
          </div>

          {specialView === "lamp" && canLamp ? (
            <div className="rowWrap">
              <div className="field" style={{ flex: 1, minWidth: 160 }}>
                <div className="label">{t("ext.home_assistant.editor.lamp_color")}</div>
                <input
                  className="input"
                  type="color"
                  value={lampColorValue}
                  onChange={(e) => update({ props: { lamp_color: readHexColor(e.target.value, DEFAULT_LAMP_COLOR) } })}
                />
              </div>
              <div className="field" style={{ flex: 1, minWidth: 180 }}>
                <div className="label">
                  {t("ext.home_assistant.editor.lamp_intensity")}: {lampIntensityValue.toFixed(2)}
                </div>
                <input
                  className="input"
                  type="range"
                  min={0.2}
                  max={3}
                  step={0.05}
                  value={lampIntensityValue}
                  onChange={(e) => update({ props: { lamp_intensity: Number(e.target.value) } })}
                />
              </div>
            </div>
          ) : null}
        </>
      )}

      <div className="sectionDivider" />
      <div className="rowWrap">
        <button className="dangerButton" type="button" onClick={remove}>
          {t("core.actions.delete")}
        </button>
        <button className="chipButton" type="button" onClick={close}>
          {t("core.actions.close")}
        </button>
      </div>
    </div>
  );
}
