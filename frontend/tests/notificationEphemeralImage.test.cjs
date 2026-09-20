const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/ui/notifications/ephemeralImage.ts");
const context = { exports: {}, atob, performance, Date, setTimeout, clearTimeout };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText, context);
const { createEphemeralImageSession, withoutEphemeralImage, attachEphemeralImage } = context.exports;

test("stripping an absent image preserves identity for unrelated application renders", () => {
  const notification = { id: "plain", type: "fixture", title: "Plain" };
  assert.equal(withoutEphemeralImage(notification), notification);
  assert.equal(attachEphemeralImage(notification, null), notification);
});
const clone = (x) => JSON.parse(JSON.stringify(x));

// Small real PNG, two by two pixels, matching the encoder's minimum dimensions.
const png = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAC0lEQVR4nGNgQAYAAA4AAamRc7EAAAAASUVORK5CYII=";
function notification(sequence = 1, published = 1000) {
  const captureEvidence = { capture_instance: "decoder", generation: 1, sequence,
    published_at: published, physical_timestamp_verified: false };
  return { id: "selected", type: "fixture", title: "Human", payload: {
    packet_id: `packet-${sequence}`, parent_packet_id: "source-packet", realtime: true, status: "open", lifecycle: "update",
  }, ephemeralImage: { schemaVersion: 1, packetId: `packet-${sequence}`, parentPacketId: "source-packet",
    cameraId: "camera", sourceStreamId: "source", artifactName: "frame", mediaTimestamp: sequence,
    captureEvidence, imageGeometry: { source_size: [2, 2], image_size: [2, 2],
      to_source: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], capture_evidence: clone(captureEvidence) },
    width: 2, height: 2, mimeType: "image/png", dataBase64: png, expiresAt: published * 1000 + 750 } };
}
function harness() {
  let wall = 1000000, mono = 0, lease = null, timerId = 0;
  const timers = new Map();
  const session = createEphemeralImageSession("selected", (next) => { lease = next; }, {
    wallNow: () => wall, monotonicNow: () => mono,
    setTimer: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, due: mono + delay }); return id; },
    clearTimer: (id) => timers.delete(id),
  });
  return { session, timers, lease: () => lease,
    time: (w, m) => { wall = w; mono = m; },
    detail: (n) => attachEphemeralImage(n, lease, wall, mono),
    due: () => { for (const [id, timer] of [...timers]) if (timer.due <= mono) { timers.delete(id); timer.callback(); } },
  };
}

test("default clock invokes browser timers without rebinding them to the clock object", () => {
  let lease = null, scheduled = 0, cleared = 0;
  const originalSet = context.setTimeout, originalClear = context.clearTimeout;
  context.setTimeout = function () {
    'use strict';
    assert.equal(this, undefined, "Window timers reject an arbitrary receiver");
    scheduled += 1;
    return 1;
  };
  context.clearTimeout = function () {
    'use strict';
    assert.equal(this, undefined);
    cleared += 1;
  };
  try {
    const session = createEphemeralImageSession("selected", next => { lease = next; });
    session.receive(notification(1, Date.now() / 1000));
    assert.ok(lease);
    assert.equal(scheduled, 1);
    session.dispose();
    assert.equal(cleared, 1);
    assert.equal(lease, null);
  } finally { context.setTimeout = originalSet; context.clearTimeout = originalClear; }
});

test("selected pixels are separate from list/history and only attach to the exact packet", () => {
  const h = harness(), n = notification();
  h.session.receive(n);
  assert.equal(h.detail(withoutEphemeralImage(n)).ephemeralImage.packetId, "packet-1");
  assert.equal(withoutEphemeralImage(n).ephemeralImage, undefined);
  assert.ok(n.ephemeralImage, "strip must not mutate SSE data");
  for (const mutate of [n => n.id = "other", n => n.payload.packet_id = "other", n => n.payload.parent_packet_id = null]) {
    const other = clone(n); mutate(other);
    assert.equal(h.detail(other).ephemeralImage, undefined);
  }
  h.session.clear();
  assert.equal(h.detail(n).ephemeralImage, undefined, "even raw historical input cannot restore pixels");
});

test("same capture cannot renew its lease or reappear after reconnect/error/expiry", () => {
  const h = harness(), n = notification(); h.session.receive(n);
  const initial = h.lease();
  h.time(1000200, 200); h.session.receive(clone(n));
  assert.equal(h.lease(), initial);
  h.session.clear(); h.session.receive(n);
  assert.equal(h.lease(), null);
  const renewed = clone(n); renewed.ephemeralImage.captureEvidence.published_at = 1000.2;
  renewed.ephemeralImage.imageGeometry.capture_evidence.published_at = 1000.2;
  renewed.ephemeralImage.expiresAt = 1000950;
  h.session.receive(renewed); assert.equal(h.lease(), null);
  h.session.receive(notification(2, 1000.2)); assert.ok(h.lease());
});

