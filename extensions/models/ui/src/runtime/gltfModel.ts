import type * as ThreeTypes from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import { readNumber, readScale, readString, readVector3 } from "../parsing";
import type { Vector3 } from "../types";

type ModelMeta = { center: Vector3; minY: number };

export type GltfModelRuntime = {
  object: ThreeTypes.Group;
  updateFromProps: (props: unknown) => void;
  setAnimated: (animated: boolean) => void;
  tick: (dt: number) => void;
  dispose: () => void;
  getHasAnimations: () => boolean;
};

export function createGltfModelRuntime(
  THREE: typeof import("three"),
  options?: { autoplay?: boolean },
): GltfModelRuntime {
  const group = new THREE.Group();

  const loader = new GLTFLoader();
  let disposed = false;
  let token = 0;

  let lastUrl = "";
  let current: ThreeTypes.Object3D | null = null;
  let mixer: ThreeTypes.AnimationMixer | null = null;
  let actions: ThreeTypes.AnimationAction[] = [];
  let hasAnimations = false;
  let isAnimated = Boolean(options?.autoplay);

  function disposeObject(obj: ThreeTypes.Object3D) {
    obj.traverse((child) => {
      const mesh = child as any;
      if (mesh.geometry?.dispose) mesh.geometry.dispose();
      const material = mesh.material;
      if (Array.isArray(material)) material.forEach((m) => m?.dispose?.());
      else material?.dispose?.();
    });
  }

  function disposeMixer() {
    if (!mixer) return;
    try {
      mixer.stopAllAction();
    } catch {
      // ignore
    }
    if (current) {
      try {
        mixer.uncacheRoot(current);
      } catch {
        // ignore
      }
    }
    mixer = null;
    actions = [];
    hasAnimations = false;
  }

  function stopAnimations() {
    if (!mixer) return;
    for (const action of actions) {
      try {
        action.stop();
      } catch {
        // ignore
      }
    }
    try {
      mixer.setTime(0);
    } catch {
      // ignore
    }
  }

  function startAnimations() {
    if (!mixer) return;
    for (const action of actions) {
      try {
        action.reset();
        action.play();
      } catch {
        // ignore
      }
    }
  }

  function applyAnimationState() {
    if (!mixer) return;
    if (isAnimated) startAnimations();
    else stopAnimations();
  }

  async function load(url: string, meta: ModelMeta | null) {
    const myToken = ++token;
    try {
      const gltf = await loader.loadAsync(url);
      if (disposed || myToken !== token) return;

      const model = gltf.scene || gltf.scenes?.[0];
      if (!model) throw new Error("Empty model");

      model.traverse((obj) => {
        const mesh = obj as any;
        if (mesh.isMesh) {
          mesh.castShadow = true;
          mesh.receiveShadow = true;
        }
      });

      const boundingBox = new THREE.Box3().setFromObject(model);
      const centerVector = boundingBox.getCenter(new THREE.Vector3());
      const minY = boundingBox.min.y;
      const center = meta?.center ?? { x: centerVector.x, y: centerVector.y, z: centerVector.z };
      const lift = meta?.minY ?? minY;
      model.position.sub(new THREE.Vector3(center.x, lift, center.z));

      if (current) {
        group.remove(current);
        disposeMixer();
        disposeObject(current);
        current = null;
      }

      current = model;
      group.add(model);

      if (Array.isArray(gltf.animations) && gltf.animations.length > 0) {
        mixer = new THREE.AnimationMixer(model);
        actions = gltf.animations.map((clip) => mixer!.clipAction(clip));
        hasAnimations = actions.length > 0;
        applyAnimationState();
      } else {
        disposeMixer();
      }
    } catch (err) {
      console.error("[models:gltfModel]", err);
    }
  }

  function updateFromProps(props: unknown) {
    const rec =
      props && typeof props === "object" && !Array.isArray(props) ? (props as Record<string, unknown>) : {};

    const dir = readString(rec.dir, "");
    const model = readString(rec.model, "");
    const scale = readScale(rec.scale, 1);
    const center = readVector3(rec.center, { x: 0, y: 0, z: 0 });
    const minY = readNumber(rec.min_y, 0);

    group.scale.setScalar(scale);

    const url = dir && model ? resolveToposyncUrl(`/files/${encodeURIComponent(dir)}/${encodeURIComponent(model)}`) : "";
    if (!url) {
      lastUrl = "";
      if (current) {
        group.remove(current);
        disposeMixer();
        disposeObject(current);
        current = null;
      }
      return;
    }
    if (url && url !== lastUrl) {
      lastUrl = url;
      void load(url, { center, minY });
    }
  }

  function setAnimated(next: boolean) {
    if (next === isAnimated) return;
    isAnimated = next;
    applyAnimationState();
  }

  return {
    object: group,
    updateFromProps,
    setAnimated,
    tick: (dt: number) => {
      if (!isAnimated) return;
      mixer?.update(dt);
    },
    dispose: () => {
      disposed = true;
      disposeMixer();
      if (current) disposeObject(current);
    },
    getHasAnimations: () => hasAnimations,
  };
}
