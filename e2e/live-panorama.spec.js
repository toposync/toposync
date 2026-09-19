const {test,expect}=require('@playwright/test');
const root='/api/cameras/live-panorama';
const artifact={id:'a'.repeat(32),revision:1,width:1600,height:800,image_url:'/fixture-panorama.svg',coverage_url:'/fixture-coverage.svg',crop:{u_start:.3,u_width:.4,v_start:.3,v_height:.4},crop_revision:2,created_at:"2026-09-19T01:35:49Z"};
const geometry={lens:{width:640,height:360,fx:400,fy:400,cx:319.5,cy:179.5,distortion:[]},panorama_to_camera:[[0,1,0],[0,0,-1],[1,0,0]]};
let controls,sessions,observation,mode,sequence,identifier,currentArtifact,currentGeometry;
test.beforeEach(async({page})=>{
 currentArtifact=artifact;currentGeometry=geometry;controls=[];sessions=0;observation=0;mode='aligned';sequence=0;identifier='';
 await page.route('**/fixture-*.svg',route=>route.fulfill({contentType:'image/svg+xml',body:`<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="800"><rect width="1600" height="800" fill="${route.request().url().includes('coverage')?'white':'#adbccc'}"/><path d="M800 0V800M0 400H1600" stroke="#fff" stroke-width="2"/></svg>`}));
 const state=()=>({session_id:identifier,can_control:true,sequence,phase:mode,moving:mode==='moving',blocked:false,commands:0,error:null});
 await page.route('**/api/cameras/live-panorama**',async route=>{
  const request=route.request(),path=new URL(request.url()).pathname,body=request.postDataJSON();
  if(path===root)return route.fulfill({json:{choices:[{camera_id:'synthetic',camera_name:'Câmera sintética',source_id:'wide',source_name:'Grande angular',kind:'active',artifact:currentArtifact,reason:null,secondary_sources:process.env.TOPOSYNC_PHOTOGRAPH_FIXTURE?[]:[{id:'tele',name:'Fonte sintética secundária'}]}]}});
  if(path===root+'/sessions'){identifier=`session-${++sessions}`;sequence=0;return route.fulfill({json:state()});}
  if(path.endsWith('/observe')){observation++;return route.fulfill({json:{...state(),status:mode==='aligned'?'localized':'unlocalized',reason:mode==='error'?'panorama_visual_localization_failed':undefined,geometry:currentGeometry,epoch:body.epoch,observation_sequence:body.sequence}});}
  if(path.endsWith('/intent')){controls.push(body);sequence=Math.max(sequence,body.sequence);mode='moving';return route.fulfill({json:state()});}
  if(path.endsWith('/stop')){controls.push({stop:body.sequence});sequence=body.sequence;mode='localizing';return route.fulfill({json:state()});}
  return route.fulfill({json:state()});
 });
 await page.goto('/');await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');
});
async function point(page,x=.5,y=.6){const box=await page.getByLabel('Panorama navegável',{exact:true}).boundingBox();return {x:box.x+box.width*x,y:box.y+box.height*y};}
async function click(page,x,y){const p=await point(page,x,y);await page.mouse.click(p.x,p.y);}

test('shared navigation and floating panel gestures do not issue PT or optical zoom',async({page})=>{
 const viewport=page.getByLabel('Panorama navegável',{exact:true}),initial=await viewport.getAttribute('data-viewport-zoom');
 const p=await point(page,.7,.65);await page.mouse.move(p.x,p.y);await page.mouse.wheel(0,-200);
 await expect.poll(()=>viewport.getAttribute('data-viewport-zoom')).not.toBe(initial);
 await page.mouse.down();await page.mouse.move(p.x+80,p.y+35,{steps:8});await page.mouse.up();
 const handle=page.getByLabel(/Mover Teleobjetiva/);await handle.focus();await page.keyboard.press('ArrowRight');
 const resize=page.getByRole('slider',{name:/Redimensionar Teleobjetiva/});await resize.focus();await page.keyboard.press('ArrowRight');await expect(resize).toHaveAttribute('aria-valuenow','340');
 await page.getByRole('button',{name:'Minimizar teleobjetiva'}).click();await expect(page.getByRole('button',{name:'Restaurar teleobjetiva'})).toBeVisible();
 await page.getByRole('button',{name:'Restaurar teleobjetiva'}).click();
 await expect(page.getByRole('button',{name:'Área útil',exact:true})).toHaveCount(0);
 await expect(page.getByRole('button',{name:'Integral',exact:true})).toHaveCount(0);
 expect(controls).toEqual([]);expect(observation).toBeGreaterThan(0);
});

