import React from "react";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import { i18n } from "../../util/i18n";

import type {
  Notification,
  Notification2DContext,
  Notification2DOverlay,
  Notification2DPin,
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

function normalizePriority(value: unknown): Priority {
  const raw = asString(value, "").toLowerCase();
  if (raw === "low" || raw === "medium" || raw === "high") return raw;
  return "medium";
}

function priorityHex(priority: Priority): number {
  if (priority === "high") return 0xff3b3b;
  if (priority === "low") return 0x9aa4b2;
  return 0x00d1ff;
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
  return ["main", "face"];
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
  return resolveToposyncUrl(`/files/${encodeURI(relPath)}`);
}

function resolveNotificationImageUrl(value: unknown): string | null {
  const url = asString(value, "").trim();
  return url ? resolveToposyncUrl(url) : null;
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
      const label = key || artifactName || "stored";
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

type WorldPoint = { x: number; z: number; compositionId: string | null };

function readCompositionId(...records: Record<string, unknown>[]): string | null {
  for (const rec of records) {
    const direct = asString(rec.composition_id, "").trim() || asString(rec.compositionId, "").trim();
    if (direct) return direct;
    const mapping = asRecord(rec.mapping);
    const mapped = asString(mapping.composition_id, "").trim() || asString(mapping.compositionId, "").trim();
    if (mapped) return mapped;
  }
  return null;
}

function normalizeWorldPoint(value: unknown, fallbackCompositionId: string | null): WorldPoint | null {
  const rec = asRecord(value);
  const x = asFiniteNumber(rec.x);
  const z = asFiniteNumber(rec.z);
  if (x == null || z == null) return null;
  const compositionId =
    asString(rec.composition_id, "").trim() ||
    asString(rec.compositionId, "").trim() ||
    fallbackCompositionId;
  return { x, z, compositionId: compositionId || null };
}

function normalizeWorldEnvelopeCenter(value: unknown, fallbackCompositionId: string | null): WorldPoint | null {
  const envelope = asRecord(value);
  const center = asRecord(envelope.center);
  const compositionId = readCompositionId(envelope) || fallbackCompositionId;
  return normalizeWorldPoint(center, compositionId);
}

function resolveOldDataWorldPoint(data: Record<string, unknown>, fallbackCompositionId: string | null): WorldPoint | null {
  const direct = normalizeWorldPoint(data.world, fallbackCompositionId);
  if (direct) return direct;
  for (const value of Object.values(data)) {
    const candidate = normalizeWorldPoint(value, fallbackCompositionId);
    if (candidate) return candidate;
  }
  return null;
}

function resolveWorldPoint(notification: Notification): WorldPoint | null {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);
  const subject = asRecord(payload.subject);
  const dataSubject = asRecord(data.subject);
  const memberSubject = asRecord(payload.member_subject);
  const dataMemberSubject = asRecord(data.member_subject);
  const fallbackCompositionId = readCompositionId(payload, data);
  const subjectType = (
    asString(subject.type, "").trim() ||
    asString(dataSubject.type, "").trim() ||
    asString(payload.subject_type, "").trim()
  ).toLowerCase();

  const candidates =
    subjectType === "group_event"
      ? [
          normalizeWorldEnvelopeCenter(subject.world_envelope, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(payload.world_envelope, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(dataSubject.world_envelope, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(data.world_envelope, fallbackCompositionId),
          normalizeWorldPoint(memberSubject.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(dataMemberSubject.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(payload.world, fallbackCompositionId),
          normalizeWorldPoint(payload.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(subject.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(data.world_anchor, fallbackCompositionId),
          resolveOldDataWorldPoint(data, fallbackCompositionId),
        ]
      : [
          normalizeWorldPoint(subject.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(payload.world, fallbackCompositionId),
          normalizeWorldPoint(payload.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(dataSubject.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(data.world, fallbackCompositionId),
          normalizeWorldPoint(data.world_anchor, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(subject.world_envelope, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(payload.world_envelope, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(dataSubject.world_envelope, fallbackCompositionId),
          normalizeWorldEnvelopeCenter(data.world_envelope, fallbackCompositionId),
          normalizeWorldPoint(memberSubject.world_anchor, fallbackCompositionId),
          normalizeWorldPoint(dataMemberSubject.world_anchor, fallbackCompositionId),
          resolveOldDataWorldPoint(data, fallbackCompositionId),
        ];

  return candidates.find((item): item is WorldPoint => item != null) ?? null;
}

function resolveTrailEntryPoint(entry: unknown, fallbackCompositionId: string | null): WorldPoint | null {
  const rec = asRecord(entry);
  return (
    normalizeWorldPoint(rec, fallbackCompositionId) ||
    normalizeWorldPoint(rec.world_anchor, fallbackCompositionId) ||
    normalizeWorldEnvelopeCenter(rec.world_envelope, fallbackCompositionId)
  );
}

function resolveTrailPoints(notification: Notification): Array<{ x: number; z: number; compositionId: string | null }> | null {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);
  const fallbackCompositionId = readCompositionId(payload, data);
  const rawTrail = payload.trail;
  if (Array.isArray(rawTrail)) {
    const out: Array<{ x: number; z: number; compositionId: string | null }> = [];
    for (const entry of rawTrail) {
      const point = resolveTrailEntryPoint(entry, fallbackCompositionId);
      if (!point) continue;
      out.push(point);
    }
    if (out.length) return out;
  }

  const point = resolveWorldPoint(notification);
  return point ? [point] : null;
}

function createHaloTexture(THREE: Scene3DContext["THREE"]): import("three").Texture {
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const c = canvas.getContext("2d");
  if (c) {
    const cx = size / 2;
    const grad = c.createRadialGradient(cx, cx, 0, cx, cx, cx);
    grad.addColorStop(0.0, "rgba(255,255,255,1)");
    grad.addColorStop(0.35, "rgba(255,255,255,0.55)");
    grad.addColorStop(1.0, "rgba(255,255,255,0)");
    c.fillStyle = grad;
    c.fillRect(0, 0, size, size);
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.needsUpdate = true;
  return tex;
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

  const initialPriorityHex = priorityHex(normalizePriority(asRecord(notification.payload).priority));
  const material = new THREE.LineBasicMaterial({ color: initialPriorityHex, transparent: true, opacity: 0.9 });
  material.depthTest = false;
  material.depthWrite = false;
  const line = new THREE.Line(geometry, material);
  line.frustumCulled = false;
  line.renderOrder = 10_000;
  group.add(line);

  // ── Marker: pulse rings on the ground + map-pin shape + soft halo ──
  const HEAD_RADIUS = 0.17;
  const CONE_BASE_RADIUS = 0.12;
  const CONE_HEIGHT = 0.40;
  const HEAD_CENTER_Y = CONE_HEIGHT + Math.sqrt(HEAD_RADIUS * HEAD_RADIUS - CONE_BASE_RADIUS * CONE_BASE_RADIUS);
  const HALO_BASE_SCALE = 0.82;
  const RING_DURATION = 1.7;

  const markerGroup = new THREE.Group();
  markerGroup.frustumCulled = false;

  const ringGeom = new THREE.RingGeometry(0.24, 0.31, 48);
  const pulseRings: { mesh: import("three").Mesh; mat: import("three").MeshBasicMaterial; phase: number }[] = [];
  for (let i = 0; i < 2; i += 1) {
    const mat = new THREE.MeshBasicMaterial({
      color: initialPriorityHex,
      transparent: true,
      opacity: 0,
      side: THREE.DoubleSide,
    });
    mat.depthTest = false;
    mat.depthWrite = false;
    const mesh = new THREE.Mesh(ringGeom, mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.y = 0.006;
    mesh.frustumCulled = false;
    mesh.renderOrder = 10_001;
    markerGroup.add(mesh);
    pulseRings.push({ mesh, mat, phase: i * 0.5 });
  }

  // Cone tip pointing down, base at y = CONE_HEIGHT, tip at y = 0.
  const coneGeom = new THREE.ConeGeometry(CONE_BASE_RADIUS, CONE_HEIGHT, 24, 1);
  coneGeom.rotateX(Math.PI);
  coneGeom.translate(0, CONE_HEIGHT / 2, 0);
  const coneMat = new THREE.MeshBasicMaterial({ color: initialPriorityHex });
  coneMat.depthTest = false;
  coneMat.depthWrite = false;
  const cone = new THREE.Mesh(coneGeom, coneMat);
  cone.frustumCulled = false;
  cone.renderOrder = 10_002;
  markerGroup.add(cone);

  const sphereGeom = new THREE.SphereGeometry(HEAD_RADIUS, 24, 24);
  sphereGeom.translate(0, HEAD_CENTER_Y, 0);
  const sphereMat = new THREE.MeshBasicMaterial({ color: initialPriorityHex });
  sphereMat.depthTest = false;
  sphereMat.depthWrite = false;
  const sphere = new THREE.Mesh(sphereGeom, sphereMat);
  sphere.frustumCulled = false;
  sphere.renderOrder = 10_002;
  markerGroup.add(sphere);

  const coreGeom = new THREE.SphereGeometry(0.058, 16, 16);
  coreGeom.translate(0, HEAD_CENTER_Y + 0.062, 0);
  const coreMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.85 });
  coreMat.depthTest = false;
  coreMat.depthWrite = false;
  const core = new THREE.Mesh(coreGeom, coreMat);
  core.frustumCulled = false;
  core.renderOrder = 10_003;
  markerGroup.add(core);

  const haloTex = createHaloTexture(THREE);
  const haloMat = new THREE.SpriteMaterial({
    map: haloTex,
    color: initialPriorityHex,
    transparent: true,
    opacity: 0.6,
    depthTest: false,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
  const halo = new THREE.Sprite(haloMat);
  halo.scale.set(HALO_BASE_SCALE, HALO_BASE_SCALE, 1);
  halo.position.set(0, HEAD_CENTER_Y, 0);
  halo.renderOrder = 10_001;
  markerGroup.add(halo);

  group.add(markerGroup);

  let closedDim = 1;
  let elapsed = 0;

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

    if (lastX != null && lastZ != null) markerGroup.position.set(lastX, 0, lastZ);
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

    markerGroup.position.set(x, 0, z);
  }

  function applyStyleFromNotification(next: Notification): void {
    const payload = asRecord(next.payload);
    const prio = normalizePriority(payload.priority);
    const colorHex = priorityHex(prio);
    const lifecycle = asString(payload.lifecycle, "").trim().toLowerCase();
    const closed = lifecycle === "close" || asString(payload.status, "").trim().toLowerCase() === "closed";
    material.color.setHex(colorHex);
    material.opacity = closed ? 0.35 : 0.9;

    closedDim = closed ? 0.45 : 1;
    const headOpacity = closed ? 0.5 : 1.0;
    for (const ring of pulseRings) ring.mat.color.setHex(colorHex);
    coneMat.color.setHex(colorHex);
    coneMat.transparent = closed;
    coneMat.opacity = headOpacity;
    sphereMat.color.setHex(colorHex);
    sphereMat.transparent = closed;
    sphereMat.opacity = headOpacity;
    coreMat.opacity = closed ? 0.4 : 0.85;
    haloMat.color.setHex(colorHex);
    haloMat.opacity = closed ? 0.25 : 0.6;
  }

  writeTrail(trail);
  applyStyleFromNotification(notification);

  return {
    object: group,
    tick: (deltaSeconds) => {
      elapsed += deltaSeconds;

      for (const ring of pulseRings) {
        const tRaw = (elapsed / RING_DURATION + ring.phase) % 1;
        const t = tRaw < 0 ? tRaw + 1 : tRaw;
        const scale = 0.45 + t * 2.6;
        ring.mesh.scale.set(scale, scale, 1);
        ring.mat.opacity = (1 - t) * 0.7 * closedDim;
      }

      const haloPulse = 1 + Math.sin(elapsed * 2.4) * 0.08;
      halo.scale.set(HALO_BASE_SCALE * haloPulse, HALO_BASE_SCALE * haloPulse, 1);

      return true;
    },
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
      ringGeom.dispose();
      for (const ring of pulseRings) ring.mat.dispose();
      coneGeom.dispose();
      coneMat.dispose();
      sphereGeom.dispose();
      sphereMat.dispose();
      coreGeom.dispose();
      coreMat.dispose();
      haloMat.dispose();
      haloTex.dispose();
    },
  };
}

export function notificationPriority(notification: Notification): Priority {
  const payload = asRecord(notification.payload);
  return normalizePriority(payload.priority);
}

export function notificationThumbnailUrl(notification: Notification): string | null {
  const imageUrl = resolveNotificationImageUrl(notification.imageUrl);
  if (imageUrl) return imageUrl;
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

  const payload = asRecord(notification.payload);

  // Prefer stored images when URLs overlap with artifacts/thumbnail.
  for (const item of resolveStoredImages(payload)) {
    push(item);
  }

  const imageUrl = resolveNotificationImageUrl(notification.imageUrl);
  if (imageUrl) {
    push({
      id: `thumbnail:${notification.id}`,
      url: imageUrl,
      label: "thumbnail",
      source: "thumbnail",
    });
  }

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

  return out;
}

function renderPipelinesNotification(notification: Notification): React.ReactNode {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);

  const status = asString(payload.status, "").trim().toLowerCase();
  const realtime = payload.realtime === true;
  const duration = formatDurationCompact(asRecord(payload.event).duration_seconds);

  const locationLabel = asString(data.area_label, "").trim();

  const description = asString(notification.description, "").trim();
  const metaParts = [locationLabel, duration].filter(Boolean);
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
              {i18n.t("core.ui.notifications.live", {}, "Live")}
            </div>
          ) : null}
        </div>
      ) : null}

      {description ? <div className="notificationText">{description}</div> : null}
    </div>
  );
}

function createPipelines2DOverlay(
  ctx: Notification2DContext,
  notification: Notification,
  _actions: NotificationOverlayActions,
): Notification2DOverlay | null {
  let current: Notification = notification;

  function compute(): Notification2DPin | null {
    const trail = resolveTrailPoints(current);
    if (!trail?.length) return null;
    const last = trail[trail.length - 1] ?? null;
    if (!last) return null;
    if (ctx.compositionId && last.compositionId && last.compositionId !== ctx.compositionId) return null;

    const payload = asRecord(current.payload);
    const lifecycle = asString(payload.lifecycle, "").trim().toLowerCase();
    const closed = lifecycle === "close" || asString(payload.status, "").trim().toLowerCase() === "closed";

    return {
      x: last.x,
      z: last.z,
      trail: trail.map((p) => ({ x: p.x, z: p.z })),
      priority: normalizePriority(payload.priority),
      closed,
    };
  }

  if (!compute()) return null;

  return {
    pin: () => compute(),
    update: (next) => {
      current = next;
    },
  };
}

export const builtinNotificationRenderers: NotificationRenderer[] = [
  {
    id: "core.pipelines_event_renderer.v1",
    type: "pipelines.event",
    render: renderPipelinesNotification,
    create3DOverlay: createPipelines3DOverlay,
    create2DOverlay: createPipelines2DOverlay,
  },
  {
    id: "core.pipelines_tracking_renderer.v1",
    type: "pipelines.tracking",
    render: renderPipelinesNotification,
    create3DOverlay: createPipelines3DOverlay,
    create2DOverlay: createPipelines2DOverlay,
  },
];
