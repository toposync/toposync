const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const modules = new Map();
function load(name) {
  if (modules.has(name)) return modules.get(name);
  const extension = name === "HumanObservationRenderer" ? "tsx" : "ts";
  const source = fs.readFileSync(path.join(__dirname, `../src/notifications/${name}.${extension}`), "utf8");
  const context = { exports: {}, Date: { now: () => 1000000 }, require(specifier) {
    // Spatial overlays and image decoding are outside this gesture-text boundary.
    if (specifier === "./humanObservationOverlays") return {};
    if (specifier === "./HumanObservationPreview") return { HumanObservationPreview: () => null };
    return specifier.startsWith("./") ? load(specifier.slice(2)) : require(specifier);
  } };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, esModuleInterop: true,
  } }).outputText, context);
  modules.set(name, context.exports);
  return context.exports;
}
const { readHumanObservation: read, createHumanObservationReader, HUMAN_OBSERVATION_TYPE } = load("humanObservation");
const plain = (value) => JSON.parse(JSON.stringify(value));
const sides = () => ({ left: { status: "unknown", reason: "landmarks_insufficient" }, right: { status: "available", reason: "usable_landmarks" } });
const gesture = (side, extra = {}) => ({ name: "hand_raised", side, evidence: "heuristic_2d", ...extra });
function notification(overrides = {}) {
  return { id: "n", type: HUMAN_OBSERVATION_TYPE, payload: { packet_id: "frame", lifecycle: "update", subject: { id: "actor" }, data: {
    subject: { id: "actor" }, camera_id: "camera", source_stream_id: "stream",
    capture_evidence: { published_at: 1000, capture_instance: "capture", generation: 1, sequence: 1 },
    spatial: {}, vision: { pose_frame_packet_id: "frame", pose_media_ts: 42, poses: [{ actor_subject_id: "actor" }],
      gestures: { schema_version: 1, actor_subject_id: "actor", camera_id: "camera", source_stream_id: "stream", frame_ts: 42,
        coordinate_basis: "body_relative_2d", status: "unknown", reason: "partial_landmarks", side_evidence: sides(),
        active: [], candidates: [], ...overrides } },
  } } };
}

test("partial dwell exposes only the usable unilateral candidate and preserves evidence diagnostics", () => {
  const result = read(notification({ candidates: [gesture("left"), gesture("right"), gesture("both")] }), 1000000);
  assert.deepEqual(plain(result.gestures).map(({ side, candidate }) => ({ side, candidate })), [{ side: "right", candidate: true }]);
  assert.deepEqual(plain(result.gestureEvidence), { status: "unknown", reason: "partial_landmarks", side_evidence: sides() });
});

test("both requires two usable sides; availability alone never produces a gesture", () => {
  const available = { left: { status: "available", reason: "usable" }, right: { status: "available", reason: "usable" } };
  assert.equal(read(notification({ side_evidence: available }), 1000000).gestures.length, 0);
  const result = read(notification({ side_evidence: available, candidates: [gesture("both")] }), 1000000);
  assert.equal(result.gestures[0]?.side, "both");
});

test("active entries require current evidence and a usable side when supplied", () => {
  const result = read(notification({ status: "active", reason: "", active: [
    gesture("right", { evidence_current: true }), gesture("left", { evidence_current: true }),
    gesture("both", { evidence_current: true }), gesture("right", { evidence_current: false }), gesture("right"),
  ] }), 1000000);
  assert.deepEqual(plain(result.gestures).map(({ side, candidate }) => ({ side, candidate })), [{ side: "right", candidate: false }]);
});

test("a current active gesture replaces only its identical candidate presentation", () => {
  const input = notification({ status: "active", active: [gesture("right", { evidence_current: true })],
    candidates: [gesture("right")] });
  const before = structuredClone(input);
  const result = read(input, 1000000);
  assert.deepEqual(plain(result.gestures), [{ name: "hand_raised", side: "right", evidence: "heuristic_2d", candidate: false }]);
  assert.equal(result.gestureEvidence.status, "active");
  assert.deepEqual(plain(result.gestureEvidence.side_evidence), sides());
  assert.deepEqual(input, before, "presentation must not mutate the diagnostic payload");
});

