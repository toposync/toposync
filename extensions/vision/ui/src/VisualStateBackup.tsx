import React, { useEffect, useState } from "react";
import { exportVisualBackup, restoreVisualBackup, visualBackupLimit, visualReferenceUrl } from "./api";
import type { VisualConfiguration, VisualDraft } from "./visualState";

type Transfer = { file: File; operationId: string; context: string };
function configurationKey(value: VisualConfiguration): string {
  return JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([left], [right]) => left.localeCompare(right))) : item);
}
type Props = {
  value: VisualConfiguration; draft: VisualDraft | null; context: string; disabled: boolean; working: boolean; canExport: boolean;
  run: (name: string, work: (signal: AbortSignal) => Promise<void>) => Promise<void>;
  synchronize: (configuration: VisualConfiguration, signal: AbortSignal) => Promise<VisualDraft>;
  apply: (configuration: VisualConfiguration, draft: VisualDraft | null) => void;
  t: (key: string, values?: Record<string, unknown>) => string;
};

export function VisualStateBackup({ value, draft, context, disabled, working, canExport, run, synchronize, apply, t }: Props) {
  const [pending, setPending] = useState<Transfer | null>(null);
  const [proposal, setProposal] = useState<{ draft: VisualDraft; context: string } | null>(null);
  const [history, setHistory] = useState<{ value: VisualConfiguration; draft: VisualDraft | null; after: string; context: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState(false);
  const serialized = configurationKey(value);
  useEffect(() => { setPending(null); setProposal(null); setProblem(null); setDownloaded(false); }, [context, serialized]);
  useEffect(() => {
    if (history && (history.after !== serialized || history.context !== context)) setHistory(null);
  }, [context, serialized, history]);

  function restore(transfer: Transfer) {
    setProblem(null); setDownloaded(false); setPending(transfer); setProposal(null);
    void run("backup_restore", async signal => {
      try {
        const result = await restoreVisualBackup(transfer.file, transfer.operationId, signal);
        signal.throwIfAborted();
        setProposal({ draft: result, context: transfer.context }); setPending(null);
      } catch (cause) {
        const status = (cause as { status?: number }).status;
        if (!signal.aborted && status && status >= 400 && status < 500 && ![408, 429].includes(status)) {
          setPending(null); setProblem(status === 401 || status === 403 ? "forbidden" : "visual_backup_invalid");
        } else if (signal.aborted) throw cause;
      }
    });
  }

  const candidate = proposal?.context === `${context}\n${serialized}` ? proposal.draft : null;
  return <details className="visualStateBackup">
    <summary>{t("backup_title")}</summary>
    <p className="identityMuted">{t("backup_help")}</p>
    {problem && <p role="alert">{t(problem)}</p>}
    <fieldset disabled={disabled}>
      <legend>{t("backup_file")}</legend>
      <button type="button" className="chipButton" disabled={!canExport} onClick={() => void run("backup_export", async signal => {
        setProblem(null); setDownloaded(false);
        const current = await synchronize(value, signal);
        const file = await exportVisualBackup(current, signal); signal.throwIfAborted();
        const url = URL.createObjectURL(file);
        try {
          const link = document.createElement("a"); link.href = url; link.download = "visual-sensor-backup.zip";
          document.body.append(link); link.click(); link.remove(); setDownloaded(true);
        } finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
      })}>{t("backup_download")}</button>
      <label className="identityField"><span>{t("backup_choose")}</span><input type="file" accept=".zip,application/zip" style={{ maxWidth: "100%" }}
        onChange={event => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (!file) return;
          setProposal(null); setPending(null); setProblem(null);
          if (file.size === 0 || file.size > visualBackupLimit) { setProblem("backup_size"); return; }
          restore({ file, operationId: crypto.randomUUID(), context: `${context}\n${serialized}` });
        }} /></label>
      {downloaded && <p role="status">{t("backup_downloaded")}</p>}
      {pending && !working && <div role="status"><p>{t("visual_backup_uncertain")}</p>
        <button type="button" className="chipButton" onClick={() => restore(pending)}>{t("backup_retry")}</button>
        <button type="button" className="chipButton" onClick={() => { setPending(null); setProblem(null); }}>{t("backup_discard")}</button>
      </div>}
      {candidate && <section aria-label={t("backup_review")}>
        <h4>{t("backup_review")}</h4>
        <p>{t("backup_review_help")}</p>
        {candidate.configuration.sensor_id !== value.sensor_id && <p role="status">{t("backup_other_sensor")}</p>}
        <p>{t("backup_model", { model: candidate.configuration.model_id ?? t("backup_no_model") })}</p>
        <ul>{candidate.configuration.classes.map(category => <li key={category.class_id}>
          {t("backup_class", { name: category.label, count: category.reference_ids.length })}
          {category.reference_ids[0] && <img src={visualReferenceUrl(candidate.draft_id, category.reference_ids[0])}
            alt={t("backup_photo", { name: category.label })} width={80} style={{ display: "block", maxWidth: "100%" }} />}
        </li>)}</ul>
        <button type="button" className="chipButton" onClick={() => {
          if (candidate.expires_at * 1000 <= Date.now()) { setProblem("backup_expired"); setProposal(null); return; }
          setHistory({ value: structuredClone(value), draft, after: configurationKey(candidate.configuration), context });
          setProposal(null); setProblem(null); apply(candidate.configuration, candidate);
        }}>{t("backup_apply")}</button>
        <button type="button" className="chipButton" onClick={() => setProposal(null)}>{t("backup_discard")}</button>
      </section>}
      {history && history.after === serialized && history.context === context && <div role="status">
        <p>{t("backup_applied")}</p>
        <button type="button" className="chipButton" onClick={() => { apply(history.value, history.draft); setHistory(null); }}>{t("backup_undo")}</button>
      </div>}
    </fieldset>
  </details>;
}
