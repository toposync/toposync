import type { CompositionElement, ElementType, Main2DMarker } from "@toposync/plugin-api";

import type { StreamTextureSnapshot } from "./streamTexture";

export type MarkerVideoStatus = {
  kind: "loading" | "error" | "pose_notice" | "pose_warning" | "unmatched";
  icon: string;
  title: string;
  color: string;
  background: string;
  border: string;
};

export function snapshotLabel(snapshot: StreamTextureSnapshot): string {
  if (snapshot.status === "playing") return `${snapshot.transport?.toUpperCase() ?? "VIDEO"} ao vivo`;
  if (snapshot.status === "loading") return "Aquecendo";
  if (snapshot.status === "error") return snapshot.message;
  return "Aguardando";
}

export function markerVideoStatus(snapshot: StreamTextureSnapshot | null, poseStatus: string | null | undefined, areaClipWarning?: string | null): MarkerVideoStatus | null {
  if (snapshot?.status === "error") {
    return {
      kind: "error",
      icon: "triangle-exclamation",
      title: snapshot.message || "Falha na transmissão espacial.",
      color: "rgb(254,202,202)",
      background: "rgba(127,29,29,0.94)",
      border: "rgba(248,113,113,0.78)",
    };
  }
  if (areaClipWarning) {
    return {
      kind: "pose_warning",
      icon: "crop-simple",
      title: areaClipWarning,
      color: "rgb(254,240,138)",
      background: "rgba(113,63,18,0.92)",
      border: "rgba(250,204,21,0.72)",
    };
  }
  if (!snapshot || snapshot.status === "idle" || snapshot.status === "loading") {
    return {
      kind: "loading",
      icon: "spinner",
      title: snapshot ? snapshotLabel(snapshot) : "Aquecendo transmissão.",
      color: "rgb(186,230,253)",
      background: "rgba(12,74,110,0.94)",
      border: "rgba(125,211,252,0.78)",
    };
  }
  if (poseStatus === "interpolated") {
    return {
      kind: "pose_notice",
      icon: "wand-magic-sparkles",
      title: "Pose interpolada entre calibrações.",
      color: "rgb(186,230,253)",
      background: "rgba(8,47,73,0.92)",
      border: "rgba(56,189,248,0.7)",
    };
  }
  if (poseStatus === "extrapolated") {
    return {
      kind: "pose_warning",
      icon: "draw-polygon",
      title: "Pose extrapolada fora da calibração.",
      color: "rgb(254,240,138)",
      background: "rgba(113,63,18,0.92)",
      border: "rgba(250,204,21,0.72)",
    };
  }
  if (poseStatus === "nearest_reference" || poseStatus === "single_reference") {
    return {
      kind: "pose_warning",
      icon: "location-dot",
      title: "Imagem renderizada na calibração mais próxima; a pose atual pode não estar bem mapeada.",
      color: "rgb(254,240,138)",
      background: "rgba(113,63,18,0.92)",
      border: "rgba(250,204,21,0.72)",
    };
  }
  if (poseStatus === "unmatched") {
    return {
      kind: "unmatched",
      icon: "location-dot",
      title: "Pose atual sem mapeamento de projeção.",
      color: "rgb(251,191,36)",
      background: "rgba(120,53,15,0.92)",
      border: "rgba(251,191,36,0.72)",
    };
  }
  return null;
}

export function markerEntries(elements: CompositionElement[], elementTypesById: Record<string, ElementType>): Array<{ elementId: string; marker: Main2DMarker }> {
  const out: Array<{ elementId: string; marker: Main2DMarker }> = [];
  for (const element of elements) {
    const def = elementTypesById[element.type];
    if (!def?.getMain2DMarker) continue;
    try {
      const marker = def.getMain2DMarker({ element });
      if (marker) out.push({ elementId: marker.elementId || element.id, marker });
    } catch (error) {
      console.warn(`[spatial-video:getMain2DMarker:${element.type}]`, error);
    }
  }
  return out;
}
