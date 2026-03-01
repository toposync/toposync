import React, { useEffect, useMemo, useRef, useState } from "react";

import type { Pipeline } from "../../../util/api";
import { i18n } from "../../../util/i18n";
import { Modal } from "../../Modal";

type Props = {
  open: boolean;
  pipeline: Pipeline | null;
  existingNames: string[];
  onClose: () => void;
  onDuplicate: (newName: string) => Promise<void>;
};

function suggestDuplicateName(baseName: string, existing: Set<string>): string {
  const base = String(baseName || "").trim() || "pipeline";
  let index = 2;
  while (existing.has(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
}

export function PipelineDuplicateModal({ open, pipeline, existingNames, onClose, onDuplicate }: Props): React.ReactElement | null {
  const { t } = i18n.useI18n();
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const initializedForName = useRef<string | null>(null);

  useEffect(() => {
    if (!open) {
      initializedForName.current = null;
      return;
    }
    if (initializedForName.current === (pipeline?.name ?? null)) return;
    initializedForName.current = pipeline?.name ?? null;
    setBusy(false);
    setError(null);
    if (!pipeline) {
      setNewName("");
      return;
    }
    const existing = new Set((existingNames ?? []).map((name) => String(name || "").trim()).filter(Boolean));
    setNewName(suggestDuplicateName(pipeline.name, existing));
  }, [open, pipeline, existingNames]);

  const canDuplicate = useMemo(() => {
    if (!pipeline) return false;
    const trimmed = newName.trim();
    if (!trimmed) return false;
    if (trimmed === pipeline.name) return false;
    return true;
  }, [newName, pipeline]);

  const doDuplicate = async () => {
    if (!pipeline || busy || !canDuplicate) return;
    const trimmed = newName.trim();
    setBusy(true);
    setError(null);
    try {
      await onDuplicate(trimmed);
      onClose();
    } catch (err: any) {
      setError(String(err?.message ?? err));
    } finally {
      setBusy(false);
    }
  };

  const title = useMemo(() => {
    if (!pipeline) return t("core.ui.pipelines.duplicate.title");
    return t("core.ui.pipelines.duplicate.title_with_name", { name: pipeline.name });
  }, [pipeline, t]);

  if (!open) return null;

  return (
    <Modal open={open} title={title} onClose={onClose}>
      {error ? (
        <div className="card cardDanger">
          <div className="cardBody">{error}</div>
        </div>
      ) : null}

      {!pipeline ? <div className="pipelinesHint">{t("core.ui.pipelines.duplicate.select_first")}</div> : null}

      <div className="pipelinesOperatorConfigCard">
        <label className="pipelinesLabel">
          <span>{t("core.ui.pipelines.duplicate.new_name")}</span>
          <input
            className="pipelinesInput"
            value={newName}
            placeholder={t("core.ui.pipelines.duplicate.placeholder")}
            onChange={(event) => setNewName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void doDuplicate();
            }}
            disabled={busy || !pipeline}
            autoFocus
          />
        </label>
        <div className="pipelinesStepHint">{t("core.ui.pipelines.duplicate.hint")}</div>

        <button className="pillButton pillButtonPrimary" type="button" disabled={!canDuplicate || busy} onClick={() => void doDuplicate()}>
          <i className="fa-solid fa-copy" aria-hidden="true" />
          {busy ? t("core.ui.pipelines.duplicate.duplicating") : t("core.ui.pipelines.duplicate.duplicate")}
        </button>
      </div>
    </Modal>
  );
}
