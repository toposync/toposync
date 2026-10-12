const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/ui/App.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["notificationHistoryRefreshMode","notificationMatchesDataFilter", "notificationMatchesFilter", "mergeNotificationPage", "upsertNotificationInState", "notificationFiltersEqual", "notificationFilterFacetsEqual", "stringArraysEqual"]);
const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text));
const context = {Date, withoutEphemeralImage: value => value, sortNotificationIdsByCreatedDesc: ids => [...ids].sort(), asRecord: value => value && typeof value === "object" ? value : {}, notificationPriority: () => "medium"};
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

test("private extension selection replaces stale results and never admits realtime data", () => {
  const filter = {priorities:["medium"],types:[],query:"",extension:{id:"vision",value:"human:person"}};
  assert.equal(context.notificationFiltersEqual(filter, {...filter,extension:{id:"vision",value:"recognized:person"}}),false);
  assert.equal(context.notificationFiltersEqual(filter, {...filter,extension:undefined}),false);
  const old = {byId:{old:{id:"old",title:"Old"}},visibleIds:["old"]};
  const selected = context.mergeNotificationPage(old,[{id:"linked",title:"Person"}],{replaceVisible:true,filter});
  assert.equal(selected.visibleIds.join(","),"linked");
  assert.equal(context.upsertNotificationInState(selected,{id:"linked",title:"Person"},filter).visibleIds.join(","),"linked");
  const added = context.upsertNotificationInState(selected,{id:"unlinked",title:"Person"},filter);
  assert.equal(added.visibleIds.join(","),"linked");
  const changed = context.upsertNotificationInState(added,{id:"linked",title:"Changed association"},filter);
  assert.equal(changed.visibleIds.length,0);
  assert.equal(changed.byId.linked,undefined);
});


test("private selection with empty semantic query retains literal pagination", () => {
  const filter = {priorities:["medium"],types:[],query:"",semantic:"visual",extension:{id:"vision",value:"human:person"}};
  const first = context.mergeNotificationPage({byId:{},visibleIds:[]},[{id:"one"}],{replaceVisible:true,filter});
  const second = context.mergeNotificationPage(first,[{id:"two"}],{replaceVisible:false,filter});
  assert.equal(second.visibleIds.join(","),"one,two");
});

