import type * as ThreeTypes from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { resolveToposyncUrl } from "@toposync/plugin-api";

import { readNumber, readScale, readString, readVector3 } from "../parsing";
import { disposeModel, validateModel } from "../validation";
import type { Vector3 } from "../types";

type ModelMeta = { center: Vector3; minY: number };

export type GltfModelRuntime = {
  object: ThreeTypes.Group;
  updateFromProps: (props: unknown) => void;
  setAnimated: (animated: boolean) => void;
  tick: (dt: number) => boolean;
  dispose: () => void;
  getHasAnimations: () => boolean;
};

export function createGltfModelRuntime(
  THREE: typeof import("three"),
  options?: { autoplay?: boolean; onInvalidate?: () => void; unavailableLabel?: string },
): GltfModelRuntime {
  const group = new THREE.Group();
  const placeholderGeometry = new THREE.BoxGeometry(1, 1, 1);
  const placeholderMaterial = new THREE.MeshBasicMaterial({
    color: 0x38bdf8,
    wireframe: true,
    transparent: true,
    opacity: 0.22,
    depthWrite: false,
  });
  const placeholder = new THREE.Mesh(placeholderGeometry, placeholderMaterial);
  placeholder.visible = false;
  group.add(placeholder);

  const loader = new GLTFLoader();
  let disposed = false;
  let token = 0;

  let lastUrl = "";
  let errorLabel: ThreeTypes.Sprite | null = null;

  function clearErrorLabel() {
    if (!errorLabel) return;
    group.remove(errorLabel);
    errorLabel.material.map?.dispose();
    errorLabel.material.dispose();
    errorLabel = null;
  }

  function showUnavailable() {
    const canvas = document.createElement("canvas");
    canvas.width = 512;
    canvas.height = 64;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.fillStyle = "#251b1b";
    context.fillRect(0, 0, 512, 64);
    context.font = "26px sans-serif";
    context.fillStyle = "#ffffff";
    context.textAlign = "center";
    context.fillText(options?.unavailableLabel ?? "Model unavailable", 256, 42, 490);
    errorLabel = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), depthTest: false }));
    errorLabel.scale.set(2 / group.scale.x, 0.25 / group.scale.y, 1);
    errorLabel.position.y = 0.2 / group.scale.y;
    group.add(errorLabel);
  }

  let current: ThreeTypes.Object3D | null = null;
  let mixer: ThreeTypes.AnimationMixer | null = null;
  let actions: ThreeTypes.AnimationAction[] = [];
  let hasAnimations = false;
  let isAnimated = Boolean(options?.autoplay);

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
    let pendingModel: ThreeTypes.Object3D | null = null;
    try {
      const gltf = await loader.loadAsync(url);
      const model = gltf.scene || gltf.scenes?.[0];
      pendingModel = model;
      if (disposed || myToken !== token) return;
      if (!model) throw new Error("Empty model");

      model.traverse((obj) => {
        const mesh = obj as any;
        if (mesh.isMesh) {
          mesh.castShadow = true;
          mesh.receiveShadow = true;
        }
      });

      const boundingBox = validateModel(model);
      const centerVector = boundingBox.getCenter(new THREE.Vector3());
      const minY = boundingBox.min.y;
      const center = meta?.center ?? { x: centerVector.x, y: centerVector.y, z: centerVector.z };
      const lift = meta?.minY ?? minY;
      model.position.sub(new THREE.Vector3(center.x, lift, center.z));

      if (current) {
        group.remove(current);
        disposeMixer();
        disposeModel(current);
        current = null;
      }

      current = model;
      pendingModel = null;
      group.add(model);
      placeholder.visible = false;

      if (Array.isArray(gltf.animations) && gltf.animations.length > 0) {
        mixer = new THREE.AnimationMixer(model);
        actions = gltf.animations.map((clip) => mixer!.clipAction(clip));
        hasAnimations = actions.length > 0;
        applyAnimationState();
      } else {
        disposeMixer();
      }
      options?.onInvalidate?.();
    } catch (err) {
      if (disposed || myToken !== token) return;
      placeholder.visible = false;
      clearErrorLabel();
      showUnavailable();
      console.error("[models:gltfModel]", err);
      options?.onInvalidate?.();
    } finally {
      if (pendingModel) disposeModel(pendingModel);
    }
  }

  function updateFromProps(props: unknown) {
    const rec =
      props && typeof props === "object" && !Array.isArray(props) ? (props as Record<string, unknown>) : {};

    const dir = readString(rec.dir, "");
    const model = readString(rec.model, "");
    const scale = readScale(rec.scale, 1);
    const size = readVector3(rec.size, { x: 1, y: 1, z: 1 });
    const center = readVector3(rec.center, { x: 0, y: 0, z: 0 });
    const minY = readNumber(rec.min_y, 0);

    group.scale.setScalar(scale);
    placeholder.scale.set(
      Math.max(0.05, Math.abs(size.x)),
      Math.max(0.05, Math.abs(size.y)),
      Math.max(0.05, Math.abs(size.z)),
    );
    placeholder.position.y = placeholder.scale.y / 2;

    const url = dir && model ? resolveToposyncUrl(`/files/${encodeURIComponent(dir)}/${encodeURIComponent(model)}`) : "";
    if (!url) {
      token++;
      clearErrorLabel();
      lastUrl = "";
      placeholder.visible = false;
      if (current) {
        group.remove(current);
        disposeMixer();
        disposeModel(current);
        current = null;
      }
      return;
    }
    if (url && url !== lastUrl) {
      lastUrl = url;
      clearErrorLabel();
      if (current) {
        group.remove(current);
        disposeMixer();
        disposeModel(current);
        current = null;
      }
      placeholder.visible = true;
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
      if (!isAnimated || !mixer) return false;
      mixer?.update(dt);
      return true;
    },
    dispose: () => {
      disposed = true;
      clearErrorLabel();
      disposeMixer();
      if (current) disposeModel(current);
      placeholderGeometry.dispose();
      placeholderMaterial.dispose();
    },
    getHasAnimations: () => hasAnimations,
  };
}
