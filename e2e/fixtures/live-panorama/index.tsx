// Deterministic UI fixture: real shared viewport, renderer and presented-frame
// hook; synthetic video and control API. This never connects to a camera.
import React, {useEffect, useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {LivePanoramaView} from '../../../extensions/cameras/ui/src/live/LivePanoramaView';
import {NavigableViewport} from '../../../frontend/src/ui/NavigableViewport';
import {usePresentedFrame} from '../../../frontend/src/ui/streams/usePresentedFrame';
import type {LiveViewPlayerProps,ToposyncHost} from '@toposync/plugin-api';
const photograph=(window as any).__photograph as undefined|{width:number;height:number};
function FixturePlayer(props:LiveViewPlayerProps){
 const video=useRef<HTMLVideoElement>(null);
 usePresentedFrame(video,true,props.sourceId??'',value=>props.onFrame?.(value?{...value,cameraId:props.cameraId,sourceId:props.sourceId,opticalSourceSize:photograph??{width:640,height:360},contentRect:{x:0,y:0,width:1,height:1}}:null));
 useEffect(()=>{
  const canvas=document.createElement('canvas');canvas.width=photograph?1280:640;canvas.height=photograph?720:360;
  const context=canvas.getContext('2d')!;let count=0;const reference=new Image();if(photograph)reference.src='/fixture-photograph.jpg';
  const draw=()=>{if(photograph){if(reference.complete&&reference.naturalWidth)context.drawImage(reference,0,0,canvas.width,canvas.height);return;}context.fillStyle='#2675c9';context.fillRect(0,0,640,360);context.fillStyle='#df9431';context.fillRect(50,30,180,180);context.fillStyle='white';context.font='30px sans-serif';context.fillText('VÍDEO SINTÉTICO',240,100);context.fillText(String(++count),10,340);};
  draw();const stream=canvas.captureStream(15);video.current!.srcObject=stream;void video.current!.play();
  const interval=setInterval(draw,65);return()=>{clearInterval(interval);stream.getTracks().forEach(track=>track.stop());};
 },[]);
 return <video data-source={props.sourceId} ref={video} muted playsInline style={{width:'100%',height:'100%'}}/>;
}
const host={ui:{NavigableViewport,LiveViewPlayer:FixturePlayer}} as unknown as ToposyncHost;
createRoot(document.getElementById('root')!).render(<><LivePanoramaView host={host}/>{photograph&&<div style={{position:'absolute',top:12,left:20,background:'#783f00',padding:10}}>REPLAY DE FOTOGRAFIA — NÃO É VÍDEO AO VIVO</div>}</>);
