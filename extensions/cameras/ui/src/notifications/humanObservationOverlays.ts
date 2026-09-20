import type { Notification, Notification2DContext, Notification2DOverlay, Notification3DOverlay, Scene3DContext } from "@toposync/plugin-api";
import { createHumanObservationReader } from "./humanObservation";
import type { HumanAnchor, HumanObservation, Position } from "./humanObservation";
import { createHumanObservationMapGate } from "./humanObservationMap";

/** The pin is only the body ground anchor. It cannot encode feet, height or a cone. */
export function createHumanObservation2D(ctx: Notification2DContext, initial: Notification, session = createHumanObservationReader()): Notification2DOverlay | null {
  if (!ctx.compositionId) return null;
  const read = (notification: Notification, now: number) => session(notification, now);
  let notification = initial, disposed = false;
  const gate = createHumanObservationMapGate(ctx.compositionId, ctx.elements, () => read(notification, Date.now()), () => ctx.requestRender?.());
  let timer: ReturnType<typeof setTimeout> | undefined;
  let scheduledCapture: string | null = null, scheduledDeadline = Infinity;
  const scheduleExpiry = () => {
    const model = read(notification, Date.now());
    if (timer && model.state === "current" && model.captureKey === scheduledCapture && model.expiresAt! >= scheduledDeadline) return;
    if (timer) clearTimeout(timer);
    if (model.state === "current" && model.expiresAt !== null) {
      scheduledCapture = model.captureKey; scheduledDeadline = model.expiresAt;
      timer = setTimeout(() => { if (!disposed) {
        read(notification, Math.max(Date.now(), model.expiresAt!));
        ctx.requestRender?.();
      } }, Math.max(0, model.expiresAt - Date.now()));
    }
  };
  scheduleExpiry();
  return {
    pin: () => {
      if (disposed) return null;
      const model = read(notification, Date.now());
      const body = gate.allows(model) ? model.body.position : null;
      return body ? { x: body[0], z: body[2], priority: "low" } : null;
    },
    update: (next) => { notification = next; gate.invalidate(); scheduleExpiry(); ctx.requestRender?.(); },
    dispose: () => { disposed = true; gate.dispose(); if (timer) clearTimeout(timer); },
  };
}

