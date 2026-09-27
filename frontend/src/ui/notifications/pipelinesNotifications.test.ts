declare const require: any;
import type { Notification } from "@toposync/plugin-api";
import { builtinNotificationRenderers } from "./pipelinesNotifications";
const test = require("node:test");
const assert = require("node:assert/strict");
const renderer = builtinNotificationRenderers[0];
const actions = { openImage: () => undefined };
function record(compositionId?: string): Notification {
  return { id:"event", type:"pipelines.event", title:"Recorded", payload:{ world:{ x:2,z:3,composition_id:compositionId } } };
}
test("recorded map pins require explicit matching composition, including updates and every trail point", () => {
  for (const scope of [undefined, "other"]) {
    assert.equal(renderer.create2DOverlay!({ compositionId:"yard" }, record(scope), actions), null);
    assert.equal(renderer.create3DOverlay!({ compositionId:"yard" } as any, record(scope), actions), null);
  }
  assert.equal(renderer.create2DOverlay!({}, record("yard"), actions), null);
  const overlay = renderer.create2DOverlay!({ compositionId:"yard" }, record("yard"), actions)!;
  assert.equal(overlay.pin()?.x, 2);
  overlay.update?.(record("other"));
  assert.equal(overlay.pin(), null);
  const mixed = record("yard");
  mixed.payload = { trail:[{x:1,z:1,composition_id:"other"},{x:2,z:3,composition_id:"yard"}] };
  overlay.update?.(mixed);
  assert.equal(overlay.pin(), null);
  overlay.update?.(record("yard"));
  assert.equal(overlay.pin()?.z, 3);
});
test("3D overlay hides stale position when update loses composition and resumes only with matching evidence", () => {
  const previous = (globalThis as any).document;
  (globalThis as any).document = { createElement: () => ({ getContext: () => null }) };
  try {
    const overlay = renderer.create3DOverlay!({ compositionId:"yard", THREE:require("three") } as any, record("yard"), actions)!;
    assert.equal(overlay.object.visible, true);
    overlay.update?.(record());
    assert.equal(overlay.object.visible, false);
    assert.equal(overlay.tick?.(0.016), false);
    overlay.update?.(record("other"));
    assert.equal(overlay.object.visible, false);
    overlay.update?.(record("yard"));
    assert.equal(overlay.object.visible, true);
    overlay.dispose?.();
  } finally { (globalThis as any).document = previous; }
});
