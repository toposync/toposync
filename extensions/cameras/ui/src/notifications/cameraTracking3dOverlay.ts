import type {
  CompositionElement,
  Notification,
  Notification3DOverlay,
  NotificationOverlayActions,
  Scene3DContext,
} from "@toposync/plugin-api";
import type { Mesh, Object3D } from "three";

import { CAMERA_ELEMENT_TYPE_ID } from "../constants";

type CamerasTrackingPayload = {
  camera_id?: string;
  camera_name?: string;
  composition_id?: string;
  tracking_id?: string;
};

type DetectionEvent = {
  ts: number;
  tracking_id: string;
  composition_id: string | null;
  world: { x: number; z: number } | null;
  image_path: string | null;
};

type CaptureUserData = {
  url: string;
  title?: string;
  subtitle?: string;
};

type CameraAnchor = { x: number; z: number };

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

function parsePayload(notification: Notification): CamerasTrackingPayload {
  const rec = asRecord(notification.payload);
  return {
    camera_id: asString(rec.camera_id) || undefined,
    camera_name: asString(rec.camera_name) || undefined,
    composition_id: asString(rec.composition_id) || undefined,
    tracking_id: asString(rec.tracking_id) || undefined,
  };
}

function encodeFilesPath(path: string): string {
  return encodeURIComponent(path).replace(/%2F/g, "/");
}

function readCaptureUserData(userData: unknown): CaptureUserData | null {
  const root = asRecord(userData);
  const cap = asRecord(root.capture);
  const url = asString(cap.url).trim();
  if (!url) return null;
  const title = asString(cap.title).trim() || undefined;
  const subtitle = asString(cap.subtitle).trim() || undefined;
  return { url, title, subtitle };
}

function parseDetectionEvent(value: unknown): DetectionEvent | null {
  const rec = asRecord(value);

  const ts = asNumber(rec.ts);
  if (ts == null) return null;

  const trackingId = asString(rec.tracking_id).trim();
  if (!trackingId) return null;

  const compositionIdRaw = asString(rec.composition_id).trim();
  const compositionId = compositionIdRaw ? compositionIdRaw : null;

  const worldRec = asRecord(rec.world);
  const wx = asNumber(worldRec.x);
  const wz = asNumber(worldRec.z);
  const world = wx != null && wz != null ? { x: wx, z: wz } : null;

  const imagePath = asString(rec.image_path).trim() || null;

  return { ts, tracking_id: trackingId, composition_id: compositionId, world, image_path: imagePath };
}

function parseCameraAnchorFromComposition(value: unknown, cameraId: string): CameraAnchor | null {
  const root = asRecord(value);
  const elements = Array.isArray(root.elements) ? root.elements : [];

  for (const raw of elements) {
    const el = asRecord(raw) as unknown as Partial<CompositionElement>;
    if (typeof el.type !== "string" || el.type !== CAMERA_ELEMENT_TYPE_ID) continue;

    const props = asRecord(el.props);
    const elementCameraId = asString(props.camera_id).trim();
    if (!elementCameraId || elementCameraId !== cameraId) continue;

    const pos = asRecord(el.position);
    const x = asNumber(pos.x);
    const z = asNumber(pos.z);
    if (x == null || z == null) continue;
    return { x, z };
  }

  return null;
}

