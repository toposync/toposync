const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
function load(name, modules = {}) {
  const context = { exports: {}, require: name => { assert.ok(name in modules, name); return modules[name]; } };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', name), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  return context.exports;
}
const model = load('visualQualification.ts');
const snapshot = () => ({ context_revision: 'a'.repeat(64), next_offset: null, physical_qualification: false, activation_authorized: false,
  items: [{ status: 'recorded_pass', record: { sensor_id: 'sensor', operation_id: 'first', actor_id: 'owner', created_at: 1000,
    context_revision: 'a'.repeat(64), scope: 'laboratory', decision: 'passed', report_sha256: 'b'.repeat(64),
    protocol_sha256: 'c'.repeat(64), environment_sha256: 'd'.repeat(64) } }] });

test('records never accept authority, mismatched context, another sensor or unbounded history', () => {
  const accepted = snapshot();
  assert.equal(model.validateVisualQualification(accepted, 'sensor'), accepted);
  const mutations = [value => value.physical_qualification = true, value => value.activation_authorized = true,
    value => value.context_revision = null, value => value.items[0].record.sensor_id = 'other',
    value => value.items[0].record.decision = 'failed', value => value.items[0].record.scope = 'physical_alarm',
    value => value.items[0].record.report_sha256 = '<script>', value => value.items[0].record.created_at = Infinity,
    value => value.next_offset = -1, value => value.items = Array(21).fill(value.items[0]),
    value => value.items[0].status = 'certified'];
  for (const mutate of mutations) {
    const value = snapshot(); mutate(value);
    assert.throws(() => model.validateVisualQualification(value, 'sensor'), /qualification_invalid/);
  }
  const suspended = snapshot(); suspended.context_revision = null; suspended.items[0].status = 'suspended';
  assert.equal(model.validateVisualQualification(suspended, 'sensor'), suspended);
});

test('qualification reads use requestJson paths and preserve cancellation after response', async () => {
  const calls = [], controller = new AbortController();
  let response = snapshot();
  const api = load('api.ts', { './visualQualification': model,
    '@toposync/plugin-api': { requestJson: async (...args) => { calls.push(args); return response; } } });
  assert.equal(await api.readVisualQualification('sensor', 20, controller.signal), response);
  assert.equal(calls[0][0], '/api/vision/visual-state/sensors/sensor/qualification?offset=20');
  assert.equal(calls[0][1].signal, controller.signal);
  controller.abort();
  await assert.rejects(api.readVisualQualification('sensor', 0, controller.signal), /abort/i);
  response = { ...snapshot(), activation_authorized: true };
  await assert.rejects(api.readVisualQualification('sensor', 0, new AbortController().signal), /qualification_invalid/);
});