test("monotonic timer expires with wall rollback; forward wall jump also hides immediately", () => {
  const h = harness(), n = notification(); h.session.receive(n);
  h.time(1000001, 750); assert.equal(h.detail(n).ephemeralImage, undefined);
  h.due(); assert.equal(h.lease(), null);
  h.session.receive(n); assert.equal(h.lease(), null);
  const other = harness(); other.session.receive(n); other.time(1000800, 1);
  assert.equal(other.detail(n).ephemeralImage, undefined);
});

test("close is terminal, disposal ignores late SSE, and unselected identities are ignored", () => {
  for (const action of ["close", "dispose"]) {
    const h = harness(), n = notification(); h.session.receive(n);
    if (action === "dispose") h.session.dispose();
    else h.session.observe({ ...n, payload: { ...n.payload, lifecycle: "close" } });
    h.time(1000100, 100); h.session.receive(notification(2, 1000.1));
    assert.equal(h.lease(), null); assert.equal(h.timers.size, 0);
  }
  const h = harness(), wrong = notification(); wrong.id = "other"; h.session.receive(wrong);
  assert.equal(h.lease(), null);
});

test("older media/sequence/generation cannot replace the latest capture", () => {
  for (const field of ["sequence", "generation", "mediaTimestamp"]) {
    const h = harness(); h.session.receive(notification(2)); h.time(1000100, 100);
    const old = notification(3, 1000.1);
    if (field === "mediaTimestamp") old.ephemeralImage.mediaTimestamp = 1;
    else old.ephemeralImage.captureEvidence[field] = 0;
    old.ephemeralImage.imageGeometry.capture_evidence = clone(old.ephemeralImage.captureEvidence);
    h.session.receive(old); assert.equal(h.lease(), null, field);
  }
});

test("real JPEG and WebP encoded dimensions are accepted without changing bytes", () => {
  const fixtures = {
    "image/jpeg": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5/ooooA//2Q==",
    "image/webp": "UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoCAAIAAUAmJaQAA3AA/v02aAA=",
  };
  for (const [mimeType, dataBase64] of Object.entries(fixtures)) {
    const h = harness(), n = notification(); Object.assign(n.ephemeralImage, { mimeType, dataBase64 });
    h.session.receive(n); assert.equal(h.lease().image.dataBase64, dataBase64);
  }
});

