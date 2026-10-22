import React from "react";
import type { HostI18n } from "@toposync/plugin-api";

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

export function ArrivalFramingNotice({ action, details, t }: {
  action: string;
  details: Record<string, unknown>;
  t: ReturnType<HostI18n["useI18n"]>["t"];
}): React.ReactElement | null {
  if (action !== "focus_acquired") return null;
  const projection = record(details.group_framing_projection ?? details.framing_projection);
  const fit = record(projection?.fit)?.status;
  let result = "unavailable";
  if (fit === "inside_frame" && projection?.status === "projected") result = "inside";
  if (["projected_outside", "member_outside_or_unqualified"].includes(String(projection?.status))) {
    if (fit === "offset_within_current_extent") result = "offset";
    if (fit === "extent_exceeds_frame") result = "extent";
  }
  return <div className="ptzAttentionNotice" data-tone={result === "inside" ? "info" : "warning"}>
    <div>{t(`ext.ptz_attention.framing.${result}`)}</div>
    <div className="ptzAttentionSubtle">{t("ext.ptz_attention.framing.presence_unconfirmed")}</div>
  </div>;
}
