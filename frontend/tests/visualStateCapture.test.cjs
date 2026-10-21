const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');
const vm = require('node:vm');

const directory = '../src/ui/screens/pipelines/';
const compile = source => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
} }).outputText;
function load(file, modules = {}, globals = {}) {
  const context = { exports: {}, AbortController, DOMException, structuredClone, ...globals,
    require(name) { assert.ok(name in modules, `unexpected import: ${name}`); return modules[name]; } };
  vm.runInNewContext(compile(fs.readFileSync(path.join(__dirname, file), 'utf8')), context);
  return context.exports;
}
const constants = load(directory + 'constants.ts');
const utils = load(directory + 'utils.ts', { './constants': constants, '../../../util/i18n': { i18n: { t: key => key } } });
const snapshots = load(directory + 'editor/panels/pipelineStepSnapshots.ts', {
  '../../utils': utils, '@toposync/plugin-api': { resolveToposyncUrl: url => '/ingress/test' + url },
});
const capture = load(directory + 'editor/panels/pipelineReferenceCapture.ts', {
  react: {}, '../../../../../util/api': {},
});
const step = (operatorId, config, nodeId) => ({ uid: nodeId, nodeId, operatorId, configText: JSON.stringify(config) });
const operators = Object.fromEntries(['camera.source', 'camera.image_crop', 'vision.visual_state'].map(operator => [operator, {
  id: operator, capabilities: operator === 'camera.source' ? ['source'] : [],
  inputs: operator === 'camera.source' ? [] : [{ name: 'in' }], outputs: [{ name: 'out' }],
}]));
const steps = [step('camera.source', { camera_id: 'actual-camera', source_id: 'night' }, 'source'),
  step('camera.image_crop', { left: 20, right: 80 }, 'crop'), step('vision.visual_state', {}, 'sensor')];
const resolve = (items = steps, server = 'local') => snapshots.buildPipelineStepReferenceRequest(items, items.length - 1, 'draft', 'sensor', operators, server);
const plain = value => JSON.parse(JSON.stringify(value));

test('reference request retains the actual upstream slice and refuses remote relocation', () => {
  const result = resolve();
  assert.equal(result.enabled, true);
  assert.deepEqual(plain(result.request.pipeline.graph.nodes.map(node => node.config)), [
    { camera_id: 'actual-camera', source_id: 'night' }, { left: 20, right: 80 },
  ]);
  assert.equal(result.request.pipeline.processing_server_id, 'local');
  assert.equal(result.request.purpose, 'reference');
  assert.equal(result.request.fallback_snapshot, undefined);
  for (const server of ['remote', '']) assert.equal(resolve(steps, server).reason.code, 'remote_execution');
});

test('invalid inputs and unavailable sources never produce an executable capture request', () => {
  assert.equal(resolve([steps[2]]).reason.code, 'no_camera_source');
  assert.equal(resolve([step('camera.source', {}, 'source'), steps[2]]).reason.code, 'no_camera_selected');
  assert.equal(resolve([steps[0], { ...steps[1], configText: '{bad' }, steps[2]]).reason.code, 'invalid_graph');
});

function pendingSession() {
  const pending = [];
  const session = capture.createPipelineReferenceCapture(resolve().request,
    (request, signal) => new Promise((resolve, reject) => pending.push({ request, signal, resolve, reject })));
  return { session, pending };
}
test('replacing a capture rejects late transport results without clearing the newer request', async () => {
  const { session, pending } = pendingSession();
  const first = session.capture(), rejected = assert.rejects(first, { name: 'AbortError' });
  const second = session.capture();
  assert.equal(pending[0].signal.aborted, true);
  pending[0].resolve({ obsolete: true });
  await rejected;
  assert.equal(pending[1].signal.aborted, false);
  pending[1].resolve({ current: true });
  assert.deepEqual(await second, { current: true });
});
test('revision change or unmount cancels pending work and rejects retained callbacks', async () => {
  const { session, pending } = pendingSession();
  const result = session.capture(), rejected = assert.rejects(result, { name: 'AbortError' });
  session.dispose();
  assert.equal(pending[0].signal.aborted, true);
  pending[0].resolve({ obsolete: true });
  await rejected;
  await assert.rejects(session.capture(), { name: 'AbortError' });
  assert.equal(pending.length, 1);
});
test('caller cancellation survives a transport that ignores abort', async () => {
  const { session, pending } = pendingSession(), controller = new AbortController();
  const result = session.capture(controller.signal), rejected = assert.rejects(result, { name: 'AbortError' });
  controller.abort();
  pending[0].resolve({ obsolete: true });
  await rejected;
  await assert.rejects(session.capture(controller.signal), { name: 'AbortError' });
  assert.equal(pending.length, 1);
});