export function createCameraTracking3dOverlay(
  ctx: Scene3DContext,
  notification: Notification,
  actions: NotificationOverlayActions,
): Notification3DOverlay | null {
  const THREE = ctx.THREE;

  const payload = parsePayload(notification);
  const cameraId = (payload.camera_id ?? "").trim();
  const trackingId = (payload.tracking_id ?? "").trim();
  if (!trackingId) return null;

  const wantedCompositionId = (payload.composition_id ?? "").trim() || null;
  if (wantedCompositionId && ctx.compositionId && wantedCompositionId !== ctx.compositionId) return null;

  const root = new THREE.Group();
  root.name = `camerasTracking:${trackingId}`;

  const trailY = 0.035;
  const captureY = 0.055;

  const trailGeometry = new THREE.BufferGeometry();
  const trailLineMaterial = new THREE.LineBasicMaterial({
    color: 0xa855f7,
    transparent: true,
    opacity: 0.9,
    depthWrite: false,
  });
  const trailLine = new THREE.Line(trailGeometry, trailLineMaterial);
  trailLine.position.y = trailY;
  trailLine.frustumCulled = false;
  trailLine.renderOrder = 2900;
  // Avoid intercepting clicks meant for capture points.
  trailLine.raycast = () => {};
  root.add(trailLine);

  const trailPointMaterial = new THREE.PointsMaterial({
    color: 0xa855f7,
    size: 6,
    sizeAttenuation: false,
    transparent: true,
    opacity: 0.75,
    depthWrite: false,
  });
  const trailPoints = new THREE.Points(trailGeometry, trailPointMaterial);
  trailPoints.position.y = trailY;
  trailPoints.frustumCulled = false;
  trailPoints.renderOrder = 2901;
  // Avoid intercepting clicks meant for capture points.
  trailPoints.raycast = () => {};
  root.add(trailPoints);

  const captureGroup = new THREE.Group();
  captureGroup.name = "captures";
  captureGroup.renderOrder = 2910;
  root.add(captureGroup);

  const captureGeometry = new THREE.SphereGeometry(0.06, 18, 12);
  const captureMaterial = new THREE.MeshStandardMaterial({
    color: 0xa855f7,
    emissive: 0x6d28d9,
    emissiveIntensity: 1.2,
    roughness: 0.25,
    metalness: 0.05,
    transparent: true,
    opacity: 0.98,
    depthWrite: false,
  });

  const pointsByTs = new Map<number, { x: number; z: number }>();
  const capturesByTs = new Map<number, Mesh>();
  const pendingNoWorldCaptures = new Map<number, string>();
  const MAX_TRAIL_POINTS = 800;

  function rebuildTrailGeometry() {
    const ordered = Array.from(pointsByTs.entries()).sort((a, b) => a[0] - b[0]);
    const slice = ordered.length > MAX_TRAIL_POINTS ? ordered.slice(ordered.length - MAX_TRAIL_POINTS) : ordered;

    if (ordered.length !== slice.length) {
      const toRemove = ordered.slice(0, ordered.length - slice.length);
      for (const [ts] of toRemove) pointsByTs.delete(ts);
    }

    const positions = new Float32Array(slice.length * 3);
    for (let i = 0; i < slice.length; i += 1) {
      const [, p] = slice[i];
      positions[i * 3] = p.x;
      positions[i * 3 + 1] = 0;
      positions[i * 3 + 2] = p.z;
    }

    trailGeometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    trailGeometry.setDrawRange(0, slice.length);
    trailGeometry.computeBoundingSphere();
    trailGeometry.attributes.position.needsUpdate = true;
  }

  function addCapturePoint(ts: number, pos: { x: number; z: number }, imagePath: string) {
    if (capturesByTs.has(ts)) return;
    const url = `/files/${encodeFilesPath(imagePath)}`;
    const mesh = new THREE.Mesh(captureGeometry, captureMaterial);
    mesh.position.set(pos.x, captureY, pos.z);
    mesh.userData.capture = {
      url,
      title: payload.camera_name?.trim() || payload.camera_id?.trim() || notification.title,
      subtitle: new Date(ts * 1000).toLocaleString(),
    };
    mesh.renderOrder = 2911;
    captureGroup.add(mesh);
    capturesByTs.set(ts, mesh);
  }

  function fallbackCapturePosition(anchor: CameraAnchor, ts: number): { x: number; z: number } {
    const seed = Math.floor(ts * 1000);
    const t1 = seed * 0.61803398875;
    const t2 = seed * 0.41421356237;
    const frac1 = t1 - Math.floor(t1);
    const frac2 = t2 - Math.floor(t2);
    const angle = frac1 * Math.PI * 2;
    const radius = 0.12 + frac2 * 0.3;
    return {
      x: anchor.x + Math.cos(angle) * radius,
      z: anchor.z + Math.sin(angle) * radius,
    };
  }

  let cameraAnchor: CameraAnchor | null = null;
  let cameraAnchorLoaded = false;

  function flushPendingNoWorldCaptures(): void {
    if (!cameraAnchorLoaded) return;
    const anchor = cameraAnchor ?? { x: 0, z: 0 };
    for (const [ts, imagePath] of pendingNoWorldCaptures.entries()) {
      addCapturePoint(ts, fallbackCapturePosition(anchor, ts), imagePath);
    }
    pendingNoWorldCaptures.clear();
  }

  function ingest(ev: DetectionEvent) {
    if (ev.tracking_id !== trackingId) return;
    if (wantedCompositionId && ev.composition_id !== wantedCompositionId) return;

    if (ev.world) {
      if (!pointsByTs.has(ev.ts)) pointsByTs.set(ev.ts, { x: ev.world.x, z: ev.world.z });
      rebuildTrailGeometry();

      if (ev.image_path) addCapturePoint(ev.ts, ev.world, ev.image_path);
      return;
    }

    if (!ev.image_path) return;
    if (!cameraAnchorLoaded) {
      pendingNoWorldCaptures.set(ev.ts, ev.image_path);
      return;
    }

    const anchor = cameraAnchor ?? { x: 0, z: 0 };
    addCapturePoint(ev.ts, fallbackCapturePosition(anchor, ev.ts), ev.image_path);
  }

  let disposed = false;
  const abort = new AbortController();
  let stream: EventSource | null = null;

  async function loadCameraAnchor() {
    if (cameraAnchorLoaded) return;
    if (!cameraId) {
      cameraAnchor = { x: 0, z: 0 };
      cameraAnchorLoaded = true;
      flushPendingNoWorldCaptures();
      return;
    }
    try {
      const res = await fetch("/api/composition", { signal: abort.signal });
      if (res.ok) {
        const body = (await res.json()) as unknown;
        cameraAnchor = parseCameraAnchorFromComposition(body, cameraId) ?? { x: 0, z: 0 };
      } else {
        cameraAnchor = { x: 0, z: 0 };
      }
    } catch {
      cameraAnchor = { x: 0, z: 0 };
    } finally {
      cameraAnchorLoaded = true;
      flushPendingNoWorldCaptures();
    }
  }

  async function loadInitial() {
    try {
      const params = new URLSearchParams();
      params.set("tracking_id", trackingId);
      if (wantedCompositionId) params.set("composition_id", wantedCompositionId);
      params.set("limit", "800");
      const res = await fetch(`/api/cameras/detections/recent?${params.toString()}`, { signal: abort.signal });
      if (!res.ok) return;
      const body = (await res.json()) as { events?: unknown[] };
      const events = Array.isArray(body?.events) ? body.events : [];
      for (const raw of events.slice().reverse()) {
        const parsed = parseDetectionEvent(raw);
        if (!parsed) continue;
        ingest(parsed);
      }
    } catch {
      // ignore
    }
  }

  function startStream() {
    try {
      const es = new EventSource("/api/cameras/detections/stream");
      stream = es;
      es.onmessage = (msg) => {
        if (disposed) return;
        try {
          const parsed = parseDetectionEvent(JSON.parse(msg.data));
          if (!parsed) return;
          ingest(parsed);
        } catch {
          // ignore
        }
      };
      es.onerror = () => {
        // EventSource auto-reconnects; keep quiet.
      };
    } catch {
      // ignore
    }
  }

  void loadCameraAnchor();

  void loadInitial().finally(() => {
    if (!disposed) startStream();
  });

  let pulseT = 0;

  return {
    object: root,
    tick(deltaSeconds) {
      pulseT += deltaSeconds;
      const amp = 0.14;
      const base = 1.0;
      let idx = 0;
      for (const mesh of capturesByTs.values()) {
        const scale = base + amp * (0.5 + 0.5 * Math.sin(pulseT * 2.2 + idx));
        mesh.scale.set(scale, scale, scale);
        idx += 1;
      }
    },
    onPointerEvent(event) {
      const hit = event.intersection;
      let cur: Object3D | null = hit.object;
      while (cur) {
        const capture = readCaptureUserData(cur.userData);
        if (capture) {
          actions.openImage({ url: capture.url, title: capture.title, subtitle: capture.subtitle });
          return true;
        }
        cur = cur.parent;
      }
      return false;
    },
    dispose() {
      disposed = true;
      try {
        abort.abort();
      } catch {
        // ignore
      }
      try {
        stream?.close();
      } catch {
        // ignore
      }
      stream = null;

      try {
        trailGeometry.dispose();
        trailLineMaterial.dispose();
        trailPointMaterial.dispose();
        captureGeometry.dispose();
        captureMaterial.dispose();
      } catch {
        // ignore
      }
    },
  };
}
