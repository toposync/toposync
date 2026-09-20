import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { StreamTilePlayer } from '../../../frontend/src/ui/streams/StreamsDashboard';
function Fixture() {
 const [token,setToken]=useState('first'),[active,setActive]=useState(true),[source,setSource]=useState('main');
 const noop=()=>{};
 return <><button onClick={()=>setToken('renewed')}>Renovar credencial</button><button onClick={()=>setActive(v=>!v)}>Alternar atividade</button>
 <button onClick={()=>setSource('other')}>Trocar fonte</button>
 <StreamTilePlayer controls={false} transmissionId="fixture" outputId="main" mseOutputId="main" hlsOutputId="main" webrtcOutputId={null} jsmpegOutputId={null} hlsQualityProfileId={null} label="Player real" overlayVisible={false} sourceHint={null} sourceHintTone="muted"
 mseUrl={`/fixture-mse?source=${source}&media_token=${token}`} hlsUrl={`/fixture-hls?media_token=${token}`} hlsNativeUrl={`/fixture-hls?media_token=${token}`} hlsAuthHeader={null} webrtcUrl={`/fixture-webrtc?media_token=${token}`} webrtcAuthHeader={null} jsmpegUrl={null} stillUrl={null}
 playbackPlan={{transmission_id:'fixture',client:'web',lease_seconds:30,heartbeat_interval_seconds:10,selected_transport:'mse',transports:[{transport:'mse',rank:1,available:true},{transport:'webrtc',rank:2,available:false}]}}
 active={active} ptzEnabled={false} lowLatencyRequested={true} qualityPreference="auto" transportPreference="auto" variantOptions={[]} variantOverrideId="" currentVariantLabel="Principal" canSetVariantDefault={false} savingVariantDefault={false}
 onQualityPreferenceChange={noop} onTransportPreferenceChange={noop} onVariantOverrideChange={noop} onSetVariantDefault={async()=>{}} onRefreshUrls={async()=>{setToken('renewed')}} onOpenPtz={noop}/></>;
}
createRoot(document.getElementById('root')!).render(<Fixture/>);
