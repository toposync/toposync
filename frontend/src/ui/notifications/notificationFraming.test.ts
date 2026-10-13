declare const require: any;
import { NotificationFraming, fitNotificationPoints, pinPoints } from "./notificationFraming";
const test = require("node:test");
const assert = require("node:assert/strict");

test("late first evidence fits once, manual navigation survives updates, selection and remount reset", () => {
  const state = new NotificationFraming();
  state.select("yard:a");
  assert.equal(state.canFit(), true);
  state.didFit(); state.select("yard:a");
  assert.equal(state.canFit(), false);
  assert.equal(state.canFitResize, true);
  state.interact();
  assert.equal(state.canFitResize, false);
  assert.equal(state.canFit(true), true);
  assert.equal(state.preserveView, true);
  state.select("yard:b");
  assert.equal(state.canFit(), true);
  state.interact(); // User navigates before delayed coordinates arrive.
  assert.equal(state.canFit(), false);
  state.reset(); state.select("yard:b");
  assert.equal(state.canFit(), true);
});

test("fit contains all independent trails and pin, including points outside the composition", () => {
  const points = pinPoints({ x: 10000, z: -5000, trails: [[{x: -20000,z: 1000}], [{x: 3,z: 7000}]] });
  const transform = fitNotificationPoints(800, 600, points.map(point => ({x:point.x,y:point.z})))!;
  for (const point of points) {
    const x = transform.x + point.x * transform.scale;
    const y = transform.y + point.z * transform.scale;
    assert.ok(x >= 48 && x <= 752);
    assert.ok(y >= 72 && y <= 552);
  }
  const single = fitNotificationPoints(800, 600, [{x:33,y:10}])!;
  assert.ok(Number.isFinite(single.scale) && single.scale < 5);
  assert.equal(fitNotificationPoints(0, 0, points.map(point => ({x:point.x,y:point.z}))), null);
  assert.equal(fitNotificationPoints(800, 600, [{x:NaN,y:0}]), null);
});
