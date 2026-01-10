import React, { useEffect, useMemo, useState } from "react";

import type { Notification, NotificationRenderer, TopoSyncHost } from "@toposync/plugin-api";

import { fetchCamerasIndex } from "../api/camerasApi";
import type { CamerasIndex } from "../types";
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

function formatDurationShort(ms: number): string {
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

const cameraNameById = new Map<string, string>();
let cameraNamesPromise: Promise<void> | null = null;
let cameraNamesLoaded = false;

async function ensureCameraNamesLoaded(): Promise<void> {
  if (cameraNamesLoaded) return;
  if (!cameraNamesPromise) {
    cameraNamesPromise = fetchCamerasIndex()
      .then((index: CamerasIndex) => {
        cameraNameById.clear();
        for (const cam of index.cameras) {
          const id = typeof cam?.id === "string" ? cam.id : "";
          if (!id) continue;
          const name = typeof cam?.name === "string" ? cam.name.trim() : "";
          cameraNameById.set(id, name || id);
        }
        cameraNamesLoaded = true;
      })
      .catch((err) => {
        cameraNamesPromise = null;
        console.warn("[cameras] Failed to load cameras index:", err);
      });
  }
  await cameraNamesPromise;
}

function useCameraNameFromIndex(cameraId: string | undefined): string | null {
  const [name, setName] = useState(() => (cameraId ? cameraNameById.get(cameraId) ?? null : null));

  useEffect(() => {
    let cancelled = false;
    if (!cameraId) {
      setName(null);
      return () => {
        cancelled = true;
      };
    }

    const cached = cameraNameById.get(cameraId);
    if (cached) {
      setName(cached);
      return () => {
        cancelled = true;
      };
    }

    ensureCameraNamesLoaded().then(() => {
      if (cancelled) return;
      setName(cameraNameById.get(cameraId) ?? null);
    });

    return () => {
      cancelled = true;
    };
  }, [cameraId]);

  return name;
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
  const { locale } = host.i18n.useI18n();
  const payload = useMemo(() => parsePayload(notification), [notification]);

  const cameraNameFromIndex = useCameraNameFromIndex(payload.camera_id);
  const cameraName =
    payload.camera_name?.trim() ||
    cameraNameFromIndex?.trim() ||
    (notification.description ?? "").trim() ||
    payload.camera_id?.trim() ||
    "—";
  const confText = formatConfidence(locale, payload.confidence ?? null);

  const createdAt = parseIso(notification.createdAt);
  const updatedAt = parseIso(notification.updatedAt);
  const durationText =
    createdAt && updatedAt
      ? formatDurationShort(updatedAt.getTime() - createdAt.getTime())
      : null;
  const isLive = Boolean(updatedAt && Date.now() - updatedAt.getTime() < 12_000);

  const metaParts = [confText, durationText].filter((value): value is string => Boolean(value));

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
