const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const react = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function load(name, modules = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', name), 'utf8');
  const context = { exports: {}, require: id => { assert.ok(id in modules, id); return modules[id]; } };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
  } }).outputText, context);
  return context.exports;
}
const ai = load('visualStateAi.ts');
const visual = load('visualState.ts');
const plain = value => JSON.parse(JSON.stringify(value));
const configuration = () => ({ sensor_id: 'door', source_identity: 'camera:synthetic', preparation_revision: 'geometry',
  strategy: 'direct', ai_profile_id: 'primary', scene_description: '', classes: ['open', 'closed'].map(class_id => ({
    class_id, key: class_id, label: class_id, enabled: true, description: class_id, reference_ids: [],
  })) });
const profile = () => ({ id: 'primary', name: 'Local visual', model: 'synthetic', provider_id: 'local', budget_group_id: 'account',
  enabled: true, local: true, images_allowed: true, capabilities: ['vision', 'structured_json', 'multiple_images'],
  timeout_seconds: 60, max_images_per_request: 5, chain: ['primary', 'second', 'third'] });
const limits = (minute = 20) => ({ max_concurrency: 1, requests_per_minute: minute,
  requests_per_hour: 300, requests_per_day: 2000, requests_per_month: null });
const options = () => ({ profiles: [profile(), { ...profile(), id: 'second' }, { ...profile(), id: 'third' }],
  limits: limits(10), budget_groups: [{ id: 'account', name: 'Account', limits: limits(7) }], checked_at: 100,
  usage: { available: true, error: '', persistent: true, profiles: { primary: { minute: 8 } },
    budget_groups: { account: { minute: 6 } }, sensors: { door: { minute: 2 } }, pauses: [], resets_at: {} } });

test('valid text configuration receives the same optional class defaults as the server', () => {
  const raw = [{ class_id: 'open', key: 'open', label: 'Aberta' }];
  const normalized = visual.visualClassDefaults(raw);
  assert.deepEqual(plain(normalized), [{ ...raw[0], description: '', enabled: true, reference_ids: [] }]);
  assert.equal(raw[0].reference_ids, undefined);
  const explicit = [{ ...raw[0], enabled: false, description: 'visible', reference_ids: ['photo'] }];
  assert.deepEqual(plain(visual.visualClassDefaults(explicit)), explicit);
});

test('direct description-only readiness requires source binding and two visual descriptions', () => {
  const value = configuration();
  assert.equal(ai.visualTestReady(value), true);
  assert.equal(ai.visualTestReady({ ...value, source_identity: null }), false);
  value.classes[0].description = ' ';
  assert.equal(ai.visualTestReady(value), false);
  value.classes[0].reference_ids = ['human-photo'];
  assert.equal(ai.visualTestReady(value), true);
  assert.equal(ai.visualTestReady({ ...value, strategy: 'fallback' }), false);
});

test('direct timing edits preserve dormant comparison thresholds and examples', () => {
  const value = { ...configuration(), comparison: { enter_threshold: .8, enter_margin: .2, hold_threshold: .7, hold_margin: .1 } };
  const fields = visual.visualDecisionValues(value);
  fields.maximum_frame_age_ms = '1000';
  const next = visual.applyVisualDecisionValues(value, fields);
  assert.equal(next.comparison, value.comparison);
  assert.equal(next.classes, value.classes);
  assert.equal(next.temporal.maximum_frame_age_ms, 1000);
  const noModel = { ...configuration(), comparison: null };
  assert.equal(visual.applyVisualDecisionValues(noModel, visual.visualDecisionValues(noModel)).comparison, null);
});

test('sensor budgets are strict, nullable per period, and never overwrite profile or class choices', () => {
  const value = configuration(), fields = ai.visualAiValues(value);
  fields.requests_per_minute = '';
  fields.ai_max_attempts = '3';
  const next = ai.applyVisualAiValues(value, fields);
  assert.equal(next.ai_limits.requests_per_minute, null);
  assert.equal(next.ai_limits.max_concurrency, 1);
  assert.equal(next.ai_max_attempts, 3);
  assert.equal(next.classes, value.classes);
  assert.equal(next.ai_profile_id, value.ai_profile_id);
  for (const [key, text] of [['ai_max_attempts', '4'], ['ai_timeout_ms', '120001'], ['ai_references_per_class', '0'],
    ['requests_per_day', '1.5'], ['ai_timeout_ms', ''], ['requests_per_hour', '-1']]) {
    assert.throws(() => ai.applyVisualAiValues(value, { ...fields, [key]: text }), /ai_settings_invalid/);
  }
});