test('retarget keeps the same decoder alive, drops projection, then requalifies',async({page})=>{
 await page.locator('video[data-source="wide"]').evaluate(video=>video.dataset.identity='original');
 const time=await page.locator('video[data-source="wide"]').evaluate(video=>video.currentTime);
 await click(page,.55,.65);await expect(page.getByRole('status')).toHaveText('Movendo');
 const preview=page.getByLabel('Vídeo atual · Movendo',{exact:true});await expect(preview).toHaveCSS('opacity','1');
 await click(page,.65,.65);await click(page,.7,.6);expect(controls.map(x=>x.sequence)).toEqual([1,2,3]);
 await expect.poll(()=>page.locator('video[data-source="wide"]').evaluate(video=>video.currentTime)).toBeGreaterThan(time+.3);
 await expect(page.locator('video[data-source="wide"]')).toHaveAttribute('data-identity','original');
 mode='aligned';await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');
 await page.screenshot({path:'.toposync-data/live-panorama-validation/synthetic-aligned.png'});
 await expect(page.getByRole('button',{name:'Parar movimento',exact:true})).toHaveCount(0);
 await click(page,.55,.65);await page.getByRole('button',{name:'Parar movimento',exact:true}).click();expect(controls.at(-1)).toEqual({stop:5});
});

test('brief buffering preserves registration while terminal loss stops stale targets',async({page})=>{
 mode='error';await expect(page.getByRole('status')).toHaveText('Controle indisponível');
 await click(page,.6,.6);expect(controls).toEqual([]);
 mode='aligned';await page.getByRole('button',{name:'Reconectar',exact:true}).click();await expect.poll(()=>sessions).toBe(2);await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');
 await page.locator('video[data-source="wide"]').evaluate(video=>{video.dispatchEvent(new Event('waiting'));video.dispatchEvent(new Event('stalled'));});
 await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');
 await click(page,.6,.65);
 await page.locator('video[data-source="wide"]').evaluate(video=>video.dispatchEvent(new Event('stalled')));
 await expect.poll(()=>controls.filter(x=>x.stop).length).toBe(0);
 await page.locator('video[data-source="wide"]').evaluate(video=>video.dispatchEvent(new Event('emptied')));
 await expect.poll(()=>controls.filter(x=>x.stop).length).toBe(1);
 mode='aligned';await page.reload();await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');expect(sessions).toBe(3);
});

test('latest click waits for a fresh registration and is then sent once',async({page})=>{
 mode='localizing';
 await expect(page.getByRole('status')).toHaveText('Localizando');
 await click(page,.55,.62);await click(page,.68,.64);
 await expect(page.getByRole('alert')).toContainText('Destino aguardando');
 expect(controls).toEqual([]);
 mode='aligned';
 await expect.poll(()=>controls.length).toBe(1);
 expect(controls[0].sequence).toBe(1);
 expect(controls[0].x).toBeGreaterThan(.5);
 await expect(page.getByRole('status')).toHaveText('Movendo');
});

test('existing photograph uses the real spherical model and coverage in the renderer',async({page})=>{
 test.skip(!process.env.TOPOSYNC_PHOTOGRAPH_FIXTURE,'Opt-in local photographs; never required or committed to the repository.');
 const fs=require('fs');const data=JSON.parse(fs.readFileSync(process.env.TOPOSYNC_PHOTOGRAPH_FIXTURE,'utf8'));
 currentArtifact=data.artifact;currentGeometry=data.geometry;
 await page.route('**/fixture-panorama.png',route=>route.fulfill({path:data.panorama}));
 await page.route('**/fixture-coverage.png',route=>route.fulfill({path:data.coverage}));
 await page.route('**/fixture-photograph.jpg',route=>route.fulfill({path:data.photograph}));
 await page.addInitScript(size=>window.__photograph=size,{width:data.geometry.lens.width,height:data.geometry.lens.height});
 await page.reload();await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');
 await page.screenshot({path:'.toposync-data/live-panorama-validation/photograph-projection.png'});
 expect(controls).toEqual([]);
});

test('reconnect refreshes a replaced panorama instead of silently retaining the old reference',async({page})=>{
 currentArtifact={...artifact,id:'b'.repeat(32),crop_revision:3};
 await page.getByRole('button',{name:'Reconectar',exact:true}).click();
 await expect(page.getByLabel('Câmera e panorama')).toHaveCount(0);
 await expect(page.getByRole('status')).toHaveText('Vídeo alinhado');
 expect(controls).toEqual([]);
});
