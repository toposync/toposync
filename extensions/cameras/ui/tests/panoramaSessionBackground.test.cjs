const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const {chromium}=require('playwright');

test('session atlas projects A → B → A, respects coverage and seam, clears and releases buffers',async()=>{
 const browser=await chromium.launch({headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 try {
  const page=await browser.newPage();
  await page.setContent('<canvas id="atlas"></canvas>');
  const source=fs.readFileSync(path.join(__dirname,'../src/live/panoramaVideo.ts'),'utf8');
  await page.addScriptTag({content:'var exports={};'+ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText});
  const result=await page.evaluate(()=>{
   const mask=document.createElement('canvas');mask.width=512;mask.height=256;
   const coverage=mask.getContext('2d');coverage.fillStyle='white';coverage.fillRect(0,0,512,256);
   coverage.fillStyle='black';coverage.fillRect(250,120,12,16);
   const atlas=document.getElementById('atlas');
   const background=exports.createSessionPanoramaBackground(atlas,mask,512,256);
   const picture=document.createElement('canvas');picture.width=160;picture.height=90;
   const context=picture.getContext('2d');
   const geometry={lens:{width:160,height:90,fx:80,fy:80,cx:80,cy:45},panorama_to_camera:[[0,1,0],[0,0,-1],[1,0,0]]};
   const reverse={...geometry,panorama_to_camera:[[0,-1,0],[0,0,-1],[-1,0,0]]};
   const pixel=(x,y)=>Array.from(atlas.getContext('2d').getImageData(x,y,1,1).data);
   context.fillStyle='red';context.fillRect(0,0,160,90);background.commit(picture,geometry);
   const a=pixel(270,128),outside=pixel(100,128),hole=pixel(255,128);
   // Mutating the player/source afterwards must not repaint a stored photograph.
   context.fillStyle='blue';context.fillRect(0,0,160,90);
   const frozen=pixel(270,128);
   background.commit(picture,reverse);
   const preserved=pixel(270,128),b=pixel(1,128),seam=pixel(511,128),copy=pixel(513,128);
   context.fillStyle='lime';context.fillRect(0,0,160,90);background.commit(picture,geometry);
   const revisited=pixel(270,128),other=pixel(1,128),holeAfter=pixel(255,128);
   background.clear();const cleared=pixel(270,128);
   background.dispose();const disposed=[atlas.width,atlas.height];
   const bounded=exports.createSessionPanoramaBackground(atlas,mask,8192,4096);
   const dimensions=[atlas.width,atlas.height];bounded.dispose();
   return {a,outside,hole,frozen,preserved,b,seam,copy,revisited,other,holeAfter,cleared,disposed,dimensions};
  });
  for(const name of ['a','frozen','preserved'])assert.deepEqual(result[name],[255,0,0,255],name);
  for(const name of ['b','seam','copy','other'])assert.deepEqual(result[name],[0,0,255,255],name);
  assert.deepEqual(result.revisited,[0,255,0,255]);
  for(const name of ['outside','hole','holeAfter','cleared'])assert.deepEqual(result[name],[0,0,0,0],name);
  assert.deepEqual(result.disposed,[1,1]);
  assert.deepEqual(result.dimensions,[4096,1024]);
 } finally {await browser.close();}
});

test('mounted session rejects a late photograph after movement and retains history across reconnect only',async()=>{
 const browser=await chromium.launch({headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 try {
  const page=await browser.newPage();
  await page.setContent('<div id="root"></div>');
  await page.addScriptTag({path:require.resolve('react').replace(/index.js$/,'umd/react.development.js')});
  await page.addScriptTag({path:require.resolve('react-dom').replace(/index.js$/,'umd/react-dom.development.js')});
  const compile=name=>ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/live',name),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.React}}).outputText;
  await page.addScriptTag({content:'var exports={};'+compile('panoramaVideo.ts')+';window.projection=exports;'});
  await page.addScriptTag({content:`var require=name=>({
   react:{...React,default:React},'@toposync/plugin-api':{resolveToposyncUrl:value=>value},
   './panoramaVideo':projection,'./FloatingVideoPanel':{FloatingVideoPanel:props=>React.createElement('div',null,props.children)},
   '../settings/panoramaCrop':{validPanoramaCrop:()=>false},
   './livePanorama':{isExternalMotionEpoch:()=>false,livePanoramaStyles:''}
  }[name]);var exports={};`+compile('LivePanoramaView.tsx')+';window.Session=LiveSession;'});
  await page.evaluate(()=>{
   window.server={session_id:'test',sequence:0,moving:false,blocked:false,phase:'aligned'};
   window.observations=[];window.requests=[];
   window.fetch=async(url,options={})=>{
    requests.push({url,method:options.method,body:options.body});
    if(url.endsWith('/stop')) server={...server,sequence:JSON.parse(options.body).sequence,moving:false,phase:'aligned'};
    if(url.endsWith('/observe')){
     const body=JSON.parse(options.body);
     return new Promise(resolve=>observations.push({body,resolve:()=>resolve({ok:true,json:async()=>({...server,status:'localized',geometry,
       observation_sequence:body.sequence,epoch:body.epoch})})}));
    }
    return {ok:true,json:async()=>({...server})};
   };
   window.geometry={lens:{width:160,height:90,fx:80,fy:80,cx:80,cy:45},panorama_to_camera:[[0,1,0],[0,0,-1],[1,0,0]]};
   const mask=document.createElement('canvas');mask.width=512;mask.height=256;
   mask.getContext('2d').fillStyle='white';mask.getContext('2d').fillRect(0,0,512,256);
   window.picture=document.createElement('canvas');picture.width=160;picture.height=90;
   picture.getContext('2d').fillStyle='red';picture.getContext('2d').fillRect(0,0,160,90);
   window.host={ui:{NavigableViewport:props=>{window.clickPanorama=props.onContentClick;return React.createElement('div',{style:{height:280,position:'relative'}},props.children);},
    LiveViewPlayer:props=>{window.deliver=props.onFrame;return null;}}};
   window.choice={camera_id:'camera',source_id:'wide',kind:'active',artifact:{id:'pano',revision:1,width:512,height:256,image_url:mask.toDataURL(),coverage_url:mask.toDataURL()}};
   window.reactRoot=ReactDOM.createRoot(document.getElementById('root'));
   window.mount=key=>reactRoot.render(React.createElement(Session,{key,host,choice,refreshReferences:()=>{}}));
   window.send=(sequence,epoch='stream-a')=>deliver({cameraId:'camera',sourceId:'wide',image:picture,width:160,height:90,
    opticalSourceSize:{width:160,height:90},contentRect:{x:0,y:0,width:1,height:1},sequence,epoch,mediaTime:sequence*.8});
   window.alpha=()=>document.querySelector('canvas[aria-label]').getContext('2d').getImageData(270,128,1,1).data[3];
   mount('first');
  });
  await page.waitForFunction(()=>window.deliver && window.requests.length>0);
  const observe=async(sequence)=>{
   await page.waitForTimeout(850);
   await page.evaluate(sequence=>send(sequence),sequence);
   await page.waitForFunction(()=>observations.length>0);
   await page.evaluate(()=>observations.shift().resolve());
  };
  await observe(1);
  assert.equal(await page.evaluate(()=>alpha()),0,'one localization is insufficient');
  await observe(2);
  await page.getByText(/Fundo atualizado nesta sessão/).waitFor();
  assert.equal(await page.evaluate(()=>alpha()),255);
  await page.getByRole('button',{name:'Restaurar fundo original'}).click();
  assert.equal(await page.evaluate(()=>alpha()),0);
  await observe(3);
  assert.equal(await page.evaluate(()=>alpha()),0,'restore is not immediately undone at the same view');
  // A new view is in flight when the controller reports movement.
  await page.waitForTimeout(850);
  await page.evaluate(()=>{geometry={...geometry,panorama_to_camera:[[0,-1,0],[0,0,-1],[-1,0,0]]};send(4);server.moving=true;});
  await page.getByRole('button',{name:'Parar movimento'}).waitFor();
  await page.evaluate(()=>observations.shift().resolve());
  assert.equal(await page.evaluate(()=>alpha()),0);
  await page.evaluate(()=>{server.moving=false;geometry={...geometry,panorama_to_camera:[[0,1,0],[0,0,-1],[1,0,0]]};});
  await page.getByRole('button',{name:'Parar movimento'}).waitFor({state:'detached'});
  // A fresh logical session starts empty and can independently qualify its view.
  await page.evaluate(()=>mount('second'));
  await observe(5);await observe(6);
  await page.getByText(/Fundo atualizado nesta sessão/).waitFor();
  if(process.env.TOPOSYNC_TEST_ARTIFACT_DIR){
   await page.evaluate(()=>{
    const label=document.createElement('p');label.textContent='TESTE SINTÉTICO — componente real, imagem e respostas de câmera simuladas';document.body.prepend(label);
   });
   await page.screenshot({path:path.join(process.env.TOPOSYNC_TEST_ARTIFACT_DIR,'fundo-sessao-componente-sintetico.png'),fullPage:true});
   await page.evaluate(()=>deliver(null));
   await page.waitForFunction(()=>document.querySelector('[data-aligned]').dataset.aligned==='false');
   await page.screenshot({path:path.join(process.env.TOPOSYNC_TEST_ARTIFACT_DIR,'fundo-sessao-historico-sem-video-sintetico.png'),fullPage:true});
  }
  await page.getByRole('button',{name:'Reconectar',exact:true}).click();
  assert.equal(await page.evaluate(()=>alpha()),255,'transport restart preserves confirmed history');
  await page.evaluate(()=>mount('third'));
  await page.waitForFunction(()=>alpha()===0);
  assert.equal(await page.getByText(/Fundo atualizado nesta sessão/).count(),0);
  await observe(7);
  await page.waitForTimeout(850);
  await page.evaluate(()=>{send(8);deliver(null);send(9);});
  await page.waitForFunction(()=>observations.length>0);
  await page.evaluate(()=>observations.shift().resolve());
  await observe(10);
  assert.equal(await page.evaluate(()=>alpha()),0,'response predating a lost frame cannot seed the next photograph');
  await observe(11);
  await page.getByText(/Fundo atualizado nesta sessão/).waitFor();
  await page.evaluate(()=>{geometry={...geometry,panorama_to_camera:[[0,-1,0],[0,0,-1],[-1,0,0]]};});
  await observe(12);
  await page.waitForTimeout(850);
  await page.evaluate(()=>{
   send(13);
   Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
   document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForFunction(()=>observations.length>0);
  const hiddenObservationCount=await page.evaluate(()=>requests.filter(value=>value.url.endsWith('/observe')).length);
  await page.waitForTimeout(850);
  await page.evaluate(()=>send(14));
  assert.equal(await page.evaluate(()=>requests.filter(value=>value.url.endsWith('/observe')).length),hiddenObservationCount,'hidden frames create no localization requests');
  assert.equal(await page.evaluate(()=>alpha()),255,'hidden view retains confirmed history');
  await page.evaluate(()=>{
   Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
   document.dispatchEvent(new Event('visibilitychange'));
   observations.shift().resolve();
  });
  await observe(15);
  assert.equal(await page.evaluate(()=>document.querySelector('canvas[aria-label]').getContext('2d').getImageData(1,128,1,1).data[3]),0,'pre-hide response cannot qualify the new region');
  await observe(16);
  await page.waitForFunction(()=>document.querySelector('canvas[aria-label]').getContext('2d').getImageData(1,128,1,1).data[3]===255);
  assert.equal(await page.evaluate(()=>alpha()),255,'new region preserves the older confirmed region');
  assert.equal(await page.evaluate(()=>requests.some(value=>value.url.endsWith('/intent'))),false);
  // Reuse the mounted production component and synthetic registration to
  // exercise both successful and failed late intent responses after polling.
  await page.evaluate(()=>{
   const ordinaryFetch=window.fetch;
   window.fetch=async(url,options={})=>{
    if(!url.endsWith('/intent'))return ordinaryFetch(url,options);
    requests.push({url,method:options.method,body:options.body});
    server={...server,sequence:JSON.parse(options.body).sequence,moving:true,phase:'moving',result:null};
    const snapshot={...server};
    return new Promise((resolve,reject)=>{
     window.releaseIntent=()=>resolve({ok:true,json:async()=>snapshot});
     window.failIntent=()=>reject(new TypeError('Synthetic response lost'));
    });
   };
   window.frameNumber=20;
   window.frameTimer=setInterval(()=>{send(++frameNumber);while(observations.length)observations.shift().resolve();},100);
  });
  for(const response of ['releaseIntent','failIntent']){
   await page.waitForFunction(()=>document.querySelector('[data-aligned]').dataset.aligned==='true');
   await page.evaluate(()=>clickPanorama({x:270,y:128}));
   await page.waitForFunction(()=>server.moving);
   await page.evaluate(()=>{server={...server,moving:false,phase:'aligned',result:{sequence:server.sequence,verified:true}};});
   await page.getByText(/Centro confirmado/).waitFor();
   await page.evaluate(()=>{
    window.arrivalChanges=[];
    const indicator=document.querySelector('[data-aligned]');
    window.arrivalObserver=new MutationObserver(()=>arrivalChanges.push({aligned:indicator.dataset.aligned,text:indicator.textContent}));
    arrivalObserver.observe(indicator,{attributes:true,childList:true,subtree:true});
   });
   await page.evaluate(response=>window[response](),response);
   await page.waitForTimeout(350);
   const result=await page.evaluate(()=>{
    arrivalObserver.disconnect();
    const indicator=document.querySelector('[data-aligned]');
    return{aligned:indicator.dataset.aligned,text:indicator.textContent,changes:arrivalChanges,alerts:[...document.querySelectorAll('[role="alert"]')].map(node=>node.textContent)};
   });
   assert.equal(result.aligned,'true',response);
   assert.match(result.text,/Centro confirmado/,response);
   assert.ok(result.changes.every(change=>change.aligned==='true'&&!change.text.includes('Movendo')),response);
   assert.deepEqual(result.alerts,[],response);
  }
  const retarget=await page.evaluate(()=>{
   const before=server.sequence;
   clickPanorama({x:260,y:128});
   const oldReply=releaseIntent;
   const firstMoving=server.moving;
   clickPanorama({x:300,y:128});
   oldReply();
   return{before,firstMoving,after:server.sequence,
    intents:requests.filter(request=>request.url.endsWith('/intent')).slice(-2).map(request=>JSON.parse(request.body))};
  });
  assert.equal(retarget.firstMoving,true);
  assert.equal(retarget.after,retarget.before+2,'second click is dispatched before the first completes');
  assert.deepEqual(retarget.intents.map(intent=>intent.sequence),[retarget.before+1,retarget.before+2]);
  assert.deepEqual(retarget.intents.map(intent=>intent.x),[260/512,300/512]);
  await page.waitForTimeout(150);
  assert.equal(await page.locator('[aria-label="Destino desejado"]').evaluate(node=>node.style.left),'300px','old response cannot restore the older target');
  await page.evaluate(()=>{server={...server,moving:false,phase:'aligned',result:{sequence:server.sequence,verified:true}};});
  await page.getByText(/Centro confirmado/).waitFor();
  await page.evaluate(()=>releaseIntent());
  await page.waitForTimeout(150);
  assert.equal(await page.locator('[data-aligned]').getAttribute('data-aligned'),'true');
  assert.equal(await page.locator('[aria-label="Destino desejado"]').evaluate(node=>node.style.left),'300px');
  if(process.env.TOPOSYNC_TEST_ARTIFACT_DIR){
   await page.screenshot({path:path.join(process.env.TOPOSYNC_TEST_ARTIFACT_DIR,'ultimo-destino-confirmado-sintetico.png'),fullPage:true});
  }
  await page.evaluate(()=>clearInterval(frameTimer));
  await page.evaluate(()=>reactRoot.unmount());
 } finally {await browser.close();}
});
