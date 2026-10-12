const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const { Vector2 } = require("three");

function load(file, requireModule, globals = {}) {
  const context = { exports: {}, require: requireModule, ...globals };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.resolve(__dirname, file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, context);
  return context.exports;
}
const geometry = load("../src/util/georeference.ts");
const projection = load("../src/ui/geographic/projection.ts", name => name === "three" ? { Vector2 } : geometry, { window: { devicePixelRatio: 2 } });
function mapAt(latitude, longitude, zoom) {
  const size = 256 * 2 ** zoom;
  const project = ([lat, lng]) => new Vector2(size * (lng + 180) / 360, size * (1 - Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) / Math.PI) / 2);
  const origin = project([latitude, longitude]).sub(new Vector2(400, 300));
  return {
    project, getCenter: () => ({ lng: longitude }), getSize: () => ({ x: 800, y: 600 }),
    latLngToContainerPoint: point => project(point).sub(origin),
    containerPointToLatLng: ([x, y]) => {
      const lng = (x + origin.x) / size * 360 - 180;
      const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * (y + origin.y) / size))) * 180 / Math.PI;
      return { lat, lng, wrap() { return { lng: ((lng + 540) % 360) - 180 }; } };
    },
  };
}
const reference = { version: 1, latitude: -27.6, longitude: -48.5, rotationDegrees: 0, scale: 1, axes: "x-east-z-south", units: "metres" };

test("canvas vertices, markers and inverse clicks share exact geographic projection", () => {
  for (const latitude of [0, -27.6, 80]) for (const longitude of [-48.5, 179.9999]) for (const rotationDegrees of [0, 90, -37]) for (const zoom of [19, 22]) {
    const ref = { ...reference, latitude, longitude, rotationDegrees, scale: 1.2 };
    const map = mapAt(latitude, longitude, zoom);
    const viewport = projection.geographicViewport(map, ref, {});
    const anchor = projection.projectGeographicPoint(map, ref, { x: 0, z: 0 });
    const angle = rotationDegrees * Math.PI / 180;
    for (const point of [{ x: 0, z: 0 }, { x: 30, z: -40 }, { x: -250, z: 180 }]) {
      const drawn = viewport.worldToScreen(point);
      const reconstructed = new Vector2(anchor.x + drawn.x * Math.cos(angle) - drawn.y * Math.sin(angle), anchor.y + drawn.x * Math.sin(angle) + drawn.y * Math.cos(angle));
      const marker = projection.projectGeographicPoint(map, ref, point);
      assert.ok(marker.distanceTo(reconstructed) < 1e-7);
      const inverse = viewport.screenToWorld(drawn);
      assert.ok(Math.hypot(inverse.x - point.x, inverse.z - point.z) < 0.000001);
    }
  }
  const map = mapAt(reference.latitude, reference.longitude, 22);
  assert.ok(projection.projectGeographicPoint(map, reference, { x: 0.1, z: 0 }).distanceTo(projection.projectGeographicPoint(map, reference, { x: 0, z: 0 })) > 1);
});

test("invalid trail positions split segments instead of bridging missing evidence", () => {
  const map = mapAt(reference.latitude, reference.longitude, 22);
  const segments = projection.splitGeographicTrail(map, reference, [{ x: 0, z: 0 }, { x: 1, z: 0 }, { x: NaN, z: 0 }, { x: 3, z: 0 }, { x: 4, z: 0 }]);
  assert.equal(segments.length, 2);
  assert.equal(segments[0].length, 2);
  assert.equal(segments[1].length, 2);
  assert.throws(() => projection.projectGeographicPoint(map, reference, { x: 20001, z: 0 }));
  assert.throws(() => projection.projectLocation(map, { lat: 86, lng: 0 }));
});

test("permissions are service and installation scoped, with shared session fallback", () => {
  const stored = new Map(); let basePath = "/"; let blocked = false;
  const globals = { localStorage: { getItem: key => stored.get(key) ?? null, setItem: (key, value) => { if (blocked) throw Error("blocked"); stored.set(key, value); } }, window: { dispatchEvent() {} }, Event };
  const consent = load("../src/ui/geographic/mapConsent.ts", name => name === "react" ? {} : { getToposyncBasePath: () => basePath }, globals);
  const key = consent.mapConsentKey("background:openstreetmap");
  assert.equal(consent.readMapConsent(key), null);
  assert.equal(consent.saveMapConsent(key, true), true);
  assert.equal(consent.readMapConsent(key), true);
  assert.equal(consent.readMapConsent(consent.mapConsentKey("search:nominatim")), null);
  assert.equal(consent.readMapConsent(consent.mapConsentKey("background:another")), null);
  basePath = "/api/hassio_ingress/another";
  assert.equal(consent.readMapConsent(consent.mapConsentKey("background:openstreetmap")), null);
  blocked = true;
  assert.equal(consent.saveMapConsent(key, false), false);
  assert.equal(consent.readMapConsent(key), false);
});
