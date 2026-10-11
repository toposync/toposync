const assert = require('node:assert/strict');
const { test, after } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const typescript = require('typescript');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'toposync-metric-references-'));
const file = path.join(directory, 'parsing.cjs');
fs.writeFileSync(file, typescript.transpileModule(fs.readFileSync(path.resolve(__dirname, '../src/parsing.ts'), 'utf8'), {
  compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022 },
}).outputText);
const { legacyMetricCalibrations } = require(file);
after(() => fs.rmSync(directory, { recursive: true, force: true }));
test('explicit metric migration preserves complete records without guessing lens or overwriting the selected references', () => {
  const metric = { id: 'metric', projection_model: { type: 'camera_ray_ground_v2', lens: { fx: 2 } }, stream_scope: { physical_view_id: 'wide' }, pose_reference: { pan: .2 }, projection_quality: { status: 'review' } };
  const props = { calibrated_views: [metric, { id: 'old-map', projection_model: { type: 'image_quad_on_world' } }] };
  const before = structuredClone(props);
  const result = legacyMetricCalibrations(props);
  assert.deepEqual(result, [metric]);
  assert.deepEqual(props, before);
  for (const selected of [[], null, [metric]]) assert.deepEqual(legacyMetricCalibrations({ ...props, metric_calibrations: selected }), []);
});
