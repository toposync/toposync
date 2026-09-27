const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/ui/App.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["notificationMatchesFilter", "mergeNotificationPage", "upsertNotificationInState"]);
const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text));
const context = {withoutEphemeralImage: value => value, sortNotificationIdsByCreatedDesc: ids => [...ids].sort(), asRecord: value => value && typeof value === "object" ? value : {}, notificationPriority: () => "medium"};
vm.createContext(context);
vm.runInContext(ts.transpileModule(functions.map(node => node.getText(ast)).join("\n"),
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
test("realtime metadata filter agrees with persisted lexical search", () => {
  const notification = {type: "pipelines.event",title:"Gato observado",description:"Bebedouro",
    payload: {data: {camera_name:"Pátio",area_label:"Água",source: {source_name:"Camera sul"}}}};
  for (const [query, expected] of [["gato bebedouro",true],["PATIO agua",true],["gato sul",true],["gato garagem",false],["%",false],["ontem",false]]) {
    assert.equal(context.notificationMatchesFilter(notification, {priorities:["medium"],types:[],query}), expected, query);
  }
});

test("nontext metadata is excluded from realtime search", () => {
  const notification = {type:"pipelines.event",title:"Objeto", payload:{data:{camera_name:998877,
    area_label:{nested:"injetado"},source:{source_name:["indevido"]}}}};
  for (const query of ["998877","nested","injetado","indevido"]) {
    assert.equal(context.notificationMatchesFilter(notification,{priorities:["medium"],types:[],query}),false);
  }
});

test("semantic results preserve ranking and realtime never applies a lexical filter", () => {
  const filter = {priorities:["medium"],types:[],query:"felino tomando água",semantic:true};
  const page = [{id:"z",title:"Gato no bebedouro"},{id:"a",title:"Outro evento"}];
  const initial = context.mergeNotificationPage({byId:{},visibleIds:[]},page,{replaceVisible:true,filter});
  assert.equal(initial.visibleIds.join(","),"z,a");
  const updated = context.upsertNotificationInState(initial,{id:"z",title:"Descrição atualizada"},filter);
  assert.equal(updated.visibleIds.join(","),"z,a");
  assert.equal(updated.byId.z.title,"Descrição atualizada");
  const added = context.upsertNotificationInState(updated,{id:"new",title:filter.query},filter);
  assert.equal(added.visibleIds.join(","),"z,a");
});
