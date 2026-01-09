import type { PlanePoint } from "@toposync/plugin-api";

export function distanceBetweenPoints(a: PlanePoint, b: PlanePoint): number {
  const dx = a.x - b.x;
  const dz = a.z - b.z;
  return Math.hypot(dx, dz);
}

export function addPoints(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x + b.x, z: a.z + b.z };
}

export function subtractPoints(a: PlanePoint, b: PlanePoint): PlanePoint {
  return { x: a.x - b.x, z: a.z - b.z };
}

export function scalePoint(point: PlanePoint, scalar: number): PlanePoint {
  return { x: point.x * scalar, z: point.z * scalar };
}

export function normalizePoint(point: PlanePoint): PlanePoint {
  const len = Math.hypot(point.x, point.z);
  if (len <= 1e-9) return { x: 1, z: 0 };
  return { x: point.x / len, z: point.z / len };
}

export function perpendicularPoint(point: PlanePoint): PlanePoint {
  return { x: -point.z, z: point.x };
}

function crossProduct(a: PlanePoint, b: PlanePoint): number {
  return a.x * b.z - a.z * b.x;
}

function lineIntersection(
  point0: PlanePoint,
  direction0: PlanePoint,
  point1: PlanePoint,
  direction1: PlanePoint,
): PlanePoint | null {
  const denom = crossProduct(direction0, direction1);
  if (Math.abs(denom) < 1e-9) return null;
  const t = crossProduct(subtractPoints(point1, point0), direction1) / denom;
  return addPoints(point0, scalePoint(direction0, t));
}

export function computeMiterJoin(
  vertex: PlanePoint,
  directionIn: PlanePoint,
  directionOut: PlanePoint,
  normalSign: number,
  halfThickness: number,
  miterLimit: number,
  fallbackDirection: PlanePoint,
): PlanePoint {
  const normalIn = scalePoint(perpendicularPoint(directionIn), normalSign * halfThickness);
  const normalOut = scalePoint(perpendicularPoint(directionOut), normalSign * halfThickness);
  const pointIn = addPoints(vertex, normalIn);
  const pointOut = addPoints(vertex, normalOut);
  const hit = lineIntersection(pointIn, directionIn, pointOut, directionOut);
  if (!hit) return addPoints(vertex, scalePoint(perpendicularPoint(fallbackDirection), normalSign * halfThickness));
  if (distanceBetweenPoints(hit, vertex) > halfThickness * miterLimit) {
    return addPoints(vertex, scalePoint(perpendicularPoint(fallbackDirection), normalSign * halfThickness));
  }
  return hit;
}

export function centerOfPoints(points: PlanePoint[]): PlanePoint {
  if (points.length === 0) return { x: 0, z: 0 };
  const sum = points.reduce((acc, p) => ({ x: acc.x + p.x, z: acc.z + p.z }), { x: 0, z: 0 });
  return { x: sum.x / points.length, z: sum.z / points.length };
}

