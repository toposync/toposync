const assert=require('node:assert/strict');
const {test}=require('node:test');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript'),vm=require('node:vm');
const {execFileSync}=require('node:child_process');
const context={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/live/panoramaVideo.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);
const {panoramaVideoPixel}=context.exports;
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
