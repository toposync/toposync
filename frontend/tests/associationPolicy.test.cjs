const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const source = fs.readFileSync(path.resolve(__dirname, '../../extensions/vision/ui/src/Association.tsx'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, esModuleInterop: true } }).outputText;
const exported = {};
vm.runInNewContext(code, { exports: exported, require: name => name === 'react' ? React : name === './ManualAssociationResult' ? { ManualAssociationResult: () => null } : {} });
const config = { scope: 'reviewed', horizon_seconds: 42, sources: [{ camera_id: 'garage', source_id: 'main', identity_policy: 'recognize_here' }, { camera_id: 'corridor', source_id: 'main', identity_policy: 'keep_confirmed_identity' }] };

test('automatic association offers nominal policies only for explicit recognition', () => {
  const i18n = { useI18n: () => ({ t: key => key }) };
  for (const recognitionAllowed of [true, false]) {
    const html = renderToStaticMarkup(React.createElement(exported.Association, { i18n, config, updateConfig: () => {}, configurationOnly: true, recognitionAllowed }));
    const selectedPolicy = html.match(/<option value="recognize_here"[^>]*>/)[0];
    assert.equal(selectedPolicy.includes('disabled'), !recognitionAllowed);
    assert.match(html, /ext.vision.association.refresh/);
    assert.doesNotMatch(html, /ext.vision.manualResult/);
  }
});

test('review retains each source policy and the observation horizon in both languages', () => {
  for (const locale of ['en', 'pt-BR']) {
    const translate = key => exported.associationTranslations[locale][key] || key;
    const i18n = { useI18n: () => ({ t: translate }) };
    const html = renderToStaticMarkup(React.createElement(exported.AssociationSummary, { i18n, config }));
    for (const text of ['42', 'garage', 'corridor', translate('ext.vision.association.recognize_here'), translate('ext.vision.association.keep_confirmed_identity')]) assert.ok(html.includes(text));
  }
});
