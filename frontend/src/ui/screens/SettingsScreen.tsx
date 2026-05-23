import React, { useEffect, useMemo, useRef, useState } from "react";

import type { GraphicsQuality, HostApi, SettingsPanel, ThemeDefinition, WallHeightPreset } from "@toposync/plugin-api";

import {
  disableManagedExtension,
  enableManagedExtension,
  fetchExtensionManagementCatalog,
  installManualManagedExtension,
  installRecommendedManagedExtension,
  removeManagedExtension,
} from "../../util/api";
import type {
  AppSettings,
  AuthUser,
  Composition,
  CompositionSummary,
  ExtensionManagementCatalog,
  ExtensionManagementItem,
} from "../../util/api";
import { i18n, resolveLocalizedString } from "../../util/i18n";
import type { Viewport3DBackground } from "../../util/theme";

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
  viewport3dBackground: Viewport3DBackground;
  onSetViewport3dBackground: (value: Viewport3DBackground) => void;
  settings: AppSettings;
  onPatchExtensionSettings: (extensionId: string, patch: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onOpenPipelines: () => void;
  onOpenProcessingServers: () => void;
  onOpenAccess: () => void;
  compositions: CompositionSummary[];
  activeCompositionId: string;
  onActivateComposition: (compositionId: string) => Promise<Composition>;
  onCreateComposition: (name: string) => Promise<Composition>;
  onRenameComposition: (compositionId: string, name: string) => Promise<Composition>;
  onDeleteComposition: (compositionId: string) => Promise<void>;
  onOpenCompositionEditor: () => void;
  canManageAccess: boolean;
  authUser: AuthUser | null;
  onLogout: () => Promise<void>;
  onClose: () => void;
};

const VIEW_PANEL_ID = "__view__";
const COMPOSITIONS_PANEL_ID = "__compositions__";
const CORE_PANEL_ID = "__core__";
const EXTENSIONS_PANEL_ID = "__extensions__";
const ACTIVE_PANEL_STORAGE_KEY = "toposync.settings.active_panel.v4";

type ExitAction = "close" | "pipelines" | "processing_servers" | "access" | "logout" | "composition_editor";

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

function extensionStatusRank(status: ExtensionManagementItem["status"]): number {
  if (status === "active") return 0;
  if (status === "pending_restart") return 1;
  if (status === "disabled") return 2;
  if (status === "error") return 3;
  if (status === "not_installed") return 4;
  return 5;
}