export function createHumanObservation3D(ctx: Scene3DContext, initial: Notification, session = createHumanObservationReader()): Notification3DOverlay | null {
  if (!ctx.compositionId) return null;
  const THREE = ctx.THREE, object = new THREE.Group();
  object.name = "human-observation-hypotheses";
  const read = (notification: Notification, now: number) => session(notification, now);
  let notification = initial, disposed = false, timer: ReturnType<typeof setTimeout> | undefined;
  let model: HumanObservation;
  let scheduledCapture: string | null = null, scheduledDeadline = Infinity;
  const gate = createHumanObservationMapGate(ctx.compositionId, ctx.elements, () => read(notification, Date.now()), () => {
    if (!disposed) { rebuild(); }
  });
  const clear = () => {
    object.traverse((child) => {
      const mesh = child as import("three").Mesh;
      mesh.geometry?.dispose();
      if (Array.isArray(mesh.material)) mesh.material.forEach((material) => material.dispose());
      else mesh.material?.dispose();
    });
    object.clear();
  };
  const vector = (point: Position) => new THREE.Vector3(...point);
  const marker = (part: HumanAnchor, kind: "body" | "left" | "right") => {
    if (!part.position) return;
    const predicted = part.provenance === "temporal_prediction";
    const color = kind === "body" ? 0xf59e0b : predicted ? 0x94a3b8 : 0x38bdf8;
    const geometry = kind === "body" ? new THREE.OctahedronGeometry(0.09)
      : kind === "left" ? new THREE.BoxGeometry(0.075, 0.075, 0.075) : new THREE.SphereGeometry(0.045, 12, 8);
    const material = new THREE.MeshBasicMaterial({ color, wireframe: true,
      transparent: predicted, opacity: predicted ? 0.55 : 1 });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.name = `human-${kind}-${part.provenance}`;
    mesh.position.copy(vector(part.position));
    object.add(mesh);
    if (part.envelope) {
      const minimum = vector(part.envelope.minimum), maximum = vector(part.envelope.maximum);
      const box = new THREE.BoxGeometry(...maximum.clone().sub(minimum).toArray() as Position);
      const edges = new THREE.EdgesGeometry(box);
      box.dispose();
      const region = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: 0xf59e0b, transparent: true, opacity: 0.5 }));
      region.position.copy(minimum.add(maximum).multiplyScalar(0.5));
      region.name = "uncalibrated-body-hypothesis-envelope";
      object.add(region);
    }
    if (part.radius) {
      let ring: import("three").Object3D;
      if (predicted) {
        const radius = part.radius;
        const points = Array.from({ length: 65 }, (_, index) => {
          const angle = index * 2 * Math.PI / 64;
          return new THREE.Vector3(radius * Math.cos(angle), 0, radius * Math.sin(angle));
        });
        const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),
          new THREE.LineDashedMaterial({ color, dashSize: radius / 3, gapSize: radius / 5, transparent: true, opacity: 0.65 }));
        line.computeLineDistances();
        ring = line;
      } else {
        ring = new THREE.Mesh(new THREE.RingGeometry(part.radius, part.radius + 0.005, 32),
          new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide, transparent: true, opacity: 0.4 }));
        ring.rotation.x = -Math.PI / 2;
      }
      ring.position.copy(vector(part.position));
      ring.name = `uncalibrated-${kind}-foot-radius`;
      object.add(ring);
    }
  };
  const rebuild = () => {
    clear();
    model = read(notification, Date.now());
    object.visible = gate.allows(model);
    if (model.state === "current") {
      if (!timer || model.captureKey !== scheduledCapture || model.expiresAt! < scheduledDeadline) {
        if (timer) clearTimeout(timer);
        scheduledCapture = model.captureKey; scheduledDeadline = model.expiresAt!;
        const deadline = scheduledDeadline;
        timer = setTimeout(() => { if (!disposed) {
          read(notification, Math.max(Date.now(), deadline));
          object.visible = false; ctx.requestRender?.();
        } }, Math.max(0, deadline - Date.now()));
      }
    } else if (timer) { clearTimeout(timer); timer = undefined; }
    if (object.visible) {
      marker(model.body, "body"); marker(model.left, "left"); marker(model.right, "right");
      const ray = model.pointing.ray;
      if (ray) {
        const origin = vector(ray.origin), direction = vector(ray.direction), end = origin.clone().addScaledVector(direction, ray.length);
        const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([origin, end]), new THREE.LineBasicMaterial({ color: 0xf59e0b }));
        line.name = "conditional-pointing-ray";
        object.add(line);
        const radius = ray.radius + Math.tan(ray.halfAngle * Math.PI / 180) * ray.length;
        if (Number.isFinite(radius)) {
          const cone = new THREE.Mesh(new THREE.CylinderGeometry(radius, ray.radius, ray.length, 16, 1, true),
            new THREE.MeshBasicMaterial({ color: 0xf59e0b, wireframe: true, transparent: true, opacity: 0.2 }));
          cone.position.copy(origin.clone().addScaledVector(direction, ray.length / 2));
          cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction);
          cone.name = "uncalibrated-pointing-cone";
          object.add(cone);
        }
      }
    }
    ctx.requestRender?.();
  };
  rebuild();
  return {
    object,
    update: (next) => { if (!disposed) { notification = next; gate.invalidate(); rebuild(); } },
    tick: () => {
      if (disposed || !object.visible) return false;
      const current = read(notification, Date.now());
      if (!gate.allows(current)) object.visible = false;
      // The host must draw the visible-to-hidden transition too. The following
      // tick returns false, so expiry does not keep an idle scene animating.
      return true;
    },
    dispose: () => { disposed = true; gate.dispose(); if (timer) clearTimeout(timer); clear(); object.visible = false; },
  };
}
