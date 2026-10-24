const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const react = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function load(name, modules = {}) {
  const context = { exports: {}, require: name => { assert.ok(name in modules, name); return modules[name]; } };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname, '../../extensions/vision/ui/src', name), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText, context);
  return context.exports;
}
const helpers = load('visualPublication.ts');
const value = () => ({ sensor_id: 'sensor', classes: [
  { class_id: 'a', key: 'open', label: 'Aberta', enabled: true },
  { class_id: 'b', key: 'closed', label: 'Fechada', enabled: true },
  { class_id: 'c', key: 'ajar', label: 'Entreaberta', enabled: true },
] });
const binary = () => ({ enabled: true, kind: 'binary', name: ' Porta ', on_class_ids: ['b'], off_class_ids: ['a'] });

test('publication defaults off; explicit mapping uses identifiers, not labels, order or class truthiness', () => {
  const config = value(), fields = binary();
  assert.equal(helpers.visualPublication(config).enabled, false);
  assert.equal(helpers.publicationIssue(config, fields), null);
  assert.deepEqual(config.classes.map(category => helpers.publicationClassValue(fields, category)), ['off', 'on', 'unknown']);
  const updated = helpers.applyVisualPublication(config, fields);
  assert.equal(updated.home_assistant.name, 'Porta');
  assert.equal(updated.classes, config.classes);
  config.classes.reverse(); config.classes[2].label = 'Nova etiqueta';
  assert.equal(helpers.publicationClassValue(fields, config.classes[2]), 'off');
  config.classes[2].class_id = 'replacement';
  assert.equal(helpers.publicationIssue(config, fields), 'publication_missing_class');
  assert.equal(helpers.publicationClassValue(fields, config.classes[2]), 'unknown');
});

test('binary mapping rejects overlaps, disabled sides and invalid text configuration; reset is explicit', () => {
  const config = value();
  for (const patch of [{ enabled: 'true' }, { kind: 'switch' }, { name: 4 }, { name: 'x'.repeat(81) },
    { on_class_ids: ['a'] }, { off_class_ids: ['a', 'a'] }, { on_class_ids: 'b' }, { on_class_ids: [false] },
    { on_class_ids: [] }, { off_class_ids: ['missing'] }]) {
    assert.ok(helpers.publicationIssue(config, { ...binary(), ...patch }));
    assert.throws(() => helpers.applyVisualPublication(config, { ...binary(), ...patch }));
  }
  config.classes[1].enabled = false;
  assert.equal(helpers.publicationIssue(config, binary()), 'publication_need_sides');
  assert.equal(helpers.publicationClassValue(binary(), config.classes[1]), 'disabled');
  assert.equal(helpers.publicationIssue(config, { ...binary(), enabled: false }), null);
  assert.equal(helpers.publicationIssue(config, helpers.publicationDefaults()), null);
});

test('mapping changes remove the old side without changing classes; named states preserve stable keys', () => {
  const fields = binary();
  const changed = helpers.mapPublicationClass(fields, 'a', 'on');
  assert.equal(helpers.publicationClassSide(changed, 'a'), 'on');
  assert.deepEqual([...changed.off_class_ids], []);
  assert.deepEqual(fields.off_class_ids, ['a']);
  assert.equal(helpers.publicationClassSide(helpers.mapPublicationClass(changed, 'a', 'unknown'), 'a'), 'unknown');
  assert.equal(helpers.publicationClassValue(helpers.publicationDefaults(), value().classes[0]), 'open');
});

const translations = load('translations.ts').visualStateTranslations;
const t = key => { const name = `ext.vision.visualState.${key}`; assert.ok(translations.en[name] && translations['pt-BR'][name], name); return translations['pt-BR'][name]; };
const { VisualPublicationSettings } = load('VisualPublicationSettings.tsx', { react: { ...react, default: react }, './visualPublication': helpers });
const render = (config = value(), disabled = false) => renderToStaticMarkup(react.createElement(VisualPublicationSettings, {
  value: config, disabled, editing: () => {}, change: () => {}, t,
}));

test('preview renders classes, unknown, expired and unavailable before opt-in and explains legacy incompatibility', () => {
  const html = render();
  assert.match(html, /publicação está desativada/);
  assert.match(html, /<code>open<\/code>/);
  assert.match(html, /Observação vencida/);
  assert.match(html, /Desconhecido \(unknown\)/);
  assert.match(html, /Indisponível \(unavailable\)/);
  assert.match(html, /input_boolean/);
  assert.match(html, /incompatíveis com a qualificação/);
  assert.match(html, /<button[^>]*disabled=""[^>]*>Aplicar publicação/);
  assert.match(render(value(), true), /<fieldset disabled=""/);
});

test('invalid removed mapping remains visible and recoverable without reusing a label or executing markup', () => {
  const config = value(); config.classes[0].label = '<script>unsafe</script>';
  config.home_assistant = { ...binary(), enabled: false, on_class_ids: ['gone'] };
  const html = render(config);
  assert.match(html, /<details open="">/);
  assert.match(html, /classe removida: gone/);
  assert.match(html, /Remover mapeamento/);
  assert.match(html, /Reiniciar publicação desativada/);
  assert.match(html, /<button[^>]*disabled=""[^>]*>Aplicar publicação/);
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
