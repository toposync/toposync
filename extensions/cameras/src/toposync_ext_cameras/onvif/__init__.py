"""Minimal ONVIF helpers (SOAP + WS-Security UsernameToken + WS-Discovery) used by the Cameras extension.

This intentionally avoids external dependencies to keep the extension lightweight.
"""

from .client import (
    OnvifAmbiguousMutationError,
    OnvifClient,
    OnvifError,
    OnvifProfile,
    OnvifPtzPreset,
    OnvifPtzStatus,
    normalize_onvif_xaddr,
    normalize_rtsp_url,
    onvif_xaddr_candidates,
)
from .reolink_cgi import (
    REOLINK_PRESET_TOKEN_PREFIX,
    ReolinkCgiClient,
    ReolinkCgiError,
    ReolinkCgiPreset,
    parse_reolink_preset_token,
)
from .discovery import (
    OnvifDiscoveredDevice,
    OnvifDiscoveryTarget,
    discover_onvif_devices,
    parse_ws_discovery_probe_matches,
    resolve_onvif_discovery_targets,
)
from .events import (
    OnvifCameraEventContext,
    OnvifEventDescriptor,
    OnvifEventItemDescription,
    OnvifEventMessage,
    OnvifEventStateManager,
    OnvifEventsClient,
    OnvifPullPointSubscription,
    OnvifService,
    humanize_onvif_event_label,
    parse_get_event_properties,
    parse_onvif_services,
    parse_pull_messages,
)

__all__ = [
    "OnvifAmbiguousMutationError",
    "OnvifClient",
    "OnvifError",
    "OnvifProfile",
    "OnvifPtzPreset",
    "OnvifPtzStatus",
    "REOLINK_PRESET_TOKEN_PREFIX",
    "ReolinkCgiClient",
    "ReolinkCgiError",
    "ReolinkCgiPreset",
    "OnvifService",
    "OnvifEventItemDescription",
    "OnvifEventDescriptor",
    "OnvifEventMessage",
    "OnvifCameraEventContext",
    "OnvifEventsClient",
    "OnvifEventStateManager",
    "OnvifPullPointSubscription",
    "OnvifDiscoveredDevice",
    "OnvifDiscoveryTarget",
    "humanize_onvif_event_label",
    "parse_onvif_services",
    "parse_get_event_properties",
    "parse_pull_messages",
    "discover_onvif_devices",
    "parse_ws_discovery_probe_matches",
    "resolve_onvif_discovery_targets",
    "normalize_onvif_xaddr",
    "onvif_xaddr_candidates",
    "normalize_rtsp_url",
    "parse_reolink_preset_token",
]
