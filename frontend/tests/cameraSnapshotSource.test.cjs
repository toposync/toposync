const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../src/util/api.ts'), 'utf8');
const ast = ts.createSourceFile('api.ts', source, ts.ScriptTarget.Latest, true);
const declarations = ast.statements.filter(node => ts.isFunctionDeclaration(node) && ['fetchCameraSnapshot', 'fetchCameraSnapshotWithEvidence'].includes(node.name?.text));
const code = ts.transpileModule(declarations.map(node => node.getText(ast).replace('export ', '')).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
test('camera snapshot preserves explicit source, ingress and abort signal', async () => {
  const requests = [];
  const controller = new AbortController();
  const blob = new Blob(['image']);
  const context = { URLSearchParams, resolveToposyncUrl: url => '/ingress/test' + url,
    fetch: async (url, options) => { requests.push({ url, options }); return { ok: true, headers: new Headers(), blob: async () => blob }; } };
  vm.createContext(context); vm.runInContext(code, context);
  assert.equal(await context.fetchCameraSnapshot('front/door', controller.signal, 'water & pets'), blob);
  assert.equal(requests[0].url, '/ingress/test/api/cameras/cameras/front%2Fdoor/snapshot?source_id=water+%26+pets');
  assert.equal(requests[0].options.signal, controller.signal);
  await context.fetchCameraSnapshot('front');
  assert.equal(requests[1].url, '/ingress/test/api/cameras/cameras/front/snapshot');
});


test('snapshot capture evidence rejects missing or malformed generation without inventing identity', async () => {
  let headers;
  const blob = new Blob(['image']);
  const context = { URLSearchParams, resolveToposyncUrl: url => url,
    fetch: async () => ({ ok: true, headers, blob: async () => blob }) };
  vm.createContext(context); vm.runInContext(code, context);
  for (const [instance, generation, valid] of [
    ['decoder', '0', true], ['decoder', '12', true], ['', '1', false],
    ['decoder', '', false], ['decoder', '-1', false], ['decoder', '1.5', false],
    ['decoder', '9007199254740992', false], ['decoder', 'true', false],
  ]) {
    headers = new Headers({ 'X-Toposync-Snapshot-Capture-Instance': instance,
      'X-Toposync-Snapshot-Frame-Generation': generation });
    const result = await context.fetchCameraSnapshotWithEvidence('camera', undefined, 'water');
    assert.equal(result.blob, blob);
    assert.deepEqual(JSON.parse(JSON.stringify(result.capture)), valid
      ? { capture_instance: instance, generation: Number(generation) } : null);
  }
});
