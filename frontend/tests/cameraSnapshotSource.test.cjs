const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../src/util/api.ts'), 'utf8');
const ast = ts.createSourceFile('api.ts', source, ts.ScriptTarget.Latest, true);
const declaration = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'fetchCameraSnapshot');
const code = ts.transpileModule(declaration.getText(ast).replace('export ', ''), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
test('camera snapshot preserves explicit source, ingress and abort signal', async () => {
  const requests = [];
  const controller = new AbortController();
  const blob = new Blob(['image']);
  const context = { URLSearchParams, resolveToposyncUrl: url => '/ingress/test' + url,
    fetch: async (url, options) => { requests.push({ url, options }); return { ok: true, blob: async () => blob }; } };
  vm.createContext(context); vm.runInContext(code, context);
  assert.equal(await context.fetchCameraSnapshot('front/door', controller.signal, 'water & pets'), blob);
  assert.equal(requests[0].url, '/ingress/test/api/cameras/cameras/front%2Fdoor/snapshot?source_id=water+%26+pets');
  assert.equal(requests[0].options.signal, controller.signal);
  await context.fetchCameraSnapshot('front');
  assert.equal(requests[1].url, '/ingress/test/api/cameras/cameras/front/snapshot');
});
