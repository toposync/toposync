const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const source = ts.createSourceFile('MainScreen.tsx', fs.readFileSync(path.resolve(__dirname,
  '../src/ui/screens/MainScreen.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let callback;
(function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect'
    && node.arguments[0]?.getText(source).includes('notificationsToggleRef.current')) {
    callback = node.arguments[0].getText(source);
  }
  ts.forEachChild(node, visit);
})(source);
assert.ok(callback);

test('closing mobile details restores a surviving control once; opening and rerenders do not steal focus', () => {
  const focused = [];
  const state = { detailsWereOpen: { current: false }, isNotificationDetailsOpen: false,
    notificationsOpen: true, notificationsToggleRef: { current: { focus: options => focused.push(options) } } };
  vm.runInNewContext(ts.transpileModule(`this.effect = ${callback}`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, state);
  state.effect();
  state.isNotificationDetailsOpen = true; state.notificationsOpen = false; state.effect();
  assert.equal(focused.length, 0);
  state.isNotificationDetailsOpen = false; state.effect();
  assert.equal(focused.length, 1);
  assert.equal(focused[0].preventScroll, true);
  state.effect();
  assert.equal(focused.length, 1);
  state.isNotificationDetailsOpen = true; state.notificationsOpen = true; state.effect();
  state.isNotificationDetailsOpen = false; state.effect();
  assert.equal(focused.length, 1, 'the shared modal restores the original desktop opener');
  state.notificationsToggleRef.current = null;
  state.isNotificationDetailsOpen = true; state.effect();
  state.notificationsOpen = false; state.isNotificationDetailsOpen = false;
  assert.doesNotThrow(() => state.effect());
});
