const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const filename = path.resolve(__dirname, '../../extensions/ai/ui/src/settings/AiSettingsPanel.tsx');
const ast = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = ['normalizeSettings', 'normalizeProviders', 'normalizeProvider', 'normalizeProviderKind',
  'normalizeProfiles', 'normalizeProfile', 'normalizeLimits', 'ensureOllamaProvider', 'ensureDefaultProfiles',
  'isRecord', 'nullableNumber', 'providerKindLabel', 'asString', 'asNumber', 'asBoolean', 'clamp', 'createId'];
const source = names.map(name => {
  const declaration = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, name);
  return declaration.getText(ast);
}).join('\n');
const context = { DEFAULT_OLLAMA_PROVIDER_ID: 'local', DEFAULT_OLLAMA_MODEL: 'synthetic',
  DEFAULT_OLLAMA_HOST: 'http://127.0.0.1:11434', DEFAULT_PROFILE_ID: 'first' };
vm.createContext(context);
vm.runInContext(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);

test('settings round trip preserves explicitly declared image capacity and capabilities', () => {
  const configured = { id: 'sensor', model: 'synthetic', max_images_per_request: 9,
    capabilities: ['vision', 'structured_json', 'multiple_images'] };
  const loaded = context.normalizeProfile(configured);
  const saved = context.normalizeProfile(JSON.parse(JSON.stringify(loaded)));
  assert.equal(saved.max_images_per_request, 9);
  assert.deepEqual(Array.from(saved.capabilities), configured.capabilities);
});

test('legacy profiles remain single-image and do not acquire an unverified capability', () => {
  const loaded = context.normalizeProfile({ id: 'legacy' });
  assert.equal(loaded.max_images_per_request, 1);
  assert.equal(loaded.capabilities.includes('multiple_images'), false);
});

test('settings round trip retains account limits and profile membership', () => {
  const configured = { default_profile_id: 'first', providers: [{ id: 'local', kind: 'ollama' }],
    profiles: [{ id: 'first', budget_group_id: 'account' }, { id: 'second', budget_group_id: 'account' }],
    limits: {}, budget_groups: [{ id: 'account', name: 'Shared account', limits: { max_concurrency: 2,
      requests_per_minute: 7, requests_per_hour: 30, requests_per_day: 100, requests_per_month: null } }] };
  const loaded = context.normalizeSettings(configured, configured);
  const saved = context.normalizeSettings(JSON.parse(JSON.stringify(loaded)), configured);
  assert.deepEqual(JSON.parse(JSON.stringify(saved.budget_groups)), configured.budget_groups);
  assert.deepEqual(Array.from(saved.profiles, profile => profile.budget_group_id), ['account', 'account']);
});

test('deleting another profile removes its fallback without losing account membership', () => {
  let declaration;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'deleteProfile') declaration = node;
    ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.ok(declaration);
  vm.runInContext(ts.transpileModule(declaration.getText(ast), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  context.profiles = [{ id: 'first', budget_group_id: 'account', fallback_profile_ids: ['second', 'third'] },
    { id: 'second', budget_group_id: 'account', fallback_profile_ids: [] }];
  context.normalized = { default_profile_id: 'second' };
  let updated;
  context.patch = value => { updated = value; };
  context.setActiveProfileId = () => {};
  context.deleteProfile('second');
  assert.equal(updated.profiles.length, 1);
  assert.equal(updated.profiles[0].budget_group_id, 'account');
  assert.deepEqual(Array.from(updated.profiles[0].fallback_profile_ids), ['third']);
  assert.equal(updated.default_profile_id, 'first');
});
