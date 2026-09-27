const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/ui/App.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["notificationMatchesDataFilter", "notificationMatchesFilter", "mergeNotificationPage", "upsertNotificationInState", "notificationFiltersEqual", "notificationFilterFacetsEqual", "stringArraysEqual"]);
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

for (const semantic of [true, "visual"]) test(`semantic ${semantic} results preserve ranking and realtime never applies a lexical filter`, () => {
  const filter = {priorities:["medium"],types:[],query:"felino tomando água",semantic};
  const page = [{id:"z",title:"Gato no bebedouro"},{id:"a",title:"Outro evento"}];
  const initial = context.mergeNotificationPage({byId:{},visibleIds:[]},page,{replaceVisible:true,filter});
  assert.equal(initial.visibleIds.join(","),"z,a");
  const updated = context.upsertNotificationInState(initial,{id:"z",title:"Descrição atualizada"},filter);
  assert.equal(updated.visibleIds.join(","),"z,a");
  assert.equal(updated.byId.z.title,"Descrição atualizada");
  const added = context.upsertNotificationInState(updated,{id:"new",title:filter.query},filter);
  assert.equal(added.visibleIds.join(","),"z,a");
});

test("temporal results replace stale history, append pages and retain updates until refresh", () => {
  const filter = {priorities:["medium"],types:[],query:"gato ontem à tarde",semantic:false};
  const old = {byId:{old:{id:"old",title:"gato ontem à tarde"}},visibleIds:["old"]};
  const first = context.mergeNotificationPage(old,[{id:"one",title:"Gato"}],
    {replaceVisible:true,filter,resolvedOnServer:true});
  assert.equal(first.visibleIds.join(","),"one");
  const second = context.mergeNotificationPage(first,[{id:"two",title:"Gato"}],
    {replaceVisible:false,filter,resolvedOnServer:true});
  assert.equal(second.visibleIds.join(","),"one,two");
  const updated = context.upsertNotificationInState(second,{id:"one",title:"Evidência atualizada"},filter);
  assert.equal(updated.visibleIds.join(","),"one,two");
  const added = context.upsertNotificationInState(updated,{id:"new",title:filter.query},filter);
  assert.equal(added.visibleIds.join(","),"one,two");
  const cleared = context.mergeNotificationPage(added,[],{replaceVisible:true,filter,resolvedOnServer:true});
  assert.equal(cleared.visibleIds.length,0);
});

test("changing text to visual search invalidates the previous result", () => {
  const filter = {priorities:["medium"],types:[],query:"pacote",semantic:true};
  assert.equal(context.notificationFiltersEqual(filter, {...filter,semantic:"visual"}),false);
  assert.equal(context.notificationFiltersEqual(filter, {...filter}),true);
});


test("location selection scopes literal updates and removes mismatches from semantic results", () => {
  const first = { id: "one", type: "event", title: "Gato", payload: {data: {camera_id: "patio", area_id: "water"}} };
  const filter = {priorities:["medium"],types:[],query:"gato",dataFilter:{camera_id:"patio",area_id:"water"}};
  assert.equal(context.notificationMatchesFilter(first, filter), true);
  for (const camera_id of ["Patio", 7, ["patio"], null]) {
    assert.equal(context.notificationMatchesFilter({...first,payload:{data:{camera_id,area_id:"water"}}}, filter), false);
  }
  const previous = {byId:{one:first},visibleIds:["one"]};
  for (const semantic of [false,true,"visual"]) {
    const next = {...first,payload:{data:{camera_id:"garage",area_id:"water"}}};
    assert.deepEqual(Array.from(context.upsertNotificationInState(previous,next,{...filter,semantic}).visibleIds), []);
  }
  assert.equal(context.notificationFiltersEqual(filter,{...filter,dataFilter:{area_id:"water",camera_id:"patio"}}),true);
  assert.equal(context.notificationFiltersEqual(filter,{...filter,dataFilter:{camera_id:"garage"}}),false);
});
