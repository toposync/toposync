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
