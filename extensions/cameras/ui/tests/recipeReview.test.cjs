const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),React=require('react');
const {renderToStaticMarkup}=require('react-dom/server');
const filename=path.resolve(__dirname,'../src/settings/CameraRecipeReview.tsx');
const context={exports:{},require:name=>name==='react'?{...React,useEffect:()=>{},useState:()=>[null,()=>{}]}:name==='react/jsx-runtime'?require(name):{}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,context);
const graph={nodes:[{id:'source',operator:'camera.source',config:{camera_id:'camera',source_id:'main'}},{id:'area',operator:'camera.area_occurrence',config:{area:{name:'Escada'},mode:'entry',categories:['cat'],minimum_duration_seconds:10}},{id:'notify',operator:'core.notify',config:{}}]};
test('recipe review handles actual area fields and defers unknown or malformed graphs to the full editor',()=>{
 const {supportsRecipeReview,createCameraRecipeReview}=context.exports;
 assert.equal(supportsRecipeReview(graph),true);
 for(const value of [null,{nodes:[null]},{nodes:[...graph.nodes,{operator:'external.unknown',config:{}}]}])assert.equal(supportsRecipeReview(value),false);
 const panel=createCameraRecipeReview({useI18n:()=>({locale:'pt-BR'})});
 const html=renderToStaticMarkup(panel.render(graph));
 assert.match(html,/Entrada · Escada/);assert.match(html,/gato/);assert.match(html,/10 s/);
 assert.match(html,/não comprova reconhecimento/);assert.doesNotMatch(html,/undefined|NaN/);
});
