import type { CompositionElement, PlanePoint } from "@toposync/plugin-api";

import { DEFAULT_WALL_WIDTH, WALL_ELEMENT_TYPE_ID } from "./constants";
import {
  addPoints,
  distanceBetweenPoints,
  normalizePoint,
  perpendicularPoint,
  scalePoint,
  subtractPoints,
} from "./geometry";
import { readNumber, readPlanePoint } from "./parsing";

export type WallEndpointName = "a" | "b";

export type ParsedStructuralWall = {
  element: CompositionElement;
  elementId: string;
  a: PlanePoint;
  b: PlanePoint;
  width: number;
  halfWidth: number;
  length: number;
  direction: PlanePoint;
  normal: PlanePoint;
};

export type WallNodeRef = {
  wall: ParsedStructuralWall;
  endpoint: WallEndpointName;
  point: PlanePoint;
};

export type WallNode = {
  id: string;
  point: PlanePoint;
  refs: WallNodeRef[];
};

export type WallFootprint = {
  wall: ParsedStructuralWall;
  startNode: WallNode;
  endNode: WallNode;
  startLeft: PlanePoint;
  startRight: PlanePoint;
  endLeft: PlanePoint;
  endRight: PlanePoint;
  polygon: PlanePoint[];
};

export type WallTopology = {
  walls: ParsedStructuralWall[];
  nodes: WallNode[];
  endpointNodes: Map<string, WallNode>;
};

export type WallIntervalFootprint = {
  start: number;
  end: number;
  points: PlanePoint[];
};

export type WallEndpointSnap = {
  elementId: string;
  endpoint: WallEndpointName;
  point: PlanePoint;
  distance: number;
};

const MIN_WALL_LENGTH = 1e-6;
const MAX_JOIN_EPSILON_METERS = 0.06;
const MITER_LIMIT_FACTOR = 2;

function endpointKey(wall: ParsedStructuralWall, endpoint: WallEndpointName): string {
  return `${wall.elementId}:${endpoint}`;
}

function endpointPoint(wall: ParsedStructuralWall, endpoint: WallEndpointName): PlanePoint {
  return endpoint === "a" ? wall.a : wall.b;
}

function endpointDirectionAwayFromNode(wall: ParsedStructuralWall, endpoint: WallEndpointName): PlanePoint {
  return endpoint === "a" ? wall.direction : scalePoint(wall.direction, -1);
}

function dot(a: PlanePoint, b: PlanePoint): number {
  return a.x * b.x + a.z * b.z;
}

function cross(a: PlanePoint, b: PlanePoint): number {
  return a.x * b.z - a.z * b.x;
}

function lineIntersection(
  point0: PlanePoint,
  direction0: PlanePoint,
  point1: PlanePoint,
  direction1: PlanePoint,
): PlanePoint | null {
  const denominator = cross(direction0, direction1);
  if (Math.abs(denominator) < 1e-9) return null;
  const t = cross(subtractPoints(point1, point0), direction1) / denominator;
  return addPoints(point0, scalePoint(direction0, t));
}

function isFinitePoint(point: PlanePoint): boolean {
  return Number.isFinite(point.x) && Number.isFinite(point.z);
}

function averagePoints(points: PlanePoint[]): PlanePoint {
  if (points.length === 0) return { x: 0, z: 0 };
  let x = 0;
  let z = 0;
  for (const point of points) {
    x += point.x;
    z += point.z;
  }
  return { x: x / points.length, z: z / points.length };
}

function localCapPoint(nodePoint: PlanePoint, wall: ParsedStructuralWall, normalSign: number): PlanePoint {
  return addPoints(nodePoint, scalePoint(wall.normal, normalSign * wall.halfWidth));
}

function miterPointForSide(
  node: WallNode,
  currentRef: WallNodeRef,
  otherRef: WallNodeRef,
  normalSign: number,
): PlanePoint {
  const currentWall = currentRef.wall;
  const otherWall = otherRef.wall;
  const currentDirection = endpointDirectionAwayFromNode(currentWall, currentRef.endpoint);
  const otherDirection = endpointDirectionAwayFromNode(otherWall, otherRef.endpoint);

  const almostColinear = Math.abs(cross(currentDirection, otherDirection)) < 1e-8;
  if (almostColinear) return localCapPoint(node.point, currentWall, normalSign);

  const currentOffsetPoint = localCapPoint(node.point, currentWall, normalSign);
  const otherOffsetPoint = addPoints(node.point, scalePoint(otherWall.normal, normalSign * otherWall.halfWidth));
  const intersection = lineIntersection(currentOffsetPoint, currentDirection, otherOffsetPoint, otherDirection);
  if (!intersection || !isFinitePoint(intersection)) return localCapPoint(node.point, currentWall, normalSign);

  const limit = Math.max(Math.min(currentWall.halfWidth, otherWall.halfWidth) * MITER_LIMIT_FACTOR, 1e-6);
  if (distanceBetweenPoints(intersection, node.point) > limit + 1e-9) {
    return localCapPoint(node.point, currentWall, normalSign);
  }

  return intersection;
}