test("candidate gestures with a distinct name or side remain visible beside an active gesture", () => {
  const result = read(notification({ status: "active",
    side_evidence: { left: { status: "available", reason: "usable" }, right: { status: "available", reason: "usable" } },
    active: [gesture("right", { evidence_current: true })],
    candidates: [gesture("right"), gesture("left"), gesture("right", { name: "pointing_candidate" }), gesture("both", { name: "both_hands_raised" })],
  }), 1000000);
  assert.deepEqual(plain(result.gestures).map(({ name, side, candidate }) => ({ name, side, candidate })), [
    { name: "hand_raised", side: "right", candidate: false },
    { name: "hand_raised", side: "left", candidate: true },
    { name: "pointing_candidate", side: "right", candidate: true },
    { name: "both_hands_raised", side: "both", candidate: true },
  ]);
});

test("an active entry rejected for stale evidence or partial status cannot suppress a valid candidate", () => {
  for (const overrides of [
    { status: "active", active: [gesture("right", { evidence_current: false })] },
    { status: "active", active: [gesture("right")] },
    { status: "unknown", reason: "partial_landmarks", active: [gesture("right", { evidence_current: true })] },
  ]) {
    const result = read(notification({ ...overrides, candidates: [gesture("right")] }), 1000000);
    assert.deepEqual(plain(result.gestures), [{ name: "hand_raised", side: "right", evidence: "heuristic_2d", candidate: true }]);
  }
  const staleFrame = notification({ reason: "stale_frame", active: [gesture("right", { evidence_current: true })], candidates: [gesture("right")] });
  assert.deepEqual(plain(read(staleFrame, 1000000).gestures), []);
});

for (const reason of ["stale_frame", "out_of_order_frame", "landmarks_insufficient", "", "partial_landmarks "]) {
  test(`unknown ${JSON.stringify(reason)} does not reuse positive candidates`, () => {
    assert.equal(read(notification({ reason, candidates: [gesture("right")] }), 1000000).gestures.length, 0);
  });
}

test("unknown partial never publishes active entries or unbound/malformed sides", () => {
  for (const side_evidence of [null, {}, { right: { status: "available" } }, { right: { status: "observed", reason: "usable" } }]) {
    assert.equal(read(notification({ side_evidence, candidates: [gesture("right")] }), 1000000).gestures.length, 0);
  }
  assert.equal(read(notification({ active: [gesture("right", { evidence_current: true })], candidates: [gesture("unrecognized")] }), 1000000).gestures.length, 0);
});

test("legacy active and none remain compatible, but legacy unknown cannot display candidates", () => {
  for (const status of ["active", "none", "unknown"]) {
    const input = notification({ status, candidates: [gesture("right")] });
    delete input.payload.data.vision.gestures.side_evidence;
    const result = read(input, 1000000);
    assert.equal(result.gestures.length, status === "unknown" ? 0 : 1);
    assert.equal(result.gestureEvidence.status, status);
    assert.equal(result.gestureEvidence.side_evidence, null);
  }
});

test("malformed global status cannot be rendered as an enum or authorize gestures", () => {
  const result = read(notification({ status: "active ", active: [gesture("right", { evidence_current: true })] }), 1000000);
  assert.equal(result.gestureEvidence.status, "unknown");
  assert.equal(result.gestures.length, 0);
});

test("partial evidence cannot bypass expiry, binding, close or frame-order guards", () => {
  const input = notification({ candidates: [gesture("right")] });
  assert.equal(read(input, 1000750).gestures.length, 0);
  const invalid = structuredClone(input); invalid.payload.data.vision.gestures.actor_subject_id = "other";
  assert.equal(read(invalid, 1000000).reason, "gesture_binding_mismatch");
  const session = createHumanObservationReader();
  assert.equal(session(input, 1000000).gestures.length, 1);
  const older = structuredClone(input); older.payload.data.capture_evidence.sequence = 0;
  assert.equal(session(older, 1000001).gestures.length, 0);
  const reordered = structuredClone(input); reordered.payload.data.capture_evidence.sequence = 2;
  reordered.payload.data.vision.pose_media_ts = reordered.payload.data.vision.gestures.frame_ts = 41;
  assert.equal(session(reordered, 1000001).reason, "superseded_frame");
  const closed = structuredClone(input); closed.payload.lifecycle = "close";
  assert.equal(session(closed, 1000001).gestures.length, 0);
  assert.equal(session(input, 1000002).state, "closed");
});

