import React, { useEffect, useState } from "react";
import { getNotificationDataOptions, isAbortError, type NotificationDataOption } from "../../util/api";
import { i18n } from "../../util/i18n";
import type { NotificationsFilter } from "../screens/MainScreen";

export function useHistoryLocationOptions(enabled: boolean) {
  const [options, setOptions] = useState<Array<NotificationDataOption[] | null>>([null, null]);
  const [failed, setFailed] = useState([false, false]);
  const [revision, setRevision] = useState(0);
  const refresh = () => setRevision(value => value + 1);
  useEffect(() => {
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setOptions([null, null]); setFailed([false, false]);
    [["camera_id", "camera_name"], ["area_id", "area_label"]].forEach(([field, label], index) => {
      void getNotificationDataOptions(field, label, controller.signal).then(values => {
        if (!controller.signal.aborted) setOptions(previous => previous.map((value, position) => position === index ? values : value));
      }).catch(error => {
        if (!isAbortError(error) && !controller.signal.aborted) setFailed(previous => previous.map((value, position) => position === index || value));
      });
    });
    return () => controller.abort();
  }, [enabled, revision]);
  return { options, failed, refresh };
}

export function HistoryLocationFilter({ filter, setFilter, locations }: {
  filter: NotificationsFilter;
  setFilter: React.Dispatch<React.SetStateAction<NotificationsFilter>>;
  locations: ReturnType<typeof useHistoryLocationOptions>;
}) {
  const { options, failed, refresh } = locations;
  const t = (key: string) => i18n.t(`core.ui.notifications.location.${key}`);
  const selected = Boolean(filter.dataFilter?.camera_id || filter.dataFilter?.area_id);
  return <details className="notificationsFilterSection" open={selected || undefined}>
    <summary>{t(selected ? "selected" : "title")}</summary>
    {["camera_id", "area_id"].map((key, index) => {
        const value = filter.dataFilter?.[key] ?? "";
        const choices = options[index] ?? [];
        return <label key={key} className="notificationsFilterSection">
          <span>{t(index === 0 ? "camera" : "area")}</span>
          <select className="notificationsFilterSearch" value={value} disabled={!options[index]} onChange={event => {
            const next = event.target.value;
            setFilter(previous => {
              const dataFilter = { ...previous.dataFilter };
              if (next) dataFilter[key] = next; else delete dataFilter[key];
              return { ...previous, dataFilter };
            });
          }}>
            <option value="">{t("all")}</option>
            {value && !choices.some(choice => choice.value === value) ? <option value={value}>{t("missing")}</option> : null}
            {choices.map((choice, position) => <option key={choice.value} value={choice.value}>
              {choice.label && choice.label !== choice.value ? choice.label : `${t("unnamed")} ${position + 1}`}
            </option>)}
          </select>
          {failed[index] ? <span role="alert">{t("failed")} <button type="button" className="chipButton" onClick={refresh}>{t("retry")}</button></span>
            : !options[index] ? <span role="status">{t("loading")}</span>
              : !choices.length ? <span className="muted">{t(index === 0 ? "no_cameras" : "no_areas")}</span> : null}
        </label>;
      })}
  </details>;
}
