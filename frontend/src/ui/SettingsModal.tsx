import React, { useEffect, useMemo, useState } from "react";

import type { HostApi, SettingsPanel, ThemeDefinition } from "@toposync/plugin-api";

import type { AppSettings } from "../util/api";
import { i18n, resolveLocalizedString } from "../util/i18n";

import { Icon } from "./Icon";
import { Modal } from "./Modal";

type Props = {
  open: boolean;
  backendAvailable: boolean;
  api: HostApi;
  panels: SettingsPanel[];
  themes: ThemeDefinition[];
  themeId: string;
  onSetThemeId: (themeId: string) => void;
  settings: AppSettings;
  onPatchExtensionSettings: (extensionId: string, patch: Record<string, unknown>) => void;
  onClose: () => void;
};

const ROOT_PANEL_ID = "__root__";
const CORE_PANEL_ID = "__core__";

export function SettingsModal({
  open,
  backendAvailable,
  api,
  panels,
  themes,
  themeId,
  onSetThemeId,
  settings,
  onPatchExtensionSettings,
  onClose,
}: Props): React.ReactElement | null {
  const { t, locale, setLocale } = i18n.useI18n();
  const [activePanelId, setActivePanelId] = useState<string>(ROOT_PANEL_ID);

  useEffect(() => {
    if (!open) return;
    setActivePanelId(ROOT_PANEL_ID);
  }, [open]);

  const orderedPanels = useMemo(() => {
    const list = [...panels];
    list.sort((a, b) => resolveLocalizedString(a.name).localeCompare(resolveLocalizedString(b.name)));
    return list;
  }, [panels, locale]);

  const coreEntry = useMemo(
    () => ({
      id: CORE_PANEL_ID,
      icon: "gear",
      title: t("core.ui.settings.sections.core"),
      desc: t("core.ui.settings.sections.core_desc"),
    }),
    [t],
  );

  const entries = useMemo(() => {
    const extEntries = orderedPanels.map((p) => ({
      id: p.id,
      icon: p.icon || "puzzle-piece",
      title: resolveLocalizedString(p.name),
      desc: p.description ? resolveLocalizedString(p.description) : "",
    }));
    return [coreEntry, ...extEntries];
  }, [coreEntry, orderedPanels]);

  const activePanel = activePanelId !== CORE_PANEL_ID ? orderedPanels.find((p) => p.id === activePanelId) ?? null : null;

  function renderCore(): React.ReactNode {
    return (
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
    );
  }

  function renderExtensionPanel(panel: SettingsPanel): React.ReactNode {
    const extSettings = settings.extensions?.[panel.id] ?? {};
    return panel.render({
      i18n,
      api,
      settings: extSettings,
      updateSettings: (patch) => onPatchExtensionSettings(panel.id, patch ?? {}),
    });
  }

  const activeTitle = useMemo(() => {
    if (activePanelId === ROOT_PANEL_ID) return t("core.ui.settings.title");
    if (activePanelId === CORE_PANEL_ID) return coreEntry.title;
    const entry = entries.find((e) => e.id === activePanelId);
    return entry ? entry.title : t("core.ui.settings.title");
  }, [activePanelId, coreEntry.title, entries, t]);

  return (
    <Modal open={open} title={t("core.ui.settings.title")} onClose={onClose}>
      <div className="rowWrap" style={{ justifyContent: "space-between" }}>
        <div className="row" style={{ gap: 8 }}>
          {activePanelId !== ROOT_PANEL_ID ? (
            <button
              className="iconButton"
              type="button"
              onClick={() => setActivePanelId(ROOT_PANEL_ID)}
              aria-label={t("core.actions.back")}
            >
              <Icon name="arrow-left" />
            </button>
          ) : null}
          <div className="cardTitle" style={{ margin: 0 }}>
            {activeTitle}
          </div>
        </div>
      </div>

      <div className="sectionDivider" />

      {activePanelId === ROOT_PANEL_ID ? (
        <div className="choiceList">
          {entries.map((entry) => (
            <div
              key={entry.id}
              className={["choiceItem", entry.id === activePanelId ? "isSelected" : ""].join(" ")}
              role="button"
              tabIndex={0}
              onClick={() => setActivePanelId(entry.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") setActivePanelId(entry.id);
              }}
            >
              <div className="row" style={{ gap: 10 }}>
                <div className="iconButton" style={{ height: 32, width: 32, borderRadius: 12, pointerEvents: "none" }}>
                  <Icon name={entry.icon} />
                </div>
                <div style={{ flex: 1 }}>
                  <div className="choiceTitle">{entry.title}</div>
                  {entry.desc ? <div className="choiceDesc">{entry.desc}</div> : null}
                </div>
              </div>
            </div>
          ))}
          {entries.length <= 1 ? (
            <div className="card">
              <div className="cardBody">{t("core.ui.settings.no_extensions")}</div>
            </div>
          ) : null}
        </div>
      ) : activePanelId === CORE_PANEL_ID ? (
        renderCore()
      ) : activePanel ? (
        <div>{renderExtensionPanel(activePanel)}</div>
      ) : (
        <div className="card">
          <div className="cardBody">{t("core.ui.settings.no_extensions")}</div>
        </div>
      )}
    </Modal>
  );
}
