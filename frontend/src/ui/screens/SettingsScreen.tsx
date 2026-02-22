import React, { useEffect, useMemo, useRef, useState } from "react";

import type { GraphicsQuality, HostApi, SettingsPanel, ThemeDefinition, WallHeightPreset } from "@toposync/plugin-api";

import type { AppSettings, AuthUser } from "../../util/api";
import { i18n, resolveLocalizedString } from "../../util/i18n";

import { Icon } from "../Icon";

type Props = {
  backendAvailable: boolean;
  api: HostApi;
  wallHeightPreset: WallHeightPreset;
  ghostWalls: boolean;
  graphicsQuality: GraphicsQuality;
  onSetWallHeightPreset: (preset: WallHeightPreset) => void;
  onSetGhostWalls: (enabled: boolean) => void;
  onSetGraphicsQuality: (quality: GraphicsQuality) => void;
  panels: SettingsPanel[];
  themes: ThemeDefinition[];
  themeId: string;
  onSetThemeId: (themeId: string) => void;
  settings: AppSettings;
  onPatchExtensionSettings: (extensionId: string, patch: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onOpenPipelines: () => void;
  onOpenProcessingServers: () => void;
  onOpenAccess: () => void;
  canManageAccess: boolean;
  authUser: AuthUser | null;
  onLogout: () => Promise<void>;
  onClose: () => void;
};

const VIEW_PANEL_ID = "__view__";
const CORE_PANEL_ID = "__core__";
const ACTIVE_PANEL_STORAGE_KEY = "toposync.settings.active_panel.v3";

type SettingsEntry =
  | {
      kind: "core";
      id: string;
      icon: string;
      title: string;
      desc: string;
      render: () => React.ReactNode;
    }
  | {
      kind: "extension";
      id: string;
      icon: string;
      title: string;
      desc: string;
      panel: SettingsPanel;
    };

function loadActivePanelId(defaultId: string): string {
  try {
    const raw = localStorage.getItem(ACTIVE_PANEL_STORAGE_KEY);
    if (raw && typeof raw === "string") return raw;
  } catch {
    // ignore
  }
  return defaultId;
}

export function SettingsScreen({
  backendAvailable,
  api,
  wallHeightPreset,
  ghostWalls,
  graphicsQuality,
  onSetWallHeightPreset,
  onSetGhostWalls,
  onSetGraphicsQuality,
  panels,
  themes,
  themeId,
  onSetThemeId,
  settings,
  onPatchExtensionSettings,
  onOpenPipelines,
  onOpenProcessingServers,
  onOpenAccess,
  canManageAccess,
  authUser,
  onLogout,
  onClose,
}: Props): React.ReactElement {
  const { t, locale, setLocale } = i18n.useI18n();
  const [activePanelId, setActivePanelId] = useState<string>(() => loadActivePanelId(VIEW_PANEL_ID));
  const [draftExtensions, setDraftExtensions] = useState<Record<string, Record<string, unknown>>>(() => settings.extensions ?? {});
  const [dirtyExtensions, setDirtyExtensions] = useState<Record<string, boolean>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmDiscardOpen, setConfirmDiscardOpen] = useState(false);
  const [confirmExitOpen, setConfirmExitOpen] = useState(false);
  const [pendingExitAction, setPendingExitAction] = useState<
    null | "close" | "pipelines" | "processing_servers" | "access" | "logout"
  >(null);
  const lastSettingsRef = useRef<AppSettings>(settings);

  const orderedPanels = useMemo(() => {
    const list = [...panels];
    list.sort((a, b) => resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)));
    return list;
  }, [panels, locale]);

  const entries = useMemo<SettingsEntry[]>(() => {
    const viewEntry: SettingsEntry = {
      kind: "core",
      id: VIEW_PANEL_ID,
      icon: "sliders",
      title: t("core.ui.settings.sections.view"),
      desc: t("core.ui.settings.sections.view_desc"),
      render: () => (
        <div>
          <div className="modalSectionTitle">{t("core.ui.view_settings.wall_height")}</div>
          <div className="choiceList">
            {(
              [
                { id: "low", title: t("core.ui.wall_height.low"), desc: t("core.ui.wall_height.low_desc") },
                { id: "medium", title: t("core.ui.wall_height.medium"), desc: t("core.ui.wall_height.medium_desc") },
                { id: "high", title: t("core.ui.wall_height.high"), desc: t("core.ui.wall_height.high_desc") },
              ] as const
            ).map((opt) => {
              const selected = wallHeightPreset === opt.id;
              return (
                <div
                  key={opt.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSetWallHeightPreset(opt.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") onSetWallHeightPreset(opt.id);
                  }}
                >
                  <div className="choiceTitle">{opt.title}</div>
                  <div className="choiceDesc">{opt.desc}</div>
                </div>
              );
            })}
          </div>

          <div className="sectionDivider" />

          <div className="modalSectionTitle">{t("core.ui.view_settings.interactivity")}</div>
          <div className="choiceList">
            {(() => {
              const selected = Boolean(ghostWalls);
              return (
                <div
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSetGhostWalls(!selected)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") onSetGhostWalls(!selected);
                  }}
                >
                  <div className="choiceTitle">{t("core.ui.view_settings.ghost_walls")}</div>
                  <div className="choiceDesc">{t("core.ui.view_settings.ghost_walls_desc")}</div>
                </div>
              );
            })()}
          </div>

          <div className="sectionDivider" />

          <div className="modalSectionTitle">{t("core.ui.view_settings.graphics_quality")}</div>
          <div className="choiceList">
            {(
              [
                { id: "simplified", title: t("core.ui.graphics_quality.simplified"), desc: t("core.ui.graphics_quality.simplified_desc") },
                { id: "detailed", title: t("core.ui.graphics_quality.detailed"), desc: t("core.ui.graphics_quality.detailed_desc") },
              ] as const
            ).map((opt) => {
              const selected = (graphicsQuality ?? "simplified") === opt.id;
              return (
                <div
                  key={opt.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSetGraphicsQuality(opt.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") onSetGraphicsQuality(opt.id);
                  }}
                >
                  <div className="choiceTitle">{opt.title}</div>
                  <div className="choiceDesc">{opt.desc}</div>
                </div>
              );
            })}
          </div>
        </div>
      ),
    };

    const coreEntry: SettingsEntry = {
      kind: "core",
      id: CORE_PANEL_ID,
      icon: "gear",
      title: t("core.ui.settings.sections.core"),
      desc: t("core.ui.settings.sections.core_desc"),
      render: () => (
        <div>
          {!backendAvailable ? (
            <div className="card">
              <div className="cardTitle">{t("core.ui.settings.backend_offline_title")}</div>
              <div className="cardBody">{t("core.ui.settings.backend_offline_desc")}</div>
            </div>
          ) : null}

          <div className="sectionDivider" />

          <div className="modalSectionTitle">{t("core.ui.settings.language")}</div>
          <div className="choiceList">
            {(
              [
                { id: "pt-BR", title: t("core.ui.settings.language.pt"), desc: t("core.ui.settings.language.pt_desc") },
                { id: "en", title: t("core.ui.settings.language.en"), desc: t("core.ui.settings.language.en_desc") },
              ] as const
            ).map((opt) => {
              const selected = locale === opt.id;
              return (
                <div
                  key={opt.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => setLocale(opt.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") setLocale(opt.id);
                  }}
                >
                  <div className="choiceTitle">{opt.title}</div>
                  <div className="choiceDesc">{opt.desc}</div>
                </div>
              );
            })}
          </div>

          <div className="sectionDivider" />

          <div className="modalSectionTitle">{t("core.ui.settings.theme")}</div>
          <div className="choiceList">
            {themes.map((opt) => {
              const selected = themeId === opt.id;
              const title = resolveLocalizedString(opt.name);
              const desc = opt.description ? resolveLocalizedString(opt.description) : "";
              return (
                <div
                  key={opt.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSetThemeId(opt.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") onSetThemeId(opt.id);
                  }}
                >
                  <div className="choiceTitle">{title}</div>
                  {desc ? <div className="choiceDesc">{desc}</div> : null}
                </div>
              );
            })}
          </div>

          <div className="sectionDivider" />

          <div className="modalSectionTitle">{t("core.ui.auth.session.title")}</div>
          <div className="card">
            <div className="cardTitle">
              {authUser?.display_name || authUser?.username || t("core.ui.auth.session.current_user_fallback")}
            </div>
            {authUser ? <div className="cardBody">{authUser.username} · {authUser.role}</div> : null}
            <div style={{ marginTop: 8 }}>
              <button className="chipButton" type="button" onClick={() => requestExit("logout")}>
                {t("core.actions.sign_out")}
              </button>
            </div>
          </div>
        </div>
      ),
    };

    const extEntries: SettingsEntry[] = orderedPanels.map((panel) => ({
      kind: "extension",
      id: panel.id,
      icon: panel.icon || "puzzle-piece",
      title: resolveLocalizedString(panel.name),
      desc: panel.description ? resolveLocalizedString(panel.description) : "",
      panel,
    }));
    return [viewEntry, coreEntry, ...extEntries];
  }, [
    backendAvailable,
    ghostWalls,
    graphicsQuality,
    locale,
    onSetGhostWalls,
    onSetGraphicsQuality,
    onSetThemeId,
    onSetWallHeightPreset,
    onLogout,
    authUser,
    orderedPanels,
    t,
    themeId,
    themes,
    wallHeightPreset,
    setLocale,
  ]);

  const activeEntry = useMemo(
    () => entries.find((entry) => entry.id === activePanelId) ?? entries[0],
    [activePanelId, entries],
  );

  const dirtyExtensionIds = useMemo(() => {
    return Object.entries(dirtyExtensions)
      .filter(([, dirty]) => Boolean(dirty))
      .map(([id]) => id)
      .sort((a, b) => a.localeCompare(b));
  }, [dirtyExtensions]);

  const hasUnsavedChanges = dirtyExtensionIds.length > 0;
  const unsavedSectionsLabel = useMemo(() => {
    const labels = dirtyExtensionIds
      .map((id) => entries.find((entry) => entry.kind === "extension" && entry.id === id)?.title)
      .filter((value): value is string => Boolean(value));
    return labels.join(", ");
  }, [dirtyExtensionIds, entries]);

  useEffect(() => {
    try {
      localStorage.setItem(ACTIVE_PANEL_STORAGE_KEY, activePanelId);
    } catch {
      // ignore
    }
  }, [activePanelId]);

  useEffect(() => {
    if (lastSettingsRef.current === settings) return;
    lastSettingsRef.current = settings;

    setDraftExtensions((prev) => {
      const next: Record<string, Record<string, unknown>> = { ...prev };
      const current = settings.extensions ?? {};

      for (const [extId, extSettings] of Object.entries(current)) {
        if (dirtyExtensions[extId]) continue;
        next[extId] = extSettings ?? {};
      }

      return next;
    });
  }, [dirtyExtensions, settings]);

  function updateDraftExtensionSettings(extensionId: string, patch: Record<string, unknown>): void {
    setDraftExtensions((prev) => {
      const current = prev[extensionId] ?? {};
      return { ...prev, [extensionId]: { ...current, ...(patch ?? {}) } };
    });
    setDirtyExtensions((prev) => ({ ...prev, [extensionId]: true }));
  }

  function renderExtensionPanel(panel: SettingsPanel): React.ReactNode {
    const extSettings = draftExtensions?.[panel.id] ?? settings.extensions?.[panel.id] ?? {};
    return panel.render({
      i18n,
      api,
      settings: extSettings,
      updateSettings: (patch) => updateDraftExtensionSettings(panel.id, patch ?? {}),
    });
  }

  async function saveAll(): Promise<void> {
    if (!backendAvailable || saving || dirtyExtensionIds.length === 0) return;
    setSaving(true);
    setSaveError(null);

    for (const extensionId of dirtyExtensionIds) {
      const draft = draftExtensions[extensionId] ?? {};
      try {
        const next = await onPatchExtensionSettings(extensionId, draft);
        setDraftExtensions((prev) => ({ ...prev, [extensionId]: next ?? {} }));
        setDirtyExtensions((prev) => ({ ...prev, [extensionId]: false }));
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : String(err));
        break;
      }
    }

    setSaving(false);
  }

  function discardAll(): void {
    setDraftExtensions(settings.extensions ?? {});
    setDirtyExtensions({});
    setSaveError(null);
  }

  function requestExit(action: "close" | "pipelines" | "processing_servers" | "access" | "logout"): void {
    if (saving) return;
    if (hasUnsavedChanges) {
      setPendingExitAction(action);
      setConfirmExitOpen(true);
      return;
    }
    if (action === "pipelines") onOpenPipelines();
    else if (action === "processing_servers") onOpenProcessingServers();
    else if (action === "access") onOpenAccess();
    else if (action === "logout") void onLogout();
    else onClose();
  }

  const exitTitle = useMemo(() => {
    if (pendingExitAction === "pipelines") return t("core.ui.settings.confirm_open_pipelines_title");
    if (pendingExitAction === "processing_servers") return t("core.ui.settings.confirm_open_processing_servers_title");
    if (pendingExitAction === "access") return "Open access control?";
    if (pendingExitAction === "logout") return t("core.ui.auth.confirm_sign_out_title");
    return t("core.ui.settings.confirm_close_title");
  }, [pendingExitAction, t]);

  const exitDesc = useMemo(() => {
    if (
      pendingExitAction === "pipelines" ||
      pendingExitAction === "processing_servers" ||
      pendingExitAction === "access" ||
      pendingExitAction === "logout"
    ) {
      const suffix = unsavedSectionsLabel ? ` (${unsavedSectionsLabel})` : "";
      return t("core.ui.settings.confirm_discard_continue_desc", { suffix });
    }
    return t("core.ui.settings.confirm_close_desc");
  }, [pendingExitAction, t, unsavedSectionsLabel]);

  const exitConfirmLabel = useMemo(() => {
    if (
      pendingExitAction === "pipelines" ||
      pendingExitAction === "processing_servers" ||
      pendingExitAction === "access" ||
      pendingExitAction === "logout"
    ) {
      return t("core.ui.settings.confirm_discard_continue");
    }
    return t("core.ui.settings.discard_and_close");
  }, [pendingExitAction, t]);

  return (
    <div className="settingsRoot screenRoot">
      <div className="settingsTopbar">
        <button className="iconButton" type="button" onClick={() => requestExit("close")} aria-label={t("core.actions.back", {}, "Back")}>
          <i className="fa-solid fa-arrow-left" aria-hidden="true" />
        </button>
        <div className="settingsTopbarTitle">{t("core.ui.settings.title")}</div>
        {authUser ? (
          <div className="row" style={{ marginLeft: "auto", gap: 8 }}>
            <span className="settingsStatusMuted">{authUser.display_name || authUser.username}</span>
            <button className="chipButton" type="button" onClick={() => requestExit("logout")}>
              {t("core.actions.sign_out")}
            </button>
          </div>
        ) : null}
      </div>

      <div className="settingsLayout">
        <div className="settingsSidebar">
          <div className="settingsSidebarList">
            <button type="button" className="settingsNavItem" onClick={() => requestExit("pipelines")}>
              <span className="settingsNavIcon">
                <Icon name="diagram-project" />
              </span>
              <span className="settingsNavText">
                <span className="settingsNavTitleRow">
                  <span className="settingsNavTitle">{t("core.ui.settings.nav.pipelines.title")}</span>
                </span>
                <span className="settingsNavDesc">{t("core.ui.settings.nav.pipelines.desc")}</span>
              </span>
            </button>

            <button type="button" className="settingsNavItem" onClick={() => requestExit("processing_servers")}>
              <span className="settingsNavIcon">
                <Icon name="server" />
              </span>
              <span className="settingsNavText">
                <span className="settingsNavTitleRow">
                  <span className="settingsNavTitle">{t("core.ui.settings.nav.processing_servers.title")}</span>
                </span>
                <span className="settingsNavDesc">{t("core.ui.settings.nav.processing_servers.desc")}</span>
              </span>
            </button>

            {canManageAccess ? (
              <button type="button" className="settingsNavItem" onClick={() => requestExit("access")}>
                <span className="settingsNavIcon">
                  <Icon name="users" />
                </span>
                <span className="settingsNavText">
                  <span className="settingsNavTitleRow">
                    <span className="settingsNavTitle">Access</span>
                  </span>
                  <span className="settingsNavDesc">Manage users and include/exclude grants.</span>
                </span>
              </button>
            ) : null}

            <div className="sectionDivider" style={{ margin: "12px 6px" }} />

            {entries.map((entry) => {
              const selected = entry.id === activePanelId;
              const isDirty = entry.kind === "extension" ? Boolean(dirtyExtensions[entry.id]) : false;
              return (
                <button
                  key={entry.id}
                  type="button"
                  className={["settingsNavItem", selected ? "isSelected" : ""].filter(Boolean).join(" ")}
                  onClick={() => setActivePanelId(entry.id)}
                >
                  <span className="settingsNavIcon">
                    <Icon name={entry.icon} />
                  </span>
                  <span className="settingsNavText">
                    <span className="settingsNavTitleRow">
                      <span className="settingsNavTitle">{entry.title}</span>
                      {isDirty ? <span className="settingsNavDirtyDot" aria-label={t("core.ui.settings.unsaved_changes")} /> : null}
                    </span>
                    {entry.desc ? <span className="settingsNavDesc">{entry.desc}</span> : null}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="settingsMain">
          <div className="settingsHeader">
            <div className="settingsHeaderTitle">{activeEntry.title}</div>
            {activeEntry.desc ? <div className="settingsHeaderDesc">{activeEntry.desc}</div> : null}
            {saveError ? <div className="errorText">{saveError}</div> : null}
          </div>

          <div className="settingsContent">
            {activeEntry.kind === "core" ? (
              activeEntry.render()
            ) : activeEntry.kind === "extension" ? (
              <div>{renderExtensionPanel(activeEntry.panel)}</div>
            ) : null}
          </div>

          <div className="settingsFooter">
            <div className="settingsFooterStatus">
              {!backendAvailable ? (
                <span className="settingsStatusMuted">{t("core.ui.settings.backend_offline_title")}</span>
              ) : saving ? (
                <span className="settingsStatusMuted">{t("core.ui.settings.saving")}</span>
              ) : hasUnsavedChanges ? (
                <span className="settingsStatusMuted">
                  {unsavedSectionsLabel
                    ? t("core.ui.settings.unsaved_changes_in", { sections: unsavedSectionsLabel }, `Unsaved: ${unsavedSectionsLabel}`)
                    : t("core.ui.settings.unsaved_changes")}
                </span>
              ) : (
                <span className="settingsStatusMuted">{t("core.ui.settings.changes_saved")}</span>
              )}
            </div>

            <div className="row" style={{ gap: 10 }}>
              {hasUnsavedChanges ? (
                <>
                  <button className="chipButton" type="button" disabled={saving} onClick={() => setConfirmDiscardOpen(true)}>
                    {t("core.ui.settings.discard_changes")}
                  </button>
                  <button className="primaryButton" type="button" disabled={saving || !backendAvailable} onClick={() => void saveAll()}>
                    {saving ? t("core.ui.settings.saving") : t("core.ui.settings.save_all_changes")}
                  </button>
                </>
              ) : (
                <button className="chipButton" type="button" onClick={() => requestExit("close")}>
                  {t("core.actions.close")}
                </button>
              )}
            </div>
          </div>
        </div>

        {confirmDiscardOpen ? (
          <div className="settingsConfirmBackdrop" role="presentation">
            <div className="settingsConfirmPanel" role="dialog" aria-modal="true" aria-label={t("core.ui.settings.confirm_discard_title")}>
              <div className="settingsConfirmTitle">{t("core.ui.settings.confirm_discard_title")}</div>
              <div className="settingsConfirmDesc">{t("core.ui.settings.confirm_discard_desc")}</div>
              <div className="rowWrap" style={{ justifyContent: "flex-end", marginTop: 14 }}>
                <button className="chipButton" type="button" onClick={() => setConfirmDiscardOpen(false)}>
                  {t("core.actions.cancel")}
                </button>
                <button
                  className="dangerButton"
                  type="button"
                  onClick={() => {
                    discardAll();
                    setConfirmDiscardOpen(false);
                  }}
                >
                  {t("core.ui.settings.discard_changes")}
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {confirmExitOpen ? (
          <div className="settingsConfirmBackdrop" role="presentation">
            <div className="settingsConfirmPanel" role="dialog" aria-modal="true" aria-label={exitTitle}>
              <div className="settingsConfirmTitle">{exitTitle}</div>
              <div className="settingsConfirmDesc">{exitDesc}</div>
              <div className="rowWrap" style={{ justifyContent: "flex-end", marginTop: 14 }}>
                <button
                  className="chipButton"
                  type="button"
                  onClick={() => {
                    setConfirmExitOpen(false);
                    setPendingExitAction(null);
                  }}
                >
                  {t("core.actions.cancel")}
                </button>
                <button
                  className="dangerButton"
                  type="button"
                  onClick={() => {
                    const action = pendingExitAction;
                    discardAll();
                    setConfirmExitOpen(false);
                    setPendingExitAction(null);
                    if (action === "pipelines") onOpenPipelines();
                    else if (action === "processing_servers") onOpenProcessingServers();
                    else if (action === "access") onOpenAccess();
                    else if (action === "logout") void onLogout();
                    else onClose();
                  }}
                >
                  {exitConfirmLabel}
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