test('effective limits subtract each scope independently and report unknown accounting', () => {
  const value = { ...configuration(), ai_limits: limits(6) }, data = options();
  assert.deepEqual(plain(ai.visualAiBudget(value, data, profile(), 'minute')), { limit: 6, remaining: 1 });
  data.usage.available = false;
  assert.deepEqual(plain(ai.visualAiBudget(value, data, profile(), 'minute')), { limit: 6, remaining: null });
  data.usage.available = true;
  data.usage.budget_groups.account.minute = 8;
  assert.equal(ai.visualAiBudget(value, data, profile(), 'minute').remaining, 0);
});

test('profile requirements distinguish missing capabilities, capacity and both image permissions', () => {
  const value = configuration(), selected = profile();
  assert.equal(ai.visualAiIssue(value, selected), null);
  assert.equal(ai.visualAiIssue(value, { ...selected, enabled: false }), 'ai_profile_unavailable');
  assert.equal(ai.visualAiIssue(value, { ...selected, capabilities: ['vision', 'structured_json'] }), 'ai_profile_incompatible');
  value.classes.forEach(item => { item.reference_ids = ['a', 'b', 'c']; });
  assert.equal(ai.visualAiIssue(value, selected), null); // Current plus two references per class.
  assert.equal(ai.visualAiIssue(value, { ...selected, max_images_per_request: 4 }), 'ai_image_capacity');
  assert.equal(ai.visualAiIssue(value, { ...selected, local: false }), 'ai_consent_required');
  value.ai_allow_remote_images = true;
  assert.equal(ai.visualAiIssue(value, { ...selected, local: false }), null);
  assert.equal(ai.visualAiIssue(value, { ...selected, images_allowed: false }), 'ai_images_blocked');
});

test('alternatives and pauses are scoped to the chosen chain and attempt budget', () => {
  const data = options(), value = configuration();
  assert.deepEqual(Array.from(ai.visualAiProfiles(value, data), item => item.id), ['primary']);
  value.ai_max_attempts = 2;
  assert.deepEqual(Array.from(ai.visualAiProfiles(value, data), item => item.id), ['primary', 'second']);
  data.usage.pauses = [{ scope: 'profile', id: 'other' }, { scope: 'group', id: 'account' }, { scope: 'provider', id: 'local' }];
  assert.deepEqual(Array.from(ai.visualAiPauses(data, profile()), item => item.scope), ['group', 'provider']);
});

test('historical failed or discarded AI latency is visible without certifying a current value', () => {
  const { VisualStatePreviewResult } = load('VisualStatePreviewResult.tsx', { react: { ...react, default: react } });
  const translations = load('translations.ts').visualStateTranslations['pt-BR'];
  const t = key => translations[`ext.vision.visualState.${key}`] ?? key;
  const result = { completed_at: 100, state: { status: 'unknown', class_id: null, reason: 'multimodal_pending',
    valid_for_ms: 0, ai_evaluation: { status: 'discarded', reason: 'multimodal_observation_expired',
      duration_ms: 900, retry_at: null, profile_id: 'primary', model: 'synthetic' } } };
  const html = renderToStaticMarkup(react.createElement(VisualStatePreviewResult, { result, classes: [], stale: false, t }));
  assert.match(html, /Descartada/);
  assert.match(html, /900 ms/);
  assert.match(html, /não confirma estado atual/);
  assert.match(html, /não qualifica uso em alarmes/);
  assert.match(html, /Validade restante[^<]*0 ms/);
  assert.doesNotMatch(html, /reason_multimodal/);
});

test('options API uses ingress-safe requestJson with encoded sensor and cancellation', async () => {
  const calls = [], controller = new AbortController();
  const api = load('api.ts', { '@toposync/plugin-api': { requestJson: (...args) => { calls.push(args); return Promise.resolve({ profiles: [] }); } } });
  await api.readVisualAiOptions('door/one', controller.signal);
  assert.equal(calls[0][0], '/api/vision/visual-state/ai-options?sensor_id=door%2Fone');
  assert.equal(calls[0][1].signal, controller.signal);
});

test('image capacity failure is distinct from a renewable request budget', () => {
  const { VisualStatePreviewResult } = load('VisualStatePreviewResult.tsx', { react: { ...react, default: react } });
  const translations = load('translations.ts').visualStateTranslations['pt-BR'];
  const t = key => translations[`ext.vision.visualState.${key}`] ?? key;
  const render = reason => renderToStaticMarkup(react.createElement(VisualStatePreviewResult, {
    result: { completed_at: 100, state: { status: 'unknown', class_id: null, reason, valid_for_ms: 0 } }, classes: [], stale: false, t,
  }));
  assert.match(render('visual_state_image_limit'), /capacidade do perfil/);
  assert.doesNotMatch(render('visual_state_image_limit'), /orçamento de consultas/);
  for (const reason of ['minute_limit', 'group_day_limit', 'sensor_month_limit']) {
    assert.match(render(reason), /orçamento de consultas esgotou/);
  }
});
