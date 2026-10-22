const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const crypto = require('node:crypto');
const ts = require('typescript');

function load(file, modules = {}, globals = {}) {
  const context = { exports: {}, crypto, structuredClone, URLSearchParams, ...globals,
    require: name => { assert.ok(name in modules, name); return modules[name]; } };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText, context);
  return context.exports;
}
const model = load('visualState.ts');
const messages = load('translations.ts').visualStateTranslations;
const translate = (key, values = {}) => {
  const name = `ext.vision.visualState.${key}`;
  assert.ok(messages.en[name] && messages['pt-BR'][name], name);
  return messages['pt-BR'][name].replace(/{{(\w+)}}/g, (_, field) => values[field]);
};
const current = () => model.newVisualConfiguration(index => `Estado ${index}`);
const restored = (value = current()) => ({ draft_id: 'restored', revision: 0, expires_at: Date.now() / 1000 + 3600,
  configuration: { ...value, reference_revision: 'restored-revision', home_assistant: { enabled: false },
    classes: value.classes.map(category => ({ ...category, label: `Restaurada ${category.label}` })) } });

function harness(restore) {
  let index = 0, dirty = false, effects = [], tree, task, controller;
  const slots = [], changes = [], failures = [], calls = [];
  const react = {
    createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useState(initial) {
      const position = index++;
      if (!(position in slots)) slots[position] = typeof initial === 'function' ? initial() : initial;
      return [slots[position], next => { if (!Object.is(slots[position], next)) { slots[position] = next; dirty = true; } }];
    },
    useEffect(callback, dependencies) {
      const position = index++, previous = slots[position];
      if (!previous || dependencies.some((value, i) => !Object.is(value, previous[i]))) { slots[position] = dependencies; effects.push(callback); }
    },
  };
  const component = load('VisualStateBackup.tsx', { react: { ...react, default: react }, './api': {
    visualBackupLimit: 64 * 1024 * 1024,
    restoreVisualBackup: (...args) => { calls.push(args); return restore(...args); },
    visualReferenceUrl: (draft, reference) => `/ingress/fixture/${draft}/${reference}`,
  } }).VisualStateBackup;
  const props = { value: current(), draft: null, context: 'source-one', disabled: false, working: false, canExport: true, t: translate,
    run: (name, work) => { controller = new AbortController(); task = work(controller.signal).catch(error => failures.push(error)); return task; },
    apply: (value, draft) => { changes.push(value); props.value = value; props.draft = draft; },
  };
  function render() {
    for (let pass = 0; pass < 4; pass++) {
      index = 0; effects = []; dirty = false; tree = component(props); effects.forEach(callback => callback());
      if (!dirty) break;
    }
    return tree;
  }
  function nodes(node) {
    if (Array.isArray(node)) return node.flatMap(nodes);
    if (!node || typeof node !== 'object') return [];
    return [node, ...node.children.flatMap(nodes)];
  }
  const button = key => { render(); return nodes(tree).find(node => node.type === 'button' && node.children.includes(translate(key))); };
  return { props, calls, changes, failures, render, nodes, button, abort: () => controller.abort(), settled: () => task,
    choose: file => { render(); nodes(tree).find(node => node.type === 'input').props.onChange({ target: { files: [file], value: 'selected' } }); },
  };
}

test('import is reviewed before replacement; undo restores the complete prior configuration', async () => {
  const response = restored();
  const h = harness(async () => response);
  const before = structuredClone(h.props.value);
  h.choose({ size: 100, name: 'sensor.zip' }); await h.settled();
  assert.equal(h.changes.length, 0);
  assert.ok(h.button('backup_apply'));
  h.button('backup_apply').props.onClick();
  h.props.value = Object.fromEntries(Object.entries(h.props.value).reverse());
  assert.equal(h.props.value.sensor_id, response.configuration.sensor_id);
  assert.equal(h.props.value.home_assistant.enabled, false);
  h.button('backup_undo').props.onClick();
  assert.deepEqual(h.props.value, before);
  assert.equal(h.props.draft, null);
  assert.equal(h.button('backup_undo'), undefined);
});