test("actual App list/page reducers strip pixels even if a server includes them", () => {
  const source = ts.createSourceFile("App.tsx", fs.readFileSync(path.resolve(__dirname, "../src/ui/App.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const functions = ["upsertNotificationInState", "mergeNotificationPage"].map(name => source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name).getText(source));
  const app = { withoutEphemeralImage, notificationMatchesFilter: () => true,
    sortNotificationIdsByCreatedDesc: ids => [...ids] };
  vm.runInNewContext(ts.transpileModule(functions.join("\n"), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, app);
  const n = notification(), poisoned = { byId: { selected: n }, visibleIds: ["selected"] };
  for (const state of [app.upsertNotificationInState(poisoned, n, {}), app.mergeNotificationPage(poisoned, [n], { replaceVisible: true, filter: {} })]) {
    assert.equal(state.byId.selected.ephemeralImage, undefined);
    assert.ok(!JSON.stringify(state).includes(png));
  }
});

function selectedStreamHarness() {
  const source = ts.createSourceFile("App.tsx", fs.readFileSync(path.resolve(__dirname, "../src/ui/App.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect, upsert;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(source) === "useEffect"
      && node.arguments[0]?.getText(source).includes("/stream?include_ephemeral_image=true")) effect = node.arguments[0].getText(source);
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === "upsertNotification") upsert = node.initializer.arguments[0].getText(source);
    ts.forEachChild(node, visit);
  }
  visit(source); assert.ok(effect); assert.ok(upsert);
  const reducer = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "upsertNotificationInState").getText(source);
  let state = { byId: {}, visibleIds: [] }, lease = null, resolveHistory, eventSource, timerId = 0;
  const timers = new Map();
  const app = { AbortController, console: { warn() {} }, backendAvailable: true, activeNotificationId: "selected",
    withoutEphemeralImage, notificationMatchesFilter: () => true, sortNotificationIdsByCreatedDesc: ids => [...ids],
    notificationsFilterRef: { current: {} }, activeImageSessionRef: { current: null }, activeNotificationFetchAbortRef: { current: null },
    setNotificationsState: updater => { state = updater(state); },
    setActiveImageLease: next => { lease = typeof next === "function" ? next(lease) : next; },
    createEphemeralImageSession: (id, changed) => createEphemeralImageSession(id, changed, {
      wallNow: () => 1000000, monotonicNow: () => 0,
      setTimer: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
      clearTimer: id => timers.delete(id),
    }),
    getNotification: () => new Promise(resolve => { resolveHistory = resolve; }),
    isAbortError: error => error?.name === "AbortError",
    resolveToposyncUrl: url => `/ingress/test${url}`,
    EventSource: class { constructor(url) { this.url = url; eventSource = this; } close() { this.closed = true; } },
  };
  vm.runInNewContext(ts.transpileModule(`${reducer}\nconst upsertNotification = ${upsert};\nthis.executeEffect = ${effect};\nthis.upsert = upsertNotification;`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, app);
  const cleanup = app.executeEffect();
  return { app, cleanup, timers, eventSource, state: () => state, lease: () => lease,
    history: n => resolveHistory(n), send: n => eventSource.onmessage({ data: JSON.stringify({ notification: n }) }) };
}

test("actual selected SSE effect opts in via ingress, never caches bytes, and clears on reconnect/error", async () => {
  const h = selectedStreamHarness(), n = notification();
  assert.equal(h.eventSource.url, "/ingress/test/api/notifications/selected/stream?include_ephemeral_image=true");
  h.eventSource.onopen(); h.send(n); assert.ok(h.lease());
  assert.equal(h.state().byId.selected.ephemeralImage, undefined);
  h.eventSource.onerror({}); assert.equal(h.lease(), null);
  h.send(n); assert.equal(h.lease(), null, "repeated capture after error cannot restore pixels");
  h.history(n); await Promise.resolve(); await Promise.resolve();
  assert.equal(h.lease(), null, "HTTP response cannot restore historical pixels");
  assert.equal(h.state().byId.selected.ephemeralImage, undefined);
  h.send(notification(2)); assert.ok(h.lease());
  h.eventSource.onopen(); assert.equal(h.lease(), null);
  h.send(notification(3)); assert.ok(h.lease());
  h.cleanup(); assert.equal(h.lease(), null); assert.equal(h.timers.size, 0); assert.equal(h.eventSource.closed, true);
  h.send(notification(4)); assert.equal(h.lease(), null, "late callback after selection cleanup is ignored");
});

test("actual selected SSE effect drops malformed/oversized events and global CLOSE cancels its image", () => {
  const h = selectedStreamHarness(); h.send(notification()); assert.ok(h.lease());
  h.eventSource.onmessage({ data: "{" }); assert.equal(h.lease(), null);
  h.send(notification(2)); assert.ok(h.lease());
  h.eventSource.onmessage({ data: "x".repeat(512 * 1024 + 1) }); assert.equal(h.lease(), null);
  h.send(notification(3)); assert.ok(h.lease());
  const closed = notification(3); closed.payload.lifecycle = "close";
  h.app.upsert(closed, "update"); assert.equal(h.lease(), null);
  h.send(notification(4)); assert.equal(h.lease(), null);
  h.cleanup();
});

for (const [name, mutate] of [
  ["expired", n => n.ephemeralImage.expiresAt = 999999],
  ["renewed duration", n => n.ephemeralImage.expiresAt = 1000751],
  ["future publication", n => n.ephemeralImage.captureEvidence.published_at = 1001],
  ["wrong schema", n => n.ephemeralImage.schemaVersion = 2],
  ["wrong packet", n => n.ephemeralImage.packetId = "foreign"],
  ["wrong parent", n => n.ephemeralImage.parentPacketId = null],
  ["wrong geometry capture", n => n.ephemeralImage.imageGeometry.capture_evidence.sequence = 3],
  ["singular geometry", n => n.ephemeralImage.imageGeometry.to_source[2] = [0, 0, 0]],
  ["NaN geometry", n => n.ephemeralImage.imageGeometry.to_source[0][0] = NaN],
  ["overflow geometry", n => n.ephemeralImage.imageGeometry.to_source = [[1e308, 0, 0], [0, 1e308, 0], [0, 0, 1e308]]],
  ["wrong dimensions", n => n.ephemeralImage.width = 3],
  ["minimum dimension", n => n.ephemeralImage.width = 1],
  ["pixel limit", n => { n.ephemeralImage.width = 2097153; n.ephemeralImage.imageGeometry.image_size[0] = 2097153; }],
  ["byte limit", n => n.ephemeralImage.dataBase64 = "A".repeat(349528)],
  ["invalid base64", n => n.ephemeralImage.dataBase64 = "not base64!!!"],
  ["unsupported mime", n => n.ephemeralImage.mimeType = "image/svg+xml"],
  ["mime mismatch", n => n.ephemeralImage.mimeType = "image/jpeg"],
  ["metadata limit", n => n.ephemeralImage.captureEvidence.extra = "x".repeat(17000)],
  ["not realtime", n => n.payload.realtime = false],
]) test(`${name} fails closed at the selected stream boundary`, () => {
  const h = harness(), n = notification(); h.session.receive(n); const bad = notification(2); mutate(bad);
  h.session.receive(bad); assert.equal(h.lease(), null);
});
