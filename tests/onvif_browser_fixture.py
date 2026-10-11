"""Real configuration and recipe API with bounded, synthetic ONVIF responses."""
import argparse
import copy
from contextlib import asynccontextmanager
from pathlib import Path
import time

import uvicorn

from source_panorama_browser_fixture import make_app, CAMERA_ID, EXTENSION
from toposync_ext_cameras.onvif.recipe_optimization import RECIPE_OPTIMIZATION

TOPIC = "RuleEngine/CellMotionDetector/Motion"
QUALIFIED = "{http://www.onvif.org/ver10/topics}" + TOPIC
STATE = {"topic": TOPIC, "qualified_topic": QUALIFIED, "item_name": "IsMotion", "label": "Motion",
         "known": True, "available": True, "value": False, "source": {"VideoSourceToken": "synthetic-one"}, "key": {}}


def create_fixture(data_dir):
    app = make_app(data_dir)
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            settings = await application.state.config_store.get_settings()
            devices = copy.deepcopy(settings.extensions[EXTENSION]["devices"])
            devices[0]["sources"][0]["origin"].update({"type": "onvif_profile", "profile_token": "synthetic-profile"})
            await application.state.config_store.update_extension_settings(EXTENSION, lambda current: {**current, "devices": devices})
            async def inspect(*args, **kwargs):
                now = time.time()
                return {"camera_id": CAMERA_ID, "queried_at": now, "valid_until": now + 300,
                    "status": "partial", "identification": {"manufacturer": "Synthetic", "model": "Fixture", "firmware_version": "1"},
                    "sections": {"services": "unknown"}, "services": [{"namespace": "urn:synthetic:relay", "status": "announced", "integrated": False}],
                    "profiles": [{"token": "synthetic-profile", "name": "Synthetic wide stream", "encoding": "H264", "width": 640, "height": 320,
                        "fps": 5, "audio_encoding": "AAC", "video_source_token": "synthetic-one", "video_source_configuration_token": "configuration-one", "ptz": None}],
                    "events": [{**STATE, "is_boolean": True, "is_property": True}], "observed_states": [STATE],
                    "observation": "connected", "recipe_optimization": RECIPE_OPTIMIZATION}
            async def snapshot(**kwargs):
                return STATE
            async def recent(**kwargs):
                return {"camera_id": CAMERA_ID, "available": True, "gap": False, "last_sequence": 1,
                    "events": [] if kwargs.get("after_sequence", 0) else [{"sequence": 1, "topic": TOPIC, "qualified_topic": QUALIFIED,
                        "operation": "Initialized", "source": STATE["source"], "key": {}, "data": {"IsMotion": "false"}, "received_at_ts": time.time()}]}
            async def descriptors(*args, **kwargs):
                return {"events": [], "observed_states": [STATE]}
            application.state.camera_onvif_capabilities.inspect = inspect
            application.state.camera_onvif_event_manager.snapshot = snapshot
            application.state.camera_onvif_event_manager.recent_events = recent
            application.state.camera_onvif_event_manager.list_descriptors = descriptors
            import toposync_ext_cameras.plugin as module
            original_ready = module._ensure_camera_preset_detection_model_ready
            async def ready(*args, **kwargs):
                return None
            module._ensure_camera_preset_detection_model_ready = ready
            try:
                yield
            finally:
                module._ensure_camera_preset_detection_model_ready = original_ready
    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8110)
    arguments = parser.parse_args()
    uvicorn.run(create_fixture(arguments.data_dir), host="127.0.0.1", port=arguments.port, log_level="warning")
