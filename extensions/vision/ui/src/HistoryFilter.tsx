import React, { useEffect, useState } from "react";
import type { HostI18n } from "@toposync/plugin-api";
import { read, type GalleryData } from "./api";

export function historyEndpoint(value: string): string {
  const [association, identity, extra] = value.split(":");
  if (extra || !identity || !/^[a-zA-Z0-9_-]{1,128}$/.test(identity) || !["human", "recognized"].includes(association)) throw new Error("Invalid identity selection");
  return `/api/vision/identities/${encodeURIComponent(identity)}/notifications?association=${association}`;
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
