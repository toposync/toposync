const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs'), path = require('node:path'), ts = require('typescript'), vm = require('node:vm');
const context = {exports: {}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/live/livePanorama.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS},
}).outputText, context);
const {isExternalMotionEpoch} = context.exports;

test('late own completion preserves the target but a later external pulse invalidates it', () => {
  assert.equal(isExternalMotionEpoch(50, 52, 52), false);
  assert.equal(isExternalMotionEpoch(52, 53, 52), true);
  assert.equal(isExternalMotionEpoch(53, 53, 52), false);
});

test('missing completion evidence remains conservative and first status establishes a baseline', () => {
  assert.equal(isExternalMotionEpoch(50, 52, null), true);
  assert.equal(isExternalMotionEpoch(null, 52, null), false);
  assert.equal(isExternalMotionEpoch(50, undefined, 50), false);
});
