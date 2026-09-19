const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const typescript = require("typescript");

const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "toposync-panorama-translations-"));
for (const relative of ["translations.ts", "settings/sourcePanoramaTranslations.ts"]) {
  const target = path.join(temporaryDirectory, relative.replace(/\.ts$/, ".js"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, typescript.transpileModule(fs.readFileSync(path.resolve(__dirname, "../src", relative), "utf8"), {
    compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022 },
  }).outputText);
}
const { camerasTranslations } = require(path.join(temporaryDirectory, "translations.js"));
const { sourcePanoramaTranslations } = require(path.join(temporaryDirectory, "settings/sourcePanoramaTranslations.js"));
after(() => fs.rmSync(temporaryDirectory, { recursive: true, force: true }));

test("camera locales have matching keys and interpolation parameters", () => {
  const { en, "pt-BR": pt } = camerasTranslations;
  assert.deepEqual(Object.keys(pt).sort(), Object.keys(en).sort());
  const parameters = value => [...value.matchAll(/{{\s*([\w.-]+)\s*}}/g)].map(match => match[1]).sort();
  for (const key of Object.keys(en)) {
    assert.ok(en[key].trim() && pt[key].trim(), key);
    assert.deepEqual(parameters(pt[key]), parameters(en[key]), key);
  }
});

test("mapping API errors have localized messages without interpreting server prose", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../../src/toposync_ext_cameras/panorama.py"), "utf8");
  const codes = new Set([...source.matchAll(/_error\(\s*"([a-z_]+)"/g)].map(match => match[1]));
  assert.ok(codes.has("revision_conflict"));
  assert.ok(codes.has("visual_control_unavailable"));
  for (const code of codes) {
    for (const locale of ["en", "pt-BR"]) assert.ok(camerasTranslations[locale][`ext.cameras.panorama.error_${code}`], `${locale}: ${code}`);
  }
});

test("scanner recovery diagnostics are localized for every supported locale", () => {
  const scannerSource = fs.readFileSync(
    path.resolve(__dirname, "../../src/toposync_ext_cameras/panorama_scan.py"),
    "utf8",
  );
  const diagnostics = [
    "absolute_grid_checkpoint_incompatible",
    "control_verification_resume_unavailable",
    "original_reference_changed",
    "pilot_cycle_unconfirmed",
    "pilot_resume_unavailable",
    "return_coarse_observation_unconfirmed",
    "return_correction_budget_exhausted",
    "visual_control_resolution_unverified",
    "visual_response_unavailable",
    "working_reference_changed",
    "working_reference_unavailable",
  ];

  for (const code of diagnostics) {
    assert.match(scannerSource, new RegExp(`(?:PanoramaCaptureError\\(|["']code["']\\s*:)\\s*["']${code}["']`), code);
    for (const locale of ["en", "pt-BR"]) {
      const message = sourcePanoramaTranslations[locale][`ext.cameras.source_panorama.diagnostic_${code}`];
      assert.ok(message?.trim(), `${locale}: ${code}`);
    }
  }
});
