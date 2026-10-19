const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const filename = path.resolve(__dirname, '../../extensions/vision/ui/src/RecognitionPolicy.tsx');
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, esModuleInterop: true } }).outputText;
const exportsObject = {};
vm.runInNewContext(code, { exports: exportsObject, require: name => name === 'react' ? React : { requestJson: () => { throw Error('No network in rendering'); } } });
const policy = { species: 'person', embedding_space: 'synthetic', calibration_revision: 'test-only', automatic_enabled: false, acceptance_similarity: .9, suggestion_similarity: .7, competitor_margin: .1, clustering_similarity: .9, continuity_seconds: 10 };

test('calibration never invents missing thresholds or accepts ambiguous policies', () => {
  const valid = exportsObject.recognitionPoliciesValid;
  assert.equal(valid([]), true);
  assert.equal(valid([{ species: 'person', embedding_space: 'synthetic', calibration_revision: '' }]), false);
  assert.equal(valid([policy]), true);
  for (const patch of [{ suggestion_similarity: .95 }, { acceptance_similarity: NaN }, { competitor_margin: -1 }, { continuity_seconds: 61 }, { calibration_revision: ' ' }]) assert.equal(valid([{ ...policy, ...patch }]), false);
  assert.equal(valid([policy, policy]), false);
  assert.equal(valid([policy, { ...policy, species: 'cat' }]), true);
});

test('review shows calibrated values and an explicit unconfirmed state in both languages', () => {
  for (const locale of ['en', 'pt-BR']) {
    const i18n = { useI18n: () => ({ locale }), t: key => key };
    const empty = renderToStaticMarkup(React.createElement(exportsObject.RecognitionPolicySummary, { i18n, config: {} }));
    assert.match(empty, locale === 'en' ? /identity unconfirmed/ : /identidade não confirmada/);
    const summary = renderToStaticMarkup(React.createElement(exportsObject.RecognitionPolicySummary, { i18n, config: { policies: [policy] } }));
    assert.match(summary, /test-only/);
    assert.match(summary, /0.9/);
    assert.match(summary, locale === 'en' ? /Suggestions only/ : /Somente sugestões/);
  }
});
