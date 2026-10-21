const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const crypto = require('node:crypto');
const ts = require('typescript');

function load(name, modules = {}, globals = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', name), 'utf8');
  const context = { ...globals, exports: {}, crypto, URLSearchParams, require: name => {
    assert.ok(name in modules, `unexpected import: ${name}`); return modules[name];
  } };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
  } }).outputText, context);
  return context.exports;
}
const model = load('visualState.ts');
const fresh = () => model.newVisualConfiguration(index => `Estado ${index}`);
const evidence = { source_identity: 'camera:synthetic', preparation_revision: 'revision', prepared_region: true };

test('rendered evidence distinguishes a retained value, candidate and historical analysis without accuracy claims', () => {
  const react = require('react'), { renderToStaticMarkup } = require('react-dom/server');
  const { VisualStatePreviewResult } = load('VisualStatePreviewResult.tsx', { react: { ...react, default: react } });
  const translations = load('translations.ts').visualStateTranslations['pt-BR'];
  const t = key => translations[`ext.vision.visualState.${key}`] ?? key;
  const classes = [{ class_id: 'closed', label: 'Porta fechada', reference_ids: ['closed-photo'] },
    { class_id: 'open', label: 'Porta aberta', reference_ids: ['open-photo'] }];
  const result = { completed_at: 100, state: { status: 'pending', class_id: 'closed', reason: 'confirming',
    runtime_epoch: 'epoch', state_version: 3, preparation_revision: 'prepare', reference_revision: 'references', model_revision: 'model',
    candidate: { class_id: 'open', started_at: 99 }, last_confirmed: { class_id: 'closed', observed_at: 98 }, analysis_age_ms: 100,
    last_analysis: { observed_at: 99.9, duration_ms: 20, region_quality: 'valid', freshness_basis: 'receipt', method: 'local',
      similarity: .95, margin: .08, declared_confidence: null, reason: 'entry_match', reference_ids: ['open-photo'],
      sample: { capture_instance: 'capture', generation: 1, sequence: 3 } } } };
  const render = () => renderToStaticMarkup(react.createElement(VisualStatePreviewResult, { result, classes, stale: true, t }));
  const pending = render();
  assert.match(pending, /<strong>[^<]*Porta fechada/);
  assert.match(pending, /Aguardando confirmação<\/dt><dd>Porta aberta/);
  assert.match(pending, /Último estado confirmado \(histórico\)/);
  assert.match(pending, /Fotos de referência mais próximas<\/dt><dd>Porta aberta · 1/);
  assert.match(pending, /0\.9500/);
  assert.match(pending, /não são precisão medida/);
  assert.match(pending, /horário físico de captura não verificado/);
  for (const [status, reason] of [['unknown', 'observation_expired'], ['unavailable', 'input_unobservable']]) {
    result.state = { ...result.state, status, reason, class_id: null, candidate: null };
    const html = render();
    assert.doesNotMatch(html.match(/<strong>(.*?)<\/strong>/)[1], /Porta/);
    assert.ok(html.includes(t(`reason_${reason}`)));
    assert.match(html, /Último estado confirmado \(histórico\)/);
  }
  result.state.reason = 'private-description';
  assert.doesNotMatch(render(), /private-description/);
  assert.ok(render().includes(t('reason_other')));
});

test('decision controls require explicit thresholds and reject unstable or invalid intervals', () => {
  const config = fresh(), defaults = model.visualDecisionValues(config);
  assert.throws(() => model.applyVisualDecisionValues(config, defaults), /visual_decision_invalid/);
  const fields = { ...defaults, enter_threshold: '.95', enter_margin: '.05', hold_threshold: '.9', hold_margin: '.025' };
  const applied = model.applyVisualDecisionValues(config, fields);
  assert.equal(applied.comparison.enter_threshold, .95);
  assert.equal(applied.temporal.confirmations, 2);
  assert.equal(applied.evaluation_interval_ms, 250);
  assert.equal(applied.classes, config.classes);
  for (const invalid of [{ hold_threshold: '.99' }, { hold_margin: '.1' }, { confirmations: '1' },
    { confirmations: '2.5' }, { evaluation_interval_ms: '49' }, { minimum_separation_ms: '1000' },
    { validity_ms: '100' }, { maximum_frame_age_ms: 'Infinity' }, { enter_threshold: '' }]) {
    assert.throws(() => model.applyVisualDecisionValues(config, { ...fields, ...invalid }), /visual_decision_invalid/);
  }
});

