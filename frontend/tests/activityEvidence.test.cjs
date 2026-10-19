const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const ts = require('typescript');
const calls = [];
function load(relative, imports = {}) {
  const exports = {};
  const context = { exports, require: name => imports[name], console };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.resolve(__dirname, relative), 'utf8'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, context);
  return exports;
}
const activity = load('../src/util/activity.ts', {
  '@toposync/plugin-api': { resolveToposyncUrl: value => '/test-ingress' + value },
  './api': { requestJson: async (...args) => { calls.push(args); return {}; } },
});
test('activity routes preserve ingress, identifiers and mutation revisions', async () => {
  const signal = new AbortController().signal;
  await activity.getActivity('visit/a', signal);
  await activity.changeActivity('visit/a', 8, 'revoke', signal);
  await activity.changeActivity('visit/a', 9, 'delete', signal);
  assert.equal(calls[0][0], '/test-ingress/api/activity/visit%2Fa');
  assert.equal(calls[1][0], '/test-ingress/api/activity/visit%2Fa/revoke-enrichment');
  assert.equal(calls[1][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[1][1].body), { expected_revision: 8 });
  assert.equal(calls[2][1].method, 'DELETE');
  assert.ok(calls.every(([, options]) => options.signal === signal && options.cache === 'no-store'));
});
test('only the stable activity identifier can select private history', () => {
  const valid = {type:'occurrences.activity', payload:{occurrence_id:'a'.repeat(64)}};
  assert.equal(activity.activityIdentifier(valid), 'a'.repeat(64));
  for (const value of [null, {}, {occurrence_id:'../../private'}, {occurrence_id:3}]) {
    assert.equal(activity.activityIdentifier({...valid,payload:value}), null);
  }
  assert.equal(activity.activityIdentifier({...valid,type:'other'}), null);
});
test('trajectory partitions participants and clocks without changing evidence', () => {
  const document = {evidence: {
    later:{participant_id:'a',clock_domain:'first',observed_at:12,gap:true},
    other:{participant_id:'b',clock_domain:'first',observed_at:5},
    incomparable:{participant_id:'a',clock_domain:'second',observed_at:1},
    earlier:{participant_id:'a',clock_domain:'first',observed_at:10},
  }};
  const original = JSON.stringify(document);
  const values = activity.participantEvidence(document,'a');
  assert.equal(values.map(([id]) => id).join(','), 'earlier,later,incomparable');
  assert.equal(values[1][1].gap,true);
  assert.equal(JSON.stringify(document), original);
});
test('privacy and conflict responses have distinct user states', () => {
  for (const [status, expected] of [[410,'expired'],[403,'unavailable'],[404,'unavailable'],[401,'unavailable'],[409,'changed'],[500,'failed']]) {
    assert.equal(activity.activityError({status}),expected);
  }
});
test('activity text is complete in Portuguese and English', () => {
  const {activityTranslations} = load('../src/ui/notifications/activityTranslations.ts');
  assert.deepEqual(Object.keys(activityTranslations.en).sort(), Object.keys(activityTranslations['pt-BR']).sort());
  assert.ok(Object.values(activityTranslations.en).every(value => value.trim()));
  assert.ok(Object.values(activityTranslations['pt-BR']).every(value => value.trim()));
});

test('private evidence expiry clears the real activity view before event expiry without a retry loop', async () => {
  const source = ts.createSourceFile('ActivityView.tsx', fs.readFileSync(path.resolve(__dirname, '../src/ui/notifications/ActivityView.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect'
      && node.arguments[0].getText(source).includes('getActivity(')) effect = node.arguments[0].getText(source);
    ts.forEachChild(node, visit);
  }
  visit(source); assert.ok(effect);
  let now = 1000000, view, refreshes = 0, calls = 0, sequence = 0;
  const timers = new Map();
  const response = { occurrence: { expires_at: 1060, artifacts: [{ expires_at: 1010 }], participants: { private: { name: 'Synthetic private name' } } } };
  const context = {
    key: 'selected', identifier: 'a'.repeat(64), AbortController, Date: { now: () => now },
    setView: value => { view = value; }, setConfirmation() {}, setMutationError() {},
    setRefresh: update => { refreshes = update(refreshes); },
    getActivity: async () => { calls++; return response; },
    document: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
    window: { addEventListener() {}, removeEventListener() {} },
    setTimeout: (callback, delay) => { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id), isAbortError: () => false, activityError: () => 'failed',
  };
  vm.runInNewContext(ts.transpileModule('this.execute = ' + effect, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
  const cleanup = context.execute(); await Promise.resolve(); await Promise.resolve();
  assert.equal(view.data, response); assert.equal(timers.size, 1);
  const timer = [...timers.values()][0]; assert.equal(timer.delay, 10000);
  now = 1010000; timer.callback();
  assert.equal(view.data, null); assert.equal(view.error, null); assert.equal(refreshes, 1);
  cleanup();
  const nextCleanup = context.execute(); await Promise.resolve(); await Promise.resolve();
  assert.equal(view.data, null); assert.equal(view.error, 'changed');
  assert.equal(timers.size, 0); assert.equal(refreshes, 1); assert.equal(calls, 2);
  nextCleanup();
});
