import asyncio
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import HTTPException

from toposync.runtime.pipelines.image_geometry import image_geometry
from toposync_ext_cameras.visual_regions import VisualRegionService
from toposync_ext_cameras.visual_region_models import VisualRegionConfig


def setup(tmp_path):
    root=tmp_path
    directory=root/'jobs'/'job'
    directory.mkdir(parents=True)
    image=np.random.default_rng(87).integers(0,256,(540,960,3),dtype=np.uint8)
    cv2.imwrite(str(directory/'source.png'),image)
    cv2.imwrite(str(root/'coverage.png'),np.full((540,960),255,np.uint8))
    evidence={"capture_instance":"decoder","generation":1,"sequence":1,"published_at":time.time()}
    binding={"id":"reference","revision":1,"width":960,"height":540,"geometry":{"projection":"image_plane","model_digest":"digest"}}
    count={"capture":0,"motion":0}
    async def context(*args):return {},{"id":"one"},{"id":"video"}
    async def capture(*args):
        count["capture"]+=1
        return {"image":image.copy(),"capture_evidence":evidence}
    def read(path):
        return {"camera_id":"one","source_id":"video"} if path.name=='job.json' else {"source_size":[960,540]}
    sources=SimpleNamespace(root=root,_context=context,_authorize=lambda *args,**kwargs:None,
        _pointer=lambda source:{"active":{"artifact_id":"reference","revision":1}},
        _artifact=lambda identifier:{"_job_id":"job"},_private_path=lambda root,path:root/path,
        _read=read,capture_frame=capture)
    panorama=SimpleNamespace(_source_artifact=lambda *args,**kwargs:(binding,{"model":root/'model.json',"coverage":root/'coverage.png'}))
    service=VisualRegionService(panorama,sources)
    config=VisualRegionConfig(camera_id="one",source_id="video",name="Nicho",
        reference={"artifact_id":"reference","revision":1,"geometry_digest":"digest"},
        strokes=[{"diameter01":.4,"points01":[[.5,.5]]}])
    return service,config,image,evidence,count


def test_real_matcher_and_preview_share_projection_without_actions(tmp_path):
    async def scenario():
        service,config,image,evidence,count=setup(tmp_path)
        result=await service.evaluate(config=config.model_dump(),image=image,evidence=evidence,
            geometry=image_geometry(960,540,evidence),detections=[{"bbox01":[.45,.4,.55,.55]}])
        assert result['observations'][0]['classification']=='inside'
        preview=await service.preview(object(),config)
        assert preview['classification']==result['classification']
        assert preview['visibility']==result['visibility']
        assert preview['detection_available'] is True and preview['image'].startswith('data:image/jpeg;base64,')
        assert count=={"capture":0,"motion":0}
        assert len(service.planar)==1
    asyncio.run(scenario())


def test_authorization_precedes_capture_and_expired_frames_are_unknown(tmp_path):
    async def scenario():
        service,config,image,evidence,count=setup(tmp_path)
        def deny(*args,**kwargs):raise HTTPException(status_code=403)
        service.sources._authorize=deny
        with pytest.raises(HTTPException):await service.preview(object(),config)
        assert count['capture']==0
        evidence['published_at']=time.time()-100
        result=await service.evaluate(config=config.model_dump(),image=image,evidence=evidence,
            geometry=image_geometry(960,540,evidence))
        assert result['reason']=='visual_region_frame_expired' and not service.planar
    asyncio.run(scenario())


def test_wrong_reference_and_source_geometry_do_not_match(tmp_path):
    async def scenario():
        service,config,image,evidence,count=setup(tmp_path)
        config.reference.geometry_digest='changed'
        with pytest.raises(HTTPException):await service.preview(object(),config)
        assert not count['capture']
        config.reference.geometry_digest='digest'
        geometry=image_geometry(960,540,evidence)
        geometry['capture_evidence']={**evidence,'sequence':99}
        result=await service.evaluate(config=config.model_dump(),image=image,evidence=evidence,geometry=geometry)
        assert result['classification']=='indeterminate'
    asyncio.run(scenario())