const apiFilename = path.resolve(__dirname, "../src/util/api.ts");
const apiAst = ts.createSourceFile(apiFilename, fs.readFileSync(apiFilename,"utf8"), ts.ScriptTarget.Latest, true);
const listFunction = apiAst.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "listNotifications");
function listContext(fetch, resolveToposyncUrl = url => url) {
  const value = {fetch,URLSearchParams,resolveToposyncUrl}; vm.createContext(value);
  vm.runInContext(ts.transpileModule(listFunction.getText(apiAst).replace("export ",""), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,value);
  return value;
}
test("private endpoint preserves filters and never falls through to public history on error", async () => {
  const calls = [];
  const privateApi = listContext(async (url,options) => {calls.push({url,options}); return {ok:false,status:404};});
  await assert.rejects(privateApi.listNotifications({historyEndpoint:"/api/vision/identities/person/notifications?association=human",semantic:"visual",query:"cat",before:12,dataFilter:{camera_id:"patio"}}), /404/);
  assert.equal(calls.length,1);
  const query = new URL(calls[0].url,"http://local").searchParams;
  assert.equal(query.get("association"),"human");assert.equal(query.get("mode"),"visual");assert.equal(query.get("before"),"12");
  assert.equal(query.get("data_filter"),'{"camera_id":"patio"}');
  await assert.rejects(privateApi.listNotifications({historyEndpoint:"https://remote.invalid/api/history"}), /Invalid/);
  assert.equal(calls.length,1);
});


test("date-only server results append pages even when semantic mode is selected", () => {
  const filter = {priorities:["medium"],types:[],query:"ontem",semantic:"visual",extension:{id:"vision",value:"human:person"}};
  const first = context.mergeNotificationPage({byId:{},visibleIds:[]},[{id:"one"}],{replaceVisible:true,filter,ranked:false});
  const second = context.mergeNotificationPage(first,[{id:"two"}],{replaceVisible:false,filter,ranked:false});
  assert.equal(second.visibleIds.join(","),"one,two");
});

test("refresh policy updates deterministic selections without rerunning models", () => {
  const filter = {priorities:["low"],types:[],query:""};
  assert.equal(context.notificationHistoryRefreshMode(filter,false),"none");
  assert.equal(context.notificationHistoryRefreshMode(filter,true),"automatic");
  assert.equal(context.notificationHistoryRefreshMode({...filter,extension:{id:"categories",value:"dog"}},false),"automatic");
  assert.equal(context.notificationHistoryRefreshMode({...filter,semantic:"visual"},true),"automatic");
  assert.equal(context.notificationHistoryRefreshMode({...filter,semantic:true,query:"cat"},true),"manual");
  assert.equal(context.notificationHistoryRefreshMode({...filter,semantic:"visual",query:"cat",extension:{id:"categories",value:"cat"}},false),"manual");
});

test("explicit periods invalidate selections and keep realtime records inside the same half-open interval", () => {
  const filter = {priorities:["medium"],types:[],query:"",period:{preset:"custom",start:"1970-01-01",end:"1970-01-01"},timeRange:{createdAfter:100,createdBefore:200,timezone:"UTC"}};
  const record = {id:"one",title:"Gato",createdAt:new Date(100000).toISOString()};
  assert.equal(context.notificationMatchesFilter(record,filter),true);
  for (const seconds of [99,200,201]) assert.equal(context.notificationMatchesFilter({...record,createdAt:new Date(seconds*1000).toISOString()},filter),false);
  assert.equal(context.notificationMatchesFilter({...record,createdAt:undefined},filter),false);
  assert.equal(context.notificationFiltersEqual(filter,{...filter,timeRange:{...filter.timeRange,createdBefore:201}}),false);
  const state = {byId:{one:record},visibleIds:["one"],resolvedOnServer:true};
  assert.equal(context.upsertNotificationInState(state,{...record,createdAt:new Date(200000).toISOString()},filter).visibleIds.length,0);
});

test("all history endpoints and their next pages carry explicit time bounds", async () => {
  const calls=[];
  const api=listContext(async url => {calls.push(url);return {ok:true,json:async()=>({notifications:[],next_cursor:5})};});
  for (const historyEndpoint of [undefined,"/api/vision/history/notifications?category=dog","/api/vision/identities/person/notifications?association=human"]) {
    for (const semantic of [false,true,"visual"]) for (const before of [null,5]) {
      await api.listNotifications({historyEndpoint,query:"gato",semantic,before,createdAfter:100,createdBefore:200});
      const params=new URL(calls.at(-1),"http://local").searchParams;
      assert.equal(params.get("created_after"),"100"); assert.equal(params.get("created_before"),"200");
      assert.equal(params.get("before"),before===null?null:"5");
    }
  }
});

test("public and private time queries preserve Home Assistant ingress", async () => {
  const {resolveToposyncUrl}=require("../packages/plugin-api/basePath.js");
  const previous=global.window;
  global.window={__TOPOSYNC_PUBLIC_BASE_PATH__:"/ingress/search",location:{origin:"http://local",href:"http://local/ingress/search/"}};
  try {
    const calls=[];
    const api=listContext(async url=>{calls.push(url);return {ok:true,json:async()=>({notifications:[]})};},resolveToposyncUrl);
    for (const historyEndpoint of [undefined,"/api/vision/identities/person/notifications?association=human"]) {
      await api.listNotifications({historyEndpoint,query:"gato",createdAfter:100,createdBefore:200});
      assert.ok(calls.at(-1).startsWith("/ingress/search/api/"));
      assert.equal(new URL(calls.at(-1),"http://local").searchParams.get("created_before"),"200");
    }
  } finally { if (previous===undefined) delete global.window; else global.window=previous; }
});
