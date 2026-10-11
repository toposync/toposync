import { useEffect, useState, type RefObject } from "react";
import * as THREE from "three";
import { requestJson, resolveToposyncUrl, type CompositionElement, type ElementType } from "@toposync/plugin-api";
import { createPanoramaProjector } from "../../../cameras/ui/src/elements/panoramaProjection";
import type { CameraPanoramaJob, CameraSourcePanoramaArtifact } from "../../../cameras/ui/src/types";
import { readAreaClipElementId, readAreaPolygon } from "./areaClip";
import { applyClipToProjectionMesh } from "./projection";
import type { ProjectionMeshData, WorldPoint } from "./types";

/** The mesh limits world coverage; the fragment shader samples spherical rays,
 * so neither a global image homography nor interpolated panorama UVs are used. */
export function panoramaGroundMesh(support: WorldPoint[], area: WorldPoint[] | null): ProjectionMeshData | null {
  const contour = support.map(point => new THREE.Vector2(point.x, point.z));
  const mesh: ProjectionMeshData = {
    positions: new Float32Array(support.flatMap(point => [point.x, 0.027, point.z])),
    uvs: new Float32Array(support.length * 2),
    indices: new Uint32Array(THREE.ShapeUtils.triangulateShape(contour, []).flat()),
  };
  if (!area) return mesh.indices.length ? mesh : null;
  // Triangulate concave areas before using the existing convex clipper.
  const faces = THREE.ShapeUtils.triangulateShape(area.map(point => new THREE.Vector2(point.x, point.z)), []);
  const positions: number[] = [], indices: number[] = [];
  for (const face of faces) {
    const clipped = applyClipToProjectionMesh(mesh, face.map(index => area[index]));
    if (!clipped) continue;
    const offset = positions.length / 3;
    positions.push(...clipped.positions);
    indices.push(...Array.from(clipped.indices, index => index + offset));
  }
  return indices.length ? { positions: new Float32Array(positions), indices: new Uint32Array(indices), uvs: new Float32Array(positions.length / 3 * 2) } : null;
}

function loadImage(url: string, signal: AbortSignal): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const abort = () => { image.src = ""; reject(new DOMException("Aborted", "AbortError")); };
    image.onload = () => { signal.removeEventListener("abort", abort); resolve(image); };
    image.onerror = () => { signal.removeEventListener("abort", abort); reject(new Error("Não foi possível carregar a panorâmica ou sua cobertura.")); };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort(); else image.src = resolveToposyncUrl(url);
  });
}

