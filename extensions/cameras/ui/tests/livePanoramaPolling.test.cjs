const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const source = ts.createSourceFile('LivePanoramaView.tsx', fs.readFileSync(path.join(__dirname, '../src/live/LivePanoramaView.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let declaration;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'interval'
    && ts.isCallExpression(node.initializer) && node.initializer.expression.getText(source) === 'setInterval') declaration = node;
  ts.forEachChild(node, visit);
}
visit(source);
const code = ts.transpileModule(`const ${declaration.getText(source)};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;

function harness() {
  let callback, now = 0;
  const calls = [], stops = [], errors = [], clears = [];
  const context = vm.createContext({
    visibleRef: { current: true }, stateRef: { current: { moving: false } },
    statusPending: false, lastStatusRequest: 0, identifier: 'session',
    controller: new AbortController(), alive: { current: true },
    session: { current: 'session' }, sequence: { current: 0 },
    lastFrame: { current: 0 }, pendingTarget: { current: null },
    performance: { now: () => now },
    setInterval: (fn, period) => { assert.equal(period, 250); callback = fn; },
    api: () => new Promise((resolve, reject) => calls.push({ at: now, resolve, reject })),
    accept() {}, clear: () => clears.push(true), setError: error => errors.push(error), setMarker() {}, setConnected() {},
    stop: () => stops.push(now),
  });
  vm.runInContext(code, context);
  return { context, calls, stops, errors, clears,
    tick(time, frame = true) { now = time; if (frame) context.lastFrame.current = time; callback(); },
    async finish(index, failed = false) {
      if (failed) calls[index].reject(Error('network')); else calls[index].resolve({});
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test('session polling is fast only during motion and stops while hidden', async () => {
  const h = harness();
  h.tick(250); h.tick(500); assert.equal(h.calls.length, 0);
  h.tick(750); await h.finish(0);
  h.tick(1000); assert.equal(h.calls.length, 1);
  h.context.stateRef.current.moving = true;
  h.tick(1250); await h.finish(1);
  h.tick(1500); await h.finish(2);
  h.context.visibleRef.current = false;
  h.tick(1750); h.tick(3000); assert.equal(h.calls.length, 3);
  h.context.visibleRef.current = true;
  h.context.stateRef.current.moving = false;
  h.tick(3250); await h.finish(3);
  h.tick(3500); h.tick(3750); assert.equal(h.calls.length, 4);
  h.tick(4000); assert.equal(h.calls.length, 5);
});

test('obsolete polling failure cannot clear the latest target presentation', async () => {
  for (const change of ['intention', 'session']) {
    const h = harness();
    h.tick(750);
    if (change === 'intention') h.context.sequence.current += 1;
    else h.context.session.current = 'replacement';
    await h.finish(0, true);
    assert.deepEqual(h.errors, []);
    assert.deepEqual(h.clears, []);
    assert.equal(h.context.statusPending, false);
  }
});

test('current polling failure still reports loss and clears alignment', async () => {
  const h = harness();
  h.tick(750);
  await h.finish(0, true);
  assert.deepEqual(h.errors, ['network']);
  assert.deepEqual(h.clears, [true]);
});

test('slow or failed status never creates overlapping requests or blocks the frame watchdog', async () => {
  const h = harness(); h.context.stateRef.current.moving = true;
  h.tick(250); h.tick(500); h.tick(750);
  assert.equal(h.calls.length, 1);
  h.tick(2500, false);
  assert.deepEqual(h.stops, [2500]);
  await h.finish(0, true);
  h.tick(2750); assert.equal(h.calls.length, 2);
});
