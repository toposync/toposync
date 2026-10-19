import React, { useEffect, useRef, useState } from "react";
import type { Notification } from "@toposync/plugin-api";
import { i18n } from "../../util/i18n";
import { isAbortError } from "../../util/api";
import { activityError, activityIdentifier, activityUrl, changeActivity, getActivity, participantEvidence, type ActivityDetail } from "../../util/activity";

type View = { key: string; data: ActivityDetail | null; error: string | null };
const pageSize = 30;

export function ActivitySummary({ notification }: { notification: Notification }) {
  const { t } = i18n.useI18n();
  const payload = notification.payload as { data?: { occurrence?: ActivityDetail["occurrence"] } } | undefined;
  const document = payload?.data?.occurrence;
  const count = document?.participants && typeof document.participants === "object" ? Object.keys(document.participants).length : 0;
  return <span className="notificationCardDesc">{t("activity.card", { count, status: t(`activity.${document?.status || "OPEN"}`) })}</span>;
}

export function ActivityView({ notification }: { notification: Notification }) {
  const { t, locale } = i18n.useI18n();
  const identifier = activityIdentifier(notification);
  const revision = (notification.payload as { occurrence_revision?: number } | undefined)?.occurrence_revision;
  const key = `${identifier}:${notification.updatedAt}:${revision}`;
  const [view, setView] = useState<View>({ key, data: null, error: null });
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState("");
  const [page, setPage] = useState(0);
  const [confirmation, setConfirmation] = useState<"delete" | "revoke" | null>(null);
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const data = view.key === key ? view.data : null;
  const error = view.key === key ? view.error : null;
  const reload = () => { setView({ key, data: null, error: null }); setRefresh(value => value + 1); };

  useEffect(() => {
    const controller = new AbortController();
    let expiry: ReturnType<typeof setTimeout> | undefined;
    setView({ key, data: null, error: null });
    setConfirmation(null); setMutationError(null);
    const invalidate = () => {
      controller.abort();
      setView({ key, data: null, error: null });
      if (document.visibilityState !== "hidden") setRefresh(value => value + 1);
    };
    if (identifier && document.visibilityState !== "hidden") {
      void getActivity(identifier, controller.signal).then(value => {
        if (controller.signal.aborted) return;
        const eventExpiry = value.occurrence.expires_at * 1000;
        const nextExpiry = Math.min(eventExpiry, ...value.occurrence.artifacts.map(item => item.expires_at * 1000));
        const remaining = nextExpiry - Date.now();
        if (remaining <= 0) { setView({ key, data: null, error: eventExpiry <= Date.now() ? "expired" : "changed" }); return; }
        setView({ key, data: value, error: null });
        expiry = setTimeout(() => {
          const ended = eventExpiry <= Date.now();
          setView({ key, data: null, error: ended ? "expired" : null });
          if (!ended) setRefresh(value => value + 1);
        }, Math.min(remaining, 2147483647));
      }).catch(error => {
        if (!controller.signal.aborted && !isAbortError(error)) setView({ key, data: null, error: activityError(error) });
      });
    } else if (!identifier) setView({ key, data: null, error: "unavailable" });
    window.addEventListener("focus", invalidate);
    window.addEventListener("toposync:identity-gallery-changed", invalidate);
    document.addEventListener("visibilitychange", invalidate);
    return () => {
      controller.abort(); if (expiry !== undefined) clearTimeout(expiry);
      window.removeEventListener("focus", invalidate);
      window.removeEventListener("toposync:identity-gallery-changed", invalidate);
      document.removeEventListener("visibilitychange", invalidate);
    };
  }, [key, identifier, refresh]);
  useEffect(() => () => mutation.current?.abort(), [identifier]);
  useEffect(() => { if (confirmation) confirmButton.current?.focus(); }, [confirmation]);
  useEffect(() => { setPage(0); }, [selected, identifier]);

  const apply = async () => {
    if (!data || !identifier || !confirmation || busy) return;
    const controller = new AbortController();
    mutation.current = controller; setBusy(true); setMutationError(null);
    try {
      await changeActivity(identifier, data.occurrence.revision, confirmation, controller.signal);
      if (controller.signal.aborted) return;
      setView({ key, data: null, error: null });
      window.dispatchEvent(new Event("toposync:activity-changed"));
      reload();
    } catch (error) {
      if (!controller.signal.aborted && !isAbortError(error)) {
        setView({ key, data: null, error: activityError(error) });
        setMutationError(activityError(error));
      }
    } finally { if (!controller.signal.aborted) { setBusy(false); setConfirmation(null); } }
  };
  const date = (value: number) => new Date(value * 1000).toLocaleString(locale);
  const number = (value: number) => value.toLocaleString(locale, { maximumFractionDigits: 3 });
  if (error) return <section className="activityView" aria-label={t("activity.title")}><p role="alert">{t(`activity.${error}`)}</p><button className="chipButton" type="button" onClick={reload}>{t("activity.refresh")}</button></section>;
  if (!data) return <p role="status">{t("activity.loading")}</p>;
  const occurrence = data.occurrence;
  const participants = Object.values(occurrence.participants);
  const participant = participants.find(item => item.id === selected) || participants[0];
  const samples = participant ? participantEvidence(occurrence, participant.id) : [];
  const start = Math.min(page, Math.max(0, Math.ceil(samples.length / pageSize) - 1)) * pageSize;
  const name = (item: typeof participant, index: number) => item.identification.status === "recognized" && item.identification.name
    ? item.identification.name : t("activity.anonymous", { number: index + 1 });
  return <section className="activityView" aria-label={t("activity.title")}>
    <h3>{t("activity.title")} · {t(`activity.${occurrence.status}`)}</h3>
    {data.maintenance_error && <p role="alert">{t("activity.maintenance_error")}</p>}
    <p className="muted">{t("activity.coverage")}</p>
    <div className="notificationDetailsGrid">
      <div className="notificationDetailField">{occurrence.duration_seconds === null ? t("activity.incomparable") : t("activity.duration", { seconds: number(occurrence.duration_seconds) })}</div>
      <div className="notificationDetailField">{t("activity.expires", { date: date(occurrence.expires_at) })}</div>
      <div className="notificationDetailField">{t("activity.revision", { number: occurrence.revision })}</div>
    </div>
    {occurrence.coverage.gaps && <p>{t("activity.gap")}</p>}
    {!occurrence.enrichment_allowed && <p>{t("activity.revoked")}</p>}
    <h4>{t("activity.participants")}</h4>
    {!participants.length && <p>{t("activity.empty")}</p>}
    <div className="activityParticipants">{participants.map((item, index) => <button key={item.id} type="button" className="notificationDetailField" aria-pressed={participant?.id === item.id} onClick={() => setSelected(item.id)}>
      <strong>{name(item, index)}</strong><span>{t(`activity.${item.identification.status === "recognized" ? item.identification.provenance === "human" ? "human" : "automatic" : item.identification.status === "unavailable" ? "identity_unavailable" : item.identification.status}`)}</span>
    </button>)}</div>
    <h4>{t("activity.trajectory")}{participant ? ` · ${name(participant, participants.indexOf(participant))}` : ""}</h4>
    {!samples.length ? <p>{t("activity.no_evidence")}</p> : <>
      <ol className="activityObservations" start={start + 1}>{samples.slice(start, start + pageSize).map(([identifier, sample]) => <li key={identifier}>
        <strong>{t("activity.source", { camera: sample.camera_name || sample.camera_id, source: sample.source_name || sample.source_id })}</strong>
        <span>{t("activity.observed", { seconds: number(sample.observed_at) })} · {t("activity.clock", { clock: sample.clock_domain })}</span>
        {sample.gap && <span>{t("activity.gap")}</span>}
        <span>{sample.position ? t("activity.position", { x: number(sample.position[0]), z: number(sample.position[1]) }) : t("activity.no_position")}</span>
        {sample.position && <span>{sample.position_error_metres === null ? t("activity.unknown_uncertainty") : t("activity.uncertainty", { metres: number(sample.position_error_metres) })}</span>}
        {occurrence.artifacts.filter(item => item.evidence_id === identifier && item.image_href).map(item => <a key={item.id} href={item.image_href!} download>{t("activity.retained_image", { name: item.name })} · {t("activity.expires", { date: date(item.expires_at) })}</a>)}
        <details><summary>{t("activity.details")}</summary><small>{identifier}<br />{sample.camera_id} · {sample.source_id}<br />{t("activity.coordinates", { reference: sample.coordinate_reference || "—", revision: sample.mapping_revision || "—" })}</small></details>
      </li>)}</ol>
      <div className="activityActions"><button className="chipButton" type="button" disabled={!start} onClick={() => setPage(value => value - 1)}>{t("activity.previous")}</button><span role="status">{t("activity.page", { start: start + 1, end: Math.min(start + pageSize, samples.length), total: samples.length })}</span><button className="chipButton" type="button" disabled={start + pageSize >= samples.length} onClick={() => setPage(value => value + 1)}>{t("activity.next")}</button></div>
    </>}
    <details><summary>{t("activity.retention")}</summary>
      <ul>{(["event_text", "images", "private_identity", "diagnostics", "video"] as const).map(kind => <li key={kind}>{t(`activity.class_${kind}`)}: {t(`activity.retention_${occurrence.retention[kind].state}`)}</li>)}</ul>
      <p>{t("activity.diagnostic_retention", { sample: occurrence.retention.diagnostics.sample_maximum_age_seconds, session: occurrence.retention.diagnostics.session_lease_seconds })}</p>
      {occurrence.retention.private_identity.gallery_retention && <p>{occurrence.retention.private_identity.gallery_retention.enabled
        ? t("activity.gallery_retention", { transient: occurrence.retention.private_identity.gallery_retention.transient_days, references: occurrence.retention.private_identity.gallery_retention.reference_days, history: occurrence.retention.private_identity.gallery_retention.curation_history_days })
        : t("activity.gallery_retention_disabled")}</p>}
      <p>{t("activity.shared_retention")}</p>
      {occurrence.retention.private_identity.maintenance_error && <p role="alert">{t("activity.maintenance_error")}</p>}
    </details>
    <details><summary>{t("activity.context")}</summary>{occurrence.enrichments.filter(item => item.kind === "context" && item.participant_id === participant?.id).map((item, index) => <p key={index}><strong>{t(`activity.context_${item.status}`)}</strong><br />{item.text}<br /><small>{t("activity.observed", { seconds: number(item.observed_at) })} · {t("activity.recorded", { date: date(item.recorded_at) })}</small></p>)}{!occurrence.enrichments.some(item => item.kind === "context" && item.participant_id === participant?.id) && <p>{t("activity.no_context")}</p>}</details>
    <details><summary>{t("activity.revisions")}</summary><ol>{data.revisions.map(item => <li key={item.revision}>{t("activity.revision", { number: item.revision })} · {t(`activity.${item.kind}`)} · {t("activity.recorded", { date: date(item.recorded_at) })}</li>)}</ol>{occurrence.close_reason && <p>{t("activity.reason")}: {t(`activity.reason_${occurrence.close_reason}`, {}, t("activity.reason_other"))}</p>}</details>
    <details><summary>{t("activity.receipts")}</summary>{!data.deliveries.length && <p>{t("activity.no_receipts")}</p>}<ul>{data.deliveries.map(item => <li key={item.id}>{t(`activity.${item.status === "failed" ? "failed_receipt" : item.status === "unknown" ? "unknown_receipt" : item.status}`)} · {t("activity.simulated")}</li>)}</ul></details>
    <div className="activityActions"><button className="chipButton" type="button" disabled={busy} onClick={reload}>{t("activity.refresh")}</button><a className="chipButton" href={activityUrl(occurrence.id, "/manifest")} download>{t("activity.export")}</a>
      {data.permissions.manage ? (["revoke", "delete"] as const).map(action => <button key={action} className="chipButton" type="button" disabled={busy || action === "revoke" && !occurrence.enrichment_allowed} onClick={event => { trigger.current = event.currentTarget; setConfirmation(action); }}>{t(`activity.${action}`)}</button>) : <span>{t("activity.read_only")}</span>}
    </div>
    {mutationError && <p role="alert">{t(`activity.${mutationError}`)}</p>}
    {confirmation && <div className="notificationDetailField" role="alertdialog" aria-label={t(`activity.${confirmation}`)}><p>{t(`activity.confirm_${confirmation}`)}</p><div className="activityActions"><button ref={confirmButton} type="button" className="chipButton" disabled={busy} onClick={() => void apply()}>{t(busy ? "activity.saving" : "activity.confirm")}</button><button type="button" className="chipButton" disabled={busy} onClick={() => { setConfirmation(null); trigger.current?.focus(); }}>{t("activity.cancel")}</button></div></div>}
  </section>;
}