for (const locale of ["en", "pt-BR"]) {
  test(`real detail translates the four backend gesture names and all sides in ${locale}`, () => {
    const dictionary = load("humanObservationTranslations").humanObservationTranslations[locale];
    const { HumanObservationDetails } = load("HumanObservationRenderer");
    const t = (key, _args, fallback) => dictionary[key] ?? fallback ?? key;
    const labels = locale === "en"
      ? [["hand_raised", "right", "Hand raised", "Right side"], ["both_hands_raised", "both", "Both hands raised", "Both sides"],
        ["wave", "left", "Wave", "Left side"], ["pointing_candidate", "right", "Pointing candidate", "Right side"]]
      : [["hand_raised", "right", "Mão levantada", "Lado direito"], ["both_hands_raised", "both", "Ambas as mãos levantadas", "Ambos os lados"],
        ["wave", "left", "Aceno", "Lado esquerdo"], ["pointing_candidate", "right", "Candidato a apontamento", "Lado direito"]];
    const input = notification({ status: "none", candidates: labels.map(([name, side]) => gesture(side, { name })) });
    delete input.payload.data.vision.gestures.side_evidence;
    input.payload.data.vision.gestures.candidates.push(gesture("legacy<side>", { name: "legacy<gesture>" }));
    const html = renderToStaticMarkup(React.createElement(HumanObservationDetails, { notification: input, i18n: { useI18n: () => ({ t }) } }));
    for (const [name, side, label, sideLabel] of labels) {
      assert.ok(html.includes(`${label} · ${sideLabel}`), `${locale}: ${name}/${side} must be localized`);
      assert.ok(!html.includes(`${name} · ${side}`));
    }
    assert.ok(html.includes("legacy&lt;gesture&gt; · legacy&lt;side&gt;"), "unknown legacy labels remain escaped text");
    assert.ok(!html.includes("ext.cameras.human."));
  });

  test(`real detail distinguishes none/unknown and labels usable versus unknown sides in ${locale}`, () => {
    const dictionary = load("humanObservationTranslations").humanObservationTranslations[locale];
    const { HumanObservationDetails } = load("HumanObservationRenderer");
    const t = (key, _args, fallback) => dictionary[key] ?? fallback ?? key;
    const render = (input) => renderToStaticMarkup(React.createElement(HumanObservationDetails, { notification: input, i18n: { useI18n: () => ({ t }) } }));
    const none = render(notification({ status: "none", reason: "no_gesture" }));
    const unknown = render(notification({ candidates: [gesture("right")] }));
    assert.notEqual(none, unknown);
    assert.ok(none.includes(locale === "en" ? "No recognized gesture" : "Nenhum gesto reconhecido"));
    assert.ok(unknown.includes(locale === "en" ? "Gesture state unknown" : "Estado do gesto desconhecido"));
    assert.ok(unknown.includes(locale === "en" ? "Right side" : "Lado direito"));
    assert.ok(unknown.includes(locale === "en" ? "Usable evidence" : "Evidência utilizável"));
    assert.ok(unknown.includes(locale === "en" ? "Left side" : "Lado esquerdo"));
    assert.ok(unknown.includes(locale === "en" ? "Insufficient evidence" : "Evidência insuficiente"));
    assert.ok(unknown.includes("partial_landmarks"));
    assert.ok(unknown.includes(locale === "en" ? "not proof of a raised arm" : "não comprova braço levantado"));
    assert.ok(!unknown.includes("ext.cameras.human."));
  });
}
