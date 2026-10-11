const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const ts = require('typescript');
const source = fs.readFileSync(require('node:path').resolve(__dirname,'../src/ui/notifications/pipelinesNotifications.tsx'),'utf8');
const ast = ts.createSourceFile('renderer.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(['asRecord','asString','asNumber','formatDurationCompact','subjectType','isLegacyActiveDescription','isDefaultGroupPresenceDescription','notificationIsClosed','cameraLabel','notificationDescription','resolveStoredImages','parseStoredTsMs','parseConfidence','toFileUrl']);
const context = {i18n:{t:(key)=>key,getLocale:()=> 'en'},resolveToposyncUrl:url=>'/ingress'+url};
vm.createContext(context);
vm.runInContext(ts.transpileModule(ast.statements.filter(node=>ts.isFunctionDeclaration(node)&&names.has(node.name?.text)).map(node=>node.getText(ast).replace('export ','')).join('\n'),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
test('zero duration means a single observed instant, while missing duration stays unknown',()=>{
  assert.equal(context.formatDurationCompact(0),'core.ui.notifications.details.single_observation');
  assert.equal(context.formatDurationCompact(undefined),null);
  assert.equal(context.formatDurationCompact(1),'1s');
});
test('default open and closed descriptions use existing translations; custom copy remains intact',()=>{
  for(const [status, description, expected] of [['open','Presence in progress - Front','description'],['closed','Presence ended - Front','closed_description']]) {
    assert.equal(context.notificationDescription({description,payload:{source:'pipelines',status,subject:{type:'group_event'}}}),`core.ui.notifications.group_presence.${expected}`);
  }
  assert.equal(context.notificationDescription({description:'Custom note',payload:{subject:{type:'group_event'},status:'closed'}}),'Custom note');
});
test('paired scene and crop have labels and ingress-safe URLs; a lone main is not mislabeled a scene',()=>{
  const item={rel_path:'pipelines/front/image.webp',stored_ts_ms:1000};
  const pair=context.resolveStoredImages({stored_images:{main:[item],subject_crop:[{...item,rel_path:'pipelines/front/crop.webp'}]}});
  assert.equal(pair.length,2);
  assert(pair.every(item=>item.url.startsWith('/ingress/files/')));
  assert(pair.some(item=>item.label==='core.ui.notifications.details.scene'));
  assert(pair.some(item=>item.label==='core.ui.notifications.details.subject_crop'));
  assert.equal(context.resolveStoredImages({stored_images:{main:[item]}})[0].label,'main');
});
