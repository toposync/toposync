const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const react = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function load(name, modules = {}) {
  const context = { exports: {}, URLSearchParams, require: name => {
    assert.ok(name in modules, `unexpected import: ${name}`); return modules[name];
  } };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', name), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText, context);
  return context.exports;
}
const helpers = load('visualStateSuggestions.ts');
const config = () => ({ sensor_id: 'sensor', strategy: 'fallback', classes: [
  { class_id: 'open', label: 'Aberta', enabled: true, reference_ids: ['human'] },
  { class_id: 'closed', label: 'Fechada', enabled: true, reference_ids: [] },
] });
const candidate = () => ({ suggestion_id: 'suggestion', status: 'pending', compatible: true, revision: 1,
  expires_at: Date.now() / 1000 + 3600, reviews: [], reference: { class_id: 'open',
    evidence: { observed_at: Date.now() / 1000, sample: { capture_instance: 'capture', generation: 1, sequence: 2 } },
    provenance: { profile_id: 'profile', provider_id: 'provider', model: 'synthetic', reason: '<script>private</script>',
      runtime_epoch: 'original-epoch', reference_revision: 'original-references', ai_configuration_revision: 'original-ai', declared_confidence: .9 } } });
const approved = () => ({ ...candidate(), status: 'accepted', reviews: [
  { action: 'accept', actor_id: 'reviewer', reviewed_at: Date.now() / 1000, class_id: 'closed', reference_id: 'approved' },
] });

test('suggestion limits preserve policy and reject empty, fractional and unbounded values', () => {
  const value = { ...config(), suggestions: { mode: 'off', maximum_pending: 20, retention_days: 7 } };
  const changed = helpers.applySuggestionLimits(value, '100', '30');
  assert.equal(changed.suggestions.mode, 'off');
  assert.equal(changed.suggestions.maximum_pending, 100);
  assert.equal(changed.classes, value.classes);
  for (const [maximum, days] of [['', '7'], ['0', '7'], ['101', '7'], ['2.5', '7'], ['20', ''], ['20', '31'], ['20', 'Infinity']]) {
    assert.throws(() => helpers.applySuggestionLimits(value, maximum, days), /suggestion_invalid_limits/);
  }
});

test('restoration reattaches only an existing human-approved reference to its corrected enabled class', () => {
  const value = config(), item = approved(), restored = helpers.restoreApprovedSuggestion(value, item);
  assert.equal(restored.classes[0], value.classes[0]);
  assert.deepEqual([...restored.classes[1].reference_ids], ['approved']);
  assert.equal(helpers.acceptedSuggestion(restored, item).attached, true);
  assert.deepEqual(value.classes[1].reference_ids, []);
  for (const invalid of [candidate(), { ...item, compatible: false }, { ...item, status: 'rejected' }, { ...item, reviews: [] }]) {
    assert.throws(() => helpers.restoreApprovedSuggestion(value, invalid), /reference_conflict/);
  }
  assert.throws(() => helpers.restoreApprovedSuggestion(restored, item), /reference_conflict/);
  assert.throws(() => helpers.restoreApprovedSuggestion({ ...value, classes: [value.classes[0]] }, item), /reference_conflict/);
  assert.throws(() => helpers.restoreApprovedSuggestion({ ...value, classes: value.classes.map(c => ({ ...c, enabled: false })) }, item), /reference_conflict/);
});