function packageLabel(item: ExtensionManagementItem): string {
  return item.package || item.pip_spec || item.extension_id;
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
  viewport3dBackground,
  onSetViewport3dBackground,
  settings,
  onPatchExtensionSettings,
  onOpenPipelines,
  onOpenProcessingServers,
  onOpenAccess,
  compositions,
  activeCompositionId,
  onActivateComposition,
  onCreateComposition,
  onRenameComposition,
  onDeleteComposition,
  onOpenCompositionEditor,
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
  const [extensionCatalog, setExtensionCatalog] = useState<ExtensionManagementCatalog | null>(null);
  const [extensionCatalogLoading, setExtensionCatalogLoading] = useState(false);
  const [extensionQuery, setExtensionQuery] = useState("");
  const [extensionManualSpec, setExtensionManualSpec] = useState("");
  const [extensionActionId, setExtensionActionId] = useState<string | null>(null);
  const [extensionError, setExtensionError] = useState<string | null>(null);
  const [extensionNotice, setExtensionNotice] = useState<string | null>(null);
  const [newCompositionName, setNewCompositionName] = useState("");
  const [editingCompositionId, setEditingCompositionId] = useState<string | null>(null);
  const [editingCompositionName, setEditingCompositionName] = useState("");
  const [confirmDeleteCompositionId, setConfirmDeleteCompositionId] = useState<string | null>(null);
  const [compositionActionId, setCompositionActionId] = useState<string | null>(null);
  const [compositionError, setCompositionError] = useState<string | null>(null);
  const [pendingExitAction, setPendingExitAction] = useState<ExitAction | null>(null);
  const [pendingCompositionEditorId, setPendingCompositionEditorId] = useState<string | null>(null);
  const lastSettingsRef = useRef<AppSettings>(settings);

  async function loadExtensionCatalog(): Promise<void> {
    if (!backendAvailable) {
      setExtensionCatalog(null);
      return;
    }
    setExtensionCatalogLoading(true);
    setExtensionError(null);
    try {
      setExtensionCatalog(await fetchExtensionManagementCatalog());
    } catch (err) {
      setExtensionError(err instanceof Error ? err.message : String(err));
    } finally {
      setExtensionCatalogLoading(false);
    }
  }

  useEffect(() => {
    if (!backendAvailable) {
      setExtensionCatalog(null);
      return;
    }
    let cancelled = false;

    async function run(): Promise<void> {
      setExtensionCatalogLoading(true);
      setExtensionError(null);
      try {
        const catalog = await fetchExtensionManagementCatalog();
        if (!cancelled) setExtensionCatalog(catalog);
      } catch (err) {
        if (!cancelled) setExtensionError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setExtensionCatalogLoading(false);
      }
    }

    void run();
    return () => {
      cancelled = true;
    };
  }, [backendAvailable]);

  const orderedPanels = useMemo(() => {
    const list = [...panels];
    list.sort((a, b) => resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)));
    return list;
  }, [panels, locale]);

  const sortedCompositions = useMemo(
    () => [...compositions].sort((a, b) => a.name.localeCompare(b.name, locale)),
    [compositions, locale],
  );

  const canDeleteComposition = compositions.length > 1;

  const dirtyExtensionIds = useMemo(() => {
    return Object.entries(dirtyExtensions)
      .filter(([, dirty]) => Boolean(dirty))
      .map(([id]) => id)
      .sort((a, b) => a.localeCompare(b));
  }, [dirtyExtensions]);

  const hasUnsavedChanges = dirtyExtensionIds.length > 0;

  useEffect(() => {
    const ids = new Set(compositions.map((composition) => composition.id));
    if (editingCompositionId && !ids.has(editingCompositionId)) {
      setEditingCompositionId(null);
      setEditingCompositionName("");
    }
    if (confirmDeleteCompositionId && !ids.has(confirmDeleteCompositionId)) {
      setConfirmDeleteCompositionId(null);
    }
  }, [compositions, confirmDeleteCompositionId, editingCompositionId]);

  const entries = useMemo<SettingsEntry[]>(() => {
    const viewEntry: SettingsEntry = {
      kind: "core",
      id: VIEW_PANEL_ID,
      icon: "sliders",
      title: t("core.ui.settings.sections.view"),
      desc: t("core.ui.settings.sections.view_desc"),
      render: () => (
        <div>
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

          <div className="sectionDivider" />

          <div className="modalSectionTitle">{t("core.ui.settings.viewport3d_background")}</div>
          <div className="choiceList">
            {(
              [
                {
                  id: "paper",
                  title: t("core.ui.settings.viewport3d_background.paper"),
                  desc: t("core.ui.settings.viewport3d_background.paper_desc"),
                },
                {
                  id: "pure",
                  title: t("core.ui.settings.viewport3d_background.pure"),
                  desc: t("core.ui.settings.viewport3d_background.pure_desc"),
                },
                {
                  id: "night",
                  title: t("core.ui.settings.viewport3d_background.night"),
                  desc: t("core.ui.settings.viewport3d_background.night_desc"),
                },
              ] as const
            ).map((opt) => {
              const selected = viewport3dBackground === opt.id;
              return (
                <div
                  key={opt.id}
                  className={["choiceItem", selected ? "isSelected" : ""].join(" ")}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSetViewport3dBackground(opt.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") onSetViewport3dBackground(opt.id);
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

    const compositionsEntry: SettingsEntry = {
      kind: "core",
      id: COMPOSITIONS_PANEL_ID,
      icon: "layer-group",
      title: t("core.ui.settings.sections.compositions"),
      desc: t("core.ui.settings.sections.compositions_desc"),
      render: () => renderCompositionsPanel(),
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

          <div className="modalSectionTitle">{t("core.ui.auth.session.title")}</div>
          <div className="card">
            <div className="cardTitle">
              {authUser?.display_name || authUser?.username || t("core.ui.auth.session.current_user_fallback")}
            </div>
            {authUser ? <div className="cardBody">{authUser.username} · {authUser.role}</div> : null}
            <div className="cardFooter">
              <button className="chipButton" type="button" onClick={() => requestExit("logout")}>
                {t("core.actions.sign_out")}
              </button>
            </div>
          </div>
        </div>
      ),
    };

    const extensionsEntry: SettingsEntry = {
      kind: "core",
      id: EXTENSIONS_PANEL_ID,
      icon: "puzzle-piece",
      title: t("core.ui.settings.sections.extensions"),
      desc: t("core.ui.settings.sections.extensions_desc"),
      render: () => null,
    };

    const extEntries: SettingsEntry[] = orderedPanels.map((panel) => ({
      kind: "extension",
      id: panel.id,
      icon: panel.icon || "puzzle-piece",
      title: resolveLocalizedString(panel.name),
      desc: panel.description ? resolveLocalizedString(panel.description) : "",
      panel,
    }));
    return [viewEntry, compositionsEntry, coreEntry, extensionsEntry, ...extEntries];
  }, [
    activeCompositionId,
    backendAvailable,
    canDeleteComposition,
    compositionActionId,
    compositionError,
    confirmDeleteCompositionId,
    editingCompositionId,
    editingCompositionName,
    ghostWalls,
    graphicsQuality,
    hasUnsavedChanges,
    locale,
    newCompositionName,
    onActivateComposition,
    onCreateComposition,
    onDeleteComposition,
    onOpenCompositionEditor,
    onRenameComposition,
    onSetGhostWalls,
    onSetGraphicsQuality,
    onSetThemeId,
    onSetViewport3dBackground,
    onSetWallHeightPreset,
    onLogout,
    authUser,
    orderedPanels,
    saving,
    sortedCompositions,
    t,
    themeId,
    themes,
    viewport3dBackground,
    wallHeightPreset,
    setLocale,
  ]);

  const activeEntry = useMemo(
    () => entries.find((entry) => entry.id === activePanelId) ?? entries[0],
    [activePanelId, entries],
  );

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
    function handleOpenSettingsPanel(event: Event): void {
      const detail = (event as CustomEvent<{ panelId?: unknown }>).detail;
      const panelId = typeof detail?.panelId === "string" ? detail.panelId : "";
      if (!panelId || !entries.some((entry) => entry.id === panelId)) return;
      setActivePanelId(panelId);
    }

    window.addEventListener("toposync:open-settings-panel", handleOpenSettingsPanel);
    return () => window.removeEventListener("toposync:open-settings-panel", handleOpenSettingsPanel);
  }, [entries]);

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

  async function createSettingsComposition(): Promise<void> {
    const name = newCompositionName.trim();
    if (!name || compositionActionId || !backendAvailable) return;
    setCompositionActionId("create");
    setCompositionError(null);
    try {
      await onCreateComposition(name);
      setNewCompositionName("");
      setEditingCompositionId(null);
      setEditingCompositionName("");
      setConfirmDeleteCompositionId(null);
    } catch (err) {
      setCompositionError(err instanceof Error ? err.message : t("core.compositions.error.create"));
    } finally {
      setCompositionActionId(null);
    }
  }

  async function activateSettingsComposition(compositionId: string): Promise<void> {
    if (compositionActionId || !backendAvailable || compositionId === activeCompositionId) return;
    setCompositionActionId(`activate:${compositionId}`);
    setCompositionError(null);
    try {
      await onActivateComposition(compositionId);
      setConfirmDeleteCompositionId(null);
    } catch (err) {
      setCompositionError(err instanceof Error ? err.message : t("core.compositions.error.activate"));
    } finally {
      setCompositionActionId(null);
    }
  }

  async function renameSettingsComposition(compositionId: string): Promise<void> {
    const name = editingCompositionName.trim();
    if (!name || compositionActionId || !backendAvailable) return;
    setCompositionActionId(`rename:${compositionId}`);
    setCompositionError(null);
    try {
      await onRenameComposition(compositionId, name);
      setEditingCompositionId(null);
      setEditingCompositionName("");
    } catch (err) {
      setCompositionError(err instanceof Error ? err.message : t("core.compositions.error.rename"));
    } finally {
      setCompositionActionId(null);
    }
  }

  async function deleteSettingsComposition(compositionId: string): Promise<void> {
    if (compositionActionId || !backendAvailable || !canDeleteComposition) return;
    setCompositionActionId(`delete:${compositionId}`);
    setCompositionError(null);
    try {
      await onDeleteComposition(compositionId);
      setConfirmDeleteCompositionId(null);
      setEditingCompositionId(null);
      setEditingCompositionName("");
    } catch (err) {
      setCompositionError(err instanceof Error ? err.message : t("core.compositions.error.delete"));
    } finally {
      setCompositionActionId(null);
    }
  }

  async function openSettingsCompositionEditor(compositionId: string): Promise<void> {
    if (compositionActionId || !backendAvailable) return;
    setCompositionActionId(`editor:${compositionId}`);
    setCompositionError(null);
    try {
      if (compositionId !== activeCompositionId) {
        await onActivateComposition(compositionId);
      }
      setCompositionActionId(null);
      onOpenCompositionEditor();
    } catch (err) {
      setCompositionError(err instanceof Error ? err.message : t("core.ui.settings.compositions.error.open_editor"));
      setCompositionActionId(null);
    }
  }

  function requestOpenSettingsCompositionEditor(compositionId: string): void {
    if (saving || compositionActionId) return;
    if (hasUnsavedChanges) {
      setPendingExitAction("composition_editor");
      setPendingCompositionEditorId(compositionId);
      setConfirmExitOpen(true);
      return;
    }
    void openSettingsCompositionEditor(compositionId);
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

  function requestExit(action: ExitAction): void {
    if (saving) return;
    if (action !== "composition_editor") setPendingCompositionEditorId(null);
    if (hasUnsavedChanges) {
      setPendingExitAction(action);
      setConfirmExitOpen(true);
      return;
    }
    if (action === "pipelines") onOpenPipelines();
    else if (action === "processing_servers") onOpenProcessingServers();
    else if (action === "access") onOpenAccess();
    else if (action === "logout") void onLogout();
    else if (action === "composition_editor" && pendingCompositionEditorId) void openSettingsCompositionEditor(pendingCompositionEditorId);
    else onClose();
  }

  const exitTitle = useMemo(() => {
    if (pendingExitAction === "pipelines") return t("core.ui.settings.confirm_open_pipelines_title");
    if (pendingExitAction === "processing_servers") return t("core.ui.settings.confirm_open_processing_servers_title");
    if (pendingExitAction === "access") return t("core.ui.settings.confirm_open_access_title");
    if (pendingExitAction === "logout") return t("core.ui.auth.confirm_sign_out_title");
    if (pendingExitAction === "composition_editor") return t("core.ui.settings.confirm_open_composition_editor_title");
    return t("core.ui.settings.confirm_close_title");
  }, [pendingExitAction, t]);

  const exitDesc = useMemo(() => {
    if (
      pendingExitAction === "pipelines" ||
      pendingExitAction === "processing_servers" ||
      pendingExitAction === "access" ||
      pendingExitAction === "logout" ||
      pendingExitAction === "composition_editor"
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
      pendingExitAction === "logout" ||
      pendingExitAction === "composition_editor"
    ) {
      return t("core.ui.settings.confirm_discard_continue");
    }
    return t("core.ui.settings.discard_and_close");
  }, [pendingExitAction, t]);

  const filteredExtensionItems = useMemo(() => {
    const query = extensionQuery.trim().toLowerCase();
    const items = [...(extensionCatalog?.items ?? [])].sort((a, b) => {
      const rank = extensionStatusRank(a.status) - extensionStatusRank(b.status);
      if (rank !== 0) return rank;
      const rec = Number(b.recommended) - Number(a.recommended);
      if (rec !== 0) return rec;
      return a.name.localeCompare(b.name, locale);
    });
    if (!query) return items;
    return items.filter((item) => {
      const haystack = [item.name, item.description, item.extension_id, item.package, item.pip_spec, item.category]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [extensionCatalog, extensionQuery, locale]);

  function extensionStatusLabel(status: ExtensionManagementItem["status"]): string {
    return t(`core.ui.settings.extensions.status.${status}`, {}, status);
  }

  function extensionSourceLabel(source: ExtensionManagementItem["source"]): string {
    return t(`core.ui.settings.extensions.source.${source}`, {}, source);
  }

  async function runExtensionAction(actionId: string, action: () => Promise<{ ok: boolean; catalog: ExtensionManagementCatalog; error: string | null }>): Promise<void> {
    if (!backendAvailable || extensionActionId) return;
    setExtensionActionId(actionId);
    setExtensionError(null);
    setExtensionNotice(null);
    try {
      const result = await action();
      setExtensionCatalog(result.catalog);
      if (!result.ok) {
        setExtensionError(result.error || t("core.ui.settings.extensions.error.operation_failed"));
      } else {
        setExtensionNotice(t("core.ui.settings.extensions.operation_done"));
      }
    } catch (err) {
      setExtensionError(err instanceof Error ? err.message : String(err));
    } finally {
      setExtensionActionId(null);
    }
  }

  async function submitManualExtension(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const spec = extensionManualSpec.trim();
    if (!spec) return;
    const confirmed = window.confirm(t("core.ui.settings.extensions.confirm_install_manual", { spec }));
    if (!confirmed) return;
    await runExtensionAction("__manual__", async () => {
      const result = await installManualManagedExtension(spec);
      if (result.ok) setExtensionManualSpec("");
      return result;
    });
  }

  function renderCompositionsPanel(): React.ReactNode {
    const busy = Boolean(compositionActionId);

    return (
      <div className="settingsPanel compositionsSettingsPanel">
        {!backendAvailable ? (
          <div className="card">
            <div className="cardTitle">{t("core.ui.settings.backend_offline_title")}</div>
            <div className="cardBody">{t("core.ui.settings.backend_offline_desc")}</div>
          </div>
        ) : null}

        <form
          className="compositionsSettingsCreate"
          onSubmit={(event) => {
            event.preventDefault();
            void createSettingsComposition();
          }}
        >
          <label className="field">
            <span className="label">{t("core.compositions.section.new")}</span>
            <input
              className="input"
              value={newCompositionName}
              placeholder={t("core.compositions.new.placeholder")}
              onChange={(event) => setNewCompositionName(event.target.value)}
              disabled={busy || !backendAvailable}
            />
          </label>
          <button
            className="primaryButton"
            type="submit"
            aria-label={t("core.compositions.aria.create")}
            disabled={busy || !backendAvailable || !newCompositionName.trim()}
          >
            <Icon name="plus" />
            <span>{t("core.ui.settings.compositions.create")}</span>
          </button>
        </form>

        <div className="sectionDivider" />

        <div className="settingsSectionHeader">
          <div>
            <div className="modalSectionTitle">{t("core.compositions.section.list")}</div>
            <div className="settingsDescription">
              {t("core.ui.settings.compositions.list_desc", { count: sortedCompositions.length })}
            </div>
          </div>
        </div>

        {compositionError ? (
          <div className="errorText" role="alert">
            {compositionError}
          </div>
        ) : null}

        <div className="compositionsSettingsList" role="list">
          {sortedCompositions.map((composition) => {
            const isActive = composition.id === activeCompositionId;
            const isEditing = editingCompositionId === composition.id;
            const isConfirmingDelete = confirmDeleteCompositionId === composition.id;
            const rowBusy = compositionActionId?.endsWith(`:${composition.id}`) === true;
            const mainLabel = isConfirmingDelete
              ? t("core.compositions.delete_confirm", { name: composition.name })
              : composition.name;

            if (isEditing) {
              return (
                <div className="compositionsSettingsRow" key={composition.id} role="listitem">
                  <div className="compositionsSettingsEditMain">
                    <input
                      className="input"
                      value={editingCompositionName}
                      onChange={(event) => setEditingCompositionName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void renameSettingsComposition(composition.id);
                        if (event.key === "Escape") {
                          setEditingCompositionId(null);
                          setEditingCompositionName("");
                        }
                      }}
                      disabled={busy || !backendAvailable}
                      autoFocus
                    />
                  </div>
                  <div className="compositionsSettingsActions">
                    <button
                      className="iconButton iconButtonPrimary"
                      type="button"
                      aria-label={t("core.compositions.aria.save_name")}
                      title={t("core.compositions.aria.save_name")}
                      disabled={busy || !backendAvailable || !editingCompositionName.trim()}
                      onClick={() => void renameSettingsComposition(composition.id)}
                    >
                      <Icon name="check" />
                    </button>
                    <button
                      className="iconButton"
                      type="button"
                      aria-label={t("core.compositions.aria.cancel")}
                      title={t("core.compositions.aria.cancel")}
                      disabled={busy}
                      onClick={() => {
                        setEditingCompositionId(null);
                        setEditingCompositionName("");
                      }}
                    >
                      <Icon name="xmark" />
                    </button>
                  </div>
                </div>
              );
            }

            return (
              <div
                className={["compositionsSettingsRow", isActive ? "isActive" : "", isConfirmingDelete ? "isDanger" : ""]
                  .filter(Boolean)
                  .join(" ")}
                key={composition.id}
                role="listitem"
              >
                <button
                  className="compositionsSettingsMain"
                  type="button"
                  disabled={busy || !backendAvailable || isConfirmingDelete || isActive}
                  onClick={() => void activateSettingsComposition(composition.id)}
                  aria-label={
                    isActive
                      ? t("core.ui.settings.compositions.aria.active", { name: composition.name })
                      : t("core.ui.settings.compositions.aria.select", { name: composition.name })
                  }
                >
                  <span className="compositionsSettingsName">{mainLabel}</span>
                  <span className="compositionsSettingsMeta">
                    {isActive
                      ? t("core.ui.settings.compositions.active")
                      : t("core.ui.settings.compositions.available")}
                  </span>
                </button>

                <div className="compositionsSettingsActions">
                  {isConfirmingDelete ? (
                    <>
                      <button
                        className="iconButton"
                        type="button"
                        aria-label={t("core.compositions.aria.cancel_delete")}
                        title={t("core.compositions.aria.cancel_delete")}
                        disabled={busy}
                        onClick={() => setConfirmDeleteCompositionId(null)}
                      >
                        <Icon name="xmark" />
                      </button>
                      <button
                        className="iconButton iconButtonDanger"
                        type="button"
                        aria-label={t("core.compositions.aria.confirm_delete")}
                        title={t("core.compositions.aria.confirm_delete")}
                        disabled={busy || !backendAvailable}
                        onClick={() => void deleteSettingsComposition(composition.id)}
                      >
                        <Icon name="trash" />
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        className="iconButton iconButtonPrimary"
                        type="button"
                        aria-label={t("core.ui.settings.compositions.aria.open_editor", { name: composition.name })}
                        title={t("core.ui.settings.compositions.open_editor")}
                        disabled={busy || !backendAvailable}
                        onClick={() => requestOpenSettingsCompositionEditor(composition.id)}
                      >
                        <Icon name={rowBusy && compositionActionId?.startsWith("editor:") ? "rotate-right" : "pen-to-square"} />
                      </button>
                      <button
                        className="iconButton"
                        type="button"
                        aria-label={t("core.compositions.aria.rename")}
                        title={t("core.compositions.aria.rename")}
                        disabled={busy || !backendAvailable}
                        onClick={() => {
                          setEditingCompositionId(composition.id);
                          setEditingCompositionName(composition.name);
                          setConfirmDeleteCompositionId(null);
                          setCompositionError(null);
                        }}
                      >
                        <Icon name="i-cursor" />
                      </button>
                      <button
                        className="iconButton iconButtonDanger"
                        type="button"
                        aria-label={t("core.compositions.aria.delete")}
                        title={!canDeleteComposition ? t("core.compositions.cannot_delete_last") : t("core.compositions.aria.delete")}
                        disabled={busy || !backendAvailable || !canDeleteComposition}
                        onClick={() => {
                          if (!canDeleteComposition) return;
                          setConfirmDeleteCompositionId(composition.id);
                          setEditingCompositionId(null);
                          setEditingCompositionName("");
                          setCompositionError(null);
                        }}
                      >
                        <Icon name="trash" />
                      </button>
                    </>
                  )}
                </div>
              </div>
            );
          })}

          {sortedCompositions.length === 0 ? (
            <div className="settingsStatusMuted">{t("core.ui.settings.compositions.empty")}</div>
          ) : null}
        </div>
      </div>
    );
  }

  function renderExtensionActions(item: ExtensionManagementItem): React.ReactNode {
    const busy = extensionActionId === item.extension_id || extensionActionId === `${item.extension_id}:remove`;
    const disabled = !backendAvailable || Boolean(extensionActionId);
    const installAction = item.recommended
      ? () => installRecommendedManagedExtension(item.extension_id)
      : () => installManualManagedExtension(item.pip_spec || item.package);

    return (
      <div className="extensionItemActions">
        {item.status === "not_installed" || item.status === "error" ? (
          item.pip_spec || item.recommended ? (
            <button
              className="primaryButton"
              type="button"
              disabled={disabled || busy}
              onClick={() => {
                const confirmed = window.confirm(
                  t("core.ui.settings.extensions.confirm_install", { name: item.name || item.extension_id }),
                );
                if (!confirmed) return;
                void runExtensionAction(item.extension_id, installAction);
              }}
            >
              <Icon name="download" />
              <span>{busy ? t("core.ui.settings.extensions.installing") : t("core.ui.settings.extensions.install")}</span>
            </button>
          ) : null
        ) : item.enabled ? (
          <button
            className="chipButton"
            type="button"
            disabled={disabled || busy}
            onClick={() => void runExtensionAction(item.extension_id, () => disableManagedExtension(item.extension_id))}
          >
            <Icon name="power-off" />
            <span>{t("core.ui.settings.extensions.disable")}</span>
          </button>
        ) : (
          <button
            className="primaryButton"
            type="button"
            disabled={disabled || busy}
            onClick={() => void runExtensionAction(item.extension_id, () => enableManagedExtension(item.extension_id))}
          >
            <Icon name="power-off" />
            <span>{t("core.ui.settings.extensions.enable")}</span>
          </button>
        )}

        {item.removable ? (
          <button
            className="dangerButton"
            type="button"
            disabled={disabled || busy}
            onClick={() => {
              const confirmed = window.confirm(
                t("core.ui.settings.extensions.confirm_remove", { name: item.name || item.extension_id }),
              );
              if (!confirmed) return;
              void runExtensionAction(`${item.extension_id}:remove`, () => removeManagedExtension(item.extension_id));
            }}
          >
            <Icon name="trash" />
            <span>{t("core.ui.settings.extensions.remove")}</span>
          </button>
        ) : null}
      </div>
    );
  }

  function renderExtensionsManagementPanel(): React.ReactNode {
    const activeCount = extensionCatalog?.items.filter((item) => item.status === "active").length ?? 0;
    const disabledCount = extensionCatalog?.items.filter((item) => item.status === "disabled").length ?? 0;
    const recommendedCount = extensionCatalog?.items.filter((item) => item.recommended).length ?? 0;

    return (
      <div className="extensionsSettingsPanel">
        {!backendAvailable ? (
          <div className="card">
            <div className="cardTitle">{t("core.ui.settings.backend_offline_title")}</div>
            <div className="cardBody">{t("core.ui.settings.backend_offline_desc")}</div>
          </div>
        ) : null}

        {extensionCatalog?.restart_required ? (
          <div className="extensionStatusBanner">
            <Icon name="rotate-right" />
            <div>
              <div className="extensionStatusBannerTitle">{t("core.ui.settings.extensions.restart_required")}</div>
              <div className="extensionStatusBannerDesc">{t("core.ui.settings.extensions.restart_required_desc")}</div>
            </div>
          </div>
        ) : null}

        {extensionError ? <div className="errorText">{extensionError}</div> : null}
        {extensionNotice ? <div className="extensionNotice">{extensionNotice}</div> : null}

        <div className="extensionSummaryGrid">
          <div className="extensionSummaryItem">
            <span>{t("core.ui.settings.extensions.summary.active")}</span>
            <strong>{activeCount}</strong>
          </div>
          <div className="extensionSummaryItem">
            <span>{t("core.ui.settings.extensions.summary.disabled")}</span>
            <strong>{disabledCount}</strong>
          </div>
          <div className="extensionSummaryItem">
            <span>{t("core.ui.settings.extensions.summary.recommended")}</span>
            <strong>{recommendedCount}</strong>
          </div>
        </div>

        <form className="extensionManualForm" onSubmit={(event) => void submitManualExtension(event)}>
          <label className="field">
            <span className="label">{t("core.ui.settings.extensions.manual_label")}</span>
            <input
              className="input"
              value={extensionManualSpec}
              onChange={(event) => setExtensionManualSpec(event.target.value)}
              placeholder={t("core.ui.settings.extensions.manual_placeholder")}
              disabled={!backendAvailable || Boolean(extensionActionId)}
            />
            <span className="extensionManualHint">{t("core.ui.settings.extensions.manual_hint")}</span>
          </label>
          <button
            className="primaryButton"
            type="submit"
            disabled={!backendAvailable || Boolean(extensionActionId) || !extensionManualSpec.trim()}
          >
            <Icon name="download" />
            <span>{extensionActionId === "__manual__" ? t("core.ui.settings.extensions.installing") : t("core.ui.settings.extensions.install")}</span>
          </button>
        </form>

        <div className="extensionsToolbar">
          <div className="extensionsSearch">
            <Icon name="magnifying-glass" />
            <input
              className="input"
              value={extensionQuery}
              onChange={(event) => setExtensionQuery(event.target.value)}
              placeholder={t("core.ui.settings.extensions.search_placeholder")}
            />
          </div>
          <button className="chipButton" type="button" disabled={!backendAvailable || extensionCatalogLoading} onClick={() => void loadExtensionCatalog()}>
            <Icon name="rotate-right" />
            <span>{t("core.actions.refresh")}</span>
          </button>
        </div>

        {extensionCatalogLoading ? <div className="settingsStatusMuted">{t("core.ui.settings.extensions.loading")}</div> : null}

        <div className="extensionItemsList">
          {filteredExtensionItems.map((item) => (
            <div className="extensionItem" key={item.extension_id}>
              <div className="extensionItemHeader">
                <div className="extensionItemTitleBlock">
                  <div className="extensionItemTitle">{item.name || item.extension_id}</div>
                  {item.description ? <div className="extensionItemDescription">{item.description}</div> : null}
                </div>
                <div className={["extensionStatusBadge", `is-${item.status}`].join(" ")}>
                  {extensionStatusLabel(item.status)}
                </div>
              </div>

              <div className="extensionItemMeta">
                <span>{packageLabel(item)}</span>
                {item.package_version ? <span>{item.package_version}</span> : null}
                <span>{extensionSourceLabel(item.source)}</span>
                {item.category ? <span>{item.category}</span> : null}
              </div>

              {item.status_detail ? <div className="extensionItemDetail">{item.status_detail}</div> : null}

              <div className="extensionItemFooter">
                <div className="extensionItemBadges">
                  {item.recommended ? <span className="extensionMiniBadge">{t("core.ui.settings.extensions.recommended")}</span> : null}
                  {item.managed ? <span className="extensionMiniBadge">{t("core.ui.settings.extensions.managed")}</span> : null}
                  {item.installed ? <span className="extensionMiniBadge">{t("core.ui.settings.extensions.installed")}</span> : null}
                </div>
                {renderExtensionActions(item)}
              </div>
            </div>
          ))}

          {!extensionCatalogLoading && filteredExtensionItems.length === 0 ? (
            <div className="settingsStatusMuted">{t("core.ui.settings.extensions.no_results")}</div>
          ) : null}
        </div>
      </div>
    );
  }

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
                    <span className="settingsNavTitle">{t("core.ui.settings.nav.access.title")}</span>
                  </span>
                  <span className="settingsNavDesc">{t("core.ui.settings.nav.access.desc")}</span>
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
            {activeEntry.id === EXTENSIONS_PANEL_ID ? (
              renderExtensionsManagementPanel()
            ) : activeEntry.kind === "core" ? (
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
	                    setPendingCompositionEditorId(null);
	                  }}
	                >
	                  {t("core.actions.cancel")}
	                </button>
	                <button
	                  className="dangerButton"
	                  type="button"
	                  onClick={() => {
	                    const action = pendingExitAction;
	                    const compositionId = pendingCompositionEditorId;
	                    discardAll();
	                    setConfirmExitOpen(false);
	                    setPendingExitAction(null);
	                    setPendingCompositionEditorId(null);
	                    if (action === "pipelines") onOpenPipelines();
	                    else if (action === "processing_servers") onOpenProcessingServers();
	                    else if (action === "access") onOpenAccess();
	                    else if (action === "logout") void onLogout();
	                    else if (action === "composition_editor" && compositionId) void openSettingsCompositionEditor(compositionId);
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