test('changing the model clears global and class comparison while preserving photos and temporal policy', () => {
  const config = fresh(), comparison = { enter_threshold: .9, enter_margin: .2, hold_threshold: .8, hold_margin: .1 };
  config.comparison = comparison;
  config.temporal = { confirmations: 3 };
  config.classes[0].comparison = comparison;
  config.classes[0].reference_ids = ['photo'];
  const choice = { model_id: 'model', revision: 'revision', artifact_present: true };
  const changed = model.selectVisualModel(config, choice);
  assert.equal(changed.model_id, choice.model_id);
  assert.equal(changed.model_revision, choice.revision);
  assert.equal(changed.comparison, null);
  assert.equal(changed.classes[0].comparison, null);
  assert.equal(changed.classes[0].reference_ids[0], 'photo');
  assert.equal(changed.temporal, config.temporal);
  assert.equal(config.comparison, comparison);
  assert.equal(model.selectVisualModel(changed, choice), changed);
  assert.throws(() => model.selectVisualModel(config, { ...choice, artifact_present: false }), /unavailable/);
  const recalibrated = { ...changed, comparison };
  assert.equal(model.selectVisualModel(recalibrated, { ...choice, revision: 'new' }).comparison, null);
});

test('empty and catalog-default panels initialize unique identities only when editable', () => {
  const identities = new Set();
  for (const readonly of [true, false]) for (const config of [{}, { sensor_id: 'unconfigured', classes: [] }]) {
    const effects = [], changes = [];
    const react = {
      createElement: (type, properties, ...children) => ({ type, properties, children }),
      useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}],
      useRef: current => ({ current }), useCallback: callback => callback,
      useEffect: callback => effects.push(callback), useLayoutEffect: callback => effects.push(callback),
    };
    const panel = load('VisualStatePanel.tsx', { react: { ...react, default: react },
      '@toposync/plugin-api': {}, './api': {}, './visualState': model, './styles': { styles: '' },
      './VisualStateModelSettings': { VisualStateModelSettings: () => null },
      './VisualStateDecisionSettings': { VisualStateDecisionSettings: () => null },
      './VisualStateAiSettings': { VisualStateAiSettings: () => null },
      './VisualPublicationSettings': { VisualPublicationSettings: () => null },
      './visualPublication': load('visualPublication.ts'),
      './visualStateAi': load('visualStateAi.ts'),
      './VisualSuggestionReview': { VisualStateSuggestions: () => null },
      './visualStateSuggestions': load('visualStateSuggestions.ts'),
      './VisualStatePreviewResult': { VisualStatePreviewResult: () => null },
      './VisualStateRegionSettings': { VisualStateRegionSettings: () => null } });
    panel.VisualStatePanel({ config, replaceConfig: value => changes.push(value),
      i18n: { useI18n: () => ({ t: key => key }) },
      referenceCapture: readonly ? { available: false, reason: 'read_only' } : undefined });
    effects.forEach(effect => effect());
    assert.equal(changes.length, readonly ? 0 : 1);
    if (!readonly) {
      assert.notEqual(changes[0].sensor_id, 'unconfigured');
      assert.equal(changes[0].classes.length, 2);
      assert.ok(!identities.has(changes[0].sensor_id));
      identities.add(changes[0].sensor_id);
    }
  }
});

test('class setup has stable unique identifiers without inventing capture evidence', () => {
  const config = fresh();
  assert.equal(config.source_identity, null);
  assert.equal(config.preparation_revision, null);
  assert.equal(config.use_full_image, false);
  assert.notEqual(config.classes[0].class_id, config.classes[1].class_id);
  const key = config.classes[0].key;
  config.classes[0].label = 'Porta aberta';
  assert.equal(config.classes[0].key, key);
  const next = model.newVisualClass([config.classes[1]], 'Outra classe');
  assert.notEqual(next.key, config.classes[1].key);
  assert.equal(model.visualClassesValid([...config.classes, next]), true);
});

test('capture requires prepared region or explicit full image, and binds only verified identity', () => {
  const config = fresh(), noRegion = { ...evidence, prepared_region: false };
  assert.throws(() => model.bindVisualConfiguration(config, noRegion), /reference_region_required/);
  const full = model.bindVisualConfiguration({ ...config, use_full_image: true }, noRegion);
  assert.equal(full.use_full_image, true);
  assert.equal(full.region_id, null);
  const cropped = model.bindVisualConfiguration(config, evidence);
  assert.equal(cropped.source_identity, evidence.source_identity);
  assert.equal(cropped.region_id, evidence.preparation_revision);
  assert.equal(cropped.use_full_image, false);
});

test('existing references cannot move to another source or preparation', () => {
  const config = model.bindVisualConfiguration(fresh(), evidence);
  config.classes[0].reference_ids = ['photo'];
  for (const changed of [{ source_identity: 'another-camera' }, { preparation_revision: 'new-crop' }]) {
    assert.throws(() => model.bindVisualConfiguration(config, { ...evidence, ...changed }), /reference_context_changed/);
  }
  assert.equal(model.bindVisualConfiguration(config, evidence).classes[0].reference_ids[0], 'photo');
});