test('suggestion requests preserve ingress, abort signal, expected revisions and idempotency identity', async () => {
  const calls = [], signal = new AbortController().signal;
  const api = load('api.ts', { '@toposync/plugin-api': {
    requestJson: async (...args) => { calls.push(args); return {}; }, resolveToposyncUrl: value => `/ingress${value}`,
  } });
  const draft = { draft_id: 'draft/id', revision: 7 }, item = { ...candidate(), suggestion_id: 'photo/id', revision: 3 };
  await api.readVisualSuggestions(draft.draft_id, 100, signal);
  await api.reviewVisualSuggestion(draft, item, 'accept', 'same-operation', 'closed', signal);
  await api.reviewVisualSuggestion(draft, item, 'accept', 'same-operation', 'closed', signal);
  assert.match(calls[0][0], /draft%2Fid\/suggestions\?offset=100$/);
  assert.equal(calls[0][1].signal, signal);
  assert.match(calls[1][0], /photo%2Fid\/review$/);
  assert.equal(calls[1][1].body, calls[2][1].body);
  assert.deepEqual(JSON.parse(calls[1][1].body), { action: 'accept', operation_id: 'same-operation', expected_revision: 7, expected_suggestion_revision: 3, class_id: 'closed' });
  assert.equal(calls[1][1].signal, signal);
  assert.match(api.visualSuggestionUrl(draft.draft_id, item.suggestion_id, false), /^\/ingress\/api\/vision\/visual-state\/drafts\/draft%2Fid\/suggestions\/photo%2Fid\/image\?thumbnail=false$/);
});

const messages = load('translations.ts').visualStateTranslations['pt-BR'];
const t = key => { assert.ok(messages[`ext.vision.visualState.${key}`], `missing translation: ${key}`); return messages[`ext.vision.visualState.${key}`]; };
const { VisualStateSuggestions } = load('VisualSuggestionReview.tsx', { react: { ...react, default: react }, './visualStateSuggestions': helpers,
  './api': { visualSuggestionUrl: (draft, id) => `/private/${draft}/${id}` } });
function render(item, overrides = {}) {
  return renderToStaticMarkup(react.createElement(VisualStateSuggestions, { value: config(), disabled: false, stale: false, t,
    data: { items: [item], next_offset: null, summary: { pending: 20, maximum_per_class: 10, policy: { maximum_pending: 20 } } },
    draftId: 'draft', offset: 0, ...overrides }));
}

test('review controls show origin and bounded counts without promoting history to current state or executing markup', () => {
  const html = render(candidate());
  assert.match(html, /A coleta está cheia/);
  assert.match(html, /Confirmar ou corrigir classe/);
  assert.match(html, /original-epoch/);
  assert.match(html, /não é precisão medida/);
  assert.match(html, /&lt;script&gt;private&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /<img[^>]*src="\/private\/draft\/suggestion"/);
  assert.match(html, /<button[^>]*>Aprovar foto<\/button>/);
});

test('incompatible and stale cards block approval while rejection remains available for old semantics', () => {
  const incompatible = render({ ...candidate(), compatible: false });
  assert.match(incompatible, /<button[^>]*disabled=""[^>]*>Aprovar foto/);
  assert.match(incompatible, /<button type="button" class="chipButton">Rejeitar foto/);
  const stale = render(candidate(), { stale: true });
  assert.match(stale, /<button[^>]*disabled=""[^>]*>Rejeitar foto/);
  assert.match(render(candidate(), { disabled: true }), /<fieldset disabled="">/);
  const expired = render({ ...candidate(), expires_at: 1 });
  assert.doesNotMatch(expired, /Aprovar foto|<img/);
});

test('approved drafts expose explicit restoration or undo; expired approved references can still be undone', () => {
  const item = approved();
  assert.match(render(item), /Adicionar referência já aprovada/);
  assert.match(render(item), /Classe aprovada[^<]*: Fechada/);
  assert.doesNotMatch(render(item), />Desfazer revisão<\/button>/);
  const value = helpers.restoreApprovedSuggestion(config(), item);
  const html = render({ ...item, expires_at: 1 }, { value });
  assert.match(html, /<img/);
  assert.match(html, />Desfazer revisão<\/button>/);
  assert.doesNotMatch(html, /Adicionar referência já aprovada/);
  assert.match(render({ ...candidate(), status: 'rejected' }), />Desfazer revisão<\/button>/);
});
