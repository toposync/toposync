import * as THREE from "three";

/** Limites dos recursos decodificados antes da renderização; não limitam o pico da decodificação. */
export const MAXIMUM_MODEL_BYTES = 64 * 1024 * 1024;
export const MAXIMUM_MODEL_VERTICES = 2_000_000;
export const MAXIMUM_TEXTURE_PIXELS = 16_777_216;

export function validateModel(model: THREE.Object3D): THREE.Box3 {
  let vertices = 0;
  let texturePixels = 0;
  const textures = new Set<THREE.Texture>();
  model.updateMatrixWorld(true);
  model.traverse((object) => {
    if (!object.matrixWorld.elements.every(Number.isFinite)) throw new Error("Model contains a non-finite transform");
    const mesh = object as THREE.Mesh;
    const positions = mesh.geometry?.getAttribute("position");
    if (positions) {
      vertices += positions.count;
      if (vertices > MAXIMUM_MODEL_VERTICES) throw new Error("Model exceeds 2 million vertices");
      for (let index = 0; index < positions.count; index++) {
        if (![positions.getX(index), positions.getY(index), positions.getZ(index)].every(Number.isFinite)) {
          throw new Error("Model contains a non-finite vertex");
        }
      }
    }
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      if (!material) continue;
      for (const value of Object.values(material)) {
        if (!(value instanceof THREE.Texture) || textures.has(value)) continue;
        textures.add(value);
        const image = value.image;
        const width = Number(image?.width ?? 0);
        const height = Number(image?.height ?? 0);
        if (!Number.isFinite(width * height) || width <= 0 || height <= 0) throw new Error("Invalid model texture");
        texturePixels += width * height;
        if (texturePixels > MAXIMUM_TEXTURE_PIXELS) throw new Error("Model textures exceed 16 million pixels");
      }
    }
  });
  const bounds = new THREE.Box3().setFromObject(model);
  const size = bounds.getSize(new THREE.Vector3());
  if (vertices === 0 || ![...bounds.min.toArray(), ...bounds.max.toArray()].every(Number.isFinite) || size.length() < 1e-6) {
    throw new Error("Model has no finite geometry");
  }
  return bounds;
}

export function disposeModel(model: THREE.Object3D): void {
  model.traverse((object) => {
    const mesh = object as THREE.Mesh;
    mesh.geometry?.dispose();
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      if (!material) continue;
      for (const value of Object.values(material)) if (value instanceof THREE.Texture) value.dispose();
      material.dispose();
    }
  });
}
