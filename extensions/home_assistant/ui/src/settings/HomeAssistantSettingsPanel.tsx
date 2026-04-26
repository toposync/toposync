import React, { useEffect, useMemo, useState } from "react";

import type { SettingsPanel, TopoSyncHost } from "@toposync/plugin-api";

import { HOME_ASSISTANT_EXTENSION_ID } from "../constants";
import { fetchHomeAssistantServers } from "../api/homeAssistantApi";
import { createUniqueId, isValidUrl, readHomeAssistantServers } from "../parsing";
import type { HomeAssistantServer, HomeAssistantServerPublic } from "../types";

export function createHomeAssistantSettingsPanel(): SettingsPanel {
  return {
    id: HOME_ASSISTANT_EXTENSION_ID,
    icon: "house",
    name: { key: "ext.home_assistant.settings.name", fallback: "Home Assistant" },
    description: { key: "ext.home_assistant.settings.desc" },
    render: ({ i18n, settings, updateSettings }) => (
      <HomeAssistantSettings i18n={i18n} settings={settings} updateSettings={updateSettings} />
    ),
  };
}

type HomeAssistantSettingsProps = {
  i18n: TopoSyncHost["i18n"];
  settings: Record<string, unknown>;
  updateSettings: (patch: Record<string, unknown>) => void;
};

function normalizeQuery(value: string): string {
  return value.trim().toLowerCase();
}

function includesQuery(value: string, query: string): boolean {
  const normalized = normalizeQuery(value);
  if (!normalized) return false;
  return normalized.includes(query);
}

function serverLabel(server: { id: string; name: string; host: string }): string {
  return server.name.trim() || server.host.trim() || server.id;
}