export function usePanoramaGroundLayer(
  sceneRef: RefObject<THREE.Scene | null>, elements: CompositionElement[],
  elementTypes: Record<string, ElementType>, requestRender: () => void,
): { ready: number; loading: boolean; error: string | null } {
  const [state, setState] = useState({ ready: 0, loading: false, error: null as string | null });
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const controller = new AbortController();
    const group = new THREE.Group();
    group.name = "saved-panorama-mappings";
    scene.add(group);
    const dispose: Array<() => void> = [];
    const mapped = elements.flatMap(element => {
      const pointer = element.props?.panorama_mapping as Record<string, unknown> | null;
      return pointer?.status === "ready" && typeof pointer.job_id === "string"
        && typeof pointer.revision === "number" && Number.isInteger(pointer.revision)
        && typeof pointer.source_id === "string"
        ? [{ element, pointer: { job_id: pointer.job_id, revision: pointer.revision, source_id: pointer.source_id } }] : [];
    });
    setState({ ready: 0, loading: mapped.length > 0, error: null });
    void Promise.allSettled(mapped.map(async ({ element, pointer }) => {
      const cameraId = String(element.props.camera_id || "");
      const job = await requestJson<CameraPanoramaJob>(`/api/cameras/cameras/${encodeURIComponent(cameraId)}/panorama/${encodeURIComponent(pointer.job_id)}`, { signal: controller.signal });
      if (!job.active || job.id !== pointer.job_id || job.revision !== pointer.revision || job.source_id !== pointer.source_id
        || !job.source_panorama?.compatible || !job.panorama_url || !job.coverage_url || job.solution?.quality.status !== "ready") {
        throw new Error("O mapeamento salvo precisa de revisão antes de ser projetado.");
      }
      const [image, coverage, source] = await Promise.all([
        loadImage(job.panorama_url, controller.signal), loadImage(job.coverage_url, controller.signal),
        requestJson<{ artifact: CameraSourcePanoramaArtifact }>(`/api/cameras/panorama-artifacts/${encodeURIComponent(job.source_panorama.id)}`, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      if (source.artifact.stale || source.artifact.id !== job.source_panorama.id || source.artifact.revision !== job.source_panorama.revision) throw new Error("A panorâmica mudou. Revise o mapeamento salvo.");
      if (image.naturalWidth !== job.source_panorama.width || image.naturalHeight !== job.source_panorama.height
        || coverage.naturalWidth !== image.naturalWidth || coverage.naturalHeight !== image.naturalHeight) throw new Error("As dimensões da panorâmica mudaram.");
      const canvas = document.createElement("canvas");
      canvas.width = coverage.naturalWidth; canvas.height = coverage.naturalHeight;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) throw new Error("Não foi possível verificar a cobertura da panorâmica.");
      context.drawImage(coverage, 0, 0);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      const data = new Uint8Array(canvas.width * canvas.height);
      for (let index = 0; index < data.length; index++) data[index] = pixels[index * 4 + 3] ? pixels[index * 4] : 0;
      if (!createPanoramaProjector(job.solution, { width: canvas.width, height: canvas.height, data })) throw new Error("A geometria da panorâmica não está disponível para projeção.");
      let area: WorldPoint[] | null = null;
      const selectedArea = readAreaClipElementId(element);
      if (selectedArea) {
        const target = elements.find(item => item.id === selectedArea);
        if (!target || elementTypes[target.type]?.layerGroup !== "areas" || (area = readAreaPolygon(target)).length < 3) {
          throw new Error("A área de recorte não está disponível. Revise a seleção da câmera.");
        }
      }
      const meshData = panoramaGroundMesh(job.solution!.support_polygon.map(([x, z]) => ({ x, z })), area);
      if (!meshData) throw new Error("A área não cruza a cobertura mapeada da panorâmica.");
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(meshData.positions, 3));
      geometry.setIndex(new THREE.BufferAttribute(meshData.indices, 1));
      const texture = new THREE.Texture(image); texture.needsUpdate = true;
      const mask = new THREE.DataTexture(data, canvas.width, canvas.height, THREE.RedFormat);
      mask.flipY = true; mask.needsUpdate = true;
      const crop = source.artifact.crop ?? { u_start: 0, u_width: 1, v_start: 0, v_height: 1 };
      const matrix = job.solution!.matrix!;
      const material = new THREE.ShaderMaterial({
        side: THREE.DoubleSide,
        uniforms: {
          panorama: { value: texture }, coverage: { value: mask },
          rayMatrix: { value: new THREE.Matrix3().set(...matrix.flat() as [number, number, number, number, number, number, number, number, number]) },
          crop: { value: new THREE.Vector4(crop.u_start, crop.u_width, crop.v_start, crop.v_height) },
        },
        vertexShader: `varying vec2 world; void main() { world = position.xz; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
        fragmentShader: `
          uniform sampler2D panorama; uniform sampler2D coverage; uniform mat3 rayMatrix; uniform vec4 crop; varying vec2 world;
          void main() {
            vec3 ray = rayMatrix * vec3(world, 1.0);
            float horizontal = length(ray.xy);
            float pan = horizontal > 0.0000001 ? atan(ray.y, ray.x) : 0.0;
            vec2 canonical = vec2(fract((pan + 3.141592653589793) / 6.283185307179586), 0.5 - atan(ray.z, horizontal) / 3.141592653589793);
            if (fract(canonical.x - crop.x + 1.0) > crop.y || canonical.y < crop.z || canonical.y > crop.z + crop.w) discard;
            vec2 uv = vec2(canonical.x, 1.0 - canonical.y);
            if (texture2D(coverage, uv).r <= 0.0) discard;
            gl_FragColor = texture2D(panorama, uv);
          }`,
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.name = `panorama-mapping:${element.id}`;
      group.add(mesh);
      dispose.push(() => { geometry.dispose(); material.dispose(); texture.dispose(); mask.dispose(); });
      requestRender();
    })).then(results => {
      if (controller.signal.aborted) return;
      const failure = results.find(result => result.status === "rejected") as PromiseRejectedResult | undefined;
      setState({ ready: group.children.length, loading: false, error: failure ? String(failure.reason?.message || "Não foi possível carregar o mapeamento panorâmico.") : null });
      requestRender();
    });
    return () => { controller.abort(); scene.remove(group); dispose.forEach(cleanup => cleanup()); requestRender(); };
  }, [sceneRef, elements, elementTypes, requestRender]);
  return state;
}
