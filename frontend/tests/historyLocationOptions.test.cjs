const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const filename=path.resolve(__dirname,"../src/ui/notifications/HistoryLocationFilter.tsx");
const ast=ts.createSourceFile(filename,fs.readFileSync(filename,"utf8"),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
const source=ast.statements.find(node=>ts.isFunctionDeclaration(node)&&node.name?.text==="useHistoryLocationOptions").getText(ast).replace("export ","");

test("a failed camera request leaves areas usable; cancellation rejects late options", async () => {
  const state=[], effects=[], requests=[];
  const context={AbortController, window:{addEventListener(){},removeEventListener(){}},isAbortError:()=>false,
    useState(initial){const index=state.length;state.push(initial);return [initial,value=>{state[index]=typeof value==="function"?value(state[index]):value;}];},
    useEffect(effect){effects.push(effect);},
    getNotificationDataOptions(field,label,signal){return new Promise((resolve,reject)=>requests.push({field,signal,resolve,reject}));}};
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
  context.useHistoryLocationOptions(true);
  const cleanup=effects.map(effect=>effect());
  requests.find(request=>request.field==="camera_id").reject(new Error("camera unavailable"));
  requests.find(request=>request.field==="area_id").resolve([{value:"garden",label:"Jardim"}]);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(state[1][0],true);assert.equal(state[1][1],false);
  assert.equal(state[0][0],null);assert.equal(state[0][1][0].value,"garden");
  cleanup.forEach(fn=>fn?.());
  assert.ok(requests.every(request=>request.signal.aborted));

  state.length=0;effects.length=0;requests.length=0;
  context.useHistoryLocationOptions(true);
  const cancel=effects.map(effect=>effect());cancel.forEach(fn=>fn?.());
  requests.forEach(request=>request.resolve([{value:"late",label:"Obsoleto"}]));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(state[0][0],null);assert.equal(state[0][1],null);
});
