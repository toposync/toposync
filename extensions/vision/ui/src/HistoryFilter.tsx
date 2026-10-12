import React, { useEffect, useState } from "react";
import type { HostI18n, Notification } from "@toposync/plugin-api";
import { read, type GalleryData } from "./api";

export function categoryHistoryEndpoint(value: string): string {
  if (!["person", "dog", "cat"].includes(value)) throw new Error("Invalid object category");
  return `/api/vision/history/notifications?category=${encodeURIComponent(value)}`;
}

export function detectedCategoryCounts(notification: Notification): Array<[string, number]> {
  const payload = notification.payload as { data?: { category_summary?: { categories?: Record<string, { member_count?: unknown }> } } } | undefined;
  const categories = payload?.data?.category_summary?.categories;
  return ["person", "dog", "cat"].flatMap(category => {
    const count = categories?.[category]?.member_count;
    return typeof count === "number" && Number.isInteger(count) && count > 0 ? [[category, count] as [string, number]] : [];
  });
}

export function DetectedCategorySummary({ notification, i18n }: { notification: Notification; i18n: HostI18n }) {
  const { t } = i18n.useI18n();
  return <p>{t("ext.vision.identity.historyCategory")}: {detectedCategoryCounts(notification)
    .map(([category, count]) => `${t(`ext.vision.identity.${category}`)}: ${count}`).join(" · ")}</p>;
}

export function CategoryHistoryFilter({ value, onChange, i18n }: {
  value: string | null; onChange: (value: string | null) => void; i18n: HostI18n;
}) {
  const { t } = i18n.useI18n();
  return <label className="notificationsFilterSection">
    <span>{t("ext.vision.identity.historyCategory")}</span>
    <select className="notificationsFilterSearch" value={value ?? ""} onChange={event => onChange(event.target.value || null)}>
      <option value="">{t("ext.vision.identity.all")}</option>
      {["person", "dog", "cat"].map(category => <option key={category} value={category}>{t(`ext.vision.identity.${category}`)}</option>)}
    </select>
  </label>;
}

export function historyEndpoint(value: string): string {
  const [association, identity, extra] = value.split(":");
  if (extra || !identity || !/^[a-zA-Z0-9_-]{1,128}$/.test(identity) || !["human", "recognized"].includes(association)) throw new Error("Invalid identity selection");
  return `/api/vision/identities/${encodeURIComponent(identity)}/notifications?association=${association}`;
}

export function HistorySelectionSummary({ value, i18n }: { value: string; i18n: HostI18n }) {
  const { t } = i18n.useI18n();
  const [association, identity] = value.split(":");
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    let controller: AbortController | null = null;
    const refresh = () => {
      controller?.abort();
      const current = new AbortController(); controller = current;
      setName(null);
      void read<GalleryData>("", current.signal).then(data => {
        if (!current.signal.aborted) setName(data.identities.find(item => item.id === identity)?.name ?? null);
      }).catch(() => { if (!current.signal.aborted) setName(null); });
    };
    refresh();
    window.addEventListener("toposync:identity-gallery-changed", refresh);
    window.addEventListener("focus", refresh);
    return () => { controller?.abort(); window.removeEventListener("toposync:identity-gallery-changed", refresh); window.removeEventListener("focus", refresh); };
  }, [identity]);
  return <>{t("ext.vision.identity.historyPeople")}: {name ?? t("ext.vision.identity.historyMissing")} · {t(`ext.vision.identity.${association === "recognized" ? "historyRecognized" : "historyHuman"}`)}</>;
}

export function HistoryFilter({ value, onChange, i18n }: {
  value: string | null; onChange: (value: string | null) => void; i18n: HostI18n;
}) {
  const { t: translate } = i18n.useI18n();
  const t = (key: string) => translate(`ext.vision.identity.${key}`);
  const [data, setData] = useState<GalleryData>();
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const [association, identity] = (value ?? "human:").split(":");
  useEffect(() => {
    const refresh = () => setRevision(previous => previous + 1);
    window.addEventListener("toposync:identity-gallery-changed", refresh);
    return () => window.removeEventListener("toposync:identity-gallery-changed", refresh);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setData(undefined); setFailed(false);
    void read<GalleryData>("", controller.signal).then(result => {
      if (!controller.signal.aborted) setData(result);
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [revision]);
  return <details className="notificationsFilterSection" open={Boolean(value) || undefined}>
    <summary>{t("historyPeople")}</summary>
    {failed ? <div role="alert">{t("historyUnavailable")} <button type="button" className="chipButton" onClick={() => setRevision(previous => previous + 1)}>{t("retry")}</button></div>
      : !data ? <p role="status">{t("loading")}</p>
      : <label className="notificationsFilterSection">{t("historyIdentity")}
        <select className="notificationsFilterSearch" value={identity ?? ""} onChange={event => onChange(event.target.value ? `human:${event.target.value}` : null)}>
          <option value="">{t("all")}</option>
          {identity && !data.identities.some(item => item.id === identity) ? <option value={identity}>{t("historyMissing")}</option> : null}
          {data.identities.map(item => <option key={item.id} value={item.id}>{item.name} · {t(item.species)}</option>)}
        </select>
      </label>}
    {value ? <>
      <label className="notificationsFilterSection"><span>{t("historyAssociation")}</span>
        <select className="notificationsFilterSearch" value={association} onChange={event => onChange(`${event.target.value}:${identity}`)}>
          <option value="human">{t("historyHuman")}</option><option value="recognized">{t("historyRecognized")}</option>
        </select>
      </label>
      <p className="muted">{t("historyAssociationHelp")}</p>
      <button type="button" className="chipButton" onClick={() => onChange(null)}>{t("historyClear")}</button>
    </> : null}
  </details>;
}
