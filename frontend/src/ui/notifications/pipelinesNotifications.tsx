import React from "react";

import type {
  Notification,
  Notification3DOverlay,
  NotificationOverlayActions,
  NotificationRenderer,
  Scene3DContext,
} from "@toposync/plugin-api";

type Priority = "low" | "medium" | "high";
type NotificationImageSource = "thumbnail" | "artifact" | "stored";

export type NotificationImageItem = {
  id: string;
  url: string;
  label: string;
  source: NotificationImageSource;
  storedTsMs?: number;
  confidence?: number;
};

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return {};
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function isSameNormalized(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

function normalizePriority(value: unknown): Priority {
  const raw = asString(value, "").toLowerCase();
  if (raw === "low" || raw === "medium" || raw === "high") return raw;
  return "medium";
}

function formatDurationCompact(secondsRaw: unknown): string | null {
  const seconds = Math.floor(asNumber(secondsRaw, 0));
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins <= 0) return `${secs}s`;
  return `${mins}m ${secs}s`;
}

function preferredArtifactNames(): string[] {
  return ["best_frame", "face", "segmented", "frame", "frame_original"];
}

function resolveArtifacts(payload: Record<string, unknown>): Record<string, string> {
  const artifactsRaw = asRecord(payload.artifacts);
  const entries = Object.entries(artifactsRaw).filter((entry): entry is [string, string] => typeof entry[0] === "string" && typeof entry[1] === "string");
  const out: Record<string, string> = {};
  for (const [name, relPath] of entries) {
    const trimmed = relPath.trim();
    if (!trimmed) continue;
    out[name] = trimmed;
  }
  return out;
}

function toFileUrl(relPath: string): string {
  return `/files/${encodeURI(relPath)}`;
}

function resolveThumbnailFromArtifacts(artifacts: Record<string, string>): { artifactName: string; url: string } | null {
  for (const candidate of preferredArtifactNames()) {
    const rel = artifacts[candidate];
    if (!rel) continue;
    return { artifactName: candidate, url: toFileUrl(rel) };
  }
  const first = Object.entries(artifacts)[0];
  if (!first) return null;
  return { artifactName: first[0], url: toFileUrl(first[1]) };
}

function parseStoredTsMs(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
  const parsed = Math.floor(value);
  return parsed > 0 ? parsed : undefined;
}

function parseConfidence(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return undefined;
  return value;
}

function resolveStoredImages(payload: Record<string, unknown>): NotificationImageItem[] {
  const rawStored = asRecord(payload.stored_images);
  const out: NotificationImageItem[] = [];

  for (const [key, entriesRaw] of Object.entries(rawStored)) {
    if (!Array.isArray(entriesRaw)) continue;
    let idx = 0;
    for (const entryRaw of entriesRaw) {
      const entry = asRecord(entryRaw);
      const relPath = asString(entry.rel_path, "").trim();
      if (!relPath) continue;
      idx += 1;
      const artifactName = asString(entry.artifact_name, "").trim();
      const label = artifactName || key || "stored";
      out.push({
        id: `stored:${key}:${idx}:${relPath}`,
        url: toFileUrl(relPath),
        label,
        source: "stored",
        storedTsMs: parseStoredTsMs(entry.stored_ts_ms),
        confidence: parseConfidence(entry.confidence),
      });
    }
  }

  out.sort((a, b) => {
    const at = a.storedTsMs ?? 0;
    const bt = b.storedTsMs ?? 0;
    if (at !== bt) return at - bt;
    return a.label.localeCompare(b.label);
  });

  return out;
}

function asFiniteNumber(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value;
}

function resolveWorldPoint(notification: Notification): { x: number; z: number; compositionId: string | null } | null {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);
  const world = asRecord(data.world);
  let x = asFiniteNumber(world.x);
  let z = asFiniteNumber(world.z);
  if (x == null || z == null) {
    for (const value of Object.values(data)) {
      const candidate = asRecord(value);
      const cx = asFiniteNumber(candidate.x);
      const cz = asFiniteNumber(candidate.z);
      if (cx == null || cz == null) continue;
      x = cx;
      z = cz;
      break;
    }
  }
  if (x == null || z == null) return null;

  const mapping = asRecord(data.mapping);
  const comp = asString(mapping.composition_id, "").trim();
  return { x, z, compositionId: comp || null };
}

