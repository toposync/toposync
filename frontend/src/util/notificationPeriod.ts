export const NOTIFICATION_PERIODS = ["all", "today", "yesterday", "this_week", "last_week", "this_month", "last_month", "custom"] as const;
export type NotificationPeriod = { preset: typeof NOTIFICATION_PERIODS[number]; start?: string; end?: string };
export type NotificationTimeRange = { createdAfter: number; createdBefore: number; timezone: string };

function civilDate(value: string | undefined): Date {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("invalid_period");
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  if (year < 1970 || date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) throw new Error("invalid_period");
  return date;
}

export function resolveNotificationPeriod(period?: NotificationPeriod, now = new Date()): NotificationTimeRange | undefined {
  if (!period || period.preset === "all") return undefined;
  if (!NOTIFICATION_PERIODS.includes(period.preset)) throw new Error("invalid_period");
  let start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let end = new Date(start);
  switch (period.preset) {
    case "today": end.setDate(end.getDate() + 1); break;
    case "yesterday": start.setDate(start.getDate() - 1); break;
    case "this_week":
    case "last_week":
      start.setDate(start.getDate() - (start.getDay() + 6) % 7 - (period.preset === "last_week" ? 7 : 0));
      end = new Date(start); end.setDate(end.getDate() + 7); break;
    case "this_month":
    case "last_month":
      start = new Date(now.getFullYear(), now.getMonth() - (period.preset === "last_month" ? 1 : 0), 1);
      end = new Date(start.getFullYear(), start.getMonth() + 1, 1); break;
    case "custom":
      start = civilDate(period.start); end = civilDate(period.end);
      if (start > end) throw new Error("invalid_period");
      end.setDate(end.getDate() + 1); break;
  }
  if (!Number.isFinite(start.getTime()) || start.getTime() < 0 || end <= start) throw new Error("invalid_period");
  return { createdAfter: start.getTime() / 1000, createdBefore: end.getTime() / 1000,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone };
}
