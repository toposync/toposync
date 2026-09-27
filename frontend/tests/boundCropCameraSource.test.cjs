const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
function declarations(file, names) {
  const ast = ts.createSourceFile(file, fs.readFileSync(path.join(__dirname, file), 'utf8'), ts.ScriptTarget.Latest, true);
  return ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text))
    .map(node => node.getText(ast).replace(/^export /, '')).join('\n');
}
const source = declarations('../src/ui/screens/pipelines/utils.ts', ['isRecord', 'safeJsonParse']) + '\n'
  + declarations('../src/ui/screens/pipelines/editor/panels/pipelineStepSnapshots.ts', ['parseInteractiveStepConfig', 'resolveBoundCropCameraSource']);
const context = vm.createContext({});
vm.runInContext(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
const step = (operatorId, config) => ({ operatorId, configText: JSON.stringify(config) });
test('bound crop reconfirmation requires explicit original camera pixels', () => {
  const camera = step('camera.source', { camera_id: 'camera', source_id: 'water' });
  const resolve = steps => context.resolveBoundCropCameraSource(steps, steps.length);
  assert.equal(JSON.stringify(resolve([camera, step('core.fps_reducer', {})])), JSON.stringify({ cameraId: 'camera', sourceId: 'water' }));
  for (const steps of [[], [camera, step('camera.image_resize', {})], [camera, step('unknown.operator', {})],
    [step('camera.source', { camera_id: 'camera' })], [step('camera.source', { rtsp_url: 'rtsp://fixture.invalid' })],
    [{ operatorId: 'camera.source', configText: '{invalid' }]]) assert.equal(resolve(steps), null);
});
