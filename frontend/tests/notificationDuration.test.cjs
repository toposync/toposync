const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const filename = path.resolve(__dirname, "../src/ui/screens/MainScreen.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["asRecord", "asTrimmedString", "asFiniteNumber", "notificationPayload",
  "notificationIsOpenRealtime", "notificationStartedMillis", "withLiveDuration"]);
const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text));
const context = {};
vm.createContext(context);
vm.runInContext(ts.transpileModule(functions.map(node => node.getText(ast)).join("\n"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);

for (const basis of [undefined, "media", "packet_created_at"]) test(`pipeline duration stays on its received timeline (${basis})`, () => {
  const notification = { createdAt: "2026-09-19T00:00:00Z", payload: {
    source: "pipelines", status: "open", realtime: true,
    event: { started_ts: 1, ts: 13, duration_seconds: 12, ...(basis ? {time_basis: basis} : {}) },
  } };
  assert.equal(context.withLiveDuration(notification, Date.parse("2026-09-19T01:00:00Z")), notification);
  assert.equal(context.withLiveDuration(notification, 0), notification, "civil clock rollback is irrelevant");
});

test("closed and non-realtime notifications retain authoritative durations", () => {
  for (const options of [{status:"closed", realtime:true}, {status:"open", realtime:false}]) {
    const notification = { payload: {...options, event: {started_ts:1, duration_seconds:12}} };
    assert.equal(context.withLiveDuration(notification, 9000), notification);
  }
});

test("unrelated realtime notification keeps its existing civil-clock behavior", () => {
  const notification = {payload:{source:"other",status:"open",realtime:true,event:{started_ts:100,duration_seconds:2}}};
  assert.equal(context.withLiveDuration(notification, 105000).payload.event.duration_seconds, 5);
});

test("details explain observed duration and clock discontinuity in both supported languages", () => {
  const translations = fs.readFileSync(path.resolve(__dirname, "../src/util/i18n.ts"), "utf8");
  for (const key of ["observed_duration", "clock_changed", "invalid_interval"]) {
    assert.ok(source.includes(`core.ui.notifications.details.meta.${key}`));
    assert.equal(translations.split(`"core.ui.notifications.details.meta.${key}"`).length - 1, 2);
  }
  assert.ok(translations.includes("Duração até a última atualização"));
  assert.ok(translations.includes("Duration through last update"));
});
