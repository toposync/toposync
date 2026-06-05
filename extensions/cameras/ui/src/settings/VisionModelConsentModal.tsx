import React from "react";

import type { HostI18n } from "@toposync/plugin-api";

import { SubModal } from "../ui/SubModal";

type TranslateFn = ReturnType<HostI18n["useI18n"]>["t"];

export function VisionModelConsentModal({
  open,
  serverLabel,
  modelName,
  runtimeLabel,
  sourceLabel,
  checked,
  submitting,
  error,
  t,
  onToggleChecked,
  onClose,
  onConfirm,
}: {
  open: boolean;
  serverLabel: string;
  modelName: string;
  runtimeLabel: string;
  sourceLabel: string;
  checked: boolean;
  submitting: boolean;
  error: string | null;
  t: TranslateFn;
  onToggleChecked: (checked: boolean) => void;
  onClose: () => void;
  onConfirm: () => void;
}): React.ReactElement | null {
  if (!open) return null;
  return (
    <SubModal
      open={open}
      title={t("ext.cameras.pipeline_preset.model.consent_title", {}, "Prepare detection model locally")}
      onClose={() => (submitting ? undefined : onClose())}
      panelStyle={{ width: "min(560px, calc(100vw - 28px))" }}
    >
      <div className="settingsPanel">
        {error ? <div className="errorText">{error}</div> : null}
        <div className="settingsStatusMuted">
          {t(
            "ext.cameras.pipeline_preset.model.consent_body",
            {},
            "Toposync will download the upstream model files and prepare them on the selected processing server.",
          )}
        </div>
        <div className="settingsList">
          <div className="settingsListItem">
            <span className="settingsListTitle">{t("ext.cameras.pipeline_preset.processing_server", {}, "Processing server")}</span>
            <span className="settingsListMeta">{serverLabel}</span>
          </div>
          <div className="settingsListItem">
            <span className="settingsListTitle">{t("ext.cameras.pipeline_preset.model.label", {}, "Detection model")}</span>
            <span className="settingsListMeta">{modelName}</span>
          </div>
          <div className="settingsListItem">
            <span className="settingsListTitle">{t("ext.cameras.pipeline_preset.model.runtime", {}, "Runtime")}</span>
            <span className="settingsListMeta">
              {runtimeLabel || t("ext.cameras.pipeline_preset.model.runtime_unknown", {}, "Runtime reported by server")}
            </span>
          </div>
          <div className="settingsListItem">
            <span className="settingsListTitle">{t("ext.cameras.pipeline_preset.model.upstream_source", {}, "Upstream source")}</span>
            <span className="settingsListMeta">
              {sourceLabel || t("ext.cameras.pipeline_preset.model.upstream_unknown", {}, "Source reported by model manifest")}
            </span>
          </div>
        </div>
        <label className="chipButton" style={{ justifyContent: "flex-start" }}>
          <input
            type="checkbox"
            checked={checked}
            disabled={submitting}
            onChange={(event) => onToggleChecked(event.target.checked)}
          />
          {t(
            "ext.cameras.pipeline_preset.model.consent_check",
            {},
            "I understand that Toposync will download and prepare this upstream model on this server.",
          )}
        </label>
        <div className="rowWrap" style={{ justifyContent: "flex-end" }}>
          <button className="chipButton" type="button" disabled={submitting} onClick={onClose}>
            {t("core.actions.cancel", {}, "Cancel")}
          </button>
          <button className="primaryButton" type="button" disabled={!checked || submitting} onClick={onConfirm}>
            {submitting
              ? t("ext.cameras.pipeline_preset.model.preparing", {}, "Preparing...")
              : t("ext.cameras.pipeline_preset.model.prepare_auto", {}, "Download and prepare automatically")}
          </button>
        </div>
      </div>
    </SubModal>
  );
}
