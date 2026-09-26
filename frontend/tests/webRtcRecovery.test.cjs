const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const source = ts.createSourceFile('StreamsDashboard.tsx', fs.readFileSync(path.join(__dirname, '../src/ui/streams/StreamsDashboard.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let declaration;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'startWebRtcPlayback') declaration = node;
  ts.forEachChild(node, visit);
}
visit(source);
const code = ts.transpileModule(`const ${declaration.getText(source)};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;

async function harness() {
  const retries = [], states = [];
  let now = 0, sample, stats = {};
  class Peer {
    constructor() { this.connectionState = this.iceConnectionState = 'connected'; this.listeners = []; }
    addEventListener(_type, listener) { this.listeners.push(listener); }
    addTransceiver() {}
    async createOffer() { return {sdp: 'offer'}; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription() {}
    emit(state) { this.connectionState = state; this.listeners.forEach(listener => listener()); }
  }
  const context = vm.createContext({
    latestEndpoints: {current: {webrtcUrl: 'http://local.test/whep'}},
    i18n: {t: (_key, _params, fallback) => fallback}, canUseWebRtc: () => true,
    setTransport() {}, peerConnection: null, RTCPeerConnection: Peer,
    recordWebPlaybackEvent() {}, withTransportTelemetry: value => value,
    MediaStream: class {}, waitForIceGatheringComplete: async () => {},
    WEBRTC_SIGNAL_TIMEOUT_MS: 5000, WEBRTC_CONNECT_TIMEOUT_MS: 5000, WEBRTC_FIRST_FRAME_TIMEOUT_MS: 8000,
    normalizeWhepSdp: value => value, AbortController, webrtcAbortController: null, webrtcAuthHeader: null,
    fetch: async () => ({ok: true, text: async () => 'answer', headers: {get: () => null}}),
    waitForPeerConnectionReady: async () => {}, collectWebRtcStats: async () => stats,
    cancelled: false, setWebRtcStats() {}, clearWebRtcStatsTimer() {},
    performance: {now: () => now},
    window: {setInterval: callback => { sample = callback; return 1; }}, webrtcStatsTimerId: null, waitForVideoElementFrame: async () => {},
    setFirstFrameReady: value => states.push(value), setStatus() {}, setErrorText() {},
    scheduleRetry: message => retries.push(message), video: {play: async () => {}, currentTime: 1, paused: false},
  });
  context.destroyPlayback = context.destroyWebRtc = () => { context.peerConnection = null; };
  context.fetchWithTimeout = context.fetch;
  vm.runInContext(code, context);
  await vm.runInContext('startWebRtcPlayback(video)', context);
  return {context, retries, states, peer: context.peerConnection, setStats: value => { stats = value; }, sampleAt: time => { now = time; sample(); }};
}

test('disconnected playback recovers once after confirmed lack of progress, without waiting for ICE failure', async () => {
  const h = await harness();
  h.peer.emit('disconnected');
  h.sampleAt(2000);
  assert.equal(h.retries.length, 0);
  h.sampleAt(4000);
  h.sampleAt(6000);
  assert.equal(h.retries.length, 1);
  assert.deepEqual(h.states, [true, false]);
});

test('ICE disconnection immediately recovers an already observed stall', async () => {
  const h = await harness();
  h.sampleAt(4000);
  assert.equal(h.retries.length, 0);
  h.peer.emit('disconnected');
  assert.equal(h.retries.length, 1);
  h.sampleAt(6000);
  assert.equal(h.retries.length, 1);
});

test('ICE disconnection does not recover if playback advanced since the last sample', async () => {
  const h = await harness();
  h.sampleAt(4000);
  h.context.video.currentTime = 2;
  h.peer.emit('disconnected');
  assert.equal(h.retries.length, 0);
});

test('advancing, paused, recovered and superseded connections do not trigger the stall recovery', async () => {
  const h = await harness();
  h.peer.emit('disconnected');
  h.context.video.currentTime = 5;
  h.sampleAt(4000);
  h.context.video.paused = true;
  h.sampleAt(8000);
  h.context.video.paused = false;
  h.peer.emit('connected');
  h.sampleAt(12000);
  h.context.peerConnection = {};
  h.peer.emit('disconnected');
  h.sampleAt(16000);
  assert.equal(h.retries.length, 0);
});

for (const failure of ['failed', 'closed']) {
  test(`established WebRTC ${failure} clears readiness and retries once`, async () => {
    const h = await harness();
    h.peer.emit(failure);
    h.peer.emit(failure);
    assert.deepEqual(h.states, [true, false]);
    assert.equal(h.retries.length, 1);
    assert.equal(h.context.peerConnection, null);
  });
}

test('transient disconnection and cancelled or replaced peers do not restart playback', async () => {
  const h = await harness();
  h.peer.emit('disconnected');
  assert.equal(h.retries.length, 0);
  h.context.cancelled = true;
  h.peer.emit('failed');
  assert.equal(h.retries.length, 0);
  h.context.cancelled = false;
  h.context.peerConnection = {};
  h.peer.emit('failed');
  assert.equal(h.retries.length, 0);
});

test('signaling request timeout aborts the request and clears its timer', async () => {
  const helper = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'fetchWithTimeout');
  const compiled = ts.transpileModule(helper.getText(source), {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  let abort, delay, cleared = false;
  const context = vm.createContext({AbortController, HLS_BROWSER_PROBE_TIMEOUT_MS: 2500,
    window: {setTimeout: (callback, milliseconds) => { abort = callback; delay = milliseconds; return 1; }, clearTimeout: () => { cleared = true; }},
    fetch: (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted')))),
  });
  vm.runInContext(compiled, context);
  const request = vm.runInContext("fetchWithTimeout('http://local.test/whep', {}, undefined, 5000)", context);
  assert.equal(delay, 5000);
  abort();
  await assert.rejects(request, /aborted/);
  assert.equal(cleared, true);
});


test('connected ICE recovers once when packets and playback both stop at a known cadence', async () => {
  const h = await harness();
  h.setStats({packetsReceived: 100, framesPerSecond: 15});
  h.sampleAt(2000); await new Promise(resolve => setImmediate(resolve));
  h.setStats({packetsReceived: 100});
  h.sampleAt(4000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 0);
  h.sampleAt(6000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 1);
  h.sampleAt(8000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 1);
});

for (const condition of ['slow', 'unknown', 'receiving', 'playing', 'paused', 'missing']) {
  test(`packet watchdog preserves ${condition} playback`, async () => {
    const h = await harness();
    h.setStats({packetsReceived: 100, framesPerSecond: condition === 'slow' ? .2 : condition === 'unknown' ? null : 15});
    h.sampleAt(2000); await new Promise(resolve => setImmediate(resolve));
    if (condition === 'receiving') h.setStats({packetsReceived: 101, framesPerSecond: 15});
    if (condition === 'playing') h.context.video.currentTime = 2;
    if (condition === 'paused') h.context.video.paused = true;
    if (condition === 'missing') h.setStats({});
    h.sampleAt(6000); await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.retries.length, 0);
  });
}

test('packet statistics stay serial and failed reads reset the continuity evidence', async () => {
  const h = await harness();
  let calls = 0, fail;
  h.context.collectWebRtcStats = () => { calls++; return new Promise((_resolve, reject) => { fail = reject; }); };
  h.sampleAt(2000); h.sampleAt(4000);
  assert.equal(calls, 1);
  fail(Error('statistics unavailable'));
  await new Promise(resolve => setImmediate(resolve));
  h.context.collectWebRtcStats = async () => ({packetsReceived: 100, framesPerSecond: 15});
  h.sampleAt(6000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 0);
  h.sampleAt(8000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 0);
  h.sampleAt(10000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 1);
});


test('only the first established-stream retry is immediate; persistent failure keeps backoff', () => {
  let retry;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'scheduleRetry') retry = node;
    ts.forEachChild(node, find);
  }
  find(source);
  const compiled = ts.transpileModule(`const ${retry.getText(source)};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText;
  let callback; const delays = [], starts = [];
  const context = vm.createContext({cancelled: false, playbackActive: true,
    allowMse: false, mseUrl: null, allowHls: false, hlsUrl: null,
    allowWebRtc: true, webrtcUrl: 'http://local.test/whep', allowJsmpeg: false, jsmpegUrl: null,
    retryTimerId: null, attempt: 0, RETRY_BASE_MS: 900, RETRY_MAX_MS: 12000,
    recordWebPlaybackEvent() {}, withTransportTelemetry: value => value,
    window: {setTimeout(fn, delay) {callback = fn; delays.push(delay); return 1;}},
    setStatus() {}, setErrorText() {}, startPlayback: () => starts.push(true),
  });
  vm.runInContext(compiled, context);
  vm.runInContext("scheduleRetry('lost', true)", context);
  vm.runInContext("scheduleRetry('duplicate', true)", context);
  assert.deepEqual(delays, [0]);
  callback();
  assert.equal(starts.length, 1);
  vm.runInContext("scheduleRetry('still lost', true)", context);
  assert.deepEqual(delays, [0, 1800]);
  callback();
  context.attempt = 0;
  vm.runInContext("scheduleRetry('startup failure')", context);
  assert.deepEqual(delays, [0, 1800, 900]);
  callback();
  context.cancelled = true;
  vm.runInContext("scheduleRetry('cancelled', true)", context);
  assert.equal(delays.length, 3);
});

test('a loss between samples can use decoded frame counts when instantaneous frame rate disappears', async () => {
  const h = await harness();
  h.setStats({packetsReceived: 100, framesDecoded: 120, framesPerSecond: 15});
  h.sampleAt(2000); await new Promise(resolve => setImmediate(resolve));
  h.context.video.currentTime = 2;
  h.setStats({packetsReceived: 130, framesDecoded: 135});
  h.sampleAt(4000); await new Promise(resolve => setImmediate(resolve));
  h.sampleAt(6000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 0);
  h.sampleAt(8000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.retries.length, 1);
});


for (const decoded of [undefined, 0]) {
  test(`missing or reset decoded count (${decoded}) does not invent a cadence`, async () => {
    const h = await harness();
    h.setStats({packetsReceived: 100, framesDecoded: 120, framesPerSecond: 15});
    h.sampleAt(2000); await new Promise(resolve => setImmediate(resolve));
    h.setStats({packetsReceived: 130, framesDecoded: decoded});
    h.sampleAt(4000); await new Promise(resolve => setImmediate(resolve));
    h.sampleAt(8000); await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.retries.length, 0);
  });
}

for (const [field, state] of [['connectionState', 'closed'], ['connectionState', 'failed'], ['iceConnectionState', 'failed']]) {
  test(`statistics watchdog recovers terminal ${field}=${state} without a state-change event`, async () => {
    const h = await harness();
    h.peer[field] = state;
    h.setStats({});
    h.sampleAt(2000);
    h.sampleAt(4000);
    assert.deepEqual(h.states, [true, false]);
    assert.equal(h.retries.length, 1);
  });
}

for (const condition of ['cancelled', 'replaced']) {
  test(`terminal watchdog ignores a ${condition} peer`, async () => {
    const h = await harness();
    h.peer.connectionState = 'closed';
    if (condition === 'cancelled') h.context.cancelled = true;
    else h.context.peerConnection = {};
    h.sampleAt(2000);
    assert.equal(h.retries.length, 0);
  });
}
