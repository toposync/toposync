const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');

const context = { exports: {}, crypto: { randomUUID: () => 'id' } };
vm.runInNewContext(ts.transpileModule(
  fs.readFileSync(path.join(__dirname, '../src/parsing.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText, context);
const { parseCameras, serializeCameras } = context.exports;

test('camera settings preserve explicit exclusive PTZ control confirmation', () => {
  const parsed = parseCameras({ devices: [{
    id: 'camera', name: 'Camera', enabled: true,
    control: { type: 'onvif', automation_exclusive_control_confirmed: true },
    onvif: { xaddr: 'http://camera' }, sources: [], metadata: {},
  }] });
  assert.equal(parsed[0].control.automation_exclusive_control_confirmed, true);
  const serialized = serializeCameras(parsed);
  assert.equal(serialized.devices[0].control.automation_exclusive_control_confirmed, true);
});

test('camera settings never carry exclusive confirmation into non-PTZ control', () => {
  const serialized = serializeCameras([{
    id: 'camera', name: 'Camera', enabled: true,
    control: { type: 'none', automation_exclusive_control_confirmed: true },
    onvif: null, sources: [], metadata: {},
  }]);
  assert.equal(serialized.devices[0].control.automation_exclusive_control_confirmed, false);
});