test('undo restores references and a removed class while preserving subsequent text edits', () => {
  const previous = model.bindVisualConfiguration(fresh(), evidence);
  previous.classes[0].reference_ids = ['photo'];
  const current = { ...previous, scene_description: 'Edited scene', classes: [
    { ...previous.classes[0], label: 'Renamed', description: 'Edited description', reference_ids: [] },
  ] };
  const restored = model.restoreVisualReferences(current, previous);
  assert.equal(restored.classes.length, 2);
  assert.equal(restored.classes[0].reference_ids[0], 'photo');
  assert.equal(restored.classes[0].label, 'Renamed');
  assert.equal(restored.classes[0].description, 'Edited description');
  assert.equal(restored.scene_description, 'Edited scene');
});

test('invalid keys, empty labels, too few enabled classes and excessive references stop capture', () => {
  for (const change of [
    item => { item.key = 'unknown'; }, item => { item.key = 'Has spaces'; },
    item => { item.label = '  '; }, item => { item.enabled = false; },
    item => { item.reference_ids = Array.from({ length: 31 }, (_, index) => `photo${index}`); },
  ]) {
    const config = fresh(); change(config.classes[0]); assert.equal(model.visualClassesValid(config.classes), false);
  }
  const config = fresh(); config.classes[1].key = config.classes[0].key;
  assert.equal(model.visualClassesValid(config.classes), false);
});

test('reference transport retains revision, receipt, abort and operation identity across retry', async () => {
  const calls = [], api = load('api.ts', { '@toposync/plugin-api': {
    requestJson: async (url, options) => { calls.push({ url, options }); return {}; },
    resolveToposyncUrl: url => '/ingress/test' + url,
  } });
  const draft = { draft_id: 'draft/escaped', revision: 7 }, signal = new AbortController().signal;
  const frame = { blob: new Blob(['png']), receipt: 'signed-proof' };
  await api.captureVisualReference(draft, 'class', 'same-operation', frame, signal);
  await api.captureVisualReference(draft, 'class', 'same-operation', frame, signal);
  assert.equal(calls[0].url, calls[1].url);
  assert.match(calls[0].url, /draft%2Fescaped\/captures\?class_id=class&operation_id=same-operation&expected_revision=7$/);
  assert.equal(calls[0].options.body, frame.blob);
  assert.equal(calls[0].options.headers['X-Toposync-Capture-Receipt'], frame.receipt);
  assert.equal(calls[0].options.signal, signal);
  assert.equal(api.visualReferenceUrl('draft', 'photo', false), '/ingress/test/api/vision/visual-state/drafts/draft/references/photo/image?thumbnail=false');
  await api.updateVisualDraft(draft, fresh(), signal);
  assert.equal(JSON.parse(calls[2].options.body).expected_revision, 7);
  assert.equal(calls[2].options.signal, signal);
  const pipeline = { graph: { nodes: [{ id: 'source' }], edges: [] } };
  await api.testVisualState(fresh(), pipeline, signal);
  assert.equal(calls[3].url, '/api/vision/visual-state/preview');
  assert.deepEqual(JSON.parse(calls[3].options.body).pipeline, pipeline);
  assert.equal(calls[3].options.signal, signal);
});


test('canonical source must be explicit and unique, and changing a region cannot silently reuse photos', () => {
  const node = { operator: 'camera.source', config: { camera_id: 'camera', source_id: 'video' } };
  const region = { camera_id: 'camera', source_id: 'video', name: 'Door', framing: 'passive',
    reference: { artifact_id: 'panorama', revision: 2, geometry_digest: 'digest' },
    strokes: [{ op: 'paint', diameter01: .1, points01: [[.5, .5]] }] };
  assert.equal(model.visualSource(null), null);
  assert.equal(model.visualSource({ graph: { nodes: [node, node] } }), null);
  assert.equal(model.visualSource({ graph: { nodes: [{ ...node, config: { rtsp_url: 'test' } }] } }), null);
  assert.equal(model.visualSource({ graph: { nodes: [node] } }).source_id, 'video');
  const selected = model.replaceVisualRegion(fresh(), region);
  const previous = model.bindVisualConfiguration(selected, evidence);
  previous.classes[0].reference_ids = ['human-photo'];
  assert.throws(() => model.replaceVisualRegion(previous, null, true), /reference_context_changed/);
  const detached = { ...previous, classes: previous.classes.map(item => ({ ...item, reference_ids: [] })) };
  const full = model.replaceVisualRegion(detached, null, true);
  assert.equal(full.preparation_revision, null);
  assert.equal(full.canonical_region, null);
  const restored = model.restoreVisualReferences(full, previous);
  assert.equal(restored.canonical_region.reference.revision, 2);
  assert.equal(restored.classes[0].reference_ids[0], 'human-photo');
  assert.equal(restored.preparation_revision, evidence.preparation_revision);
});

