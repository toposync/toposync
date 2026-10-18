const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');

const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)];
const snapshot = session => ({session, nodes: [], dropped: 0});

function harness() {
  const state = [], effects = [], timers = new Map(), listeners = new Set(), requests = [], closed = [];
  let cursor = 0, dirty = true, tree, nextTimer = 0;
  const schedule = (callback, delay, interval = false) => { timers.set(++nextTimer, {callback, delay, interval}); return nextTimer; };
  const document = {hidden: false, addEventListener: (_, callback) => listeners.add(callback), removeEventListener: (_, callback) => listeners.delete(callback)};
  const hooks = {...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], value => {
        const next = typeof value === 'function' ? value(state[index]) : value;
        if (!Object.is(next, state[index])) { state[index] = next; dirty = true; }
      }];
    },
    useEffect(callback, dependencies) {
      const index = cursor++, previous = effects[index];
      if (!previous || dependencies.some((value, i) => !Object.is(value, previous.dependencies[i]))) {
        effects[index] = {dependencies, callback, cleanup: previous?.cleanup, pending: true};
      }
    },
  };
  const request = (kind, value, options) => new Promise((resolve, reject) => requests.push({kind, value, signal: options?.signal, resolve, reject}));
  const api = {
    listPipelines: async () => [{name: 'demo'}],
    openPipelineInspection: (value, options) => request('open', value, options),
    getPipelineInspection: (value, options) => request('get', value, options),
    closePipelineInspection: async session => { closed.push(session); },
  };
  const context = {exports: {}, document, AbortController,
    window: {setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: id => timers.delete(id), setInterval: (fn, ms) => schedule(fn, ms, true), clearInterval: id => timers.delete(id)},
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return require(name);
      if (name.endsWith('/api')) return api;
      if (name.endsWith('/i18n')) return {i18n: {useI18n: () => ({t: key => key})}};
      if (name === '@toposync/plugin-api') return {resolveToposyncUrl: value => value};
      if (name.endsWith('/Modal')) return {Modal: 'modal'};
      if (name.endsWith('/Icon')) return {Icon: 'icon'};
      if (name.endsWith('.css')) return {};
      throw Error(name);
    },
  };
  const source = fs.readFileSync(path.join(__dirname, '../src/ui/streams/PipelineInspectionPanel.tsx'), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText, context);
  function render() {
    let iterations = 0;
    while (dirty) {
      assert.ok(++iterations <= 25, 'effects must settle without a render loop');
      dirty = false; cursor = 0; tree = context.exports.PipelineInspectionPanel();
      for (const effect of effects) if (effect?.pending) { effect.pending = false; effect.cleanup?.(); effect.cleanup = effect.callback(); }
    }
    return tree;
  }
  const find = predicate => nodes(render()).find(predicate);
  const flush = async () => { await new Promise(setImmediate); render(); };
  const fire = delay => {
    for (const [id, timer] of [...timers]) if (timer.delay === delay) {
      if (!timer.interval) timers.delete(id);
      timer.callback();
    }
    render();
  };
  render();
  return {requests, closed, timers, flush, fire,
    open: async () => { find(n => n.type === 'button').props.onClick(); render(); await flush(); },
    choose: value => { find(n => n.type === 'select').props.onChange({target: {value}}); render(); },
    close: () => { find(n => n.type === 'modal').props.onClose(); render(); },
    hidden: value => { document.hidden = value; for (const listener of listeners) listener(); render(); },
    loading: () => !!find(n => n.props?.role === 'status'),
    error: () => !!find(n => n.props?.role === 'alert'),
    unmount: () => { for (const effect of effects) effect?.cleanup?.(); assert.equal(timers.size, 0); assert.equal(listeners.size, 0); },
  };
}

test('polls are sequential and an error does not start a retry loop', async () => {
  const h = harness(); await h.open(); h.choose('demo');
  h.fire(400); h.fire(400); assert.equal(h.requests.length, 1);
  h.requests[0].resolve(snapshot('session')); await h.flush();
  h.fire(400); h.fire(400); assert.equal(h.requests.length, 2);
  h.requests[1].reject(Error('denied')); await h.flush();
  h.fire(400); assert.equal(h.requests.length, 2); assert.equal(h.error(), true);
  h.close(); h.unmount();
});

test('clearing a selection cancels pending work and leaves the loading state', async () => {
  const h = harness(); await h.open(); h.choose('demo'); assert.equal(h.loading(), true);
  h.choose(''); assert.equal(h.loading(), false);
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[0].resolve(snapshot('late')); await h.flush();
  assert.deepEqual(h.closed, ['late']); h.fire(400); assert.equal(h.requests.length, 1);
  h.close(); h.unmount();
});

test('closing during a snapshot aborts it and releases the session only once', async () => {
  const h = harness(); await h.open(); h.choose('demo');
  h.requests[0].resolve(snapshot('session')); await h.flush(); h.fire(400);
  h.close(); assert.equal(h.requests[1].signal.aborted, true);
  h.requests[1].resolve(snapshot('session')); await h.flush();
  assert.deepEqual(h.closed, ['session']); h.fire(400); assert.equal(h.requests.length, 2); h.unmount();
});

test('hidden panels have no clock or polling timers; repeated visibility changes stay bounded', async () => {
  const h = harness(); await h.open(); h.choose('demo');
  for (let i = 0; i < 50; i++) {
    const pending = h.requests.at(-1);
    h.hidden(true); assert.equal(h.timers.size, 0); assert.equal(pending.signal.aborted, true);
    pending.resolve(snapshot('late-' + i)); await h.flush();
    h.hidden(false); await h.flush(); assert.ok(h.timers.size <= 2);
  }
  assert.equal(h.requests.length, 51); assert.equal(h.closed.length, 50);
  h.close(); h.requests.at(-1).reject(Error('aborted')); await h.flush(); h.unmount();
});

test('a stalled request times out without automatic retry or a late resurrection', async () => {
  const h = harness(); await h.open(); h.choose('demo');
  const pending = h.requests[0]; h.fire(2500);
  assert.equal(pending.signal.aborted, true);
  pending.resolve(snapshot('too-late')); await h.flush();
  assert.equal(h.error(), true); assert.deepEqual(h.closed, ['too-late']);
  h.fire(400); assert.equal(h.requests.length, 1); h.close(); h.unmount();
});
