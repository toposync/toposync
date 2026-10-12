import React, { useEffect, useState } from "react";
import { i18n } from "../../util/i18n";
import { NOTIFICATION_PERIODS, resolveNotificationPeriod, type NotificationPeriod } from "../../util/notificationPeriod";
import type { NotificationsFilter } from "../screens/MainScreen";

export function HistoryPeriodFilter({ filter, setFilter }: {
  filter: NotificationsFilter;
  setFilter: React.Dispatch<React.SetStateAction<NotificationsFilter>>;
}) {
  const t = (key: string) => i18n.t(`core.ui.notifications.period.${key}`);
  const [preset, setPreset] = useState<NotificationPeriod["preset"]>(filter.period?.preset ?? "all");
  const [start, setStart] = useState(filter.period?.start ?? "");
  const [end, setEnd] = useState(filter.period?.end ?? "");
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setPreset(filter.period?.preset ?? "all"); setStart(filter.period?.start ?? ""); setEnd(filter.period?.end ?? ""); setInvalid(false);
  }, [filter.period]);
  const apply = (period: NotificationPeriod) => {
    try {
      const timeRange = resolveNotificationPeriod(period);
      setFilter(previous => ({ ...previous, period: timeRange ? period : undefined, timeRange }));
      setInvalid(false);
    } catch { setInvalid(true); }
  };
  return <div className="notificationsFilterSection">
    <label htmlFor="notificationsPeriod">{t("title")}</label>
    <select id="notificationsPeriod" className="notificationsFilterSearch" value={preset} onChange={event => {
      const next = event.target.value as NotificationPeriod["preset"];
      setPreset(next); setInvalid(false);
      if (next !== "custom") apply({ preset: next });
    }}>
      {NOTIFICATION_PERIODS.map(value => <option key={value} value={value}>{t(value)}</option>)}
    </select>
    {preset === "custom" && <>
      <div className="notificationsPeriodDates">
        <label>{t("start")}<input className="notificationsFilterSearch" type="date" min="1970-01-01" value={start} aria-invalid={invalid} onChange={event => { setStart(event.target.value); setInvalid(false); }} /></label>
        <label>{t("end")}<input className="notificationsFilterSearch" type="date" min="1970-01-01" value={end} aria-invalid={invalid} onChange={event => { setEnd(event.target.value); setInvalid(false); }} /></label>
      </div>
      <button type="button" className="chipButton" onClick={() => apply({ preset: "custom", start, end })}>{t("apply")}</button>
      {invalid && <p role="alert">{t("invalid")}</p>}
    </>}
    <p className="muted">{t("zone")} {Intl.DateTimeFormat().resolvedOptions().timeZone}</p>
    {filter.timeRange && <p className="muted">{t("precedence")}</p>}
  </div>;
}
