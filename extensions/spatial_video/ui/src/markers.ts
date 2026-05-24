import type { CompositionElement, ElementType, Main2DMarker } from "@toposync/plugin-api";

import type { StreamTextureSnapshot } from "./streamTexture";

export type MarkerVideoStatus = {
  kind: "loading" | "error" | "unmatched";
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

export function markerVideoStatus(snapshot: StreamTextureSnapshot | null, poseStatus: string | null | undefined): MarkerVideoStatus | null {
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
