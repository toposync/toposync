const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const filename = path.resolve(__dirname, '../../extensions/ai/ui/src/settings/AiSettingsPanel.tsx');
const ast = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = ['normalizeProfile', 'asString', 'asNumber', 'asBoolean', 'clamp', 'createId'];
const source = names.map(name => {
  const declaration = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, name);
  return declaration.getText(ast);
}).join('\n');
const context = { DEFAULT_OLLAMA_PROVIDER_ID: 'local', DEFAULT_OLLAMA_MODEL: 'synthetic' };
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
