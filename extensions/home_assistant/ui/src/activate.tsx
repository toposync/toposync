import React, { useEffect, useMemo, useState } from "react";

import type { SettingsPanel, TopoSyncHost } from "@toposync/plugin-api";

const EXTENSION_ID = "com.toposync.home_assistant";

type HaServer = {
  id: string;
  name: string;
  host: string;
  apiKey: string;
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

export function activate(host: TopoSyncHost): void {
  host.i18n.registerTranslations(translations);
  host.registerSettingsPanel(settingsPanel());
}

const translations = {
  en: {
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
  },
  "pt-BR": {
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

