const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const typescript = require("typescript");

const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "toposync-panorama-crop-"));
const sourceFile = path.resolve(__dirname, "../src/settings/panoramaCrop.ts");
const compiledFile = path.join(temporaryDirectory, "panoramaCrop.cjs");
fs.writeFileSync(compiledFile, typescript.transpileModule(fs.readFileSync(sourceFile, "utf8"), {
  compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022 },
}).outputText);
const { FULL_PANORAMA_CROP, adjustPanoramaCrop, panoramaCoverageCrop, panoramaPreviewCrop, panoramaCropFromCorners, panoramaCropSeamOffset, panoramaCropSegments, samePanoramaCrop, validPanoramaCrop, wrapPanoramaCoordinate } = require(compiledFile);
after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));

function close(actual, expected) { assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} should equal ${expected}`); }

test("photographed bounds fit the preview with exclusive pixel edges", () => {
  assert.deepEqual(panoramaCoverageCrop(1280, 640, { left: 160, top: 160, right: 1120, bottom: 480 }), {
    u_start: 0.125, u_width: 0.75, v_start: 0.25, v_height: 0.5,
  });
  assert.deepEqual(panoramaCoverageCrop(1280, 640, { left: 1279, top: 639, right: 1280, bottom: 640 }), {
    u_start: 1279 / 1280, u_width: 1 / 1280, v_start: 639 / 640, v_height: 1 / 640,
  });
  // A seam-crossing artifact already supplies conservative bounds. Keep their full width.
  assert.deepEqual(panoramaCoverageCrop(1280, 640, { left: 0, top: 160, right: 1280, bottom: 480 }), {
    u_start: 0, u_width: 1, v_start: 0.25, v_height: 0.5,
  });
});

test("missing or invalid photographed bounds use the whole image", () => {
  const bounds = { left: 100, top: 100, right: 800, bottom: 300 };
  for (const invalid of [null, undefined, {}, [], "bounds", { ...bounds, left: "100" },
    { ...bounds, left: NaN }, { ...bounds, top: Infinity }, { ...bounds, bottom: -Infinity },
    { ...bounds, left: -1 }, { ...bounds, top: -1 }, { ...bounds, right: 1281 },
    { ...bounds, bottom: 641 }, { ...bounds, right: 100 }, { ...bounds, bottom: 99 }]) {
    assert.deepEqual(panoramaCoverageCrop(1280, 640, invalid), FULL_PANORAMA_CROP);
  }
  for (const [width, height] of [[0, 640], [1280, 0], [-1280, 640], [NaN, 640], [1280, Infinity]]) {
    assert.deepEqual(panoramaCoverageCrop(width, height, bounds), FULL_PANORAMA_CROP);
  }
});

test("preview fit never overwrites selection coordinates and an explicitly saved full image wins", () => {
  const artifact = {
    width: 1280, height: 640, crop: { ...FULL_PANORAMA_CROP }, crop_revision: 1,
    coverage: { bounds_pixels: { left: 160, top: 160, right: 1120, bottom: 480 } },
  };
  const original = structuredClone(artifact);
  const preview = panoramaPreviewCrop(artifact);
  assert.equal(preview.u_width, 0.75);
  assert.deepEqual(artifact, original);
  const wrappedCrop = { u_start: 0.9, u_width: 0.3, v_start: 0.2, v_height: 0.6 };
  assert.deepEqual(panoramaPreviewCrop({ ...artifact, crop: wrappedCrop }), wrappedCrop);
  assert.deepEqual(panoramaPreviewCrop({ ...artifact, crop_revision: 2 }), FULL_PANORAMA_CROP);
  assert.deepEqual(panoramaPreviewCrop({ ...artifact, crop: null }), preview);
  assert.deepEqual(panoramaPreviewCrop({ ...artifact, crop: { ...FULL_PANORAMA_CROP, u_start: NaN } }), preview);
});

test("a rectangle drawn across the displayed seam retains its canonical region", () => {
  const crop = panoramaCropFromCorners({ x: 0.1, y: 0.2 }, { x: 0.4, y: 0.8 }, 0.8);
  close(crop.u_start, 0.9);
  close(crop.u_width, 0.3);
  close(crop.v_start, 0.2);
  close(crop.v_height, 0.6);
  const segments = panoramaCropSegments(crop, 0);
  assert.equal(segments.length, 2);
  close(segments[0].left, 0.9);
  close(segments[0].width, 0.1);
  close(segments[1].left, 0);
  close(segments[1].width, 0.2);
});

test("moving the presentation seam does not change saved crop coordinates", () => {
  const crop = { u_start: 0.9, u_width: 0.3, v_start: 0.2, v_height: 0.6 };
  const before = structuredClone(crop);
  const offset = panoramaCropSeamOffset(crop);
  const segments = panoramaCropSegments(crop, offset);
  assert.equal(segments.length, 1);
  close(segments[0].left, 0.35);
  close(segments[0].width, 0.3);
  assert.deepEqual(crop, before);
  const restored = panoramaCropFromCorners({ x: 0.35, y: 0.2 }, { x: 0.65, y: 0.8 }, offset);
  assert.ok(samePanoramaCrop(crop, restored));
});

test("all four drawing directions select the same area", () => {
  const expected = { u_start: 0.2, u_width: 0.6, v_start: 0.1, v_height: 0.4 };
  for (const [start, end] of [
    [{ x: 0.2, y: 0.1 }, { x: 0.8, y: 0.5 }], [{ x: 0.8, y: 0.5 }, { x: 0.2, y: 0.1 }],
    [{ x: 0.8, y: 0.1 }, { x: 0.2, y: 0.5 }], [{ x: 0.2, y: 0.5 }, { x: 0.8, y: 0.1 }],
  ]) assert.ok(samePanoramaCrop(panoramaCropFromCorners(start, end, 0), expected));
});

test("move wraps horizontally and clamps vertically without changing dimensions", () => {
  const original = { u_start: 0.95, u_width: 0.2, v_start: 0.1, v_height: 0.3 };
  const moved = adjustPanoramaCrop(original, "move", 0.1, 10);
  close(moved.u_start, 0.05);
  close(moved.v_start, 0.7);
  assert.equal(moved.u_width, original.u_width);
  assert.equal(moved.v_height, original.v_height);
  assert.equal(adjustPanoramaCrop(original, "move", 0, -10).v_start, 0);
});

test("resize preserves the opposite edge across the seam", () => {
  const original = { u_start: 0.95, u_width: 0.2, v_start: 0.1, v_height: 0.3 };
  const resized = adjustPanoramaCrop(original, "nw", 0.05, 0.1);
  close(resized.u_start, 0);
  close(resized.u_width, 0.15);
  close(wrapPanoramaCoordinate(resized.u_start + resized.u_width), wrapPanoramaCoordinate(original.u_start + original.u_width));
  close(resized.v_start + resized.v_height, original.v_start + original.v_height);
});

test("large movements never produce inverted or out-of-range rectangles", () => {
  for (const handle of ["move", "n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
    for (const horizontal of [-10, -1, -0.01, 0, 0.1, 1, 10]) {
      for (const vertical of [-10, -1, -0.01, 0, 0.1, 1, 10]) {
        assert.ok(validPanoramaCrop(adjustPanoramaCrop({ u_start: 0.9, u_width: 0.3, v_start: 0.2, v_height: 0.6 }, handle, horizontal, vertical)));
      }
    }
  }
});

test("full image and invalid persisted crops have explicit meanings", () => {
  assert.ok(validPanoramaCrop(FULL_PANORAMA_CROP));
  assert.deepEqual(panoramaCropSegments(FULL_PANORAMA_CROP, 0.8), [{ left: 0, width: 1 }]);
  assert.deepEqual(adjustPanoramaCrop(FULL_PANORAMA_CROP, "move", 0.5, 0.5), FULL_PANORAMA_CROP);
  for (const invalid of [null, {}, { ...FULL_PANORAMA_CROP, u_width: 0 }, { ...FULL_PANORAMA_CROP, v_start: 0.2 }, { ...FULL_PANORAMA_CROP, u_start: NaN }]) assert.equal(validPanoramaCrop(invalid), false);
});