function resolveTrailPoints(notification: Notification): Array<{ x: number; z: number; compositionId: string | null }> | null {
  const payload = asRecord(notification.payload);
  const rawTrail = payload.trail;
  if (Array.isArray(rawTrail)) {
    const out: Array<{ x: number; z: number; compositionId: string | null }> = [];
    for (const entry of rawTrail) {
      const rec = asRecord(entry);
      const x = asFiniteNumber(rec.x);
      const z = asFiniteNumber(rec.z);
      if (x == null || z == null) continue;
      const comp = asString(rec.composition_id, "").trim();
      out.push({ x, z, compositionId: comp || null });
    }
    if (out.length) return out;
  }

  const point = resolveWorldPoint(notification);
  return point ? [point] : null;
}

function createPipelines3DOverlay(
  ctx: Scene3DContext,
  notification: Notification,
  actions: NotificationOverlayActions,
): Notification3DOverlay | null {
  const trail = resolveTrailPoints(notification);
  if (!trail?.length) return null;
  const lastPoint = trail[trail.length - 1] ?? null;
  if (ctx.compositionId && lastPoint?.compositionId && lastPoint.compositionId !== ctx.compositionId) return null;

  const { THREE } = ctx;
  const group = new THREE.Group();

  const maxPoints = 512;
  const positions = new Float32Array(maxPoints * 3);
  let pointCount = 0;
  let lastX: number | null = null;
  let lastZ: number | null = null;

  const geometry = new THREE.BufferGeometry();
  const positionAttr = new THREE.BufferAttribute(positions, 3);
  geometry.setAttribute("position", positionAttr);
  geometry.setDrawRange(0, 0);

  const material = new THREE.LineBasicMaterial({ color: 0x00d1ff, transparent: true, opacity: 0.9 });
  material.depthTest = false;
  material.depthWrite = false;
  const line = new THREE.Line(geometry, material);
  line.frustumCulled = false;
  line.renderOrder = 10_000;
  group.add(line);

  const markerMat = new THREE.MeshBasicMaterial({ color: 0xff3b81 });
  markerMat.depthTest = false;
  markerMat.depthWrite = false;
  const marker = new THREE.Mesh(new THREE.SphereGeometry(0.08, 12, 12), markerMat);
  marker.frustumCulled = false;
  marker.renderOrder = 10_001;
  group.add(marker);

  function writeTrail(points: Array<{ x: number; z: number }>): void {
    pointCount = 0;
    lastX = null;
    lastZ = null;

    const eps2 = 0.000_001;
    const start = points.length > maxPoints ? points.length - maxPoints : 0;
    for (let i = start; i < points.length; i += 1) {
      const { x, z } = points[i]!;
      if (lastX != null && lastZ != null) {
        const dx = x - lastX;
        const dz = z - lastZ;
        if (dx * dx + dz * dz <= eps2) continue;
      }

      lastX = x;
      lastZ = z;

      const base = pointCount * 3;
      positions[base] = x;
      positions[base + 1] = 0.05;
      positions[base + 2] = z;
      pointCount += 1;
      if (pointCount >= maxPoints) break;
    }

    positionAttr.needsUpdate = true;
    geometry.setDrawRange(0, pointCount);
    geometry.computeBoundingSphere();

    if (lastX != null && lastZ != null) marker.position.set(lastX, 0.06, lastZ);
  }

  function append(x: number, z: number): void {
    const eps2 = 0.000_001;
    if (lastX != null && lastZ != null) {
      const dx = x - lastX;
      const dz = z - lastZ;
      if (dx * dx + dz * dz <= eps2) return;
    }
    lastX = x;
    lastZ = z;

    if (pointCount >= maxPoints) {
      positions.copyWithin(0, 3, positions.length);
      pointCount = maxPoints - 1;
    }

    const base = pointCount * 3;
    positions[base] = x;
    positions[base + 1] = 0.05;
    positions[base + 2] = z;
    pointCount += 1;

    positionAttr.needsUpdate = true;
    geometry.setDrawRange(0, pointCount);
    geometry.computeBoundingSphere();

    marker.position.set(x, 0.06, z);
  }

  function applyStyleFromNotification(next: Notification): void {
    const payload = asRecord(next.payload);
    const prio = normalizePriority(payload.priority);
    const lifecycle = asString(payload.lifecycle, "").trim().toLowerCase();
    const closed = lifecycle === "close" || asString(payload.status, "").trim().toLowerCase() === "closed";
    if (prio === "high") material.color.setHex(0xff3b3b);
    else if (prio === "low") material.color.setHex(0x9aa4b2);
    else material.color.setHex(0x00d1ff);
    material.opacity = closed ? 0.35 : 0.9;
    markerMat.opacity = closed ? 0.4 : 1.0;
    markerMat.transparent = closed;
  }

  writeTrail(trail);
  applyStyleFromNotification(notification);

  return {
    object: group,
    update: (next) => {
      const nextTrail = resolveTrailPoints(next);
      const nextLast = nextTrail?.length ? nextTrail[nextTrail.length - 1] ?? null : null;
      if (nextLast && (!ctx.compositionId || !nextLast.compositionId || nextLast.compositionId === ctx.compositionId)) {
        writeTrail(nextTrail ?? []);
      } else {
        const nextPoint = resolveWorldPoint(next);
        if (nextPoint) {
          if (!ctx.compositionId || !nextPoint.compositionId || nextPoint.compositionId === ctx.compositionId) {
            append(nextPoint.x, nextPoint.z);
          }
        }
      }
      applyStyleFromNotification(next);
    },
    onPointerEvent: (event) => {
      if (event.kind !== "click") return false;
      const url = notificationThumbnailUrl(event.notification);
      if (!url) return false;
      actions.openImage({ url, title: event.notification.title });
      return true;
    },
    dispose: () => {
      geometry.dispose();
      material.dispose();
      marker.geometry.dispose();
      markerMat.dispose();
    },
  };
}

