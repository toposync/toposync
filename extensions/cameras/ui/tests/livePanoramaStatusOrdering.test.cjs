const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const directory = path.join(__dirname, '../src/live');
const source = ts.createSourceFile('LivePanoramaView.tsx', fs.readFileSync(path.join(directory, 'LivePanoramaView.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let declaration;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'inspect') declaration = node;
  ts.forEachChild(node, visit);
}
visit(source);
const code = ts.transpileModule(`const ${declaration.getText(source)};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const helpers = { exports: {} };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(directory, 'livePanorama.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText, helpers);

function harness() {
  const pending = [], cleared = [], external = [];
  const context = vm.createContext({
    URLSearchParams, encodeURIComponent, choice: { camera_id: 'camera', source_id: 'source' },
    controller: new AbortController(), disposed: false, motionEpoch: 10,
    statusRequested: 0, statusApplied: 0,
    request: () => new Promise(resolve => pending.push(resolve)),
    stateRef: { current: { moving: false, sequence: 0 } },
    observationGeneration: { current: 0 }, pendingTarget: { current: null },
    externalMovingRef: { current: false }, setMarker() {}, setError() {},
    setExternalMoving: value => external.push(value), clear: () => cleared.push(true),
    isExternalMotionEpoch: helpers.exports.isExternalMotionEpoch,
  });
  vm.runInContext(code, context);
  return { context, cleared, external, inspect: () => vm.runInContext('inspect()', context),
    async resolve(index, epoch, moving = false) {
      pending[index]({ status: { move_status: moving ? 'MOVING' : 'IDLE' }, control: { motion_epoch: epoch } });
      await new Promise(resolve => setImmediate(resolve));
    } };
}

test('late older status cannot invalidate a newer registration or report obsolete movement', async () => {
  const h = harness();
  h.inspect(); h.inspect();
  await h.resolve(1, 12);
  assert.equal(h.context.motionEpoch, 12);
  assert.equal(h.cleared.length, 1);
  await h.resolve(0, 11, true);
  assert.equal(h.context.motionEpoch, 12);
  assert.equal(h.cleared.length, 1);
  assert.deepEqual(h.external, []);
});

test('new status still invalidates movement and stop epochs in request order', async () => {
  const h = harness();
  h.inspect(); await h.resolve(0, 11, true);
  assert.equal(h.context.externalMovingRef.current, true);
  h.inspect(); await h.resolve(1, 12);
  assert.equal(h.context.motionEpoch, 12);
  assert.equal(h.context.externalMovingRef.current, false);
  assert.deepEqual(h.external, [true, false]);
  assert.equal(h.cleared.length, 4);
});

test('disposed status request never updates the replaced component', async () => {
  const h = harness(); h.inspect(); h.context.disposed = true;
  await h.resolve(0, 11, true);
  assert.equal(h.context.motionEpoch, 10);
  assert.equal(h.cleared.length, 0);
});

let acceptDeclaration;
(function find(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'accept') acceptDeclaration = node;
  ts.forEachChild(node, find);
})(source);
const acceptCode = ts.transpileModule(acceptDeclaration.getText(source), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;

function acceptance() {
  const cleared = [], states = [];
  const context = vm.createContext({
    alive: { current: true }, session: { current: 'session' }, sequence: { current: 1 },
    stateRef: { current: { session_id: 'session', sequence: 1, moving: false, result: { sequence: 1, verified: true } } },
    observationGeneration: { current: 0 }, pendingTarget: { current: null },
    setMarker() {}, clear: () => cleared.push(true), setState: state => states.push(state),
  });
  vm.runInContext(acceptCode, context);
  return { context, cleared, states, accept(next) { context.next = next; vm.runInContext('accept(next)', context); } };
}

test('late moving snapshot cannot undo a completed arrival in the same intention', () => {
  const h = acceptance();
  h.accept({ session_id: 'session', sequence: 1, moving: true, result: null });
  assert.equal(h.states.length, 0);
  assert.deepEqual(h.cleared, []);
  assert.equal(h.context.stateRef.current.result.verified, true);
});

test('new intentions and uncertain network outcomes still reconcile actual movement', () => {
  for (const kind of ['new', 'uncertain', 'still_finishing', 'replaced_session', 'blocked']) {
    const h = acceptance();
    const next = { session_id: 'session', sequence: 1, moving: true, result: null };
    if (kind === 'new') next.sequence = 2;
    if (kind === 'uncertain') h.context.stateRef.current.result = null;
    if (kind === 'still_finishing') h.context.stateRef.current.moving = true;
    if (kind === 'replaced_session') h.context.stateRef.current.session_id = 'previous';
    if (kind === 'blocked') next.blocked = true;
    h.accept(next);
    assert.equal(h.states.length, 1, kind);
    assert.equal(h.context.stateRef.current.moving, true, kind);
    assert.ok(h.cleared.length, kind);
  }
});

let dispatchDeclaration;
(function find(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'dispatchIntent') dispatchDeclaration = node;
  ts.forEachChild(node, find);
})(source);
const dispatchCode = ts.transpileModule(dispatchDeclaration.getText(source), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;

test('failed intent response arriving after verified completion preserves the confirmed state', async () => {
  const h = acceptance();
  let reject;
  const errors = [];
  h.context.api = () => new Promise((resolve, fail) => { reject = fail; });
  h.context.setError = error => errors.push(error);
  vm.runInContext(dispatchCode, h.context);
  vm.runInContext('dispatchIntent({x: .4, y: .5, marker: {x: 40, y: 50}, queuedAt: 0})', h.context);
  h.accept({ session_id: 'session', sequence: 2, moving: false, phase: 'aligned', result: { sequence: 2, verified: true } });
  const confirmed = h.context.stateRef.current;
  reject(new Error('Response connection lost'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.context.stateRef.current, confirmed);
  assert.deepEqual(errors, ['']);
});

test('unconfirmed intent failure remains visible but obsolete failures cannot affect a later destination', async () => {
  for (const kind of ['unconfirmed', 'older_result', 'moving', 'new_intention', 'new_session']) {
    const h = acceptance();
    let reject;
    const errors = [];
    h.context.api = () => new Promise((resolve, fail) => { reject = fail; });
    h.context.setError = error => errors.push(error);
    vm.runInContext(dispatchCode, h.context);
    vm.runInContext('dispatchIntent({x: .4, y: .5, marker: {x: 40, y: 50}, queuedAt: 0})', h.context);
    if (kind === 'unconfirmed') h.context.stateRef.current.result = null;
    if (kind === 'older_result') h.context.stateRef.current.moving = false;
    if (kind === 'moving') h.context.stateRef.current.result = { sequence: 2, verified: true };
    if (kind === 'new_intention') h.context.sequence.current = 3;
    if (kind === 'new_session') h.context.session.current = 'replacement';
    const before = h.context.stateRef.current;
    reject(new Error('Response connection lost'));
    await new Promise(resolve => setImmediate(resolve));
    if (kind.startsWith('new_')) {
      assert.equal(h.context.stateRef.current, before, kind);
      assert.deepEqual(errors, [''], kind);
    } else {
      assert.equal(h.context.stateRef.current.phase, 'error', kind);
      assert.deepEqual(errors, ['', 'Response connection lost'], kind);
    }
  }
});
