"""Minimal ONVIF helpers (SOAP + WS-Security UsernameToken + WS-Discovery) used by the Cameras extension.

This intentionally avoids external dependencies to keep the extension lightweight.
"""

from .client import (
    OnvifClient,
    OnvifError,
    OnvifProfile,
    OnvifPtzPreset,
    OnvifPtzStatus,
    normalize_onvif_xaddr,
    normalize_rtsp_url,
)
from .discovery import (
    OnvifDiscoveredDevice,
    OnvifDiscoveryTarget,
    discover_onvif_devices,
    parse_ws_discovery_probe_matches,
    resolve_onvif_discovery_targets,
)

__all__ = [
    "OnvifClient",
    "OnvifError",
    "OnvifProfile",
    "OnvifPtzPreset",
    "OnvifPtzStatus",
    "OnvifDiscoveredDevice",
    "OnvifDiscoveryTarget",
    "discover_onvif_devices",
    "parse_ws_discovery_probe_matches",
    "resolve_onvif_discovery_targets",
    "normalize_onvif_xaddr",
    "normalize_rtsp_url",
]
