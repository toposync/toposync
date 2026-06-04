import * as THREE from "three";

import { projectionStrategies, type ProjectionMeshDensity, type ProjectionStrategyId } from "./projection";
import type { CameraControlPointSet, WorldPoint } from "./types";

export function createProjectionGeometry(
  set: CameraControlPointSet,
  strategyId: ProjectionStrategyId,
  meshDensity: ProjectionMeshDensity,
  options?: { clipPolygon?: WorldPoint[] | null },
): THREE.BufferGeometry | null {
  const meshData = projectionStrategies[strategyId].buildMesh(set, { gridDivisions: meshDensity, clipPolygon: options?.clipPolygon ?? null });
  if (!meshData) return null;
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(meshData.positions, 3));
  geometry.setAttribute("uv", new THREE.BufferAttribute(meshData.uvs, 2));
  geometry.setIndex(new THREE.BufferAttribute(meshData.indices, 1));
  geometry.computeBoundingBox();
  geometry.computeBoundingSphere();
  return geometry;
}
