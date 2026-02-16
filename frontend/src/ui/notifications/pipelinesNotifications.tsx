import React from "react";

import type {
  Notification,
  Notification3DOverlay,
  NotificationOverlayActions,
  NotificationRenderer,
  Scene3DContext,
} from "@toposync/plugin-api";

type Priority = "low" | "medium" | "high";

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

function resolveThumbnailFromArtifacts(artifacts: Record<string, string>): { artifactName: string; url: string } | null {
  for (const candidate of preferredArtifactNames()) {
    const rel = artifacts[candidate];
    if (!rel) continue;
    return { artifactName: candidate, url: `/files/${encodeURI(rel)}` };
  }
  const first = Object.entries(artifacts)[0];
  if (!first) return null;
  return { artifactName: first[0], url: `/files/${encodeURI(first[1])}` };
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

function createPipelines3DOverlay(
  ctx: Scene3DContext,
  notification: Notification,
  actions: NotificationOverlayActions,
): Notification3DOverlay | null {
  const point = resolveWorldPoint(notification);
  if (!point) return null;
  if (ctx.compositionId && point.compositionId && point.compositionId !== ctx.compositionId) return null;

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

  append(point.x, point.z);
  applyStyleFromNotification(notification);

  return {
    object: group,
    update: (next) => {
      const nextPoint = resolveWorldPoint(next);
      if (nextPoint) {
        if (!ctx.compositionId || !nextPoint.compositionId || nextPoint.compositionId === ctx.compositionId) {
          append(nextPoint.x, nextPoint.z);
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

function renderPipelinesNotification(notification: Notification): React.ReactNode {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);

  const priority = normalizePriority(payload.priority);
  const lifecycle = asString(payload.lifecycle, "").trim();
  const status = asString(payload.status, "").trim();
  const realtime = payload.realtime === true;
  const duration = formatDurationCompact(asRecord(payload.event).duration_seconds);

  const pipelineName = asString(payload.pipeline_name, "").trim();
  const cameraName = asString(data.camera_name, "").trim();
  const cameraId = asString(data.camera_id, "").trim();
  const locationLabel = asString(data.area_label, "").trim();

  const artifacts = resolveArtifacts(payload);
  const artifactNames = [
    ...preferredArtifactNames().filter((name) => Boolean(artifacts[name])),
    ...Object.keys(artifacts).filter((name) => !preferredArtifactNames().includes(name)),
  ];
  const thumb = resolveThumbnailFromArtifacts(artifacts);

  const subtitleParts = [cameraName || cameraId || pipelineName, locationLabel].filter(Boolean);
  const subtitle = subtitleParts.join(" • ");
  const isLive = status === "open" && realtime;

  return (
    <div className="notificationCameraBody">
      {subtitle || isLive ? (
        <div className="notificationCameraTopRow">
          <div className="notificationCameraName">{subtitle || pipelineName || "Pipeline"}</div>
          {isLive ? (
            <div className="notificationLivePip">
              <span className="notificationLiveDot" />
              LIVE
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="notificationChips">
        <span className="notificationChip">{priority.toUpperCase()}</span>
        {lifecycle ? <span className="notificationChip">{lifecycle.toUpperCase()}</span> : null}
        {duration ? <span className="notificationChip">{duration}</span> : null}
        {thumb?.artifactName ? <span className="notificationChip">thumb: {thumb.artifactName}</span> : null}
        {artifactNames.length ? <span className="notificationChip">artifacts: {artifactNames.length}</span> : null}
      </div>

      {artifactNames.length ? (
        <div className="notificationChips">
          {artifactNames.slice(0, 4).map((name) => (
            <span className="notificationChip" key={name}>
              {name}
            </span>
          ))}
          {artifactNames.length > 4 ? <span className="notificationChip">+{artifactNames.length - 4}</span> : null}
        </div>
      ) : null}

      {notification.description ? <div className="notificationText">{notification.description}</div> : null}
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
