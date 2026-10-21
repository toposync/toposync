const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const crypto = require('node:crypto');
const ts = require('typescript');

function load(name, modules = {}) {
  const source = fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', name), 'utf8');
  const context = { exports: {}, crypto, URLSearchParams, require: name => {
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
      '@toposync/plugin-api': {}, './api': {}, './visualState': model, './styles': { styles: '' } });
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
});
