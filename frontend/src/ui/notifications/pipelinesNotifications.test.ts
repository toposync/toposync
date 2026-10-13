declare const require: any;
import type { Notification } from "@toposync/plugin-api";
import { builtinNotificationRenderers, renderNotificationSpatialStatus } from "./pipelinesNotifications";
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

test("individual histories preserve gaps and identities in both overlays without synthetic connections", () => {
  const notification = record("yard");
  notification.payload = { world:{x:33,z:8,composition_id:"yard"}, spatial_history:{ version:1, trails:[
    {subject_id:"a",points:[{x:0,z:0,composition_id:"yard",scope:"one"},{x:1,z:0,composition_id:"yard",scope:"one"},
      {x:2,z:1,composition_id:"yard",scope:"one",gap_before:true},{x:3,z:1,composition_id:"yard",scope:"one"}]},
    {subject_id:"b",points:[{x:5,z:0,composition_id:"yard",scope:"two"},{x:5,z:1,composition_id:"yard",scope:"two"}]},
    {subject_id:"wrong",points:[{x:1000,z:1000,composition_id:"other"}]},
  ]}};
  const pin = renderer.create2DOverlay!({compositionId:"yard"}, notification, actions)!.pin()!;
  assert.equal(pin.x, 33);
  assert.deepEqual(pin.trails, [[{x:0,z:0},{x:1,z:0}], [{x:2,z:1},{x:3,z:1}], [{x:5,z:0},{x:5,z:1}]]);
  const previous = (globalThis as any).document;
  (globalThis as any).document = { createElement: () => ({ getContext: () => null }) };
  try {
    const overlay = renderer.create3DOverlay!({compositionId:"yard",THREE:require("three")} as any, notification, actions)!;
    assert.deepEqual(overlay.bounds?.(), {minX:0,maxX:33,minZ:0,maxZ:8});
    const lines = overlay.object.children.find(child => child.type === "LineSegments") as any;
    assert.equal(lines.geometry.drawRange.count, 6);
    const attribute = lines.geometry.getAttribute("position");
    overlay.update?.(notification);
    assert.equal(lines.geometry.getAttribute("position"), attribute);
    assert.equal(lines.geometry.drawRange.count, 6);
    overlay.dispose?.();
  } finally { (globalThis as any).document = previous; }
});

test("recovered mapping keeps its explanation and legacy trails stay bounded", () => {
  const notification = record("yard");
  notification.payload = { world:{x:2,z:3,composition_id:"yard"}, spatial_status: { status:"ready", had_mapping_gaps:true,
    failure_reasons:["camera_configuration_changed"] },
    trail: Array.from({length:3000}, (_, index)=>({x:index,z:1,composition_id:"yard"})) };
  const markup = require("react-dom/server").renderToStaticMarkup(renderNotificationSpatialStatus(notification));
  assert.match(markup, /notificationSpatialStatus/);
  assert.match(markup, /Camera settings did not match the calibration/);
  const pin = renderer.create2DOverlay!({compositionId:"yard"}, notification, actions)!.pin()!;
  assert.equal(pin.trails?.[0]?.length, 512);
});
