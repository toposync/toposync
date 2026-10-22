import React, { useState } from "react";
import { readVisualQualification } from "./api";
import type { VisualQualification } from "./visualQualification";

export function VisualStateQualification({ sensorId, context, disabled, run, t }: {
  sensorId: string; context: string; disabled: boolean;
  run: (name: string, work: (signal: AbortSignal) => Promise<void>) => Promise<void>;
  t: (key: string, values?: Record<string, unknown>) => string;
}) {
  const [result, setResult] = useState<{ data: VisualQualification; context: string; offset: number; checkedAt: number } | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const current = result?.context === context ? result : null;
  const refresh = (offset = 0) => void run("qualification", async signal => {
    setResult(null); setUnavailable(false);
    try {
      const data = await readVisualQualification(sensorId, offset, signal);
      signal.throwIfAborted();
      setResult({ data, context, offset, checkedAt: Date.now() });
    } catch (error) {
      if (!signal.aborted && [404, 405].includes((error as { status?: number }).status ?? 0)) setUnavailable(true);
      else throw error;
    }
  });
  return <details className="visualStateQualification">
    <summary>{t("qualification_title")}</summary>
    <p className="identityMuted">{t("qualification_help")}</p>
    <button type="button" className="chipButton" disabled={disabled || !sensorId || sensorId === "unconfigured"}
      onClick={() => refresh()}>{t("qualification_read")}</button>
    {unavailable && <p role="status">{t("qualification_host_unavailable")}</p>}
    {result && !current && <p role="status">{t("qualification_editor_changed")}</p>}
    {current && <section aria-label={t("qualification_results")}>
      <p role="status">{t("qualification_checked", { time: new Date(current.checkedAt).toLocaleString() })}</p>
      {current.data.operational_suspension_reasons === undefined
        ? <p>{t("qualification_operation_unverified")}</p>
        : current.data.operational_suspension_reasons.length > 0
          ? <p role="status">{t("qualification_operation_suspended")} {current.data.operational_suspension_reasons
            .map(reason => t(`qualification_${reason}`)).join(" ")}</p>
          : <p>{t("qualification_operation_no_blocker")}</p>}
      {!current.data.context_revision && <p>{t("qualification_context_unavailable")}</p>}
      {!current.data.items.length && <p>{t("qualification_empty")}</p>}
      <ul>{current.data.items.map(({ record, status }) => <li key={record.operation_id}>
        <strong>{t(`qualification_${record.scope}`)}</strong> — {t(`qualification_${status}`)}
        <p>{t(`qualification_decision_${record.decision}`)} · <time dateTime={new Date(record.created_at * 1000).toISOString()}>
          {new Date(record.created_at * 1000).toLocaleString()}</time></p>
        <details><summary>{t("qualification_record_details")}</summary><dl>
          <dt>{t("qualification_actor")}</dt><dd>{record.actor_id}</dd>
          <dt>{t("qualification_report")}</dt><dd><code>{record.report_sha256}</code></dd>
          <dt>{t("qualification_protocol")}</dt><dd><code>{record.protocol_sha256}</code></dd>
          <dt>{t("qualification_environment")}</dt><dd><code>{record.environment_sha256}</code></dd>
          <dt>{t("qualification_revision")}</dt><dd><code>{record.context_revision}</code></dd>
        </dl></details>
      </li>)}</ul>
      <div className="identityToolbar">
        {current.offset > 0 && <button type="button" className="chipButton" disabled={disabled}
          onClick={() => refresh(Math.max(0, current.offset - 20))}>{t("qualification_previous")}</button>}
        {current.data.next_offset !== null && <button type="button" className="chipButton" disabled={disabled}
          onClick={() => refresh(current.data.next_offset!)}>{t("qualification_next")}</button>}
      </div>
    </section>}
    <p className="identityMuted">{t("qualification_limits")}</p>
  </details>;
}
