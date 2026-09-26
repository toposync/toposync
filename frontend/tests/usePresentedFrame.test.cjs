const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');
const vm = require('node:vm');

function loadFrameEpoch(crypto) {
  const source = fs.readFileSync(path.join(__dirname, '../src/ui/streams/usePresentedFrame.ts'), 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const context = {
    exports: {},
    crypto,
    require(moduleName) {
      if (moduleName === 'react') return {};
      throw new Error(`Unexpected module: ${moduleName}`);
    },
  };
  vm.runInNewContext(output, context);
  return context.exports.createPresentedFrameEpoch;
}

test('uses Crypto.randomUUID when the browser supports it', () => {
  const createEpoch = loadFrameEpoch({ randomUUID: () => 'native-uuid' });
  assert.equal(createEpoch(), 'native-uuid');
});

test('creates a frame epoch when Crypto.randomUUID is unavailable', () => {
  const createEpoch = loadFrameEpoch({});
  assert.match(createEpoch(), /^\d+-[0-9a-f]+$/);
});
