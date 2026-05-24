import type { ProjectionMeshDensity, ProjectionStrategyId } from "./projection";

export const SPATIAL_VIDEO_RENDER_VIEW_ID = "spatial_video";
export const SPATIAL_VIDEO_3D_RENDER_VIEW_ID = "spatial_video_3d";

export type SpatialVideoSettings = {
  projectionStrategyId: ProjectionStrategyId;
  meshDensity: ProjectionMeshDensity;
};

export const DEFAULT_SPATIAL_VIDEO_SETTINGS: SpatialVideoSettings = {
  projectionStrategyId: "homography_grid",
  meshDensity: 34,
};

export const SPATIAL_VIDEO_PROJECTION_OPTIONS: Array<{
  id: ProjectionStrategyId;
  label: string;
  description: string;
}> = [
  {
    id: "homography_grid",
    label: "Calibrado",
    description: "Usa a região calibrada por pontos confiáveis e evita extrapolar outliers.",
  },
  {
    id: "constrained_trapezoid",
    label: "Trapézio",
    description: "Projeta o frame completo em um trapézio leve, calculado a partir dos pontos confiáveis.",
  },
];

export const SPATIAL_VIDEO_MESH_DENSITY_OPTIONS: Array<{
  id: ProjectionMeshDensity;
  label: string;
  description: string;
}> = [
  {
    id: 34,
    label: "34",
    description: "Equilíbrio padrão entre suavidade e custo de GPU.",
  },
  {
    id: 64,
    label: "64",
    description: "Mais detalhe para projeções curvas ou próximas.",
  },
  {
    id: 96,
    label: "96",
    description: "Malha densa para validação fina; use apenas quando necessário.",
  },
];

function isProjectionStrategyId(value: unknown): value is ProjectionStrategyId {
  return value === "homography_grid" || value === "constrained_trapezoid";
}

function isProjectionMeshDensity(value: unknown): value is ProjectionMeshDensity {
  return value === 34 || value === 64 || value === 96;
}

export function readSpatialVideoSettings(settings: Record<string, unknown> | null | undefined): SpatialVideoSettings {
  return {
    projectionStrategyId: isProjectionStrategyId(settings?.projection_strategy_id)
      ? settings.projection_strategy_id
      : DEFAULT_SPATIAL_VIDEO_SETTINGS.projectionStrategyId,
    meshDensity: isProjectionMeshDensity(settings?.mesh_density)
      ? settings.mesh_density
      : DEFAULT_SPATIAL_VIDEO_SETTINGS.meshDensity,
  };
}
