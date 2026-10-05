const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');
const vm = require('node:vm');

// Execute the real catalog effects with deterministic visibility/network lifetimes.
// Native browser validation remains separate from this cancellation regression.
function harness(file) {
  const text = fs.readFileSync(path.join(__dirname, '../src/ui', file), 'utf8');
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect'
        && node.arguments[0]?.getText(source).includes('await listStreamingCameraLiveViews(')) effect = node;
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(effect, 'catalog owner must be found');
  const intervals = new Set(), listeners = new Set(), requests = [], values = {};
  const scope = {
    exports: {}, AbortController, streamingAvailability: 'available', tabVisible: true, isActive: true,
    LIVE_VIEWS_REFRESH_MS: 15000, TRANSMISSIONS_REFRESH_MS: 15000,
    liveViewsRequestRef: { current: null },
    document: {
      hidden: false,
      addEventListener: (_, listener) => listeners.add(listener),
      removeEventListener: (_, listener) => listeners.delete(listener),
    },
    window: { setInterval: callback => { intervals.add(callback); return callback; }, clearInterval: callback => intervals.delete(callback) },
    listStreamingCameraLiveViews: ({ signal }) => new Promise(resolve => requests.push({ signal, resolve })),
    isAbortError: error => error?.name === 'AbortError', errorMessage: String, asErrorMessage: String,
  };
  for (const name of ['CameraLiveViews', 'CameraLiveViewsError', 'CameraLiveViewsLoading', 'LiveViews', 'Error', 'Loading']) {
    scope[`set${name}`] = value => { values[name] = value; };
  }
  const context = vm.createContext(scope);
  vm.runInContext(ts.transpileModule(`exports.run = ${effect.arguments[0].getText(source)};
    exports.dependencies = () => ${effect.arguments[1].getText(source)};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context);
  return { scope, values, requests, intervals, listeners, run: scope.exports.run,
    dependencies: scope.exports.dependencies,
    visibility(hidden) { scope.document.hidden = hidden; for (const listener of listeners) listener(); },
    tick() { for (const callback of intervals) callback(); },
  };
}

test('catalog polling suspends hidden/inactive owners and ignores cancelled responses on resume', async () => {
  const flush = () => new Promise(setImmediate);
  const main = harness('screens/MainScreen.tsx');
  let close = main.run();
  assert.equal(main.requests.length, 1);
  main.visibility(true);
  assert.equal(main.requests[0].signal.aborted, true);
  main.tick();
  assert.equal(main.requests.length, 1, 'hidden shell must not poll');
  main.visibility(false);
  assert.equal(main.requests.length, 2);
  main.requests[0].resolve(['stale']);
  await flush();
  assert.equal(main.values.CameraLiveViews, undefined, 'late response must not overwrite resumed catalog');
  main.requests[1].resolve(['fresh']);
  await flush();
  assert.equal(main.values.CameraLiveViews[0], 'fresh');
  assert.equal(main.values.CameraLiveViewsLoading, false, 'hide during first load must not leave a spinner');
  close();
  assert.equal(main.listeners.size + main.intervals.size, 0);
  main.visibility(true); close = main.run(); main.tick();
  assert.equal(main.requests.length, 2, 'initially hidden shell must not fetch');
  close();

  const player = harness('streams/StreamsDashboard.tsx');
  player.scope.tabVisible = false;
  player.run();
  assert.equal(player.requests.length, 0);
  player.scope.tabVisible = true; player.scope.isActive = false;
  player.run();
  assert.equal(player.requests.length, 0, 'inactive embedded player must not poll');
  assert.deepEqual(Array.from(player.dependencies()), [true, false]);
  player.scope.isActive = true; close = player.run();
  assert.equal(player.requests.length, 1);
  close();
  assert.equal(player.requests[0].signal.aborted, true);
  player.requests[0].resolve(['stale']); await flush();
  assert.equal(player.values.LiveViews, undefined);
  assert.equal(player.intervals.size, 0);
  player.scope.tabVisible = false;
  assert.deepEqual(Array.from(player.dependencies()), [false, true]);
  player.run();
  assert.equal(player.requests.length, 1);
  player.scope.tabVisible = true; close = player.run();
  player.requests[1].resolve(['fresh']); await flush();
  assert.equal(player.values.LiveViews[0], 'fresh');
  assert.equal(player.values.Loading, false);
  close();
  assert.equal(player.intervals.size, 0);
});