function endpointJoinPoints(node: WallNode, ref: WallNodeRef): { left: PlanePoint; right: PlanePoint } {
  if (node.refs.length !== 2) {
    return {
      left: localCapPoint(node.point, ref.wall, 1),
      right: localCapPoint(node.point, ref.wall, -1),
    };
  }

  const otherRef = node.refs[0] === ref ? node.refs[1] : node.refs[0];
  return {
    left: miterPointForSide(node, ref, otherRef, 1),
    right: miterPointForSide(node, ref, otherRef, -1),
  };
}

function pointAtDistance(wall: ParsedStructuralWall, distanceMeters: number): PlanePoint {
  return addPoints(wall.a, scalePoint(wall.direction, distanceMeters));
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function unionFind(size: number): { find: (index: number) => number; union: (a: number, b: number) => void } {
  const parent = Array.from({ length: size }, (_, index) => index);

  function find(index: number): number {
    let current = index;
    while (parent[current] !== current) {
      parent[current] = parent[parent[current]];
      current = parent[current];
    }
    return current;
  }

  function union(a: number, b: number): void {
    const rootA = find(a);
    const rootB = find(b);
    if (rootA !== rootB) parent[rootB] = rootA;
  }

  return { find, union };
}

export function wallJoinEpsilon(a: ParsedStructuralWall, b: ParsedStructuralWall): number {
  return Math.min(Math.min(a.width, b.width) / 2, MAX_JOIN_EPSILON_METERS);
}

export function parseStructuralWalls(elements: CompositionElement[]): ParsedStructuralWall[] {
  const walls: ParsedStructuralWall[] = [];
  for (const element of elements) {
    if (element.type !== WALL_ELEMENT_TYPE_ID) continue;

    const a = readPlanePoint(element.props.a, { x: element.position.x, z: element.position.z });
    const b = readPlanePoint(element.props.b, { x: element.position.x + 1, z: element.position.z });
    const length = distanceBetweenPoints(a, b);
    if (!Number.isFinite(length) || length <= MIN_WALL_LENGTH) continue;

    const width = Math.max(0.04, readNumber(element.props.width, DEFAULT_WALL_WIDTH));
    const direction = normalizePoint(subtractPoints(b, a));
    walls.push({
      element,
      elementId: element.id,
      a,
      b,
      width,
      halfWidth: width / 2,
      length,
      direction,
      normal: perpendicularPoint(direction),
    });
  }
  return walls;
}

export function buildWallTopology(elements: CompositionElement[]): WallTopology {
  const walls = parseStructuralWalls(elements);
  const endpointRefs: WallNodeRef[] = [];
  for (const wall of walls) {
    endpointRefs.push({ wall, endpoint: "a", point: wall.a });
    endpointRefs.push({ wall, endpoint: "b", point: wall.b });
  }

  const groups = unionFind(endpointRefs.length);
  for (let i = 0; i < endpointRefs.length; i += 1) {
    for (let j = i + 1; j < endpointRefs.length; j += 1) {
      const a = endpointRefs[i];
      const b = endpointRefs[j];
      if (a.wall.elementId === b.wall.elementId) continue;
      const tolerance = wallJoinEpsilon(a.wall, b.wall);
      if (distanceBetweenPoints(a.point, b.point) <= tolerance + 1e-9) groups.union(i, j);
    }
  }

  const refsByRoot = new Map<number, WallNodeRef[]>();
  for (let i = 0; i < endpointRefs.length; i += 1) {
    const root = groups.find(i);
    const bucket = refsByRoot.get(root) ?? [];
    bucket.push(endpointRefs[i]);
    refsByRoot.set(root, bucket);
  }

  const nodes: WallNode[] = [];
  const endpointNodes = new Map<string, WallNode>();
  let nodeIndex = 0;
  for (const refs of refsByRoot.values()) {
    const node: WallNode = {
      id: `wall-node:${nodeIndex}`,
      point: averagePoints(refs.map((ref) => ref.point)),
      refs,
    };
    nodeIndex += 1;
    nodes.push(node);
    for (const ref of refs) endpointNodes.set(endpointKey(ref.wall, ref.endpoint), node);
  }

  return { walls, nodes, endpointNodes };
}

export function buildWallFootprints(elements: CompositionElement[]): Map<string, WallFootprint> {
  const topology = buildWallTopology(elements);
  const footprints = new Map<string, WallFootprint>();

  for (const wall of topology.walls) {
    const startNode = topology.endpointNodes.get(endpointKey(wall, "a"));
    const endNode = topology.endpointNodes.get(endpointKey(wall, "b"));
    if (!startNode || !endNode) continue;

    const startRef = startNode.refs.find((ref) => ref.wall.elementId === wall.elementId && ref.endpoint === "a");
    const endRef = endNode.refs.find((ref) => ref.wall.elementId === wall.elementId && ref.endpoint === "b");
    if (!startRef || !endRef) continue;

    const start = endpointJoinPoints(startNode, startRef);
    const end = endpointJoinPoints(endNode, endRef);
    const polygon = [start.left, end.left, end.right, start.right];
    if (!polygon.every(isFinitePoint)) continue;

    footprints.set(wall.elementId, {
      wall,
      startNode,
      endNode,
      startLeft: start.left,
      startRight: start.right,
      endLeft: end.left,
      endRight: end.right,
      polygon,
    });
  }

  return footprints;
}

export function getWallFootprint(element: CompositionElement, elements: CompositionElement[]): WallFootprint | null {
  const sourceElements = elements.some((candidate) => candidate.id === element.id) ? elements : [...elements, element];
  return buildWallFootprints(sourceElements).get(element.id) ?? null;
}

export function buildWallIntervalFootprint(
  footprint: WallFootprint,
  startMeters: number,
  endMeters: number,
): WallIntervalFootprint | null {
  const wall = footprint.wall;
  const start = clamp(Math.min(startMeters, endMeters), 0, wall.length);
  const end = clamp(Math.max(startMeters, endMeters), 0, wall.length);
  if (end - start <= 1e-6) return null;

  const startPoint = pointAtDistance(wall, start);
  const endPoint = pointAtDistance(wall, end);
  const useJoinedStart = start <= 1e-6;
  const useJoinedEnd = end >= wall.length - 1e-6;

  const startLeft = useJoinedStart ? footprint.startLeft : addPoints(startPoint, scalePoint(wall.normal, wall.halfWidth));
  const startRight = useJoinedStart ? footprint.startRight : addPoints(startPoint, scalePoint(wall.normal, -wall.halfWidth));
  const endLeft = useJoinedEnd ? footprint.endLeft : addPoints(endPoint, scalePoint(wall.normal, wall.halfWidth));
  const endRight = useJoinedEnd ? footprint.endRight : addPoints(endPoint, scalePoint(wall.normal, -wall.halfWidth));
  const points = [startLeft, endLeft, endRight, startRight];
  if (!points.every(isFinitePoint)) return null;
  return { start, end, points };
}

export function wallLocalPoint(wall: ParsedStructuralWall, point: PlanePoint): PlanePoint {
  const delta = subtractPoints(point, wall.a);
  return {
    x: dot(delta, wall.direction),
    z: dot(delta, wall.normal),
  };
}

export function findNearestWallEndpoint(
  elements: CompositionElement[],
  point: PlanePoint,
  radiusMeters: number,
  options: { excludeElementId?: string } = {},
): WallEndpointSnap | null {
  const radius = Math.max(0, radiusMeters);
  let best: WallEndpointSnap | null = null;

  for (const wall of parseStructuralWalls(elements)) {
    if (options.excludeElementId && wall.elementId === options.excludeElementId) continue;
    for (const endpoint of ["a", "b"] as const) {
      const endpointPosition = endpointPoint(wall, endpoint);
      const distance = distanceBetweenPoints(point, endpointPosition);
      if (distance > radius) continue;
      if (!best || distance < best.distance) {
        best = {
          elementId: wall.elementId,
          endpoint,
          point: endpointPosition,
          distance,
        };
      }
    }
  }

  return best;
}
