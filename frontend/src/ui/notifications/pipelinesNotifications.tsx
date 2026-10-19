import React from "react";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import { i18n } from "../../util/i18n";
import { ActivitySummary, ActivityView } from "./ActivityView";

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
  publishedAtMs?: number;
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
  if (priority === "high") return 0xef4444;
  if (priority === "low") return 0x9aa4b2;
  return 0xf59e0b;
}

function formatDurationCompact(secondsRaw: unknown): string | null {
  if (secondsRaw === 0) return i18n.t("core.ui.notifications.details.single_observation");
  const roundedSeconds = Math.round(asNumber(secondsRaw, 0) * 10) / 10;
  if (!Number.isFinite(roundedSeconds) || roundedSeconds <= 0) return null;
  if (roundedSeconds < 60) {
    return `${roundedSeconds.toLocaleString(i18n.getLocale(), { maximumFractionDigits: 1 })}s`;
  }
  const seconds = Math.floor(roundedSeconds);
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins <= 0) return `${secs}s`;
  return `${mins}m ${secs}s`;
}

function subjectType(payload: Record<string, unknown>, data: Record<string, unknown>): string {
  const subject = asRecord(payload.subject);
  const dataSubject = asRecord(data.subject);
  return (
    asString(subject.type, "").trim() ||
    asString(dataSubject.type, "").trim() ||
    asString(payload.subject_type, "").trim()
  ).toLowerCase();
}

function isLegacyActiveDescription(value: string): boolean {
  const normalized = value.trim().replace(/\s+/g, " ").toLowerCase();
  return /^(?:\d+\s+)?active(?:\s*-\s*.+)?$/.test(normalized);
}

function isDefaultGroupPresenceDescription(value: string): boolean {
  const normalized = value.trim().replace(/\s+/g, " ").toLowerCase();
  return (
    !normalized ||
    isLegacyActiveDescription(normalized) ||
    /^presence (?:in progress|ended)(?:\s*-\s*.+)?$/.test(normalized)
  );
}

function notificationIsClosed(payload: Record<string, unknown>): boolean {
  const status = asString(payload.status, "").trim().toLowerCase();
  const lifecycle = asString(payload.lifecycle, "").trim().toLowerCase();
  return status === "closed" || lifecycle === "close";
}

function cameraLabel(payload: Record<string, unknown>, data: Record<string, unknown>): string {
  const subject = asRecord(payload.subject);
  const dataSubject = asRecord(data.subject);
  return (
    asString(data.camera_name, "").trim() ||
    asString(payload.camera_name, "").trim() ||
    asString(dataSubject.camera_name, "").trim() ||
    asString(subject.camera_name, "").trim()
  );
}