function HomeAssistantSettings({ i18n, settings, updateSettings }: HomeAssistantSettingsProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const servers = useMemo(() => readHomeAssistantServers(settings), [settings]);
  const [backendServers, setBackendServers] = useState<HomeAssistantServerPublic[]>([]);
  const [backendServersError, setBackendServersError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchHomeAssistantServers()
      .then((next) => {
        if (cancelled) return;
        setBackendServers(Array.isArray(next) ? next : []);
        setBackendServersError(null);
      })
      .catch((error) => {
        if (cancelled) return;
        setBackendServers([]);
        setBackendServersError(String(error?.message ?? error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const managedServers = useMemo(() => backendServers.filter((server) => server.managed), [backendServers]);
  const supervisorManaged = managedServers.length > 0;

  const [serverQuery, setServerQuery] = useState("");
  const [activeServerId, setActiveServerId] = useState<string | null>(null);
  const [confirmDeleteServerId, setConfirmDeleteServerId] = useState<string | null>(null);
  const [showTokensByServerId, setShowTokensByServerId] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (activeServerId && servers.some((server) => server.id === activeServerId)) return;
    setActiveServerId(servers[0]?.id ?? null);
  }, [activeServerId, servers]);

  const filteredServers = useMemo(() => {
    const q = normalizeQuery(serverQuery);
    if (!q) return servers;
    return servers.filter(
      (server) => includesQuery(server.name || "", q) || includesQuery(server.id, q) || includesQuery(server.host, q),
    );
  }, [serverQuery, servers]);

  const hasInvalidHosts = useMemo(
    () => servers.some((server) => server.host.trim() !== "" && !isValidUrl(server.host.trim())),
    [servers],
  );

  function addServer(): void {
    const id = createUniqueId();
    updateSettings({ servers: [{ id, name: "", host: "", apiKey: "" }, ...servers] });
    setActiveServerId(id);
    setConfirmDeleteServerId(null);
  }

  function updateServer(serverId: string, patch: Partial<HomeAssistantServer>): void {
    updateSettings({ servers: servers.map((server) => (server.id === serverId ? { ...server, ...patch } : server)) });
  }

  function deleteServer(serverId: string): void {
    updateSettings({ servers: servers.filter((server) => server.id !== serverId) });
    setConfirmDeleteServerId(null);
    if (activeServerId === serverId) setActiveServerId(null);
    setShowTokensByServerId((prev) => {
      if (!prev[serverId]) return prev;
      const next = { ...prev };
      delete next[serverId];
      return next;
    });
  }

  const activeServer = activeServerId ? servers.find((server) => server.id === activeServerId) ?? null : null;

  return (
    <div>
      <div className="card">
        <div className="cardBody">{t("ext.home_assistant.settings.notice")}</div>
      </div>

      <div className="sectionDivider" />

      {hasInvalidHosts ? (
        <>
          <div className="card">
            <div className="cardBody">{t("ext.home_assistant.settings.invalid_host")}</div>
          </div>
          <div className="sectionDivider" />
        </>
      ) : null}

      {backendServersError ? (
        <>
          <div className="card">
            <div className="cardBody">{backendServersError}</div>
          </div>
          <div className="sectionDivider" />
        </>
      ) : null}

      <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
        {t("ext.home_assistant.settings.servers")}
      </div>

      <div className="settingsSplit">
        <div className="settingsSplitSidebar">
          <div className="settingsSplitToolbar">
            <input
              className="input"
              placeholder={t("ext.home_assistant.settings.search_servers", {}, "Search servers…")}
              value={serverQuery}
              onChange={(event) => setServerQuery(event.target.value)}
            />
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.home_assistant.settings.add")}
              onClick={addServer}
              disabled={supervisorManaged}
              title={
                supervisorManaged
                  ? t(
                      "ext.home_assistant.settings.supervisor_managed_button_disabled",
                      {},
                      "Managed by Home Assistant Supervisor",
                    )
                  : undefined
              }
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          {supervisorManaged ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>
                  {t(
                    "ext.home_assistant.settings.supervisor_managed",
                    {},
                    "This connection is managed by Home Assistant Supervisor. Toposync uses the internal Core API automatically.",
                  )}
                </div>
                <div className="settingsList">
                  {managedServers.map((server) => (
                    <div key={server.id} className="choiceItem isSelected">
                      <div className="settingsListItemRow">
                        <div className="settingsListItemMain">
                          <div className="settingsListItemTitle" title={serverLabel(server)}>
                            {serverLabel(server)}
                          </div>
                          <div className="settingsListItemMeta" title={server.host}>
                            {server.host}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : filteredServers.length === 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>{t("ext.home_assistant.settings.empty")}</div>
                <button className="primaryButton" type="button" onClick={addServer}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.home_assistant.settings.add")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsList">
              {filteredServers.map((server) => {
                const selected = server.id === activeServerId;
                const name = server.name.trim() || t("ext.home_assistant.settings.unnamed_server", {}, "Untitled server");
                const meta = server.host.trim() || t("ext.home_assistant.settings.missing_host", {}, "Host URL missing");
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
          {supervisorManaged ? (
            <div className="card">
              <div className="cardBody">
                {t(
                  "ext.home_assistant.settings.supervisor_managed_readonly",
                  {},
                  "No manual server setup is required in this environment. Toposync uses the managed Home Assistant connection automatically.",
                )}
              </div>
            </div>
          ) : !activeServer ? (
            <div className="card">
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>
                  {t("ext.home_assistant.settings.select_server", {}, "Select a server to edit.")}
                </div>
                <button className="primaryButton" type="button" onClick={addServer}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.home_assistant.settings.add")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsDetail">
              <div className="settingsDetailHeader">
                <div>
                  <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                    {activeServer.name.trim() || t("ext.home_assistant.settings.unnamed_server", {}, "Untitled server")}
                  </div>
                  <div className="cardMeta">{activeServer.host.trim() || activeServer.id}</div>
                </div>

                <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                  <button
                    className={confirmDeleteServerId === activeServer.id ? "dangerButton" : "iconButton iconButtonDanger"}
                    type="button"
                    aria-label={t("core.actions.delete")}
                    title={t("core.actions.delete")}
                    onClick={() => {
                      if (confirmDeleteServerId === activeServer.id) {
                        deleteServer(activeServer.id);
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
                    <div className="label">{t("ext.home_assistant.settings.server_name")}</div>
                    <input
                      className="input"
                      value={activeServer.name}
                      onChange={(e) => updateServer(activeServer.id, { name: e.target.value.slice(0, 64) })}
                    />
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.host")}</div>
                    <input
                      className="input"
                      value={activeServer.host}
                      onChange={(e) => updateServer(activeServer.id, { host: e.target.value.slice(0, 256) })}
                    />
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.api_key")}</div>
                    <div className="row" style={{ gap: 10 }}>
                      <input
                        className="input"
                        style={{ flex: 1, minWidth: 0 }}
                        type={showTokensByServerId[activeServer.id] ? "text" : "password"}
                        value={activeServer.apiKey}
                        onChange={(e) => updateServer(activeServer.id, { apiKey: e.target.value.slice(0, 512) })}
                      />

                      <button
                        className="iconButton"
                        type="button"
                        aria-label={
                          showTokensByServerId[activeServer.id]
                            ? t("ext.home_assistant.settings.hide_key")
                            : t("ext.home_assistant.settings.show_key")
                        }
                        onClick={() =>
                          setShowTokensByServerId((prev) => ({
                            ...prev,
                            [activeServer.id]: !prev[activeServer.id],
                          }))
                        }
                      >
                        <i
                          className={["fa-solid", showTokensByServerId[activeServer.id] ? "fa-eye-slash" : "fa-eye"].join(" ")}
                          aria-hidden="true"
                        />
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
