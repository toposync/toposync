import React, { useEffect, useMemo, useState } from "react";

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

function normalizeFaSvgName(value: string): string {
  const key = sanitizeFaIconName(value);
  if (key === "thermometer-half" || key === "thermometer") return "temperature-half";
  return key;
}

function resolveFaSvg(value: string): string {
  const key = normalizeFaSvgName(value);
  return FA_SVG_BY_NAME[key] ?? FA_SVG_BY_NAME.house;
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
      "Your API token is stored locally in TopoSync configuration (local-first).",
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
    "ext.home_assistant.editor.icon_hint": "Font Awesome (solid) icon name, e.g. lightbulb, toggle-on, thermostat.",
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
      "Seu token de API é armazenado localmente na configuração do TopoSync (local-first).",
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
    "ext.home_assistant.editor.icon_hint":
      "Nome do ícone Font Awesome (solid), ex.: lightbulb, toggle-on, thermostat.",
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
  const ICON_EXTRUDE_DEPTH = 32; // in SVG coordinate units (Font Awesome uses ~512x512 viewBox)

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
      if (typeof state === "string") update({ props: { primary_state: state } });
      return true;
    },
    create3D: ({ THREE }, element) => {
      function getIconGeometry(iconName: string): { geometry: any; scale: number } {
        const normalized = normalizeFaSvgName(iconName);
        const cacheKey = FA_SVG_BY_NAME[normalized] ? normalized : "house";
        const cached = iconGeometryCache.get(cacheKey);
        if (cached) return cached;

        const svgText = resolveFaSvg(cacheKey);
        const data = new SVGLoader().parse(svgText);

        const shapes: any[] = [];
        for (const path of data.paths) shapes.push(...path.toShapes(true));

        const geometry = new THREE.ExtrudeGeometry(shapes, {
          depth: ICON_EXTRUDE_DEPTH,
          bevelEnabled: false,
          curveSegments: 4,
          steps: 1,
        });

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
        const bbox2 = geometry.boundingBox;
        if (bbox2) geometry.translate(0, -bbox2.min.y, 0);
        geometry.computeVertexNormals();

        geometry.computeBoundingBox();
        const bbox3 = geometry.boundingBox;
        const sizeX = bbox3 ? bbox3.max.x - bbox3.min.x : 1;
        const sizeZ = bbox3 ? bbox3.max.z - bbox3.min.z : 1;
        const maxXZ = Math.max(sizeX, sizeZ, 1e-9);
        const scale = ICON_TARGET_SIZE / maxXZ;

        const entry = { geometry, scale };
        iconGeometryCache.set(cacheKey, entry);
        return entry;
      }

      const group = new THREE.Group();

      const topY = BUTTON_RADIUS * Math.cos(BUTTON_THETA_TOP_CUT);
      const topRadius = BUTTON_RADIUS * Math.sin(BUTTON_THETA_TOP_CUT);

      const domeGeom = new THREE.SphereGeometry(
        BUTTON_RADIUS,
        48,
        24,
        0,
        Math.PI * 2,
        BUTTON_THETA_TOP_CUT,
        Math.PI / 2 - BUTTON_THETA_TOP_CUT,
      );
      const baseMat = new THREE.MeshStandardMaterial({
        color: 0x334155,
        roughness: 0.62,
        metalness: 0.06,
      });

      const dome = new THREE.Mesh(domeGeom, baseMat);
      group.add(dome);

      const topCapGeom = new THREE.CircleGeometry(topRadius, 44);
      const topCap = new THREE.Mesh(topCapGeom, baseMat);
      topCap.rotation.x = -Math.PI / 2;
      topCap.position.set(0, topY, 0);
      group.add(topCap);

      const bottomCapGeom = new THREE.CircleGeometry(BUTTON_RADIUS, 44);
      const bottomCap = new THREE.Mesh(bottomCapGeom, baseMat);
      bottomCap.rotation.x = Math.PI / 2;
      bottomCap.position.set(0, 0, 0);
      group.add(bottomCap);

      const ringGeom = new THREE.RingGeometry(topRadius * 0.72, topRadius * 0.98, 44);
      const ringMat = new THREE.MeshBasicMaterial({
        color: 0x38bdf8,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.65,
        depthWrite: false,
      });
      const ring = new THREE.Mesh(ringGeom, ringMat);
      ring.rotation.x = -Math.PI / 2;
      ring.position.set(0, topY + 0.001, 0);
      group.add(ring);

      const iconMat = new THREE.MeshStandardMaterial({
        color: 0xe2e8f0,
        side: THREE.DoubleSide,
        roughness: 0.35,
        metalness: 0.0,
      });
      const iconMesh = new THREE.Mesh(getIconGeometry("house").geometry, iconMat);
      iconMesh.scale.setScalar(getIconGeometry("house").scale);
      iconMesh.position.set(0, topY + 0.002, 0);
      group.add(iconMesh);

      let currentIconKey = "house";

      function apply(el: CompositionElement) {
        const p = asRecord(el.props);
        const icon = sanitizeFaIconName(asString(p.icon, "house")) || "house";
        const primaryState = asString(p.primary_state).trim().toLowerCase();
        const isOn = primaryState === "on";

        const iconKey = normalizeFaSvgName(icon);
        if (iconKey !== currentIconKey) {
          currentIconKey = iconKey;
          const entry = getIconGeometry(iconKey);
          iconMesh.geometry = entry.geometry;
          iconMesh.scale.setScalar(entry.scale);
        }

        ringMat.color.set(isOn ? 0xfbbf24 : 0x38bdf8);
        ringMat.opacity = isOn ? 0.88 : 0.58;
        baseMat.color.set(isOn ? 0x1f2937 : 0x334155);
        iconMat.color.set(isOn ? 0xfff4d2 : 0xe2e8f0);
      }

      apply(element);

      return {
        object: group,
        update: apply,
        dispose: () => {
          domeGeom.dispose();
          topCapGeom.dispose();
          bottomCapGeom.dispose();
          baseMat.dispose();
          ringGeom.dispose();
          ringMat.dispose();
          iconMat.dispose();
        },
      };
    },
    render2D: ({ ctx, element, viewport }) => {
      const p = asRecord(element.props);
      const primaryState = asString(p.primary_state).trim().toLowerCase();
      const isOn = primaryState === "on";

      const center = viewport.worldToScreen({ x: element.position.x, z: element.position.z });
      const r = 11;

      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, Math.PI * 2);
      ctx.fillStyle = isOn ? "rgba(251,191,36,0.22)" : "rgba(56,189,248,0.14)";
      ctx.fill();
      ctx.strokeStyle = isOn ? "rgba(251,191,36,0.70)" : "rgba(230,232,242,0.24)";
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
          props: { server_id: "", items: [], icon: "house", primary_entity_id: "", primary_state: "" },
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
  const items = useMemo(() => readItemRefs(props.items), [props.items]);

  const [servers, setServers] = useState<HaServerPublic[]>([]);
  const [registry, setRegistry] = useState<RegistryResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

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
    if (serverId) return;
    if (servers.length === 1) update({ props: { server_id: servers[0].id } });
  }, [serverId, servers, update]);

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
            <input
              className="input"
              value={icon}
              onChange={(e) => update({ props: { icon: sanitizeFaIconName(e.target.value) } })}
              placeholder="lightbulb"
            />
            <div className="label" style={{ marginTop: 6 }}>
              {t("ext.home_assistant.editor.icon_hint")}
            </div>
          </div>
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
