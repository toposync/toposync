import React from "react";

import { i18n } from "../util/i18n";
import { Modal } from "./Modal";

type Props = {
  open: boolean;
  action: "prepare" | "update";
  serverId: string;
  modelName: string;
  runtimeLabel: string;
  sourceLabel: string;
  submitting?: boolean;
  checked: boolean;
  error?: string | null;
  extraHint?: React.ReactNode;
  onToggleChecked: (next: boolean) => void;
  onClose: () => void;
  onConfirm: () => void;
};

function isHttpUrl(value: string): boolean {
  const clean = String(value || "").trim().toLowerCase();
  return clean.startsWith("http://") || clean.startsWith("https://");
}

export function LocalBuildConsentModal({
  open,
  action,
  serverId,
  modelName,
  runtimeLabel,
  sourceLabel,
  submitting = false,
  checked,
  error,
  extraHint,
  onToggleChecked,
  onClose,
  onConfirm,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const actionId = action === "update" ? "update" : "prepare";

  return (
    <Modal
      open={open}
      title={t(`core.ui.vision.local_build_modal.title_${actionId}`)}
      onClose={onClose}
    >
      <div className="pipelinesHint">
        {t(`core.ui.vision.local_build_modal.intro_${actionId}`, {
          model: modelName,
        })}
      </div>

      <div className="processingServerConsentSummary">
        <div className="pipelinesStatsRow">
          <div className="pipelinesStatsItem">
            <div className="cardMeta">{t("core.ui.vision.local_build_modal.field.machine")}</div>
            <div>{serverId}</div>
          </div>
          <div className="pipelinesStatsItem">
            <div className="cardMeta">{t("core.ui.vision.local_build_modal.field.model")}</div>
            <div>{modelName}</div>
          </div>
        </div>

        <div className="pipelinesStatsRow">
          <div className="pipelinesStatsItem">
            <div className="cardMeta">{t("core.ui.vision.local_build_modal.field.runtime")}</div>
            <div>{runtimeLabel || "docker / podman"}</div>
          </div>
          <div className="pipelinesStatsItem">
            <div className="cardMeta">{t("core.ui.vision.local_build_modal.field.source")}</div>
            <div className="processingServerConsentSource">
              {sourceLabel || t("core.ui.vision.local_build_modal.field.source_unknown")}
            </div>
          </div>
        </div>

        <div className="pipelinesHint">{t("core.ui.vision.local_build_modal.local_only")}</div>
        {extraHint ? <div className="pipelinesHint">{extraHint}</div> : null}

        {isHttpUrl(sourceLabel) ? (
          <div>
            <a className="pillButton" href={sourceLabel} target="_blank" rel="noreferrer">
              {t("core.ui.vision.local_build_modal.open_source")}
            </a>
          </div>
        ) : null}

        <label className="processingServerConsentCheck">
          <input
            type="checkbox"
            checked={checked}
            onChange={(event) => onToggleChecked(event.target.checked)}
            disabled={submitting}
          />
          <span>{t(`core.ui.vision.local_build_modal.acknowledge_${actionId}`)}</span>
        </label>
      </div>

      {error ? (
        <div className="card">
          <div className="cardBody errorText" style={{ marginTop: 0 }}>{error}</div>
        </div>
      ) : null}

      <div className="modalFooter">
        <button className="pillButton" type="button" onClick={onClose} disabled={submitting}>
          {t("core.actions.cancel")}
        </button>
        <button
          className="pillButton pillButtonPrimary"
          type="button"
          disabled={submitting || !checked}
          onClick={onConfirm}
        >
          {submitting
            ? t("core.ui.vision.local_build_modal.starting")
            : t(`core.ui.vision.local_build_modal.start_${actionId}`)}
        </button>
      </div>
    </Modal>
  );
}
