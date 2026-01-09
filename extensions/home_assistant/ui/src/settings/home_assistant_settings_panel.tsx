import React, { useEffect, useMemo, useState } from "react";

import type { SettingsPanel, TopoSyncHost } from "@toposync/plugin-api";

import { HOME_ASSISTANT_EXTENSION_ID } from "../constants";
import { createUniqueId, isValidUrl, readHomeAssistantServers } from "../parsing";
import type { HomeAssistantServer } from "../types";

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

function HomeAssistantSettings({ i18n, settings, updateSettings }: HomeAssistantSettingsProps): React.ReactElement {
  const { t } = i18n.useI18n();
  const serversFromSettings = useMemo(() => readHomeAssistantServers(settings), [settings]);

  const [draftServers, setDraftServers] = useState<HomeAssistantServer[]>(serversFromSettings);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [showTokensByServerId, setShowTokensByServerId] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (hasUnsavedChanges) return;
    setDraftServers(serversFromSettings);
  }, [hasUnsavedChanges, serversFromSettings]);

  const hasInvalidHosts = useMemo(
    () => draftServers.some((server) => server.host.trim() !== "" && !isValidUrl(server.host.trim())),
    [draftServers],
  );

  const canSave = useMemo(() => {
    if (draftServers.length === 0) return true;
    return draftServers.every(
      (server) =>
        Boolean(server.host.trim()) && Boolean(server.apiKey.trim()) && isValidUrl(server.host.trim()),
    );
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
          {hasUnsavedChanges ? <div className="label">{t("ext.home_assistant.settings.unsaved")}</div> : null}
        </div>

        <div className="row" style={{ gap: 10 }}>
          <button
            className="iconButton iconButtonPrimary"
            type="button"
            aria-label={t("ext.home_assistant.settings.add")}
            onClick={() => {
              setDraftServers((prev) => [{ id: createUniqueId(), name: "", host: "", apiKey: "" }, ...prev]);
              setHasUnsavedChanges(true);
            }}
          >
            <i className="fa-solid fa-plus" aria-hidden="true" />
          </button>

          <button
            className="primaryButton"
            type="button"
            disabled={!canSave || !hasUnsavedChanges}
            onClick={() => {
              updateSettings({ servers: draftServers });
              setHasUnsavedChanges(false);
            }}
          >
            {t("core.actions.save")}
          </button>

          <button
            className="chipButton"
            type="button"
            disabled={!hasUnsavedChanges}
            onClick={() => {
              setDraftServers(serversFromSettings);
              setHasUnsavedChanges(false);
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
          {draftServers.map((server) => {
            const showToken = Boolean(showTokensByServerId[server.id]);
            return (
              <div className="card" key={server.id}>
                <div className="cardBody">
                  <div className="field">
                    <div className="label">{t("ext.home_assistant.settings.server_name")}</div>
                    <input
                      className="input"
                      value={server.name}
                      onChange={(e) => {
                        const nextName = e.target.value.slice(0, 64);
                        setDraftServers((prev) =>
                          prev.map((x) => (x.id === server.id ? { ...x, name: nextName } : x)),
                        );
                        setHasUnsavedChanges(true);
                      }}
                    />
                  </div>

                  <div className="rowWrap">
                    <div className="field" style={{ flex: 1, minWidth: 220 }}>
                      <div className="label">{t("ext.home_assistant.settings.host")}</div>
                      <input
                        className="input"
                        value={server.host}
                        onChange={(e) => {
                          const nextHost = e.target.value.slice(0, 256);
                          setDraftServers((prev) =>
                            prev.map((x) => (x.id === server.id ? { ...x, host: nextHost } : x)),
                          );
                          setHasUnsavedChanges(true);
                        }}
                      />
                    </div>

                    <div className="field" style={{ flex: 1, minWidth: 220 }}>
                      <div className="label">{t("ext.home_assistant.settings.api_key")}</div>
                      <div className="row" style={{ gap: 10 }}>
                        <input
                          className="input"
                          style={{ flex: 1, minWidth: 0 }}
                          type={showToken ? "text" : "password"}
                          value={server.apiKey}
                          onChange={(e) => {
                            const nextToken = e.target.value.slice(0, 512);
                            setDraftServers((prev) =>
                              prev.map((x) => (x.id === server.id ? { ...x, apiKey: nextToken } : x)),
                            );
                            setHasUnsavedChanges(true);
                          }}
                        />

                        <button
                          className="iconButton"
                          type="button"
                          aria-label={showToken ? t("ext.home_assistant.settings.hide_key") : t("ext.home_assistant.settings.show_key")}
                          onClick={() =>
                            setShowTokensByServerId((prev) => ({
                              ...prev,
                              [server.id]: !prev[server.id],
                            }))
                          }
                        >
                          <i className={["fa-solid", showToken ? "fa-eye-slash" : "fa-eye"].join(" ")} aria-hidden="true" />
                        </button>

                        <button
                          className="iconButton"
                          type="button"
                          aria-label={t("core.actions.delete")}
                          onClick={() => {
                            setDraftServers((prev) => prev.filter((x) => x.id !== server.id));
                            setHasUnsavedChanges(true);
                          }}
                        >
                          <i className="fa-solid fa-trash" aria-hidden="true" />
                        </button>
                      </div>
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

