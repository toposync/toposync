const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/ui/App.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["notificationMatchesFilter"]);
const functions = ast.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text));
const context = {asRecord: value => value && typeof value === "object" ? value : {}, notificationPriority: () => "medium"};
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
