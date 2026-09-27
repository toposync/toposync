import React, { useEffect, useState } from "react";
import { getNotificationDataOptions, isAbortError, type NotificationDataOption } from "../../util/api";
import { i18n } from "../../util/i18n";
import type { NotificationsFilter } from "../screens/MainScreen";

export function HistoryLocationFilter({ filter, setFilter }: {
  filter: NotificationsFilter;
  setFilter: React.Dispatch<React.SetStateAction<NotificationsFilter>>;
}) {
  const [options, setOptions] = useState<NotificationDataOption[][] | null>(null);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const t = (key: string) => i18n.t(`core.ui.notifications.location.${key}`);
  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    void Promise.all([
      getNotificationDataOptions("camera_id", "camera_name", controller.signal),
      getNotificationDataOptions("area_id", "area_label", controller.signal),
    ]).then(values => { if (!controller.signal.aborted) setOptions(values); })
      .catch(error => { if (!isAbortError(error) && !controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [revision]);
  const selected = Boolean(filter.dataFilter?.camera_id || filter.dataFilter?.area_id);
  return <details className="notificationsFilterSection" open={selected || undefined}>
    <summary>{t(selected ? "selected" : "title")}</summary>
    {failed ? <div role="alert">{t("failed")} <button type="button" className="chipButton" onClick={() => setRevision(value => value + 1)}>{t("retry")}</button></div>
      : !options ? <p role="status">{t("loading")}</p>
      : ["camera_id", "area_id"].map((key, index) => {
        const value = filter.dataFilter?.[key] ?? "";
        const choices = options[index];
        return <label key={key} className="notificationsFilterSection">
          <span>{t(index === 0 ? "camera" : "area")}</span>
          <select className="notificationsFilterSearch" value={value} onChange={event => {
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
        </label>;
      })}
  </details>;
}
