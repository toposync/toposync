import React from "react";
import type { HostI18n } from "@toposync/plugin-api";
import type { AttentionSession } from "../types";

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

export function ObservedFramingNotice({ observation, statusAge, t }: {
  observation: AttentionSession["observed_framing"];
  statusAge: number;
  t: ReturnType<HostI18n["useI18n"]>["t"];
}): React.ReactElement {
  const age = observation?.publication_age_seconds;
  const recent = typeof age === "number" && Number.isFinite(age) && age >= 0
    && Number.isFinite(statusAge) && statusAge >= 0 && age + statusAge <= 1;
  const observed = recent && observation?.observed_after_arrival === true
    && observation.publication_recent === true && observation.on_selected_source === true
    && observation.box_with_margin === true;
  return <div className="ptzAttentionNotice" data-tone="info">
    {t(observed ? "ext.ptz_attention.framing.observed_after_arrival" : "ext.ptz_attention.framing.presence_unconfirmed")}
  </div>;
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
