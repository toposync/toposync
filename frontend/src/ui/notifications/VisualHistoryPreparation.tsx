import React, { useEffect, useRef, useState } from "react";
import { cancelProcessingServerVisionModel, getVisualHistorySetup, installProcessingServerVisionModel, isAbortError, type VisualHistorySetup } from "../../util/api";
import { i18n } from "../../util/i18n";

const activeStatuses = new Set(["queued", "downloading", "verifying", "installing", "canceling"]);

export function VisualHistoryPreparation({ onReady }: { onReady: () => void }) {
  const [setup, setSetup] = useState<VisualHistorySetup | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const readyCallback = useRef(onReady);
  readyCallback.current = onReady;
  const awaitingReady = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const t = (key: string) => i18n.t(`core.ui.notifications.prepare.${key}`);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function check() {
      try {
        const value = await getVisualHistorySetup(controller.signal);
        if (controller.signal.aborted) return;
        setSetup(value);
        setError(false);
        const active = activeStatuses.has(value.job?.status ?? "");
        if (active) {
          awaitingReady.current = true;
          timer = setTimeout(check, 1000);
        } else if (awaitingReady.current) {
          awaitingReady.current = false;
          if (value.ready) readyCallback.current();
        }
      } catch (cause) {
        if (!isAbortError(cause) && !controller.signal.aborted) setError(true);
      }
    }
    void check();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [revision]);

  async function act(cancel: boolean) {
    if (!setup?.model_id || !setup.server_id || busy) return;
    setBusy(true);
    setError(false);
    try {
      if (cancel) await cancelProcessingServerVisionModel(setup.server_id, setup.model_id);
      else {
        await installProcessingServerVisionModel(setup.server_id, setup.model_id, { force: setup.reason === "artifact_checksum_mismatch" });
        awaitingReady.current = true;
      }
      if (mounted.current) setRevision(value => value + 1);
    } catch {
      // A failed response does not prove the server rejected the job. Check before resubmitting.
      if (mounted.current) setError(true);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  if (error) return <div className="notificationsFilterSection">
    <p role="alert">{t("error")}</p>
    <button type="button" className="chipButton" disabled={busy} onClick={() => setRevision(value => value + 1)}>{t("check")}</button>
  </div>;
  if (!setup) return <p className="muted" role="status">{t("checking")}</p>;
  const active = activeStatuses.has(setup.job?.status ?? "");
  if (setup.ready && !active) return null;
  const progress = Math.max(0, Math.min(100, setup.job?.progress_pct ?? 0));
  return <div className="notificationsFilterSection">
    <p className="muted" role="status">{t(active ? "working" : !setup.install_supported ? "unavailable" : setup.reason === "artifact_checksum_mismatch" ? "repair" : setup.job?.status === "failed" ? "failed" : setup.job?.status === "canceled" ? "canceled" : "description")}</p>
    {active ? <>
      <progress max={100} value={progress} aria-label={t("working")} style={{ maxWidth: "100%" }} />
      <button type="button" className="chipButton" disabled={busy || setup.job?.status === "canceling"} onClick={() => void act(true)}>{t("cancel")}</button>
    </> : setup.install_supported ? <button type="button" className="chipButton" disabled={busy} onClick={() => void act(false)}>{t(busy ? "working" : "start")}</button> : null}
  </div>;
}