test('canonical capture preserves ingress and signed evidence and rejects malformed replies', async () => {
  const supplied = { source_identity: 'camera:one:video', preparation_revision: 'a'.repeat(64),
    sample: { capture_instance: 'decoder', generation: 0, sequence: 1 }, observed_at: 123,
    freshness_basis: 'receipt', historical_fallback: false, prepared_region: true };
  let current = supplied, status = 200, request;
  const api = load('api.ts', { '@toposync/plugin-api': { resolveToposyncUrl: path => '/ingress' + path } }, {
    fetch: async (url, options) => {
      request = { url, options };
      return new Response(status === 200 ? new Blob(['pixels'], { type: 'image/png' }) : JSON.stringify({ detail: 'region_unavailable' }), {
        status, headers: { 'content-type': status === 200 ? 'image/png' : 'application/json',
          'X-Toposync-Capture-Evidence': JSON.stringify(current), 'X-Toposync-Capture-Receipt': 'signed-receipt' } });
    },
  });
  const signal = new AbortController().signal;
  const captured = await api.captureVisualFrame(fresh(), { graph: {} }, signal);
  assert.equal(request.url, '/ingress/api/vision/visual-state/capture-frame');
  assert.equal(request.options.signal, signal);
  assert.equal(captured.receipt, 'signed-receipt');
  assert.equal(captured.evidence.preparation_revision, supplied.preparation_revision);
  assert.deepEqual(JSON.parse(request.options.body).pipeline, { graph: {} });
  for (const invalid of [null, { ...supplied, prepared_region: false }, { ...supplied, historical_fallback: true },
    { ...supplied, sample: { ...supplied.sample, capture_instance: 123 } }, { ...supplied, preparation_revision: [supplied.preparation_revision] },
    { ...supplied, sample: { ...supplied.sample, sequence: -1 } }, { ...supplied, preparation_revision: 'forged' }]) {
    current = invalid;
    await assert.rejects(api.captureVisualFrame(fresh(), {}, signal), /reference_capture_evidence_invalid/);
  }
  status = 409;
  await assert.rejects(api.captureVisualFrame(fresh(), {}, signal), error => error.status === 409 && error.message === 'region_unavailable');
});

for (const changedContext of ['references', 'source', 'readonly']) test(`late panorama reference cannot open after ${changedContext} changes`, async () => {
  const slots = []; let cursor = 0, dependencies, cleanup, resolve, signal;
  const react = { createElement: (type, properties, ...children) => ({ type, properties, children }),
    useState: initial => { const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = value; }]; },
    useRef: initial => { const index = cursor++; return slots[index] ??= { current: initial }; },
    useLayoutEffect: (effect, next) => {
      if (!dependencies || next.some((value, index) => value !== dependencies[index])) {
        cleanup?.(); dependencies = next; cleanup = effect();
      }
    } };
  const component = load('VisualStateRegionSettings.tsx', { react: { ...react, default: react }, './visualState': model,
    '@toposync/plugin-api': { resolveToposyncUrl: value => value, requestJson: (_, options) => {
      signal = options.signal; return new Promise(done => { resolve = done; });
    } } }, { AbortController });
  const props = { ui: { ImageMaskEditor: () => null }, value: fresh(), disabled: false, t: key => key,
    change: () => assert.fail('a late reference cannot change configuration'),
    pipeline: { graph: { nodes: [{ operator: 'camera.source', config: { camera_id: 'one', source_id: 'video' } }] } } };
  const render = () => { cursor = 0; return component.VisualStateRegionSettings(props); };
  const root = render();
  root.children.find(child => child?.type === 'button').properties.onClick();
  assert.equal(signal.aborted, false);
  if (changedContext === 'references') props.value.classes[0].reference_ids = ['existing-human-photo'];
  if (changedContext === 'source') props.pipeline.graph.nodes[0].config.source_id = 'another-stream';
  if (changedContext === 'readonly') props.disabled = true;
  render();
  assert.equal(signal.aborted, true);
  resolve({ id: 'old-reference', revision: 1, image_url: '/old.png', geometry: { model_digest: 'old', projection: 'image_plane' } });
  await new Promise(done => setImmediate(done));
  assert.equal(slots[0], null); // No drawing modal from the discarded request.
  assert.equal(slots[1], false);
  assert.equal(slots[2], false);
  cleanup?.();
});
