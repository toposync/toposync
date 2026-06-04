declare const require: any;

import type { CompositionElement, ElementType } from "@toposync/plugin-api";

import {
  buildMain2DSignatureElements,
  clusterMain2DMarkers,
  computeFitTransform,
  computeMain2DBounds,
  computeMain2DVectorViewBox,
  stableStringify,
} from "./shared";
import { computeMain2DEffectDeltaCrop, type Main2DEffectPixelBuffer } from "./effectDelta";

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

function compositeSourceOver(base: number, overlay: number, alpha: number): number {
  const a = alpha / 255;
  return overlay * a + base * (1 - a);
}

function compositeScreen(base: number, overlay: number, alpha: number): number {
  const a = alpha / 255;
  return base + (255 - base) * (overlay / 255) * a;
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

test("computeMain2DVectorViewBox maps the pan zoom transform back to world coordinates", () => {
  const viewBox = computeMain2DVectorViewBox({
    bounds: { minX: 0, maxX: 10, minZ: 0, maxZ: 20 },
    stageWidth: 1000,
    stageHeight: 2000,
    viewportWidth: 500,
    viewportHeight: 500,
    transform: { scale: 2, x: -100, y: 50 },
  });

  assert.deepEqual(viewBox, { minX: 0.5, maxX: 3, minZ: -0.25, maxZ: 2.25 });
});

test("computeFitTransform remains finite for hidden containers", () => {
  const transform = computeFitTransform(0, 0, 1000, 1000);
  assert.equal(Number.isFinite(transform.scale), true);
  assert.equal(Number.isFinite(transform.x), true);
  assert.equal(Number.isFinite(transform.y), true);
  assert.equal(transform.scale > 0, true);
});

test("buildMain2DSignatureElements sorts elements by id", () => {
  const signature = buildMain2DSignatureElements([element("z", "box", 0, 0), element("a", "box", 1, 1)]);
  assert.deepEqual(
    signature.map((entry: any) => entry.id),
    ["a", "z"],
  );
});

test("computeMain2DEffectDeltaCrop returns a source-over positive delta crop", () => {
  const base = buffer(3, 3, Array.from({ length: 9 }, () => [10, 10, 10, 255]));
  const active = buffer(3, 3, Array.from({ length: 9 }, () => [10, 10, 10, 255]));
  active.data.set([90, 30, 10, 255], 4 * 4);

  const delta = computeMain2DEffectDeltaCrop(base, active);
  const idx = 4 * 4;
  assert.deepEqual(delta?.crop, { x: 0, y: 0, width: 3, height: 3 });
  assert.ok((delta?.data[idx + 3] ?? 0) > 0);
  assert.ok(Math.abs(compositeSourceOver(10, delta?.data[idx] ?? 0, delta?.data[idx + 3] ?? 0) - 90) <= 1);
  assert.ok(Math.abs(compositeSourceOver(10, delta?.data[idx + 1] ?? 0, delta?.data[idx + 3] ?? 0) - 30) <= 1);
  assert.ok(Math.abs(compositeSourceOver(10, delta?.data[idx + 2] ?? 0, delta?.data[idx + 3] ?? 0) - 10) <= 1);
});

test("computeMain2DEffectDeltaCrop can encode light deltas for screen blending without dark channels", () => {
  const base = buffer(1, 1, [[80, 90, 100, 255]]);
  const active = buffer(1, 1, [[120, 105, 90, 255]]);

  const delta = computeMain2DEffectDeltaCrop(base, active, { blendMode: "screen" });
  assert.deepEqual(delta?.crop, { x: 0, y: 0, width: 1, height: 1 });
  assert.ok((delta?.data[3] ?? 0) > 0);
  assert.equal(delta?.data[2], 0);
  assert.ok(Math.abs(compositeScreen(80, delta?.data[0] ?? 0, delta?.data[3] ?? 0) - 120) <= 1);
  assert.ok(Math.abs(compositeScreen(90, delta?.data[1] ?? 0, delta?.data[3] ?? 0) - 105) <= 1);
  assert.ok(Math.abs(compositeScreen(100, delta?.data[2] ?? 0, delta?.data[3] ?? 0) - 100) <= 1);
});

test("computeMain2DEffectDeltaCrop returns null when nothing changed", () => {
  const base = buffer(2, 2, Array.from({ length: 4 }, () => [10, 10, 10, 255]));
  const active = buffer(2, 2, Array.from({ length: 4 }, () => [10, 10, 10, 255]));
  assert.equal(computeMain2DEffectDeltaCrop(base, active), null);
});