def test_single_image_recovers_small_displacement_without_pose_protocol(tmp_path):
    async def scenario():
        service,config,image,evidence,count=setup(tmp_path)
        shifted=cv2.warpAffine(image,np.float32([[1,0,22],[0,1,-12]]),(960,540))
        result=await service.evaluate(config=config.model_dump(),image=shifted,evidence=evidence,
            geometry=image_geometry(960,540,evidence),detections=[{"bbox01":[.45,.4,.55,.55]}])
        assert result['observations'][0]['classification']=='inside'
        y,x=np.nonzero(result['_mask'])
        assert x.mean()==pytest.approx(480+22,abs=2)
        assert y.mean()==pytest.approx(270-12,abs=2)
        assert count['capture']==0 and count['motion']==0
    asyncio.run(scenario())


def test_area_preview_uses_composition_identifier_and_refuses_replacement(tmp_path):
    from toposync_ext_cameras.processing.panorama_mapping import estimate_image_plane_mapping
    from toposync_ext_cameras.visual_regions import AreaPreview
    async def scenario():
        service,config,image,evidence,count=setup(tmp_path)
        binding,paths=await service.reference('one','video',config.reference)
        points=[{'id':str(index),'world_x':x,'world_z':z,'image_u':.2+.05*x,'image_v':.2+.05*z}
                for index,(x,z) in enumerate([(0,0),(8,0),(8,8),(0,8),(4,0),(0,4)])]
        job={'id':'mapping','revision':1,'source_panorama':binding,'solution':estimate_image_plane_mapping(points)}
        async def active(**kwargs):return {'job':job}
        async def current(*args):pass
        composition={'id':'composition','elements':[{'id':'area','name':'Nicho','type':'com.toposync.structural.area',
            'props':{'vertices':[{'x':2,'z':2},{'x':6,'z':2},{'x':6,'z':6},{'x':2,'z':6}]}}]}
        async def configuration():return SimpleNamespace(compositions=[composition])
        service.sources.store=SimpleNamespace(get_config=configuration)
        service.panorama.get_active=active
        service.panorama._current=current
        service.panorama.jobs={'mapping':job}
        body=AreaPreview(camera_id='one',source_id='video',composition_id='composition',area_ids=['area'])
        result=await service.preview_areas(object(),body)
        assert result['classification']=='observed' and result['visibility']['state']=='full'
        composition['elements'][0]['id']='different'
        with pytest.raises(HTTPException):await service.preview_areas(object(),body)
        assert count['capture']==1
    asyncio.run(scenario())


def test_framing_uses_existing_attention_arbitration_and_releases_without_capture(tmp_path,monkeypatch):
    async def scenario():
        service,config,image,evidence,count=setup(tmp_path)
        config.framing='maintain'
        config.native_tracking_disabled_confirmed=True
        config.strokes[0].diameter01=.03
        submitted=[]
        async def call(name,**payload):
            if name.endswith('submit'): submitted.append(payload); return {'ok':True}
            return {'devices':[]}
        service.panorama.services=SimpleNamespace(call=call)
        profile=SimpleNamespace(id='profile',ptz_device_id='motor')
        async def qualified(*args):return profile,{'solution':{}}
        service.framing_profile=qualified
        original_read=service.sources._read
        service.sources._read=lambda path: {'lens':{'width':960,'height':540,'fx':640,'fy':640,'cx':479.5,'cy':269.5,'distortion':[]}} if path.name=='model.json' else original_read(path)
        monkeypatch.setattr('toposync_ext_cameras.processing.panorama_mapping.map_ray_to_world',lambda *args:(1,2))
        kwargs=dict(config=config.model_dump(),owner='pipeline:node',pipeline_name='pipeline',node_id='node')
        assert (await service.framing(**kwargs))['state']=='requested'
        await service.framing(**kwargs)
        await service.framing(**kwargs,close=True)
        assert [item['lifecycle'] for item in submitted]==['open','update','close']
        assert all(item['hold_after_close_seconds']==0 for item in submitted)
        assert not service.framing_requests and count['capture']==0
        config.strokes[0].diameter01=.5
        assert (await service.framing(**{**kwargs,'config':config.model_dump()}))['reason']=='visual_region_cannot_fit_frame'
        assert len(submitted)==3
    asyncio.run(scenario())


