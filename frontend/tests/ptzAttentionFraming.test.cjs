const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const directory = '../../extensions/ptz_attention/ui/src/';
function load(file, imports = {}, expose = '') {
  const context = { exports: {}, require: name => name in imports ? imports[name] : require(name) };
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.resolve(__dirname, directory + file), 'utf8') + expose, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, context);
  return context.exports;
}
const { ptzAttentionTranslations } = load('translations.ts');
const { ArrivalFramingNotice, ObservedFramingNotice } = load('settings/ArrivalFramingNotice.tsx');
function render(details, language = 'pt-BR', action = 'focus_acquired') {
  const t = key => {
    assert.ok(key in ptzAttentionTranslations[language], key);
    return ptzAttentionTranslations[language][key];
  };
  return renderToStaticMarkup(React.createElement(ArrivalFramingNotice, { details, action, t }));
}
test('arrival rendering distinguishes projected extent and never certifies subject presence', () => {
  for (const [status, text] of [
    ['inside_frame', 'dentro da imagem'],
    ['offset_within_current_extent', 'deslocada para fora'],
    ['extent_exceeds_frame', 'maior que a imagem'],
  ]) {
    const projection = { status: status === 'inside_frame' ? 'projected' : 'projected_outside', fit: { status } };
    const markup = render({ framing_projection: projection });
    assert.ok(markup.includes(text), markup);
    assert.ok(markup.includes('Presença atual do sujeito não confirmada'), markup);
    assert.ok(!markup.includes('data-tone="success"'));
  }
});
test('group uncertainty wins over a positive individual projection', () => {
  const markup = render({ group_framing_projection: { status: 'member_outside_or_unqualified' },
    framing_projection: { status: 'projected', fit: { status: 'inside_frame' } } });
  assert.ok(markup.includes('Avaliação do enquadramento indisponível'), markup);
  assert.ok(!markup.includes('dentro da imagem'));
  const group = render({ group_framing_projection: { status: 'member_outside_or_unqualified',
    fit: { status: 'extent_exceeds_frame' } } });
  assert.ok(group.includes('maior que a imagem'));
});
test('missing, old, invalid or inconsistent evidence does not become a framing success', () => {
  for (const projection of [null, [], 'projected', {}, {status: 'geometry_unavailable'},
    {status: 'projected', fit: {status: 'future_status'}},
    {status: 'reference_mismatch', fit: {status: 'inside_frame'}}]) {
    assert.ok(render({framing_projection: projection}).includes('Avaliação do enquadramento indisponível'));
  }
  assert.equal(render({}, 'pt-BR', 'shadow_focus'), '');
  assert.ok(render({}, 'en').includes('Framing assessment unavailable'));
});

test('decision history actually renders the framing assessment beside arrival, not as present visibility', () => {
  const { DecisionReview, SessionCockpit } = load('settings/PtzAttentionSettingsPanel.tsx', {
    '../api': {}, './PtzAttentionWizard': {}, './ArrivalFramingNotice': { ArrivalFramingNotice, ObservedFramingNotice },
  }, '\nexport { DecisionReview, SessionCockpit };');
  const t = (key, args, fallback) => ptzAttentionTranslations['pt-BR'][key] || fallback || key;
  const decision = {id: 'arrival', action: 'focus_acquired', created_at: 10, state: 'FOCUSED', details: {
    group_framing_projection: {status: 'member_outside_or_unqualified', fit: {status: 'extent_exceeds_frame'}},
  }};
  const markup = renderToStaticMarkup(React.createElement(DecisionReview, {
    decisions: [decision], t, locale: 'pt-BR', camera: null,
  }));
  assert.ok(markup.includes('Chegada ao ponto confirmada'));
  assert.ok(markup.includes('maior que a imagem nesta posição'));
  assert.ok(markup.includes('Presença atual do sujeito não confirmada'));
  assert.ok(!markup.includes('Foco adquirido'));
  const session = renderToStaticMarkup(React.createElement(SessionCockpit, {
    t, locale: 'pt-BR', profile: {mode: 'live_panorama', name: 'Frente'},
    session: {state: 'FOCUSED'}, camera: null, bindingReady: true,
  }));
  assert.ok(session.includes('Ocorrência selecionada'));
  assert.ok(session.includes('Presença atual do sujeito não confirmada'));
});

test('observed framing expires locally and requires explicit source and arrival qualification', () => {
  const t = key => ptzAttentionTranslations['pt-BR'][key];
  const value = {observed_after_arrival: true, publication_age_seconds: .2,
    publication_recent: true, on_selected_source: true, box_with_margin: true};
  const render = (observation, statusAge) => renderToStaticMarkup(React.createElement(ObservedFramingNotice, {observation, statusAge, t}));
  assert.ok(render(value, .1).includes('Caixa do evento observada com margem após a chegada'));
  for (const statusAge of [1, Infinity, -1, NaN]) assert.ok(render(value, statusAge).includes('não confirmada'));
  for (const key of ['observed_after_arrival', 'publication_recent', 'on_selected_source', 'box_with_margin']) {
    assert.ok(render({...value, [key]: false}, .1).includes('não confirmada'));
  }
  assert.ok(render({...value, publication_age_seconds: -1}, .1).includes('não confirmada'));
  assert.ok(render(null, 0).includes('não confirmada'));
});
