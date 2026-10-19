const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function functions(file, names, context) {
  const filename = path.resolve(__dirname, file);
  const source = fs.readFileSync(filename, 'utf8');
  const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
  assert.equal(found.length, names.length);
  const code = found.map(node => node.getText(ast).replace(/^export /, '')).join('\n');
  vm.runInNewContext(ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
  return context;
}

for (const base of ['/', '/api/hassio_ingress/test-token']) {
  test(`pipeline link survives initial load and reload at ${base}`, () => {
    const window = { location: { pathname: `${base === '/' ? '' : base}/settings/pipelines/manual_%C3%A1rea` } };
    const context = { window, getToposyncBasePath: () => base, PIPELINES_BASE_PATH: '/settings/pipelines' };
    functions('../src/ui/router.ts', ['snapshotPathname'], context);
    functions('../src/ui/screens/PipelinesScreen.tsx', ['pipelineNameFromPipelinesPath'], context);
    assert.equal(context.pipelineNameFromPipelinesPath(context.snapshotPathname()), 'manual_área');
    window.location.pathname = `${base === '/' ? '' : base}/settings/pipelines`;
    assert.equal(context.pipelineNameFromPipelinesPath(context.snapshotPathname()), null);
  });
}

test('derived Automatic graph is read-only; its independent manual consumer remains editable', () => {
  const context = { isRecord: value => value !== null && typeof value === 'object' && !Array.isArray(value) };
  functions('../src/ui/screens/PipelinesScreen.tsx', ['isAutomaticPipeline', 'isImplicitPipeline'], context);
  assert.equal(context.isImplicitPipeline({ name: 'automatic_one', graph: { meta: { automatic: { intent_id: 'one' } } } }), true);
  assert.equal(context.isImplicitPipeline({ name: 'manual_one', graph: { meta: { automatic_consumer: { intent_id: 'one' } } } }), false);
  assert.equal(context.isImplicitPipeline({ name: 'automatic_user_named', graph: {} }), false);
  assert.equal(context.isImplicitPipeline({ name: 'live', graph: { meta: { streaming: { generated_by: 'camera_live_view' } } } }), true);
});
