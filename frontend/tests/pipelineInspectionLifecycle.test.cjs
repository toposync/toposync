const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');

const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)];
const snapshot = session => ({session, sequence: 0, nodes: [], deliveries: [], dropped: 0});

function harness({ publication = false } = {}) {
  const state = [], effects = [], timers = new Map(), requests = [], closed = [], observers = new Set();
  let cursor = 0, dirty = true, tree, nextTimer = 0;
  const schedule = (callback, delay, interval = false) => { timers.set(++nextTimer, {callback, delay, interval}); return nextTimer; };
  const hooks = {...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial;
      return [state[index], value => {
        const next = typeof value === 'function' ? value(state[index]) : value;
        if (!Object.is(next, state[index])) { state[index] = next; dirty = true; }
      }];
    },
    useRef(initial) { const index = cursor++; return state[index] ||= {current: initial}; },
    useEffect(callback, dependencies) {
      const index = cursor++, previous = effects[index];
      if (!previous || dependencies.some((value, i) => !Object.is(value, previous.dependencies[i]))) {
        effects[index] = {dependencies, callback, cleanup: previous?.cleanup, pending: true};
      }
    },
  };
  const request = (kind, value, options) => new Promise((resolve, reject) => requests.push({kind, value, signal: options?.signal, resolve, reject}));
  const api = {
    openPipelineInspection: (value, options) => request('open', value, options),
    getPipelineInspection: (value, options) => request('get', value, options),
    closePipelineInspection: async session => { closed.push(session); },
  };
  const context = {exports: {}, AbortController, DOMException,
    setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: id => timers.delete(id),
    window: {setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: id => timers.delete(id), setInterval: (fn, ms) => schedule(fn, ms, true), clearInterval: id => timers.delete(id)},
    IntersectionObserver: class {
      constructor(callback) { this.callback = callback; }
      observe() { observers.add(this); }
      disconnect() { observers.delete(this); }
    },
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react-dom') return {createPortal: children => children};
      if (name === 'react/jsx-runtime') return require(name);
      if (name.endsWith('/api')) return api;
      if (name.endsWith('/i18n')) return {i18n: {useI18n: () => ({t: key => key})}};
      if (name === '@toposync/plugin-api') return {resolveToposyncUrl: value => value};
      if (name.endsWith('/Icon')) return {Icon: 'icon'};
      if (name === './inspectionRead') return readContext.exports;
      if (name === './PairedInspectionVideo') return {PairedInspectionVideo: 'paired-video'};
      if (['./inspectionSources', './inspectionSharing', './pairedInspection'].includes(name)) return {};
      if (name.endsWith('.css')) return {};
      throw Error(name);
    },
  };
  const compile = filename => ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/ui/streams', filename), 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
  }).outputText;
  const readContext = {...context, exports: {}};
  vm.runInNewContext(compile('inspectionRead.ts'), readContext);
  vm.runInNewContext(compile('PipelineInspectionPanel.tsx') + '\nexports.Flow = InspectionFlow;', context);
  const props = {pipeline: {name: 'demo', graph: {nodes: publication ? [{id: 'publish', operator: 'stream.publish_video'}] : []}},
    onRemove() {}, reportExecutions() {}};
  function render() {
    let iterations = 0;
    while (dirty) {
      assert.ok(++iterations <= 25, 'effects must settle without a render loop');
      dirty = false; cursor = 0; tree = context.exports.Flow(props);
      for (const node of nodes(tree)) if (node.ref && typeof node.ref === 'object') node.ref.current ||= {};
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
    visible: value => { for (const observer of observers) observer.callback([{isIntersecting: value}]); render(); },
    loading: () => !!find(n => n.props?.role === 'status'),
    error: () => !!find(n => n.props?.role === 'alert'),
    unmount: () => { for (const effect of effects) effect?.cleanup?.(); assert.equal(observers.size, 0); },
  };
}

test('an initially offscreen flow never opens a session or starts a clock', async () => {
  const h = harness(); await h.flush();
  h.visible(false); await h.flush();
  assert.equal(h.requests.length, 0); assert.equal(h.timers.size, 0);
  h.visible(true); await h.flush(); assert.equal(h.requests.length, 1);
  h.requests[0].resolve(snapshot('visible')); await h.flush();
  h.visible(false); await h.flush();
  assert.deepEqual(h.closed, ['visible']); assert.equal(h.timers.size, 0);
  h.unmount();
});

test('snapshot reads are sequential and an error does not start a retry loop', async () => {
  const h = harness(); h.visible(true); await h.flush();
  h.fire(400); h.fire(400); assert.equal(h.requests.length, 1);
  h.requests[0].resolve(snapshot('session')); await h.flush();
  h.fire(400); await h.flush(); h.fire(400); assert.equal(h.requests.length, 2);
  h.requests[1].reject(Error('denied')); await h.flush();
  h.fire(400); assert.equal(h.requests.length, 2); assert.equal(h.error(), true);
  assert.deepEqual(h.closed, ['session']); h.unmount(); assert.equal(h.timers.size, 0);
});

test('unmount during a snapshot aborts it and releases the session only once', async () => {
  const h = harness(); h.visible(true); await h.flush();
  h.requests[0].resolve(snapshot('session')); await h.flush(); h.fire(400); await h.flush();
  h.unmount(); assert.equal(h.requests[1].signal.aborted, true);
  h.requests[1].resolve(snapshot('session')); await h.flush();
  assert.deepEqual(h.closed, ['session']); h.fire(400); assert.equal(h.requests.length, 2); assert.equal(h.timers.size, 0);
});

test('repeated visibility changes cancel pending reads and release late sessions', async () => {
  const h = harness(); h.visible(true); await h.flush();
  for (let i = 0; i < 50; i++) {
    const pending = h.requests.at(-1);
    h.visible(false); assert.equal(pending.signal.aborted, true);
    pending.resolve(snapshot('late-' + i)); await h.flush();
    assert.equal(h.timers.size, 0);
    h.visible(true); await h.flush(); assert.ok(h.timers.size <= 2);
  }
  assert.equal(h.requests.length, 51); assert.equal(h.closed.length, 50);
  h.unmount(); h.requests.at(-1).reject(Error('aborted')); await h.flush(); assert.equal(h.timers.size, 0);
});

test('a stalled opening times out without automatic retry or late resurrection', async () => {
  const h = harness(); h.visible(true); await h.flush();
  const pending = h.requests[0]; h.fire(2500);
  assert.equal(pending.signal.aborted, true);
  pending.reject(new DOMException('Aborted', 'AbortError')); await h.flush();
  assert.equal(h.error(), true); h.fire(400); assert.equal(h.requests.length, 1);
  h.unmount(); assert.equal(h.timers.size, 0);
});

test('published flows hand over reads to paired video without a second snapshot loop', async () => {
  const h = harness({publication: true}); h.visible(true); await h.flush();
  h.requests[0].resolve(snapshot('published')); await h.flush();
  h.fire(400); h.fire(400); await h.flush(); assert.equal(h.requests.length, 1);
  h.visible(false); assert.deepEqual(h.closed, ['published']);
  h.unmount(); assert.equal(h.timers.size, 0);
});
