"""Read-only completion of E2: obtain canonical ONVIF stream URI availability.

The URI itself is never written or printed. This distinguishes a working
ONVIF Media endpoint from a separately configured RTSP relay.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from toposync_ext_cameras.onvif.client import OnvifClient, OnvifError


DIRECTORY = Path(__file__).resolve().parent
CAMERA_ID = "camera_3_177980"


def _safe(error: Exception) -> str:
    return re.sub(r"(?:https?|rtsp)://\S+", "[endpoint]", str(error))[:200]


async def _run() -> dict:
    settings = json.loads(Path('.toposync-data/config.json').read_text())
    devices = settings['settings']['extensions']['com.toposync.cameras']['devices']
    camera = next(device for device in devices if device.get('id') == CAMERA_ID)
    onvif = camera['onvif']
    client = OnvifClient(
        xaddr=str(onvif.get('xaddr') or ''),
        username=str(onvif.get('username') or ''),
        password=str(onvif.get('password') or ''),
        timeout_s=5.0,
    )
    try:
        profiles = await client.get_profiles(str(onvif.get('media_xaddr') or ''))
    except OnvifError as error:
        return {'get_profiles_completed': False, 'error': _safe(error), 'profiles': []}
    streams = []
    for profile in profiles:
        try:
            uri = await client.get_stream_uri(str(onvif.get('media_xaddr') or ''), profile_token=profile.token)
            parsed = urlsplit(uri)
            streams.append({
                'profile_token': profile.token,
                'stream_uri_received': bool(uri),
                'scheme': parsed.scheme if uri else '',
                'has_authority': bool(parsed.netloc) if uri else False,
            })
        except OnvifError as error:
            streams.append({'profile_token': profile.token, 'stream_uri_received': False, 'error': _safe(error)})
    return {'get_profiles_completed': True, 'profiles': streams}


def main() -> None:
    report = {
        'experiment_id': 'E2',
        'attempt': 'onvif_get_stream_uri_metadata',
        'camera': {'id': CAMERA_ID, 'label': 'Garagem'},
        'ptz_commands_issued': 0,
        'onvif_media': asyncio.run(_run()),
        'persisted_uri_values': False,
    }
    (DIRECTORY / 'report-onvif-stream-uri.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'onvif_media': report['onvif_media'], 'ptz_commands_issued': 0}, sort_keys=True))


if __name__ == '__main__':
    main()
