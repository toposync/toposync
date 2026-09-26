const assert=require('node:assert/strict');
const {test}=require('node:test');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript'),vm=require('node:vm');
const {execFileSync}=require('node:child_process');
const context={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/live/panoramaVideo.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);
const {panoramaVideoPixel,frameStillSharesRegisteredView}=context.exports;
const {sameRegisteredView,stationaryPhotoPair}=context.exports;
const repository=path.resolve(__dirname,'../../../..');
const probes=JSON.parse(execFileSync(path.join(repository,'.venv/bin/python'),['-c',`
import json, numpy as np, cv2
from toposync_ext_cameras.processing.panorama_mapping import image_pixel_to_ray,ray_to_panorama_pixel,_rotation_basis
lens=dict(width=1920,height=1080,fx=950,fy=940,cx=938,cy=530,distortion=[-.16,.03,.001,-.002,.002,.01,0,0])
result=[]
for yaw in [0,1,3.13,-3.13]:
 rotation=cv2.Rodrigues(np.array([.23,yaw,-.1]))[0]
 geometry=dict(lens=lens,panorama_to_camera=_rotation_basis(rotation).T.tolist())
 for x,y in [(20,20),(960,540),(1890,1040),(700,100),(1300,820)]:
  point=ray_to_panorama_pixel(image_pixel_to_ray(x,y,lens,rotation_matrix=rotation))
  result.append(dict(point=point,expected=[x,y],geometry=geometry))
print(json.dumps(result))
`],{cwd:repository,encoding:'utf8'}));
test('inverse projection matches optical Python roundtrip including Brown distortion and seam',()=>{
 for(const {point,expected,geometry} of probes){const actual=panoramaVideoPixel(...point,geometry);assert.ok(actual);assert.ok(Math.hypot(actual[0]-expected[0],actual[1]-expected[1])<1e-4);}
});
test('out of panorama and behind-camera rays are rejected',()=>{
 const geometry=probes[0].geometry;
 for(const point of [[-1,.5],[.5,2],[NaN,0]]) assert.equal(panoramaVideoPixel(...point,geometry),null);
 assert.equal(panoramaVideoPixel(0,.5,geometry),null);
});
function frame(width,height,pixel){
 const result=new Uint8ClampedArray(width*height*4);
 for(let y=0;y<height;y++)for(let x=0;x<width;x++){
  const offset=(y*width+x)*4,[red,green,blue]=pixel(x,y);
  result[offset]=red;result[offset+1]=green;result[offset+2]=blue;result[offset+3]=255;
 }
 return result;
}
test('registration drift veto tolerates local motion, exposure and compression noise',()=>{
 const width=100,height=60;
 const reference=frame(width,height,(x,y)=>[(x*37+y*17)%256,(x*11+y*43)%256,(x*29+y*7)%256]);
 const foreground=new Uint8ClampedArray(reference);
 for(let y=12;y<48;y++)for(let x=8;x<34;x++){
  const offset=(y*width+x)*4;foreground[offset]=245;foreground[offset+1]=170;foreground[offset+2]=25;
 }
 assert.equal(frameStillSharesRegisteredView(reference,foreground,width,height),true);
 const exposure=Uint8ClampedArray.from(reference,(value,index)=>index%4===3?255:Math.min(255,value+15));
 assert.equal(frameStillSharesRegisteredView(reference,exposure,width,height),true);
 const noise=Uint8ClampedArray.from(reference,(value,index)=>index%4===3?255:Math.max(0,Math.min(255,value+(index%7)-3)));
 assert.equal(frameStillSharesRegisteredView(reference,noise,width,height),true);
});
test('registration drift veto rejects a global camera view change',()=>{
 const width=100,height=60;
 const reference=frame(width,height,(x,y)=>[(x*37+y*17)%256,(x*11+y*43)%256,(x*29+y*7)%256]);
 const shifted=frame(width,height,(x,y)=>[((x+13)*37+y*17)%256,((x+13)*11+y*43)%256,((x+13)*29+y*7)%256]);
 assert.equal(frameStillSharesRegisteredView(reference,shifted,width,height),false);
 assert.equal(frameStillSharesRegisteredView(reference,reference,width-1,height),false);
});

test('session photograph requires advancing independent observations with stable optical geometry',()=>{
 const geometry=probes[0].geometry;
 const first={geometry,epoch:'stream-a',sequence:10,mediaTime:4,receivedAt:1000};
 const second={...first,sequence:30,mediaTime:4.8,receivedAt:1800};
 assert.equal(stationaryPhotoPair(first,second),true);
 assert.equal(stationaryPhotoPair(null,second),false);
 for(const changes of [{epoch:'stream-b'},{sequence:10},{sequence:9},{sequence:NaN},{mediaTime:4},{mediaTime:8},{receivedAt:999},{receivedAt:5000}])
   assert.equal(stationaryPhotoPair(first,{...second,...changes}),false,JSON.stringify(changes));
 assert.equal(stationaryPhotoPair(first,{...second,geometry:probes[5].geometry}),false);
 assert.equal(sameRegisteredView(geometry,{...geometry,lens:{...geometry.lens,fx:geometry.lens.fx*2}}),false);
 assert.equal(sameRegisteredView(geometry,{...geometry,panorama_to_camera:[[1,0,0]]}),false);
 assert.equal(sameRegisteredView(geometry,{...geometry,panorama_to_camera:[[NaN,0,0],[0,1,0],[0,0,1]]}),false);
});
