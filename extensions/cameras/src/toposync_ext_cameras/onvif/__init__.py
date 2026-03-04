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
from .discovery import OnvifDiscoveredDevice, discover_onvif_devices, parse_ws_discovery_probe_matches

__all__ = [
    "OnvifClient",
    "OnvifError",
    "OnvifProfile",
    "OnvifPtzPreset",
    "OnvifPtzStatus",
    "OnvifDiscoveredDevice",
    "discover_onvif_devices",
    "parse_ws_discovery_probe_matches",
    "normalize_onvif_xaddr",
    "normalize_rtsp_url",
]
