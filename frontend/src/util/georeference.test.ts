declare const require: any;
import { localToGeographic, geographicToLocal, longitudeNear, readGeoreference, type Georeference } from "./georeference";
const test = require("node:test");
const assert = require("node:assert/strict");
test("geographic control points, metre distances, rotation and inverse at equator, Brazil and antimeridian", () => {
  for (const latitude of [0, -23.55, 80]) for (const longitude of [0, 179.999]) for (const rotationDegrees of [0, 90, -37]) {
    const reference: Georeference = { version: 1, latitude, longitude, rotationDegrees, scale: 2, axes: "x-east-z-south", units: "metres" };
    for (const point of [{x:0,z:0}, {x:3,z:4}, {x:-50,z:200}]) {
      const geographic = localToGeographic(point, reference);
      const back = geographicToLocal(geographic, reference);
      assert.ok(Math.hypot(back.x-point.x, back.z-point.z) < 0.000001);
    }
  }
  const reference: Georeference = { version:1, latitude:0, longitude:0, rotationDegrees:0, scale:1, axes:"x-east-z-south", units:"metres" };
  assert.ok(Math.abs(localToGeographic({x:1000,z:0}, reference).lng - 0.008993203637) < 1e-10);
  assert.ok(localToGeographic({x:0,z:10}, reference).lat < 0);
  assert.ok(localToGeographic({x:10,z:0}, {...reference, rotationDegrees:90}).lat < 0);
  assert.equal(readGeoreference({...reference, scale:NaN}), null);
  assert.throws(() => localToGeographic({x:20001,z:0}, reference));
});
test("map scale uses the adjacent world copy across either antimeridian", () => {
  const reference: Georeference = { version:1, latitude:0, longitude:179.999999, rotationDegrees:0, scale:1.2, axes:"x-east-z-south", units:"metres" };
  const east = localToGeographic({ x:1, z:0 }, reference);
  assert.ok(east.lng < 0);
  const delta = longitudeNear(east.lng, reference.longitude) - reference.longitude;
  assert.ok(Math.abs(delta - 1.2 * 0.000008993203637) < 1e-10);
  assert.ok(Math.abs(longitudeNear(179.999999, -179.999999) + 180.000001) < 1e-10);
  assert.ok(Math.abs(longitudeNear(-46.63, -46.64) + 46.63) < 1e-10);
});