export function notificationPriority(notification: Notification): Priority {
  const payload = asRecord(notification.payload);
  return normalizePriority(payload.priority);
}

export function notificationThumbnailUrl(notification: Notification): string | null {
  if (typeof notification.imageUrl === "string" && notification.imageUrl.trim()) return notification.imageUrl;
  const payload = asRecord(notification.payload);
  const artifacts = resolveArtifacts(payload);
  return resolveThumbnailFromArtifacts(artifacts)?.url ?? null;
}

export function notificationImageItems(notification: Notification): NotificationImageItem[] {
  const out: NotificationImageItem[] = [];
  const seenUrls = new Set<string>();

  function push(item: NotificationImageItem): void {
    const key = item.url.trim();
    if (!key || seenUrls.has(key)) return;
    seenUrls.add(key);
    out.push(item);
  }

  if (typeof notification.imageUrl === "string" && notification.imageUrl.trim()) {
    push({
      id: `thumbnail:${notification.id}`,
      url: notification.imageUrl.trim(),
      label: "thumbnail",
      source: "thumbnail",
    });
  }

  const payload = asRecord(notification.payload);
  const artifacts = resolveArtifacts(payload);
  const orderedArtifactNames = [
    ...preferredArtifactNames().filter((name) => Boolean(artifacts[name])),
    ...Object.keys(artifacts).filter((name) => !preferredArtifactNames().includes(name)),
  ];
  for (const name of orderedArtifactNames) {
    const relPath = artifacts[name];
    if (!relPath) continue;
    push({
      id: `artifact:${name}:${relPath}`,
      url: toFileUrl(relPath),
      label: name,
      source: "artifact",
    });
  }

  for (const item of resolveStoredImages(payload)) {
    push(item);
  }

  return out;
}

function renderPipelinesNotification(notification: Notification): React.ReactNode {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);

  const status = asString(payload.status, "").trim().toLowerCase();
  const realtime = payload.realtime === true;
  const duration = formatDurationCompact(asRecord(payload.event).duration_seconds);

  const cameraName = asString(data.camera_name, "").trim();
  const cameraId = asString(data.camera_id, "").trim();
  const locationLabel = asString(data.area_label, "").trim();

  const title = asString(notification.title, "").trim();
  const description = asString(notification.description, "").trim();
  const cameraLabel = cameraName || cameraId;
  const titleIncludesCamera = cameraLabel ? title.toLowerCase().includes(cameraLabel.toLowerCase()) : false;
  const shouldHideDescription =
    !description || (cameraLabel && isSameNormalized(description, cameraLabel)) || (title && isSameNormalized(description, title));
  const metaParts = [titleIncludesCamera ? "" : cameraLabel, locationLabel, duration].filter(Boolean);
  const meta = metaParts.join(" • ");
  const isLive = status === "open" && realtime;

  return (
    <div className="notificationCameraBody">
      {meta || isLive ? (
        <div className="notificationCameraTopRow">
          <div className="notificationCameraName">{meta}</div>
          {isLive ? (
            <div className="notificationLivePip">
              <span className="notificationLiveDot" />
              LIVE
            </div>
          ) : null}
        </div>
      ) : null}

      {!shouldHideDescription ? <div className="notificationText">{description}</div> : null}
    </div>
  );
}

export const builtinNotificationRenderers: NotificationRenderer[] = [
  {
    id: "core.pipelines_event_renderer.v1",
    type: "pipelines.event",
    render: renderPipelinesNotification,
    create3DOverlay: createPipelines3DOverlay,
  },
  {
    id: "core.pipelines_tracking_renderer.v1",
    type: "pipelines.tracking",
    render: renderPipelinesNotification,
    create3DOverlay: createPipelines3DOverlay,
  },
];
