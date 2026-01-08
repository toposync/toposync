import * as THREEStandalone from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

import { MAXIMUM_MODEL_SCALE, MINIMUM_MODEL_SCALE } from "../constants";
import { clamp } from "../parsing";
import type { ModelPreviewResult, Vector3 } from "../types";

export function stripFileExtension(filename: string): string {
  const dotIndex = filename.lastIndexOf(".");
  if (dotIndex <= 0) return filename;
  return filename.slice(0, dotIndex);
}

export function suggestInitialScale(size: Vector3): number {
  const maximumDimension = Math.max(Math.abs(size.x), Math.abs(size.y), Math.abs(size.z));
  if (!Number.isFinite(maximumDimension) || maximumDimension <= 1e-6) return 1;

  const minimumTargetMeters = 0.25;
  const oversizedThresholdMeters = 3;
  const oversizedTargetMeters = 1;

  if (maximumDimension > oversizedThresholdMeters) {
    return clamp(oversizedTargetMeters / maximumDimension, MINIMUM_MODEL_SCALE, MAXIMUM_MODEL_SCALE);
  }
  if (maximumDimension < minimumTargetMeters) {
    return clamp(minimumTargetMeters / maximumDimension, MINIMUM_MODEL_SCALE, MAXIMUM_MODEL_SCALE);
  }
  return 1;
}

function createRenderer(width: number, height: number): THREEStandalone.WebGLRenderer {
  const renderer = new THREEStandalone.WebGLRenderer({
    antialias: true,
    alpha: true,
    preserveDrawingBuffer: true,
  });
  renderer.setPixelRatio(1);
  renderer.setSize(width, height, false);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREEStandalone.SRGBColorSpace;
  return renderer;
}

function buildCamera(viewWidth: number, viewHeight: number): THREEStandalone.OrthographicCamera {
  const camera = new THREEStandalone.OrthographicCamera(
    -viewWidth / 2,
    viewWidth / 2,
    viewHeight / 2,
    -viewHeight / 2,
    0.01,
    Math.max(viewWidth, viewHeight) * 8,
  );
  camera.position.set(0, Math.max(viewWidth, viewHeight), 0);
  camera.up.set(0, 0, -1);
  camera.lookAt(new THREEStandalone.Vector3(0, 0, 0));
  return camera;
}

function addLights(scene: THREEStandalone.Scene): void {
  const hemisphereLight = new THREEStandalone.HemisphereLight(0xffffff, 0x222233, 0.9);
  scene.add(hemisphereLight);
  const directionalLight = new THREEStandalone.DirectionalLight(0xffffff, 0.8);
  directionalLight.position.set(3, 6, 3);
  directionalLight.castShadow = false;
  scene.add(directionalLight);
}

export async function generateModelTopDownPreview(
  modelUrl: string,
  options?: { renderSize?: number; paddingRatio?: number },
): Promise<ModelPreviewResult> {
  const maximumRenderSize = Math.max(128, Math.min(1024, options?.renderSize ?? 640));
  const paddingRatio = clamp(options?.paddingRatio ?? 0.08, 0, 0.5);

  const loader = new GLTFLoader();
  const gltf = await loader.loadAsync(modelUrl);
  const model = gltf.scene || gltf.scenes?.[0];
  if (!model) throw new Error("Empty model");

  const workingModel = model.clone(true);
  workingModel.updateMatrixWorld(true);

  const boundingBox = new THREEStandalone.Box3().setFromObject(workingModel);
  const sizeVector = boundingBox.getSize(new THREEStandalone.Vector3());
  const centerVector = boundingBox.getCenter(new THREEStandalone.Vector3());
  const minimumY = boundingBox.min.y;

  if (
    !Number.isFinite(sizeVector.x) ||
    !Number.isFinite(sizeVector.y) ||
    !Number.isFinite(sizeVector.z) ||
    sizeVector.length() < 1e-6
  ) {
    throw new Error("Could not compute model size");
  }

  workingModel.position.sub(new THREEStandalone.Vector3(centerVector.x, minimumY, centerVector.z));

  const safeX = Math.max(sizeVector.x, 1e-6);
  const safeZ = Math.max(sizeVector.z, 1e-6);
  const viewWidth = safeX * (1 + paddingRatio * 2);
  const viewHeight = safeZ * (1 + paddingRatio * 2);

  const renderScale = maximumRenderSize / Math.max(viewWidth, viewHeight);
  const renderWidth = Math.max(64, Math.round(viewWidth * renderScale));
  const renderHeight = Math.max(64, Math.round(viewHeight * renderScale));

  const camera = buildCamera(viewWidth || 1, viewHeight || 1);
  const renderer = createRenderer(renderWidth, renderHeight);
  const scene = new THREEStandalone.Scene();
  addLights(scene);
  scene.add(workingModel);

  renderer.render(scene, camera);

  const cropScaleX = safeX / Math.max(viewWidth, 1e-9);
  const cropScaleZ = safeZ / Math.max(viewHeight, 1e-9);
  const cropWidth = Math.max(1, Math.min(renderWidth, Math.round(renderWidth * cropScaleX)));
  const cropHeight = Math.max(1, Math.min(renderHeight, Math.round(renderHeight * cropScaleZ)));
  const cropX = Math.max(0, Math.min(renderWidth - cropWidth, Math.round((renderWidth - cropWidth) / 2)));
  const cropY = Math.max(0, Math.min(renderHeight - cropHeight, Math.round((renderHeight - cropHeight) / 2)));

  let outputWidth = renderWidth;
  let outputHeight = renderHeight;
  let dataUrl = "";
  if (cropWidth !== renderWidth || cropHeight !== renderHeight) {
    const outputCanvas = document.createElement("canvas");
    outputCanvas.width = cropWidth;
    outputCanvas.height = cropHeight;
    const outputContext = outputCanvas.getContext("2d");
    if (outputContext) {
      outputContext.drawImage(renderer.domElement, cropX, cropY, cropWidth, cropHeight, 0, 0, cropWidth, cropHeight);
      dataUrl = outputCanvas.toDataURL("image/png");
      outputWidth = cropWidth;
      outputHeight = cropHeight;
    }
  }
  if (!dataUrl) dataUrl = renderer.domElement.toDataURL("image/png");

  try {
    renderer.dispose();
  } catch {
    // ignore
  }

  return {
    dataUrl,
    widthPx: outputWidth,
    heightPx: outputHeight,
    size: { x: sizeVector.x, y: sizeVector.y, z: sizeVector.z },
    center: { x: centerVector.x, y: centerVector.y, z: centerVector.z },
    minY: minimumY,
  };
}

