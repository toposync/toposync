import React, { useMemo } from "react";

import type { Notification, NotificationRenderer, TopoSyncHost } from "@toposync/plugin-api";

import { YOLO_LEGACY_CATEGORY_MAP, YOLO_V12_CATEGORIES, formatYoloCategoryLabel, type YoloV12Category } from "../yolo";
import { createCameraTracking3dOverlay } from "./cameraTracking3dOverlay";

type CamerasTrackingPayload = {
  source?: string;
  camera_id?: string;
  camera_name?: string;
  composition_id?: string;
  tracking_id?: string;
  kind?: string;
  label?: string;
  confidence?: number;
};

const YOLO_V12_CATEGORY_SET = new Set<string>(YOLO_V12_CATEGORIES);

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return {};
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

function parseIso(value: string | undefined): Date | null {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

function formatDateTimeShort(locale: string, date: Date): string {
  try {
    return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
  } catch {
    return date.toLocaleString();
  }
}

function formatDurationShort(locale: string, ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;

  const parts: string[] = [];
  if (h > 0) parts.push(`${h}h`);
  if (m > 0 || h > 0) parts.push(`${m}m`);
  parts.push(`${s}s`);
  return parts.join(" ");
}

function normalizeYoloLabel(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  const lower = trimmed.toLowerCase();
  const mapped = YOLO_LEGACY_CATEGORY_MAP[lower] ?? lower;
  return mapped;
}

function asYoloV12Category(value: string): YoloV12Category | null {
  return YOLO_V12_CATEGORY_SET.has(value) ? (value as YoloV12Category) : null;
}

function toTitleCaseWords(value: string): string {
  return value
    .split(" ")
    .map((part) => {
      const trimmed = part.trim();
      if (!trimmed) return "";
      return `${trimmed.slice(0, 1).toUpperCase()}${trimmed.slice(1)}`;
    })
    .join(" ")
    .trim();
}

function formatYoloFallbackLabel(category: string): string {
  if (!category) return "";
  if (category === "tv") return "TV";
  return toTitleCaseWords(category);
}

function yoloI18nKey(category: string): string {
  return `ext.cameras.yolo.${category.replace(/\s+/g, "_")}`;
}

function formatConfidence(locale: string, value: number | null): string | null {
  if (value == null) return null;
  const ratio = value > 1.0 ? value / 100.0 : value;
  if (!Number.isFinite(ratio)) return null;
  const clamped = Math.max(0, Math.min(1, ratio));
  try {
    return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format(clamped);
  } catch {
    return `${Math.round(clamped * 100)}%`;
  }
}

function parsePayload(notification: Notification): CamerasTrackingPayload {
  const rec = asRecord(notification.payload);
  return {
    source: asString(rec.source) || undefined,
    camera_id: asString(rec.camera_id) || undefined,
    camera_name: asString(rec.camera_name) || undefined,
    composition_id: asString(rec.composition_id) || undefined,
    tracking_id: asString(rec.tracking_id) || undefined,
    kind: asString(rec.kind) || undefined,
    label: asString(rec.label) || undefined,
    confidence: asNumber(rec.confidence) ?? undefined,
  };
}

function LivePip(): React.ReactElement {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "2px 10px",
        borderRadius: 999,
        border: "1px solid rgba(16,185,129,0.35)",
        background: "rgba(16,185,129,0.10)",
        color: "rgba(167, 243, 208, 0.95)",
        fontSize: 11,
        fontWeight: 800,
        letterSpacing: "0.08em",
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: 999,
          background: "rgba(52,211,153,0.95)",
          boxShadow: "0 0 0 4px rgba(16,185,129,0.12)",
        }}
      />
      LIVE
    </span>
  );
}

function CameraTrackingNotificationBody({ notification, host }: { notification: Notification; host: TopoSyncHost }): React.ReactElement {
  const { t, locale } = host.i18n.useI18n();
  const payload = useMemo(() => parsePayload(notification), [notification]);

  const cameraName = payload.camera_name?.trim() || payload.camera_id?.trim() || (notification.description ?? "").trim() || "—";
  const yoloLabelRaw = payload.label?.trim() || "";
  const yoloCategory = normalizeYoloLabel(yoloLabelRaw);
  const typedYoloCategory = yoloCategory ? asYoloV12Category(yoloCategory) : null;
  const yoloFallback = yoloCategory ? (typedYoloCategory ? formatYoloCategoryLabel(typedYoloCategory) : formatYoloFallbackLabel(yoloCategory)) : undefined;
  const translatedLabel = yoloCategory ? t(yoloI18nKey(yoloCategory), {}, yoloFallback) : null;
  const confText = formatConfidence(locale, payload.confidence ?? null);

  const createdAt = parseIso(notification.createdAt);
  const updatedAt = parseIso(notification.updatedAt);
  const startedText = createdAt ? formatDateTimeShort(locale, createdAt) : null;
  const durationText =
    createdAt && updatedAt
      ? formatDurationShort(locale, updatedAt.getTime() - createdAt.getTime())
      : null;
  const isLive = Boolean(updatedAt && Date.now() - updatedAt.getTime() < 12_000);

  const metaParts = [translatedLabel, confText, durationText].filter((value): value is string => Boolean(value));

  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
          <div
            style={{
              fontWeight: 800,
              fontSize: 13,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={cameraName}
          >
            {cameraName}
          </div>
          {isLive ? <LivePip /> : null}
        </div>

        {metaParts.length > 0 ? (
          <div className="cardMeta" style={{ margin: 0, display: "flex", flexWrap: "wrap", gap: 8 }}>
            {metaParts.map((part, idx) => (
              <span key={`${idx}-${String(part)}`}>{part}</span>
            ))}
          </div>
        ) : null}

        {startedText ? (
          <div className="cardMeta" style={{ margin: 0 }}>
            {startedText}
          </div>
        ) : null}
      </div>

      {notification.imageUrl ? (
        <img
          src={notification.imageUrl}
          alt=""
          loading="lazy"
          style={{
            width: 72,
            height: 72,
            borderRadius: 14,
            border: "1px solid rgba(255,255,255,0.10)",
            background: "rgba(0,0,0,0.18)",
            objectFit: "cover",
          }}
        />
      ) : null}
    </div>
  );
}

export function createCameraTrackingNotificationRenderer(host: TopoSyncHost): NotificationRenderer {
  return {
    id: "com.toposync.cameras.notification.tracking",
    type: "cameras.tracking",
    render: (notification) => <CameraTrackingNotificationBody notification={notification} host={host} />,
    create3DOverlay: createCameraTracking3dOverlay,
  };
}