export function notificationDescription(
  notification: Notification,
  payload: Record<string, unknown> = asRecord(notification.payload),
  data: Record<string, unknown> = asRecord(payload.data),
): string {
  const description = asString(notification.description, "").trim();
  const resolvedSubjectType = subjectType(payload, data);
  const isPipelinesNotification = asString(payload.source, "").trim() === "pipelines";
  const closed = notificationIsClosed(payload);
  const shouldReplace =
    resolvedSubjectType === "group_event"
      ? isDefaultGroupPresenceDescription(description)
      : isPipelinesNotification && isLegacyActiveDescription(description);
  if (!shouldReplace) {
    return description;
  }
  const camera = cameraLabel(payload, data);
  return i18n.t(
    closed ? "core.ui.notifications.group_presence.closed_description" : "core.ui.notifications.group_presence.description",
    { camera_suffix: camera ? ` - ${camera}` : "" },
    closed ? "Presence ended{{camera_suffix}}" : "Presence in progress{{camera_suffix}}",
  );
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
      const name = key || artifactName || "stored";
      const label = name === "subject_crop"
        ? i18n.t("core.ui.notifications.details.subject_crop")
        : name === "main" && Array.isArray(rawStored.subject_crop)
        ? i18n.t("core.ui.notifications.details.scene") : name;
      out.push({
        id: `stored:${key}:${idx}:${relPath}`,
        url: toFileUrl(relPath),
        label,
        source: "stored",
        storedTsMs: parseStoredTsMs(entry.stored_ts_ms),
        publishedAtMs: parseStoredTsMs(entry.published_at_ms),
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

type WorldPoint = { x: number; z: number; compositionId: string | null; gapBefore?: boolean };

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
  return { x, z, compositionId: compositionId || null, gapBefore: rec.gap_before === true };
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
  const memberSubject = asRecord(payload.member_subject);
  const dataMemberSubject = asRecord(data.member_subject);
  const fallbackCompositionId = readCompositionId(payload, data);
  const resolvedSubjectType = subjectType(payload, data);
  const subject = asRecord(payload.subject);
  const dataSubject = asRecord(data.subject);

  const candidates =
    resolvedSubjectType === "group_event"
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

function resolveUnscopedTrailPoints(notification: Notification): WorldPoint[] | null {
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);
  const fallbackCompositionId = readCompositionId(payload, data);
  const rawTrail = payload.trail;
  if (Array.isArray(rawTrail)) {
    const out: WorldPoint[] = [];
    for (const entry of rawTrail.slice(-512)) {
      const point = resolveTrailEntryPoint(entry, fallbackCompositionId);
      if (!point) continue;
      out.push(point);
    }
    if (out.length) return out;
  }

  const point = resolveWorldPoint(notification);
  return point ? [point] : null;
}

function resolveTrailPoints(notification: Notification, compositionId: string | undefined): WorldPoint[] | null {
  const points = resolveUnscopedTrailPoints(notification);
  return compositionId && points?.length && points.every(point => point.compositionId === compositionId) ? points : null;
}

function splitTrail(points: WorldPoint[]): WorldPoint[][] {
  const segments: WorldPoint[][] = [];
  for (const point of points) {
    if (!segments.length || point.gapBefore) segments.push([]);
    segments[segments.length - 1]!.push(point);
  }
  return segments;
}

function resolveSpatialEvidence(notification: Notification, compositionId: string | undefined) {
  if (!compositionId) return null;
  const history = asRecord(asRecord(notification.payload).spatial_history);
  const trails: WorldPoint[][] = [];
  if (history.version === 1 && Array.isArray(history.trails)) {
    let remaining = 2048;
    for (const raw of history.trails.slice(0, 64)) {
      const record = asRecord(raw);
      if (!Array.isArray(record.points)) continue;
      let segment: WorldPoint[] = [];
      let scope = "";
      for (const rawPoint of record.points.slice(0, Math.min(512, remaining))) {
        remaining -= 1;
        const point = normalizeWorldPoint(rawPoint, null);
        const nextScope = asString(asRecord(rawPoint).scope, "");
        if (!point || point.compositionId !== compositionId) {
          if (segment.length) trails.push(segment);
          segment = []; scope = "";
          continue;
        }
        if (point.gapBefore || (scope && scope !== nextScope)) {
          if (segment.length) trails.push(segment);
          segment = [];
        }
        segment.push(point); scope = nextScope;
      }
      if (segment.length) trails.push(segment);
    }
  } else {
    const legacy = resolveTrailPoints(notification, compositionId);
    if (legacy) trails.push(...splitTrail(legacy));
  }
  const representative = resolveWorldPoint(notification);
  const legacy = resolveTrailPoints(notification, compositionId);
  const head = representative?.compositionId === compositionId ? representative
    : legacy?.[legacy.length - 1] ?? trails.flat().slice(-1)[0];
  // Reject unscoped or conflicting legacy trails rather than borrowing a current map.
  if (history.version !== 1 && !legacy) return null;
  return head ? { head, trails } : null;
}

export function renderNotificationSpatialStatus(notification: Notification): React.ReactNode {
  if (!["pipelines.tracking", "pipelines.event"].includes(notification.type)) return null;
  const payload = asRecord(notification.payload);
  const data = asRecord(payload.data);
  const spatial = asRecord(payload.spatial_status);
  const mapping = asRecord(data.mapping);
  if (notification.type !== "pipelines.tracking" && !data.camera_id && !Object.keys(mapping).length && !Object.keys(spatial).length) return null;
  const history = asRecord(payload.spatial_history);
  const truncated = history.truncated === true || (Array.isArray(history.trails) && history.trails.some(trail => asRecord(trail).truncated === true));
  const points = resolveUnscopedTrailPoints(notification);
  const degraded = spatial.status === "degraded" || ["unmapped", "partial"].includes(asString(mapping.status, ""));
  const missing = !points?.length;
  if (!degraded && !missing && spatial.had_mapping_gaps !== true && !truncated) return null;
  const recordedReasons = Array.isArray(spatial.failure_reasons) ? spatial.failure_reasons : [];
  const reason = asString(spatial.reason, "") || asString(mapping.reason, "") || asString(recordedReasons.slice(-1)[0], "");
  const label = missing ? "unavailable" : degraded ? "degraded" : "partial";
  const explanation = reason === "camera_configuration_changed" ? "configuration_changed"
    : reason === "composition_changed" || reason === "panorama_composition_mismatch" ? "composition_changed"
    : reason.includes("geometry") || reason.includes("binding") || reason.includes("identity") ? "frame_incompatible"
    : reason.includes("outside") || reason === "no_ground_intersection" || reason === "source_point_unavailable" ? "outside_coverage"
    : reason.includes("localization") ? "localization_failed" : "unknown";
  return <div className="notificationSpatialStatus" role="status">
    {missing || degraded || spatial.had_mapping_gaps === true ? <strong>{i18n.t(`core.ui.notifications.spatial.${label}`)}</strong> : null}
    {truncated ? <div>{i18n.t("core.ui.notifications.spatial.truncated")}</div> : null}
    {missing || degraded || (spatial.had_mapping_gaps === true && reason) ? <div>{i18n.t(`core.ui.notifications.spatial.reason.${explanation}`)}</div> : null}
  </div>;
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
  const initialEvidence = resolveSpatialEvidence(notification, ctx.compositionId);
  if (!initialEvidence) return null;
  let evidence = initialEvidence;

  const { THREE } = ctx;
  const group = new THREE.Group();

  const initialPriorityHex = priorityHex(notificationPriority(notification));
  const trailMaterial = new THREE.LineBasicMaterial({ color: initialPriorityHex, transparent: true, opacity: 0.9 });
  trailMaterial.depthTest = false;
  trailMaterial.depthWrite = false;
  const trailGeometry = new THREE.BufferGeometry();
  const trailAttribute = new THREE.Float32BufferAttribute(new Float32Array(2048 * 6), 3);
  trailGeometry.setAttribute("position", trailAttribute);
  const trailMesh = new THREE.LineSegments(trailGeometry, trailMaterial);
  trailMesh.frustumCulled = false;
  trailMesh.renderOrder = 10_000;
  group.add(trailMesh);

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

  function writeEvidence(next: NonNullable<ReturnType<typeof resolveSpatialEvidence>>): void {
    evidence = next;
    const vertices: number[] = [];
    for (const segment of next.trails) {
      for (let index = 1; index < segment.length; index += 1) {
        const a = segment[index - 1]!;
        const b = segment[index]!;
        vertices.push(a.x, 0.055, a.z, b.x, 0.055, b.z);
      }
    }
    trailAttribute.array.set(vertices);
    trailAttribute.needsUpdate = true;
    trailGeometry.setDrawRange(0, vertices.length / 3);
    markerGroup.position.set(next.head.x, 0, next.head.z);
  }

  function applyStyleFromNotification(next: Notification): void {
    const payload = asRecord(next.payload);
    const prio = notificationPriority(next);
    const colorHex = priorityHex(prio);
    const lifecycle = asString(payload.lifecycle, "").trim().toLowerCase();
    const closed = lifecycle === "close" || asString(payload.status, "").trim().toLowerCase() === "closed";
    trailMaterial.color.setHex(colorHex);
    trailMaterial.opacity = closed ? 0.35 : 0.9;

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

  writeEvidence(initialEvidence);
  applyStyleFromNotification(notification);

  return {
    object: group,
    bounds: () => {
      if (!group.visible) return null;
      const points = [evidence.head, ...evidence.trails.flat()];
      return {
        minX: Math.min(...points.map(point => point.x)), maxX: Math.max(...points.map(point => point.x)),
        minZ: Math.min(...points.map(point => point.z)), maxZ: Math.max(...points.map(point => point.z)),
      };
    },
    tick: (deltaSeconds) => {
      if (!group.visible) return false;
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
      const nextEvidence = resolveSpatialEvidence(next, ctx.compositionId);
      group.visible = Boolean(nextEvidence);
      if (nextEvidence) writeEvidence(nextEvidence);
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
      trailGeometry.dispose();
      trailMaterial.dispose();
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
  if (notification.priority) return normalizePriority(notification.priority);
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

  const description = notificationDescription(notification, payload, data);
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
      {renderNotificationSpatialStatus(notification)}
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
    const evidence = resolveSpatialEvidence(current, ctx.compositionId);
    if (!evidence) return null;
    const last = evidence.head;

    const payload = asRecord(current.payload);
    const lifecycle = asString(payload.lifecycle, "").trim().toLowerCase();
    const closed = lifecycle === "close" || asString(payload.status, "").trim().toLowerCase() === "closed";

    return {
      x: last.x,
      z: last.z,
      trails: evidence.trails.map(segment => segment.map(point => ({ x: point.x, z: point.z }))),
      priority: notificationPriority(current),
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
    id: "core.activity",
    type: "occurrences.activity",
    render: notification => <ActivitySummary notification={notification} />,
    renderDetails: notification => <ActivityView key={notification.id} notification={notification} />,
  },
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
