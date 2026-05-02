declare const require: any;

import type { CompositionElement, ElementType } from "@toposync/plugin-api";

import { buildMain2DSignatureElements, clusterMain2DMarkers, computeMain2DBounds, stableStringify } from "./shared";
import { computeMain2DEffectDeltaCrop, type Main2DEffectPixelBuffer } from "./vectorEffectCache";

const test: (name: string, fn: () => void | Promise<void>) => void = require("node:test").test;
const assert: any = require("node:assert/strict");

function element(id: string, type: string, x: number, z: number, props: Record<string, unknown> = {}): CompositionElement {
  return {
    id,
    type,
    name: id,
    position: { x, y: 0, z },
    rotation: { x: 0, y: 0, z: 0 },
    props,
  };
}

function buffer(width: number, height: number, pixels?: Array<[number, number, number, number]>): Main2DEffectPixelBuffer {
  const data = new Uint8ClampedArray(width * height * 4);
  pixels?.forEach((pixel, index) => {
    data.set(pixel, index * 4);
  });
  return { width, height, data };
}

test("stableStringify keeps signatures stable regardless of object key order", () => {
  const left = stableStringify({ b: 2, a: { d: 4, c: 3 } });
  const right = stableStringify({ a: { c: 3, d: 4 }, b: 2 });
  assert.equal(left, right);
});

test("computeMain2DBounds uses vector contracts and falls back for unknown extensions", () => {
  const customType: ElementType = {
    type: "custom",
    name: "Custom",
    getMain2DBounds: () => ({ minX: -2, maxX: -1, minZ: 3, maxZ: 4 }),
  };
  const bounds = computeMain2DBounds(
    [element("a", "custom", 20, 20), element("b", "fallback", 8, 9)],
    { custom: customType },
  );

  assert.deepEqual(bounds, { minX: -2, maxX: 8.35, minZ: 3, maxZ: 9.35 });
});

test("clusterMain2DMarkers clusters by screen distance after transform", () => {
  const entries = clusterMain2DMarkers({
    markers: [
      { id: "a", elementId: "a", title: "A", x: 0, z: 0, stageX: 10, stageY: 10 },
      { id: "b", elementId: "b", title: "B", x: 0, z: 0, stageX: 16, stageY: 14 },
      { id: "c", elementId: "c", title: "C", x: 0, z: 0, stageX: 80, stageY: 80 },
    ],
    transform: { scale: 2, x: 5, y: 7 },
    thresholdPx: 16,
    clusterTitle: (count) => `${count} items`,
  });

  const cluster = entries.find((entry) => entry.kind === "cluster");
  const singles = entries.filter((entry) => entry.kind === "single");
  assert.equal(cluster?.kind, "cluster");
  assert.deepEqual(cluster?.markers.map((marker) => marker.id), ["a", "b"]);
  assert.equal(singles.length, 1);
  assert.equal(singles[0].id, "c");
});

test("buildMain2DSignatureElements sorts elements by id", () => {
  const signature = buildMain2DSignatureElements([element("z", "box", 0, 0), element("a", "box", 1, 1)]);
  assert.deepEqual(
    signature.map((entry: any) => entry.id),
    ["a", "z"],
  );
});

test("computeMain2DEffectDeltaCrop returns a padded positive delta crop", () => {
  const base = buffer(3, 3, Array.from({ length: 9 }, () => [10, 10, 10, 255]));
  const active = buffer(3, 3, Array.from({ length: 9 }, () => [10, 10, 10, 255]));
  active.data.set([90, 30, 10, 255], 4 * 4);

  const delta = computeMain2DEffectDeltaCrop(base, active);
  assert.deepEqual(delta?.crop, { x: 0, y: 0, width: 3, height: 3 });
  assert.equal(delta?.data[4 * 4], 90);
  assert.equal(delta?.data[4 * 4 + 1], 30);
  assert.equal(delta?.data[4 * 4 + 2], 10);
  assert.ok((delta?.data[4 * 4 + 3] ?? 0) > 0);
});

test("computeMain2DEffectDeltaCrop returns null when nothing changed", () => {
  const base = buffer(2, 2, Array.from({ length: 4 }, () => [10, 10, 10, 255]));
  const active = buffer(2, 2, Array.from({ length: 4 }, () => [10, 10, 10, 255]));
  assert.equal(computeMain2DEffectDeltaCrop(base, active), null);
});
