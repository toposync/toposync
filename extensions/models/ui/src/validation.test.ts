import { strict as assert } from "node:assert";
import { test } from "node:test";
import * as THREE from "three";
import { scaleFromKnownHeight } from "./parsing";
import { disposeModel, MAXIMUM_TEXTURE_PIXELS, MAXIMUM_MODEL_VERTICES, validateModel } from "./validation";

test("known height preserves proportions and floor contact after serialization", () => {
  const object = new THREE.Mesh(new THREE.BoxGeometry(2, 4, 6));
  const bounds = validateModel(object);
  const scale = scaleFromKnownHeight(4, 1.6);
  assert.equal(scale, 0.4);
  const persisted = JSON.parse(JSON.stringify({ scale, min_y: bounds.min.y }));
  object.position.y -= persisted.min_y;
  const group = new THREE.Group();
  group.add(object);
  group.scale.setScalar(persisted.scale);
  const restored = validateModel(group);
  assert.equal(restored.min.y, 0);
  assert.deepEqual(restored.getSize(new THREE.Vector3()).toArray(), [0.8, 1.6, 2.4000000000000004]);
  for (const height of [0, -1, NaN, Infinity, 1e9]) assert.equal(scaleFromKnownHeight(4, height), null);
  assert.equal(scaleFromKnownHeight(0, 1), null);
  disposeModel(group);
});

test("invalid geometry and excessive decoded textures are rejected before rendering", () => {
  const object = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial());
  const positions = object.geometry.getAttribute("position");
  const vertexCount = positions.count;
  Object.defineProperty(positions, "count", { value: MAXIMUM_MODEL_VERTICES + 1, configurable: true });
  assert.throws(() => validateModel(object), /million vertices/);
  Object.defineProperty(positions, "count", { value: vertexCount, configurable: true });
  positions.setX(0, Infinity);
  assert.throws(() => validateModel(object), /non-finite vertex/);
  positions.setX(0, 0.5);
  object.position.x = NaN;
  assert.throws(() => validateModel(object), /non-finite transform/);
  object.position.x = 0;
  object.material.map = new THREE.Texture();
  assert.throws(() => validateModel(object), /Invalid model texture/);
  object.material.map.image = { width: Infinity, height: 2 };
  assert.throws(() => validateModel(object), /Invalid model texture/);
  object.material.map.image = { width: MAXIMUM_TEXTURE_PIXELS, height: 2 };
  assert.throws(() => validateModel(object), /textures exceed/);
  assert.throws(() => validateModel(new THREE.Group()), /no finite geometry/);
  disposeModel(object);
});

test("a rejected replacement removes the previous model and displays unavailable", async () => {
  const { GLTFLoader } = await import("three/examples/jsm/loaders/GLTFLoader.js");
  const { createGltfModelRuntime } = await import("./runtime/gltfModel");
  const originalLoad = GLTFLoader.prototype.loadAsync;
  const originalDocument = globalThis.document;
  const originalError = console.error;
  const labels: string[] = [];
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    createElement: () => ({ width: 0, height: 0, getContext: () => ({ fillRect() {}, fillText(text: string) { labels.push(text); } }) }),
  } });
  console.error = () => {};
  const scene = new THREE.Group();
  scene.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1)));
  GLTFLoader.prototype.loadAsync = async (url) => {
    if (url.includes("invalid")) throw new Error("Invalid geometry");
    return { scene, scenes: [scene], animations: [] } as any;
  };
  const runtime = createGltfModelRuntime(THREE, { unavailableLabel: "Modelo indisponível" });
  try {
    runtime.updateFromProps({ dir: "test", model: "valid.gltf" });
    await new Promise(setImmediate);
    assert.ok(runtime.object.children.includes(scene));
    runtime.updateFromProps({ dir: "test", model: "invalid.gltf" });
    assert.ok(!runtime.object.children.includes(scene));
    await new Promise(setImmediate);
    assert.deepEqual(labels, ["Modelo indisponível"]);
    assert.equal(runtime.object.children.filter((child) => child.visible).length, 1);
  } finally {
    runtime.dispose();
    GLTFLoader.prototype.loadAsync = originalLoad;
    Object.defineProperty(globalThis, "document", { configurable: true, value: originalDocument });
    console.error = originalError;
  }
});
