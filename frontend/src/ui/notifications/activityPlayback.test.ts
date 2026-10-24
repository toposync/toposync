declare const require: any;
import { imageAt, publishPlayback, sameImageScope, sameImageSource } from "./activityPlayback";
import { createActivity2DOverlay, createActivity3DOverlay, imageTrails } from "./activityOverlay";
import type { ActivityImage } from "../../util/activity";
const test = require("node:test"), assert = require("node:assert/strict");
const image = (changes: Partial<ActivityImage> = {}): ActivityImage => ({ id: "image", sequence: 1, evidence_id: "evidence", participant_id: "person", camera_id: "camera", source_id: "source", clock_domain: "clock", observed_at: 1, gap: false, position: [1, 2], coordinate_reference: "map", mapping_revision: "revision", position_error_metres: null, expires_at: Date.now() / 1000 + 60, available: true, image_href: "images/image", width: 20, height: 40, kind: "rectangle", size_bytes: 100, capture_instance: "instance", capture_generation: 1, frame_sequence: 1, rate: 2, ...changes });
test("playback holds only an observed image for a bounded interval and hides expired images", () => {
  const frames = [image(), image({ id: "later", observed_at: 10 })];
  assert.equal(imageAt(frames, 1.4)?.id, "image");
  assert.equal(imageAt(frames, 5), null);
  assert.equal(imageAt([image({ expires_at: 1 })], 1), null);
  assert.equal(imageAt([image({ available: false })], 1), null);
  for (const rate of [1, 2, 3]) {
    assert.ok(imageAt([image({ observed_at: 0, rate })], 1.5 / rate));
    assert.equal(imageAt([image({ observed_at: 0, rate })], 1.5 / rate + 0.001), null);
  }
});
test("group source matching shares the scope rules without merging different participants' trails", () => {
  const first = image(), second = image({ participant_id: "second" });
  assert.equal(sameImageSource(first, second), true);
  assert.equal(sameImageScope(first, second), false);
  for (const change of [{ camera_id: "other" }, { source_id: "other" }, { clock_domain: "other" },
    { capture_instance: "other" }, { capture_generation: 2 }, { coordinate_reference: "other" }, { mapping_revision: "other" }]) {
    assert.equal(sameImageSource(first, image(change)), false);
    assert.equal(sameImageScope(first, image(change)), false);
  }
});
test("trails split on gaps, source reset, calibration changes and missing positions", () => {
  const frames = [image(), image({ observed_at: 1.5 }), image({ observed_at: 2, gap: true }),
    image({ observed_at: 2.5, capture_generation: 2 }), image({ observed_at: 3, mapping_revision: "changed" })];
  assert.deepEqual(imageTrails(frames).map(segment => segment.length), [2, 1, 1, 1]);
});
test("map overlay follows only the selected occurrence and matching coordinate reference", () => {
  const identifier = "a".repeat(64);
  const notification = { id: "notice", type: "occurrences.activity", title: "Activity", payload: { occurrence_id: identifier } };
  let updates = 0;
  const overlay = createActivity2DOverlay({ compositionId: "map", requestRender: () => updates++ }, notification);
  assert.equal(overlay.pin(), null);
  publishPlayback({ occurrence: identifier, image: image(), url: "blob:test", trail: [image()], companions: [{ image: image({ participant_id: "second", position: [3, 4] }), url: "blob:second", trail: [] }] });
  assert.equal(overlay.pin()?.x, 1);
  assert.equal(overlay.pin()?.companions?.[0].x, 3);
  publishPlayback({ occurrence: identifier, image: image({ coordinate_reference: "other" }), url: "blob:test", trail: [] });
  assert.equal(overlay.pin(), null);
  assert.equal(updates, 2);
  overlay.dispose?.(); publishPlayback(null);
});
test("both map renderers exclude unavailable or foreign companions from pins, trails and framing", () => {
  const identifier = "a".repeat(64);
  const notification = { id: "notice", type: "occurrences.activity", title: "Activity", payload: { occurrence_id: identifier } };
  const planar = createActivity2DOverlay({ compositionId: "map" }, notification);
  const spatial = createActivity3DOverlay({ THREE: require("three"), compositionId: "map" } as any, notification);
  const original = globalThis.Image;
  (globalThis as any).Image = class { set src(_url: string) {} };
  try {
    const first = image();
    const excluded = [image({ coordinate_reference: "other" }), image({ expires_at: 1 }),
      image({ available: false }), image({ position: [NaN, 2] })];
    publishPlayback({ occurrence: identifier, image: first, url: "blob:first", trail: [first],
      companions: excluded.map(item => ({ image: item, url: "blob:excluded", trail: [item] })) });
    assert.deepEqual(planar.pin()?.companions, []);
    assert.deepEqual(planar.pin()?.trails, [[{ x: 1, z: 2 }]]);
    assert.equal(spatial.object.children.filter(child => child.type === "Sprite").length, 1);
    assert.deepEqual(spatial.bounds?.(), { minX: 1, maxX: 1, minZ: 2, maxZ: 2 });
    publishPlayback({ occurrence: identifier, image: image({ available: false }), url: "blob:missing", trail: [first] });
    assert.equal(planar.pin(), null);
    assert.equal(spatial.bounds?.(), null);
    assert.equal(spatial.object.visible, false);
  } finally { planar.dispose?.(); spatial.dispose?.(); publishPlayback(null); globalThis.Image = original; }
});
test("invalid trail samples leave a gap instead of joining unrelated coordinates", () => {
  const reference = image();
  for (const change of [{ available: false }, { expires_at: 1 }, { position: null },
    { position: [NaN, 2] as [number, number] }, { mapping_revision: "other" }]) {
    assert.deepEqual(imageTrails([reference, image({ ...change, observed_at: 1.25 }), image({ observed_at: 1.5 })], reference)
      .map(segment => segment.length), [1, 1]);
  }
});
test("three-dimensional group anchors share a frame and release replaced textures", () => {
  const original = globalThis.Image;
  (globalThis as any).Image = class { width = 20; height = 40; onload = () => {}; set src(_url: string) { this.onload(); } };
  const identifier = "a".repeat(64);
  const first = image(), second = image({ participant_id: "second", position: [3, 4] });
  const overlay = createActivity3DOverlay({ THREE: require("three"), compositionId: "map" } as any,
    { id: "notice", type: "occurrences.activity", title: "Activity", payload: { occurrence_id: identifier } });
  try {
    publishPlayback({ occurrence: identifier, image: first, url: "blob:first", trail: [first], companions: [{ image: second, url: "blob:second", trail: [second] }] });
    const sprites = overlay.object.children.filter(child => child.type === "Sprite") as any[];
    assert.equal(sprites.length, 2); assert.equal(overlay.object.visible, true);
    assert.deepEqual(sprites.map(sprite => [sprite.position.x, sprite.position.z]), [[1, 2], [3, 4]]);
    assert.deepEqual(overlay.bounds?.(), { minX: 1, maxX: 3, minZ: 2, maxZ: 4 });
    let disposed = 0;
    sprites.forEach(sprite => sprite.material.map.addEventListener("dispose", () => disposed++));
    publishPlayback(null);
    assert.equal(overlay.object.visible, false); assert.equal(disposed, 2);
  } finally { overlay.dispose?.(); globalThis.Image = original; }
});
