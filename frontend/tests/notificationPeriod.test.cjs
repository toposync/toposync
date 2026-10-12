const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const source = fs.readFileSync(path.resolve(__dirname, "../src/util/notificationPeriod.ts"), "utf8");
const context = { exports: {}, Date, Intl };
vm.createContext(context);
vm.runInContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, context);
const resolve = context.exports.resolveNotificationPeriod;

test("calendar presets cross weeks, months and years in the browser zone", () => {
  const previous = process.env.TZ; process.env.TZ = "America/Sao_Paulo";
  try {
    const now = new Date("2026-01-01T01:00:00Z"); // Still December in the interface.
    const pairs = {
      today: ["2025-12-31", "2026-01-01"], yesterday: ["2025-12-30", "2025-12-31"],
      this_week: ["2025-12-29", "2026-01-05"], last_week: ["2025-12-22", "2025-12-29"],
      this_month: ["2025-12-01", "2026-01-01"], last_month: ["2025-11-01", "2025-12-01"],
    };
    for (const [preset, [start, end]] of Object.entries(pairs)) {
      const range = resolve({ preset }, now);
      assert.equal(range.createdAfter, new Date(`${start}T00:00:00-03:00`).getTime() / 1000, preset);
      assert.equal(range.createdBefore, new Date(`${end}T00:00:00-03:00`).getTime() / 1000, preset);
      assert.equal(range.timezone, "America/Sao_Paulo");
    }
    assert.equal(resolve({ preset: "all" }), undefined);
  } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
});

test("custom dates are inclusive civil days, including leap years and daylight saving", () => {
  const previous = process.env.TZ; process.env.TZ = "America/New_York";
  try {
    for (const [day, hours] of [["2026-03-08", 23], ["2026-11-01", 25], ["2024-02-29", 24]]) {
      const range = resolve({ preset: "custom", start: day, end: day });
      assert.equal((range.createdBefore - range.createdAfter) / 3600, hours);
    }
    for (const period of [{ preset: "custom" }, { preset: "custom", start: "2026-02-29", end: "2026-03-01" },
      { preset: "custom", start: "2026-03-09", end: "2026-03-08" }, { preset: "unexpected" }]) {
      assert.throws(() => resolve(period), /invalid_period/);
    }
  } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
});

test("a skipped midnight does not shift the other civil day boundaries", () => {
  const previous = process.env.TZ; process.env.TZ = "America/Sao_Paulo";
  try {
    const now = new Date("2018-11-04T12:00:00Z");
    const cases = [
      [{ preset: "today" }, "2018-11-04T01:00:00-02:00", "2018-11-05T00:00:00-02:00"],
      [{ preset: "yesterday" }, "2018-11-03T00:00:00-03:00", "2018-11-04T01:00:00-02:00"],
      [{ preset: "this_week" }, "2018-10-29T00:00:00-03:00", "2018-11-05T00:00:00-02:00"],
      [{ preset: "last_week" }, "2018-10-22T00:00:00-03:00", "2018-10-29T00:00:00-03:00"],
      [{ preset: "custom", start: "2018-11-04", end: "2018-11-04" }, "2018-11-04T01:00:00-02:00", "2018-11-05T00:00:00-02:00"],
    ];
    for (const [period, start, end] of cases) {
      const range = resolve(period, now);
      assert.equal(range.createdAfter, new Date(start).getTime() / 1000, period.preset);
      assert.equal(range.createdBefore, new Date(end).getTime() / 1000, period.preset);
    }
  } finally { if (previous === undefined) delete process.env.TZ; else process.env.TZ = previous; }
});
