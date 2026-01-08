import React from "react";

import type { HostI18n } from "@toposync/plugin-api";

import type { DetectionCondition } from "../types";
import { YOLO_V12_CATEGORIES, formatYoloCategoryLabel } from "../yolo";

export function describeDetectionCondition(
  condition: DetectionCondition,
  translate: (key: string, variables?: Record<string, unknown>) => string,
): string {
  if (condition.kind === "motion") return translate("ext.cameras.detections.cond.motion");
  if (condition.kind === "ha_sensor") {
    return condition.entity_id
      ? `${translate("ext.cameras.detections.cond.ha_sensor")}: ${condition.entity_id}`
      : translate("ext.cameras.detections.cond.ha_sensor");
  }
  if (condition.kind === "ha_state") {
    const base = condition.entity_id
      ? `${translate("ext.cameras.detections.cond.ha_state")}: ${condition.entity_id}`
      : translate("ext.cameras.detections.cond.ha_state");
    return condition.state ? `${base} = ${condition.state}` : base;
  }
  return `${translate("ext.cameras.detections.cond.object")}: ${formatYoloCategoryLabel(condition.category)}`;
}

export function DetectionConditionEditor({
  value,
  onChange,
  i18n,
}: {
  value: DetectionCondition;
  onChange: (next: DetectionCondition) => void;
  i18n: HostI18n;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  return (
    <div className="rowWrap" style={{ gap: 8, alignItems: "center" }}>
      <select
        className="input"
        value={value.kind}
        onChange={(event) => {
          const nextKind = event.target.value as DetectionCondition["kind"];
          if (nextKind === "motion") onChange({ kind: "motion" });
          else if (nextKind === "object") onChange({ kind: "object", category: "person" });
          else if (nextKind === "ha_sensor") onChange({ kind: "ha_sensor", entity_id: "" });
          else if (nextKind === "ha_state") onChange({ kind: "ha_state", entity_id: "", state: "" });
        }}
        style={{ minWidth: 220 }}
      >
        <option value="motion">{t("ext.cameras.detections.cond.motion")}</option>
        <option value="object">{t("ext.cameras.detections.cond.object")}</option>
        <option value="ha_sensor">{t("ext.cameras.detections.cond.ha_sensor")}</option>
        <option value="ha_state">{t("ext.cameras.detections.cond.ha_state")}</option>
      </select>

      {value.kind === "object" ? (
        <select
          className="input"
          value={value.category}
          onChange={(event) => {
            const rawCategory = event.target.value;
            const category = YOLO_V12_CATEGORIES.find((it) => it === rawCategory);
            if (!category) return;
            onChange({ kind: "object", category });
          }}
          style={{ minWidth: 240, flex: 1 }}
        >
          {YOLO_V12_CATEGORIES.map((category) => (
            <option key={category} value={category}>
              {formatYoloCategoryLabel(category)}
            </option>
          ))}
        </select>
      ) : null}

      {value.kind === "ha_sensor" || value.kind === "ha_state" ? (
        <input
          className="input"
          value={value.entity_id}
          onChange={(event) => {
            const nextEntityId = event.target.value;
            onChange({ ...value, entity_id: nextEntityId } as DetectionCondition);
          }}
          placeholder="sensor.some_entity"
          style={{ minWidth: 240, flex: 1 }}
        />
      ) : null}

      {value.kind === "ha_state" ? (
        <input
          className="input"
          value={value.state}
          onChange={(event) => onChange({ ...value, state: event.target.value })}
          placeholder="on"
          style={{ width: 120 }}
        />
      ) : null}
    </div>
  );
}

