const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const file=path.resolve(__dirname,'../src/ui/App.tsx');
const source=ts.createSourceFile(file,fs.readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let callback;
(function visit(node){if(ts.isVariableDeclaration(node)&&node.name.getText(source)==='openHistoryNotification')callback=node.initializer.arguments[0].getText(source);ts.forEachChild(node,visit)})(source);
assert.ok(callback);
function harness(read){
  let state={byId:{visible:{id:'visible'}},visibleIds:['visible']},selected='visible';
  const context={getNotification:read,setNotificationsState:update=>state=update(state),selectNotification:id=>selected=id,setHistorySource:()=>{},
    withoutEphemeralImage:({ephemeralImage,...notification})=>notification};
  vm.runInNewContext(ts.transpileModule(`this.open = ${callback}`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
  return {...context,state:()=>state,selected:()=>selected};
}
test('opening an authorized source outside the page preserves the search and strips temporary pixels',async()=>{
  const h=harness(async id=>({id,title:'source',ephemeralImage:{private:'pixels'}}));
  await h.open('outside');
  assert.equal(h.selected(),'outside');
  assert.equal(h.state().visibleIds.join(','),'visible');
  assert.equal(h.state().byId.outside.title,'source');
  assert.equal(h.state().byId.outside.ephemeralImage,undefined);
});
test('removed, unauthorized and mismatched sources do not change selection or cache',async()=>{
  for(const read of [async()=>{throw new Error('404')},async()=>{throw new Error('403')},async()=>({id:'wrong'})]){
    const h=harness(read);await assert.rejects(h.open('outside'));
    assert.equal(h.selected(),'visible');assert.equal(Object.keys(h.state().byId).join(','),'visible');
  }
});

test('source revalidation hides prior details and removes revoked records instead of restoring stale private data',async()=>{
 let refresh;
 (function visit(node){if(ts.isVariableDeclaration(node)&&node.name.getText(source)==='refreshHistorySource')refresh=node.initializer.arguments[0].getText(source);ts.forEachChild(node,visit)})(source);
 for(const allowed of [true,false]){
  let detail={id:'source',notification:{id:'source',title:'old'}},selected='source',state={byId:{source:detail.notification,other:{id:'other'}},visibleIds:['source','other']},resolve;
  const waiting=new Promise((yes,no)=>{resolve=()=>allowed?yes({id:'source',title:'fresh',ephemeralImage:{pixels:'private'}}):no(new Error('403'))});
  const context={AbortController,historySourceRef:{current:detail},historySourceRead:{current:null},getNotification:()=>waiting,setHistorySource:update=>detail=update(detail),setActiveNotificationId:update=>selected=update(selected),setNotificationsState:update=>state=update(state),withoutEphemeralImage:({ephemeralImage,...record})=>record};
  vm.runInNewContext(ts.transpileModule(`this.refresh = ${refresh}`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
  const pending=context.refresh();assert.equal(detail.notification,null);resolve();await pending;
  if(allowed){assert.equal(detail.notification.title,'fresh');assert.equal(detail.notification.ephemeralImage,undefined)}
  else{assert.equal(detail,null);assert.equal(selected,null);assert.equal(state.byId.source,undefined);assert.equal(state.visibleIds.join(','),'other')}
 }
});
