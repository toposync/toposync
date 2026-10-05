const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');
const vm = require('node:vm');

test('spatial streams release hidden demand, ignore late startup, and resume until destroyed', async () => {
  const listeners = new Set(), intervals = new Map(), timeouts = new Map();
  const requests = [], heartbeats = [];
  let timer = 0, releasePrime, playbackStarts = 0, closed = 0;
  const document = {
    hidden: true,
    addEventListener: (name, listener) => { assert.equal(name, 'visibilitychange'); listeners.add(listener); },
    removeEventListener: (name, listener) => { assert.equal(name, 'visibilitychange'); listeners.delete(listener); },
  };
  const visibility = hidden => { document.hidden = hidden; for (const listener of listeners) listener(); };
  const flush = () => new Promise(setImmediate);
  const api = {
    async fetchLiveViewPlayback(...args) {
      requests.push(args);
      return { transmission: { id: 'transmission' }, urls: { outputs: [{ protocol: 'mse', output_id: 'sub', url: '/video' }] } };
    },
    primeTransmissionDemand: () => releasePrime ? Promise.resolve() : new Promise(resolve => { releasePrime = resolve; }),
    async heartbeatTransmissionDemand(value) { heartbeats.push(value); },
  };
  const context = {
    exports: {}, document, AbortController, DOMException,
    crypto: { randomUUID: () => 'session' },
    URL: { revokeObjectURL() {} },
    window: {
      setInterval: callback => { intervals.set(++timer, callback); return timer; },
      clearInterval: id => intervals.delete(id),
      setTimeout: callback => { timeouts.set(++timer, callback); return timer; },
      clearTimeout: id => timeouts.delete(id),
    },
    require(name) {
      if (name === './api') return api;
      if (name === './projection') return { sanitizeMediaContentRect: value => value };
      if (['hls.js', '@cycjimmy/jsmpeg-player', 'three'].includes(name)) return {};
      throw new Error(`Unexpected dependency: ${name}`);
    },
  };
  const source = fs.readFileSync(path.join(__dirname, '../../extensions/spatial_video/ui/src/streamTexture.ts'), 'utf8');
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, context);
  const stream = new context.exports.StreamTextureSource({ id: 'projection', liveViewId: 'view', variantId: 'sub' });
  stream.startMse = async function () {
    playbackStarts++;
    this.mseSocket = { close() { closed++; } };
    this.video = { pause() {}, removeAttribute() {}, load() {} };
    this.texture = { dispose() {} };
    this.setSnapshot({ status: 'playing', texture: this.texture });
  };

  stream.start();
  assert.equal(requests.length, 0, 'initially hidden must not acquire a source');
  assert.equal(listeners.size, 1);
  visibility(false);
  await flush();
  assert.equal(requests.length, 1);
  visibility(true);
  assert.equal(requests[0][2].aborted, true);
  releasePrime();
  await flush();
  assert.equal(playbackStarts, 0, 'late priming must not revive a hidden stream');
  assert.equal(intervals.size, 0);

  visibility(false);
  await flush();
  stream.start();
  assert.equal(requests.length, 2, 'repeated start must not duplicate acquisition');
  assert.equal(playbackStarts, 1);
  assert.equal(intervals.size, 1);
  visibility(true);
  assert.equal(heartbeats[0].signal.aborted, true, 'in-flight demand belongs to the playback lifetime');
  assert.equal(closed, 1);
  assert.equal(intervals.size, 0);
  assert.equal(stream.getSnapshot().texture, null);

  visibility(false);
  await flush();
  assert.equal(playbackStarts, 2);
  stream.destroy();
  assert.equal(closed, 2);
  assert.equal(listeners.size, 0);
  assert.equal(intervals.size, 0);
  visibility(true); visibility(false); stream.start();
  assert.equal(requests.length, 3, 'destroyed streams must stay closed');

  const aborted = new AbortController();
  aborted.abort();
  await assert.rejects(stream.waitForVideoFrame({}, aborted.signal), { name: 'AbortError' });
  assert.equal(timeouts.size, 0, 'late play completion must not install an orphan frame timer');
});
