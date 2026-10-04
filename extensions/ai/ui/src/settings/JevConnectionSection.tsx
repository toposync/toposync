import React, { useEffect, useId, useRef, useState } from "react";
import type { ToposyncHost } from "@toposync/plugin-api";
import { fetchJevConnection, testJevConnection } from "../api/aiApi";
import type { JevConnectionInfo, JevConnectionTest, OllayaConnectionSettings } from "../types";

const DEFAULT_CONNECTION: OllayaConnectionSettings = {
  enabled: false, url: "http://127.0.0.1:11435", model: "laya:multilingual",
};

export function JevConnectionSection({ t, connection, onChange }: {
  t: ToposyncHost["i18n"]["t"];
  connection: OllayaConnectionSettings | null | undefined;
  onChange: (value: OllayaConnectionSettings) => void;
}): React.ReactElement {
  const fieldId = useId();
  const [info, setInfo] = useState<JevConnectionInfo | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ signature: string; value: JevConnectionTest | null } | null>(null);
  const request = useRef<AbortController | null>(null);
  const current = connection ?? info?.connection ?? DEFAULT_CONNECTION;
  const signature = JSON.stringify(current);

  useEffect(() => {
    const controller = new AbortController();
    void fetchJevConnection(controller.signal).then(setInfo).catch(() => {
      if (!controller.signal.aborted) setLoadFailed(true);
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    setResult(null);
    setBusy(false);
    return () => request.current?.abort();
  }, [signature]);

  async function verify(): Promise<void> {
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setBusy(true);
    setResult(null);
    try {
      const value = await testJevConnection(current, controller.signal);
      if (!controller.signal.aborted) setResult({ signature, value });
    } catch {
      if (!controller.signal.aborted) setResult({ signature, value: null });
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const visibleResult = result?.signature === signature ? result : null;
  const reason = visibleResult?.value?.reason;
  const failureKey = reason?.endsWith("_limit") ? "limit"
    : reason === "service_rejected_request" ? "rejected"
    : reason === "service_unavailable" ? "unavailable"
    : reason && ["invalid_response", "unexpected_model", "response_exceeds_budget"].includes(reason) ? "incompatible" : "invalid";
  const loading = !info && !loadFailed;

  return (
    <section className="card" aria-labelledby={`${fieldId}-title`}>
      <div className="cardBody" style={{ display: "grid", gap: 24 }}>
        <div style={{ display: "grid", gap: 8 }}>
          <h3 id={`${fieldId}-title`} className="modalSectionTitle" style={{ margin: 0 }}>{t("ext.ai.jev.title")}</h3>
          <p style={{ margin: 0 }}>{t("ext.ai.jev.scope")}</p>
          <p className="cardMeta" style={{ margin: 0 }}>{t("ext.ai.jev.validation")}</p>
        </div>

        {loadFailed ? <p role="alert" style={{ margin: 0 }}>{t("ext.ai.jev.load_failed")}</p> : null}
        {info?.source === "environment" && !connection ? (
          <p className="cardMeta" style={{ margin: 0 }}>
            {t(info.reason ? "ext.ai.jev.environment_invalid" : "ext.ai.jev.environment")}
            {info.active_backend === "typesafe" ? ` ${t("ext.ai.jev.typesafe_active")}` : ""}
          </p>
        ) : null}

        <div style={{ display: "grid", gap: 8 }}>
          <label className="row" style={{ gap: 8, minHeight: 44 }}>
            <input type="checkbox" checked={current.enabled} disabled={loading || (loadFailed && !connection)}
              onChange={(event) => onChange({ ...current, enabled: event.target.checked })} />
            <span>{t("ext.ai.jev.enable")}</span>
          </label>
          <div className="cardMeta" style={{ overflowWrap: "anywhere" }}>{current.model} · {current.url}</div>
        </div>

        <details>
          <summary style={{ cursor: "pointer", minHeight: 44 }}>{t("ext.ai.jev.advanced")}</summary>
          <div style={{ display: "grid", gap: 16, marginTop: 12 }}>
            <label className="field">
              <span>{t("ext.ai.jev.address")}</span>
              <input className="input" style={{ minWidth: 0, width: "100%" }} type="url" value={current.url} autoComplete="off" spellCheck={false}
                onChange={(event) => onChange({ ...current, url: event.target.value })} />
              <span className="cardMeta">{t("ext.ai.jev.address_help")}</span>
            </label>
            <label className="field">
              <span>{t("ext.ai.jev.model")}</span>
              <input className="input" style={{ minWidth: 0, width: "100%" }} value={current.model} autoComplete="off" spellCheck={false}
                onChange={(event) => onChange({ ...current, model: event.target.value })} />
              <span className="cardMeta">{t("ext.ai.jev.model_help")}</span>
            </label>
          </div>
        </details>

        <div style={{ display: "grid", gap: 12, justifyItems: "start" }}>
          <p className="cardMeta" style={{ margin: 0 }}>{t("ext.ai.jev.configuration")}</p>
          <button className="chipButton" style={{ minHeight: 44 }} type="button" disabled={!current.enabled || busy || loading}
            onClick={() => void verify()}>{t(busy ? "ext.ai.jev.checking" : "ext.ai.jev.check")}</button>
          <div role="status" aria-live="polite" style={{ width: "100%" }}>
            {busy ? t("ext.ai.jev.checking") : visibleResult ? (
              <div style={{ display: "grid", gap: 4 }}>
                <strong>{t(visibleResult.value?.ok ? "ext.ai.jev.connected" : `ext.ai.jev.failure.${failureKey}`)}</strong>
                {visibleResult.value?.checked_at ? (
                  <span className="cardMeta">{t("ext.ai.jev.checked_at", { time: new Date(visibleResult.value.checked_at).toLocaleString() })}</span>
                ) : null}
              </div>
            ) : t(current.enabled ? "ext.ai.jev.not_checked" : "ext.ai.jev.disabled")}
          </div>
        </div>
      </div>
    </section>
  );
}