test('panel hook cancels on revision, readonly and unmount, including Strict Mode effect replay', async () => {
  const requests = [], reference = { current: null };
  let effect, previousDependencies, cleanup;
  const hooks = {
    useRef: () => reference,
    useCallback: callback => callback,
    useLayoutEffect(callback, dependencies) {
      if (!previousDependencies || dependencies.some((value, index) => value !== previousDependencies[index])) {
        previousDependencies = dependencies; effect = callback;
      }
    },
  };
  const module = load(directory + 'editor/panels/pipelineReferenceCapture.ts', {
    react: hooks,
    '../../../../../util/api': { fetchPipelineReferenceFrame: (request, signal) => new Promise(resolve => requests.push({ request, signal, resolve })) },
  });
  function render(request, revision) {
    const capability = module.usePipelineReferenceCapture(request, revision, 'read_only');
    if (effect) { cleanup?.(); cleanup = effect(); effect = undefined; }
    return capability;
  }
  const request = resolve().request;
  const first = render(request, 'first');
  assert.deepEqual(plain(first.inputPipeline), plain(request.pipeline));
  first.inputPipeline.graph.nodes[0].config.camera_id = 'mutated-by-extension';
  assert.equal(request.pipeline.graph.nodes[0].config.camera_id, 'actual-camera');
  const result = first.capture(), rejected = assert.rejects(result, { name: 'AbortError' });
  const second = render(request, 'second');
  assert.equal(requests[0].signal.aborted, true);
  requests[0].resolve({ stale: true });
  await rejected;
  await assert.rejects(first.capture(), { name: 'AbortError' });
  cleanup(); previousDependencies = undefined;
  const replayed = render(request, 'second');
  const fresh = replayed.capture(); requests[1].resolve({ current: true });
  assert.deepEqual(await fresh, { current: true });
  const pending = second.capture(), cancelled = assert.rejects(pending, { name: 'AbortError' });
  const readonly = render(null, 'readonly');
  assert.equal(readonly.available, false);
  assert.equal(readonly.reason, 'read_only');
  requests[2].resolve({ stale: true });
  await cancelled;
  cleanup?.();
  await assert.rejects(replayed.capture(), { name: 'AbortError' });
});

function apiHarness(evidence) {
  const file = fs.readFileSync(path.join(__dirname, '../src/util/api.ts'), 'utf8');
  const ast = ts.createSourceFile('api.ts', file, ts.ScriptTarget.Latest, true);
  const names = ['fetchPipelinePreviewResponse', 'fetchPipelineReferenceFrame'];
  const source = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text))
    .map(node => node.getText(ast)).join('\n');
  const requests = [], blob = new Blob(['image'], { type: 'image/png' });
  const context = { exports: {}, resolveToposyncUrl: url => '/ingress/test' + url,
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, headers: new Headers(evidence === undefined ? {} : {
        'X-Toposync-Capture-Evidence': JSON.stringify(evidence), 'X-Toposync-Capture-Receipt': 'server-proof',
      }), blob: async () => blob };
    } };
  vm.runInNewContext(compile(source), context);
  return { api: context.exports.fetchPipelineReferenceFrame, requests, blob };
}
const evidence = { source_identity: 'camera:actual-camera:night', preparation_revision: 'a'.repeat(64),
  sample: { capture_instance: 'decoder', generation: 0, sequence: 1 }, observed_at: 1000,
  freshness_basis: 'receipt', historical_fallback: false, prepared_region: true };
test('capture transport preserves ingress, abort and server evidence without historical fallback', async () => {
  const h = apiHarness(evidence), signal = new AbortController().signal;
  const result = await h.api({ ...resolve().request, fallback_snapshot: { pipeline_name: 'old' } }, signal);
  assert.equal(result.blob, h.blob);
  assert.equal(result.receipt, 'server-proof');
  assert.deepEqual(plain(result.evidence), evidence);
  assert.equal(h.requests[0].url, '/ingress/test/api/pipelines/preview/frame');
  assert.equal(h.requests[0].options.signal, signal);
  const sent = JSON.parse(h.requests[0].options.body);
  assert.equal(sent.purpose, 'reference');
  assert.equal(sent.fallback_snapshot, null);
});
test('missing or invalid evidence fails closed for older hosts and invalid responses', async () => {
  for (const invalid of [undefined, null, {}, { ...evidence, historical_fallback: true },
    { ...evidence, prepared_region: undefined }, { ...evidence, prepared_region: "true" },
    { ...evidence, sample: { ...evidence.sample, sequence: 0 } },
    { ...evidence, sample: { ...evidence.sample, generation: '1' } },
    { ...evidence, observed_at: '1000' }, { ...evidence, preparation_revision: 'unknown' }]) {
    await assert.rejects(apiHarness(invalid).api(resolve().request), /reference_capture_evidence_invalid/);
  }
});
