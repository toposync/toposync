import React, { useEffect, useMemo, useState } from "react";

import type { SettingsPanel, TopoSyncHost } from "@toposync/plugin-api";

import { HOME_ASSISTANT_EXTENSION_ID } from "../constants";
import { fetchHomeAssistantServers } from "../api/homeAssistantApi";
import {
  createUniqueId,
  isValidUrl,
  readHomeAssistantNotificationRoutes,
  readHomeAssistantServers,
} from "../parsing";
import type { HomeAssistantNotificationRoute, HomeAssistantServer, HomeAssistantServerPublic } from "../types";

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

function parseNotificationTypesInput(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatNotificationTypesInput(value: string[]): string {
  return value.join(", ");
}

function serverLabel(server: { id: string; name: string; host: string }): string {
  return server.name.trim() || server.host.trim() || server.id;
}

function HomeAssistantSettings({ i18n, settings, updateSettings }: HomeAssistantSettingsProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const servers = useMemo(() => readHomeAssistantServers(settings), [settings]);
  const routes = useMemo(() => readHomeAssistantNotificationRoutes(settings), [settings]);
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
  const routeServers = useMemo(() => {
    const combined: Array<{ id: string; name: string; host: string }> = [];
    const seen = new Set<string>();
    for (const server of managedServers) {
      if (seen.has(server.id)) continue;
      seen.add(server.id);
      combined.push(server);
    }
    for (const server of servers) {
      if (seen.has(server.id)) continue;
      seen.add(server.id);
      combined.push(server);
    }
    return combined;
  }, [managedServers, servers]);
  const serversById = useMemo(() => new Map(routeServers.map((server) => [server.id, server])), [routeServers]);
  const supervisorManaged = managedServers.length > 0;

  const [serverQuery, setServerQuery] = useState("");
  const [activeServerId, setActiveServerId] = useState<string | null>(null);
  const [confirmDeleteServerId, setConfirmDeleteServerId] = useState<string | null>(null);
  const [showTokensByServerId, setShowTokensByServerId] = useState<Record<string, boolean>>({});

  const [routeQuery, setRouteQuery] = useState("");
  const [activeRouteId, setActiveRouteId] = useState<string | null>(null);
  const [confirmDeleteRouteId, setConfirmDeleteRouteId] = useState<string | null>(null);

  useEffect(() => {
    if (activeServerId && servers.some((server) => server.id === activeServerId)) return;
    setActiveServerId(servers[0]?.id ?? null);
  }, [activeServerId, servers]);

  useEffect(() => {
    if (activeRouteId && routes.some((route) => route.id === activeRouteId)) return;
    setActiveRouteId(routes[0]?.id ?? null);
  }, [activeRouteId, routes]);

  useEffect(() => {
    if (!routes.some((route) => route.closeAction !== "ignore")) return;
    updateSettings({
      notificationRoutes: routes.map((route) => (route.closeAction === "ignore" ? route : { ...route, closeAction: "ignore" })),
    });
  }, [routes, updateSettings]);

  const filteredServers = useMemo(() => {
    const q = normalizeQuery(serverQuery);
    if (!q) return servers;
    return servers.filter(
      (server) => includesQuery(server.name || "", q) || includesQuery(server.id, q) || includesQuery(server.host, q),
    );
  }, [serverQuery, servers]);

  const filteredRoutes = useMemo(() => {
    const q = normalizeQuery(routeQuery);
    if (!q) return routes;
    return routes.filter((route) => {
      const server = serversById.get(route.serverId);
      return (
        includesQuery(route.name || "", q) ||
        includesQuery(route.notifyService, q) ||
        includesQuery(route.id, q) ||
        includesQuery(route.notificationTypes.join(","), q) ||
        includesQuery(server?.name || "", q) ||
        includesQuery(server?.host || "", q)
      );
    });
  }, [routeQuery, routes, serversById]);

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

  function addRoute(): void {
    const id = createUniqueId();
    updateSettings({
      notificationRoutes: [
        {
          id,
          name: "",
          enabled: true,
          serverId: routeServers[0]?.id ?? "",
          notifyService: "",
          notificationTypes: ["pipelines.event"],
          closeAction: "ignore",
          sendUpdates: false,
        },
        ...routes,
      ],
    });
    setActiveRouteId(id);
    setConfirmDeleteRouteId(null);
  }

  function updateRoute(routeId: string, patch: Partial<HomeAssistantNotificationRoute>): void {
    updateSettings({ notificationRoutes: routes.map((route) => (route.id === routeId ? { ...route, ...patch } : route)) });
  }

  function deleteRoute(routeId: string): void {
    updateSettings({ notificationRoutes: routes.filter((route) => route.id !== routeId) });
    setConfirmDeleteRouteId(null);
    if (activeRouteId === routeId) setActiveRouteId(null);
  }

  const activeServer = activeServerId ? servers.find((server) => server.id === activeServerId) ?? null : null;
  const activeRoute = activeRouteId ? routes.find((route) => route.id === activeRouteId) ?? null : null;

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
                  "No manual server setup is required in this environment. Notification routes can target the managed Home Assistant connection below.",
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

      <div className="sectionDivider" />

      <div className="card">
        <div className="cardBody">{t("ext.home_assistant.settings.routes_notice")}</div>
      </div>

      {backendServersError ? (
        <>
          <div className="sectionDivider" />
          <div className="card">
            <div className="cardBody">{backendServersError}</div>
          </div>
        </>
      ) : null}

      <div className="sectionDivider" />

      <div className="modalSectionTitle" style={{ marginBottom: 10 }}>
        {t("ext.home_assistant.settings.routes")}
      </div>

      <div className="settingsSplit">
        <div className="settingsSplitSidebar">
          <div className="settingsSplitToolbar">
            <input
              className="input"
              placeholder={t("ext.home_assistant.settings.search_routes", {}, "Search routes…")}
              value={routeQuery}
              onChange={(event) => setRouteQuery(event.target.value)}
            />
            <button
              className="iconButton iconButtonPrimary"
              type="button"
              aria-label={t("ext.home_assistant.settings.add_route")}
              onClick={addRoute}
            >
              <i className="fa-solid fa-plus" aria-hidden="true" />
            </button>
          </div>

          {filteredRoutes.length === 0 ? (
            <div className="card" style={{ marginTop: 10 }}>
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>{t("ext.home_assistant.settings.empty_routes")}</div>
                <button className="primaryButton" type="button" onClick={addRoute}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.home_assistant.settings.add_route")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsList">
              {filteredRoutes.map((route) => {
                const selected = route.id === activeRouteId;
                const name = route.name.trim() || t("ext.home_assistant.settings.unnamed_route", {}, "Untitled route");
                const server = serversById.get(route.serverId);
                const serverText = server ? serverLabel(server) : t("ext.home_assistant.settings.missing_route_server", {}, "Server not selected");
                const notifyText =
                  route.notifyService.trim() || t("ext.home_assistant.settings.missing_notify_service", {}, "Notify service missing");
                const meta = `${serverText} -> ${notifyText}`;
                return (
                  <button
                    key={route.id}
                    type="button"
                    className={["choiceItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                    onClick={() => {
                      setActiveRouteId(route.id);
                      setConfirmDeleteRouteId(null);
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
          {!activeRoute ? (
            <div className="card">
              <div className="cardBody">
                <div style={{ marginBottom: 10 }}>
                  {t("ext.home_assistant.settings.select_route", {}, "Select a route to edit.")}
                </div>
                <button className="primaryButton" type="button" onClick={addRoute}>
                  <i className="fa-solid fa-plus" aria-hidden="true" /> {t("ext.home_assistant.settings.add_route")}
                </button>
              </div>
            </div>
          ) : (
            <div className="settingsDetail">
              <div className="settingsDetailHeader">
                <div>
                  <div className="modalSectionTitle" style={{ marginBottom: 4 }}>
                    {activeRoute.name.trim() || t("ext.home_assistant.settings.unnamed_route", {}, "Untitled route")}
                  </div>
                  <div className="cardMeta">
                    {activeRoute.notifyService.trim() || t("ext.home_assistant.settings.missing_notify_service", {}, "Notify service missing")}
                  </div>
                </div>

                <div className="rowWrap" style={{ gap: 10, justifyContent: "flex-end" }}>
                  <button
                    className={confirmDeleteRouteId === activeRoute.id ? "dangerButton" : "iconButton iconButtonDanger"}
                    type="button"
                    aria-label={t("core.actions.delete")}
                    title={t("core.actions.delete")}
                    onClick={() => {
                      if (confirmDeleteRouteId === activeRoute.id) {
                        deleteRoute(activeRoute.id);
                        return;
                      }
                      setConfirmDeleteRouteId(activeRoute.id);
                    }}
                  >
                    {confirmDeleteRouteId === activeRoute.id ? (
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
                    <label className="row" style={{ gap: 10, alignItems: "center" }}>
                      <input
                        type="checkbox"
                        checked={activeRoute.enabled}
                        onChange={(event) => updateRoute(activeRoute.id, { enabled: event.target.checked })}
                      />
                      <span>{t("ext.home_assistant.settings.route_enabled")}</span>
                    </label>
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.route_name")}</div>
                    <input
                      className="input"
                      value={activeRoute.name}
                      onChange={(event) => updateRoute(activeRoute.id, { name: event.target.value.slice(0, 64) })}
                    />
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.route_server")}</div>
                    <select
                      className="input"
                      value={activeRoute.serverId}
                      onChange={(event) => updateRoute(activeRoute.id, { serverId: event.target.value })}
                    >
                      <option value="">{t("ext.home_assistant.settings.missing_route_server", {}, "Server not selected")}</option>
                      {routeServers.map((server) => (
                        <option key={server.id} value={server.id}>
                          {serverLabel(server)}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.route_notify_service")}</div>
                    <input
                      className="input"
                      value={activeRoute.notifyService}
                      placeholder="notify.mobile_app_phone"
                      onChange={(event) =>
                        updateRoute(activeRoute.id, { notifyService: event.target.value.slice(0, 128) })
                      }
                    />
                    <div className="cardMeta" style={{ marginTop: 6 }}>
                      {t("ext.home_assistant.settings.route_notify_service_hint")}
                    </div>
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.route_notification_types")}</div>
                    <input
                      className="input"
                      value={formatNotificationTypesInput(activeRoute.notificationTypes)}
                      onChange={(event) =>
                        updateRoute(activeRoute.id, {
                          notificationTypes: parseNotificationTypesInput(event.target.value),
                        })
                      }
                    />
                    <div className="cardMeta" style={{ marginTop: 6 }}>
                      {t("ext.home_assistant.settings.route_notification_types_hint")}
                    </div>
                  </div>

                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.route_close_action")}</div>
                    <div className="cardMeta">{t("ext.home_assistant.settings.route_close_action.fixed_ignore")}</div>
                    <div className="cardMeta" style={{ marginTop: 6 }}>
                      {t("ext.home_assistant.settings.route_close_action_hint")}
                    </div>
                  </div>

                  <div className="field">
                    <label className="row" style={{ gap: 10, alignItems: "center" }}>
                      <input
                        type="checkbox"
                        checked={activeRoute.sendUpdates}
                        onChange={(event) => updateRoute(activeRoute.id, { sendUpdates: event.target.checked })}
                      />
                      <span>{t("ext.home_assistant.settings.route_send_updates")}</span>
                    </label>
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