def test_framing_rejects_changed_revision_and_ambiguous_profiles(tmp_path):
    async def scenario():
        service, config, *_ = setup(tmp_path)
        binding, _ = await service.reference('one', 'video', config.reference)
        profile = SimpleNamespace(camera_id='one', source_id='video', mode='live_panorama',
            operator_managed=False, follow_view_updates=False, same_head_observer_acknowledged=True,
            stale_timeout_seconds=12, composition_id='composition', panorama_element_id='element',
            event_policies=[SimpleNamespace(enabled=True, event_type='visual_region')])
        profiles = [profile]
        service.panorama.app = SimpleNamespace(state=SimpleNamespace(ptz_attention_store=SimpleNamespace(list_profiles=lambda: profiles)))
        job = {'source_panorama': binding.copy()}
        async def active(**kwargs): return {'job': {'id': 'job'}}
        service.panorama.get_active = active
        service.panorama.jobs = {'job': job}
        service.panorama._attention_qualified = lambda job: True
        assert await service.framing_profile('one', 'video', binding)
        job['source_panorama']['revision'] = 2
        assert await service.framing_profile('one', 'video', binding) is None
        job['source_panorama']['revision'] = 1
        profiles.append(profile)
        assert await service.framing_profile('one', 'video', binding) is None
        profiles.pop()
        profile.stale_timeout_seconds = 3600
        assert await service.framing_profile('one', 'video', binding) is None
    asyncio.run(scenario())


@pytest.mark.parametrize('region_count', [1, 4, 8])
def test_regions_share_frame_matching_and_bound_caches(tmp_path, monkeypatch, region_count):
    from toposync_ext_cameras.processing.planar_localization import PlanarLocalizer
    async def scenario():
        service, config, image, evidence, _ = setup(tmp_path)
        calls = []
        original = PlanarLocalizer._features
        def features(self, image):
            calls.append(1)
            return original(self, image)
        monkeypatch.setattr(PlanarLocalizer, '_features', features)
        started = time.perf_counter()
        for index in range(region_count):
            config.strokes[0].points01 = [(.45 + index * .01, .5)]
            result = await service.evaluate(config=config.model_dump(), image=image, evidence=evidence,
                geometry=image_geometry(960,540,evidence), detections=[{'bbox01':[.45,.4,.55,.55]}])
            assert result['classification'] == 'observed'
        assert len(calls) == 2  # One immutable reference and one shared current frame.
        assert len(service.planar) == 1 and len(service.masks) <= 4 and len(service.recent_decisions) <= 2
        print(f'Controlled synthetic workload: {region_count} regions, {time.perf_counter()-started:.3f}s, two feature extractions')
    asyncio.run(scenario())


def test_disabled_pipeline_reference_remains_protected():
    from toposync_ext_cameras.source_panorama import SourcePanoramaService
    pipeline = SimpleNamespace(enabled=False, graph={'nodes':[{'operator':'camera.panorama_region',
        'config': {'camera_id':'one','reference':{'artifact_id':'0123456789abcdef0123456789abcdef'}}}]})
    assert SourcePanoramaService._referenced_artifacts({}, 'one', [pipeline]) == {'0123456789abcdef0123456789abcdef'}
    assert SourcePanoramaService._referenced_artifacts({}, 'other', [pipeline]) == set()
