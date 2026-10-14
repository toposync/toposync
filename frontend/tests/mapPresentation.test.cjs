const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
function load(file, requireModule = require) {
  const context = {exports: {}, require: requireModule};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.resolve(__dirname,file),'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS, target:ts.ScriptTarget.ES2022, jsx:ts.JsxEmit.ReactJSX}}).outputText,context);
  return context.exports;
}
const presentation = load('../src/ui/geographic/mapPresentation.ts');
test('unnamed elements expose localized type rather than internal identifiers',()=>{
  const t = key => ({camera:'Câmera','core.map.element':'Elemento'})[key];
  assert.equal(presentation.geographicElementLabel({id:'secret-id',name:'  '},{name:{key:'camera'}},t),'Câmera');
  assert.equal(presentation.geographicElementLabel({id:'secret-id'},undefined,t),'Elemento');
  assert.equal(presentation.geographicElementLabel({name:' Frente '},{name:'Camera'},t),'Frente');
});
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree,...nodes(tree.props?.children)];
function editorHarness(onSave = async()=>{}) {
  let cursor=0,closed=0; const state=[];
  const hooks={...React,useState(initial){const index=cursor++;if(!(index in state))state[index]=typeof initial==='function'?initial():initial;return [state[index],value=>state[index]=typeof value==='function'?value(state[index]):value];},useRef(value){return state[cursor++]??=( {current:value});},useEffect(){},useMemo:fn=>fn()};
  const geometry=load('../src/util/georeference.ts');
  const module=load('../src/ui/GeoreferenceEditor.tsx',name=>name==='react'?hooks:name==='react/jsx-runtime'?require(name):name.endsWith('/georeference')?geometry:name.endsWith('/i18n')?{i18n:{useI18n:()=>({t:key=>key,locale:'pt-BR'})}}:new Proxy({},{get:()=>function Stub(){}}));
  const props={value:null,compositionId:'room',elements:[],elementTypesById:{},notificationRenderers:[],activeNotification:null,onSave,onClose:()=>closed++};
  const render=()=>{cursor=0;return module.GeoreferenceEditor(props);};
  const find=predicate=>nodes(render()).find(predicate);
  const button=key=>find(node=>node.type==='button'&&nodes(node).some(child=>child.props?.children===key));
  const submit=()=>find(n=>n.type==='form').props.onSubmit({preventDefault(){}});
  return {render,find,button,submit,closed:()=>closed,change:()=>find(n=>n.type?.name==='NumericField').props.onValue(25)};
}
test('map search remains outside the save form; unchanged close does not prompt',()=>{
  const h=editorHarness();assert.equal(nodes(h.find(n=>n.type==='form')).some(n=>n.props?.compositions),false);
  h.render().props.onClose();assert.equal(h.closed(),1);
});
test('dirty edits require discard and undo restores original values',()=>{
  const h=editorHarness();h.change();h.render().props.onClose();assert.equal(h.closed(),0);
  assert.equal(h.find(n=>n.props?.title==='core.map.unsaved').props.open,true);
  h.button('core.map.keep_editing').props.onClick();h.button('core.map.undo').props.onClick();
  assert.equal(h.find(n=>n.type?.name==='NumericField').props.value,0);
  h.render().props.onClose();assert.equal(h.closed(),1);
});
test('pending saves prevent duplicate requests and close; conflict retains edits',async()=>{
  let reject,calls=0;const h=editorHarness(()=>{calls++;return new Promise((_,r)=>reject=r);});h.change();
  const pending=h.submit();await h.submit();h.render().props.onClose();assert.equal(calls,1);assert.equal(h.closed(),0);
  reject({status:409});await pending;assert.equal(h.find(n=>n.props?.role==='alert').props.children,'core.map.save_conflict');
  assert.equal(h.find(n=>n.type?.name==='NumericField').props.value,25);assert.equal(h.closed(),0);
});
test('invalid numeric drafts block save; successful save closes once',async()=>{
  let calls=0;const h=editorHarness(async()=>{calls++});h.find(n=>n.type?.name==='NumericField').props.onValidity(false);await h.submit();assert.equal(calls,0);
  h.find(n=>n.type?.name==='NumericField').props.onValidity(true);await h.submit();assert.equal(calls,1);assert.equal(h.closed(),1);
});
const parsing={readRecord:value=>value&&typeof value==='object'&&!Array.isArray(value)?value:{},readString:value=>typeof value==='string'?value:''};
const observations=load('../../extensions/cameras/ui/src/notifications/StationaryObservationSummary.tsx',name=>name==='../parsing'?parsing:require(name));
const renderText=tree=>typeof tree==='string'||typeof tree==='number'?String(tree):Array.isArray(tree)?tree.map(renderText).join(' '):tree?.props?renderText(tree.props.children):'';
const host={useI18n:()=>({locale:'pt-BR',t:(key,parameters={})=>Object.entries(parameters).reduce((value,[name,text])=>value.replace(`{{${name}}}`,text),observations.stationaryObservationTranslations['pt-BR'][key]??key)})};
test('stationary summaries localize structured evidence and preserve original description',()=>{
  const notification={type:'pipelines.tracking',payload:{description:'original raw text',subject:{category:'car'},event:{duration_seconds:75},data:{area_label:'Frente',velocity:{valid:true,speed_kmh:0.123456},stationary_event:{stationary_seconds:3.45}}}};
  const original=JSON.stringify(notification); assert.equal(observations.supportsStationaryObservation(notification),true);
  const text=renderText(observations.StationaryObservationSummary({notification,i18n:host}));
  assert.match(text,/Carro · Frente/);assert.match(text,/0,1 km\/h/);assert.match(text,/Tempo parado: 3,5 s/);assert.match(text,/Acontecimento: 1 min 15 s/);assert.equal(JSON.stringify(notification),original);
  notification.payload.data.velocity={valid:false,speed_kmh:Infinity};notification.payload.data.stationary_event.stationary_seconds=-1;
  const missing=renderText(observations.StationaryObservationSummary({notification,i18n:host}));assert.doesNotMatch(missing,/Infinity|km\/h|Tempo parado/);
});
const notices=load('../../extensions/spatial_video/ui/src/SpatialVideoCompatibilityNotice.tsx',name=>name==='react'?{...React,useState:()=>[false,()=>{}]}:require(name));
test('partial projection failure preserves scene and routes recovery to the affected source',()=>{
  let target;const base={loading:false,error:null,issues:[],hasCompatibleProjection:true,i18n:host,onConfigure:(...args)=>target=args};
  assert.equal(notices.SpatialVideoCompatibilityNotice(base),null);
  const issue={elementId:'camera-a',name:'Frente',message:'Geometria indisponível'};
  const partial=notices.SpatialVideoCompatibilityNotice({...base,issues:[issue]});assert.match(partial.props.className,/isPartial/);
  let stopped=0;const body=nodes(partial).find(n=>n.props?.className==='spatialProjectionNoticeBody');body.props.onPointerDown({stopPropagation(){stopped++}});body.props.onWheel({stopPropagation(){stopped++}});assert.equal(stopped,2);
  nodes(partial).find(n=>n.type==='button').props.onClick();assert.deepEqual(target,['camera-a','panorama-mapping']);
  const total=notices.SpatialVideoCompatibilityNotice({...base,hasCompatibleProjection:false,issues:[issue]});assert.doesNotMatch(total.props.className,/isPartial/);assert.equal(nodes(total).find(n=>n.type==='details').props.open,true);
});
test('composition positions distinguish unknown, missing and defined and ignore a cancelled response',async()=>{
  let state=null,effect,resolve;const hooks={useState:()=>[state,value=>state=value],useEffect:fn=>effect=fn};
  const geometry=load('../src/util/georeference.ts');
  const source=fs.readFileSync(path.resolve(__dirname,'../src/ui/geographic/useCompositionPositions.ts'),'utf8');
  const context={exports:{},AbortController,require:name=>name==='react'?hooks:name.endsWith('/api')?{getGeographicCompositions:()=>new Promise(done=>resolve=done)}:geometry};
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);
  assert.equal(context.exports.useCompositionPositions(true),null);const cleanup=effect();
  resolve([{id:'empty',meta:{}},{id:'located',meta:{georeference:{version:1,latitude:-27,longitude:-48,rotationDegrees:0,scale:1,axes:'x-east-z-south',units:'metres'}}}]);await Promise.resolve();
  assert.equal(state.empty,false);assert.equal(state.located,true);cleanup();
  context.exports.useCompositionPositions(true);const cancel=effect();cancel();resolve([{id:'stale',meta:{}}]);await Promise.resolve();assert.equal(state,null);
});
