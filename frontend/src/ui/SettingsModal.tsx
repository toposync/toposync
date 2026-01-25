import React, { useEffect, useMemo, useRef, useState } from "react";

import type { GraphicsQuality, HostApi, SettingsPanel, ThemeDefinition, WallHeightPreset } from "@toposync/plugin-api";

import type { AppSettings } from "../util/api";
import { i18n, resolveLocalizedString } from "../util/i18n";

import { Icon } from "./Icon";
import { Modal } from "./Modal";

type Props = {
  open: boolean;
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

export function SettingsModal({
  open,
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
  onClose,
}: Props): React.ReactElement | null {
  const { t, locale, setLocale } = i18n.useI18n();
  const [activePanelId, setActivePanelId] = useState<string>(() => loadActivePanelId(VIEW_PANEL_ID));
  const [draftExtensions, setDraftExtensions] = useState<Record<string, Record<string, unknown>>>(() => settings.extensions ?? {});
  const [dirtyExtensions, setDirtyExtensions] = useState<Record<string, boolean>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmCloseOpen, setConfirmCloseOpen] = useState(false);
  const [confirmDiscardOpen, setConfirmDiscardOpen] = useState(false);
  const lastSettingsRef = useRef<AppSettings>(settings);

  useEffect(() => {
    if (!open) return;
    setSaveError(null);
    setSaving(false);
    setConfirmCloseOpen(false);
    setConfirmDiscardOpen(false);

    lastSettingsRef.current = settings;
    setDraftExtensions(settings.extensions ?? {});
    setDirtyExtensions({});

    // Open "View options" by default, since it's the most common entry point.
    setActivePanelId(VIEW_PANEL_ID);
  }, [open]);

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
    if (!open) return;
    try {
      localStorage.setItem(ACTIVE_PANEL_STORAGE_KEY, activePanelId);
    } catch {
      // ignore
    }
  }, [activePanelId, open]);

  useEffect(() => {
    if (!open) return;
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
  }, [dirtyExtensions, open, settings]);

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

  function requestClose(): void {
    if (saving) return;
    if (hasUnsavedChanges) {
      setConfirmCloseOpen(true);
      return;
    }
    onClose();
  }

  return (
    <Modal
      open={open}
      title={t("core.ui.settings.title")}
      onClose={requestClose}
      panelClassName="settingsModalPanel"
      bodyClassName="settingsModalBody"
      bodyStyle={{ padding: 0, overflow: "hidden" }}
    >
      <div className="settingsLayout">
        <div className="settingsSidebar">
          <div className="settingsSidebarList">
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
                  <button
                    className="primaryButton"
                    type="button"
                    disabled={saving || !backendAvailable}
                    onClick={() => void saveAll()}
                  >
                    {saving ? t("core.ui.settings.saving") : t("core.ui.settings.save_all_changes")}
                  </button>
                </>
              ) : (
                <button className="chipButton" type="button" onClick={requestClose}>
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

        {confirmCloseOpen ? (
          <div className="settingsConfirmBackdrop" role="presentation">
            <div className="settingsConfirmPanel" role="dialog" aria-modal="true" aria-label={t("core.ui.settings.confirm_close_title")}>
              <div className="settingsConfirmTitle">{t("core.ui.settings.confirm_close_title")}</div>
              <div className="settingsConfirmDesc">{t("core.ui.settings.confirm_close_desc")}</div>
              <div className="rowWrap" style={{ justifyContent: "flex-end", marginTop: 14 }}>
                <button className="chipButton" type="button" onClick={() => setConfirmCloseOpen(false)}>
                  {t("core.actions.cancel")}
                </button>
                <button
                  className="dangerButton"
                  type="button"
                  onClick={() => {
                    discardAll();
                    setConfirmCloseOpen(false);
                    onClose();
                  }}
                >
                  {t("core.ui.settings.discard_and_close")}
                </button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