test('another edit or input context removes old review and undo instead of overwriting new work', async () => {
  const h = harness(async () => restored());
  h.choose({ size: 100 }); await h.settled();
  h.props.context = 'different-source';
  assert.equal(h.button('backup_apply'), undefined);
  h.choose({ size: 100 }); await h.settled(); h.button('backup_apply').props.onClick();
  assert.ok(h.button('backup_undo'));
  h.props.value = { ...h.props.value, model_revision: 'another-edit' };
  assert.equal(h.button('backup_undo'), undefined);
});

test('late response after cancellation cannot apply; retry reuses exactly the same operation', async () => {
  let resolve;
  let waiting = true;
  const response = restored();
  const h = harness(async () => waiting ? new Promise(done => { resolve = done; }) : response);
  const file = { size: 100, name: 'sensor.zip' };
  h.choose(file); h.abort(); resolve(response); await h.settled();
  assert.equal(h.button('backup_apply'), undefined);
  assert.equal(h.changes.length, 0);
  waiting = false;
  h.button('backup_retry').props.onClick(); await h.settled();
  assert.equal(h.calls[0][0], h.calls[1][0]);
  assert.equal(h.calls[0][1], h.calls[1][1]);
  assert.ok(h.button('backup_apply'));
});

test('invalid, oversized and expired imports preserve the current configuration', async () => {
  const h = harness(async () => { throw Object.assign(new Error('reference_conflict'), { status: 409 }); });
  const before = h.props.value;
  h.choose({ size: 0 }); assert.equal(h.calls.length, 0);
  h.choose({ size: 64 * 1024 * 1024 + 1 }); assert.equal(h.calls.length, 0);
  h.choose({ size: 100 }); await h.settled();
  assert.equal(h.props.value, before);
  assert.equal(h.button('backup_apply'), undefined);
  assert.equal(h.button('backup_retry'), undefined);
  const expired = harness(async () => ({ ...restored(), expires_at: 1 }));
  expired.choose({ size: 100 }); await expired.settled(); expired.button('backup_apply').props.onClick();
  assert.equal(expired.changes.length, 0);
  assert.equal(expired.button('backup_apply'), undefined);
});

test('late import after switching input cannot create a review for the new context', async () => {
  let resolve;
  const h = harness(async () => new Promise(done => { resolve = done; }));
  h.choose({ size: 100 });
  h.props.context = 'other-sensor-input'; h.render();
  resolve(restored()); await h.settled();
  assert.equal(h.button('backup_apply'), undefined);
  assert.equal(h.changes.length, 0);
});

test('readonly controls and pending validation are exposed without executing class labels as markup', async () => {
  const config = current(); config.classes[0].label = '<script>private</script>';
  const h = harness(async () => restored(config));
  h.choose({ size: 100 }); await h.settled();
  const tree = h.render();
  assert.ok(h.nodes(tree).some(node => node.type === 'li' && node.children.some(text => typeof text === 'string' && text.includes('<script>'))));
  assert.ok(!h.nodes(tree).some(node => node.type === 'script' || node.props.dangerouslySetInnerHTML));
  h.props.disabled = true;
  assert.equal(h.nodes(h.render()).find(node => node.type === 'fieldset').props.disabled, true);
});

test('backup requests preserve ingress, draft revision and idempotency parameters', async () => {
  const requests = [];
  const api = load('api.ts', { '@toposync/plugin-api': {
    resolveToposyncUrl: path => `/ingress/fixture${path}`,
    requestJson: (path, options) => { requests.push({ path, options }); return Promise.resolve(restored()); },
  } }, { fetch: async (path, options) => { requests.push({ path, options }); return {
    ok: true, blob: async () => new Blob(['zip'], { type: 'application/zip' }),
  }; } });
  const signal = new AbortController().signal;
  const file = { size: 100 };
  await api.exportVisualBackup({ draft_id: 'draft', revision: 7 }, signal);
  assert.equal(requests[0].path, '/ingress/fixture/api/vision/visual-state/drafts/draft/backup?expected_revision=7');
  await api.restoreVisualBackup(file, 'stable-operation', signal);
  assert.equal(requests[1].path, '/api/vision/visual-state/backups/restore?operation_id=stable-operation');
  assert.equal(requests[1].options.body, file);
  assert.equal(requests[1].options.headers['content-type'], 'application/zip');
  assert.equal(requests[1].options.signal, signal);
});
