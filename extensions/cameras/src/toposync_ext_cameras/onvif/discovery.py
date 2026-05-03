from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import uuid
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass


MULTICAST_HOST = "239.255.255.250"
MULTICAST_PORT = 3702
LIMITED_BROADCAST_HOST = "255.255.255.255"


@dataclass(frozen=True, slots=True)
class OnvifDiscoveredDevice:
    device_id: str
    xaddrs: list[str]
    scopes: list[str]
    source_ip: str = ""
    name: str = ""
    hardware: str = ""

    @property
    def xaddr(self) -> str:
        return self.xaddrs[0] if self.xaddrs else ""


@dataclass(frozen=True, slots=True)
class OnvifDiscoveryTarget:
    host: str
    port: int = MULTICAST_PORT
    source: str = "configured"

    @property
    def label(self) -> str:
        if self.port == MULTICAST_PORT:
            return self.host
        return f"{self.host}:{self.port}"


def _safe_text(value: str | None) -> str:
    return str(value or "").strip()


def _split_ws(value: str) -> list[str]:
    return [item for item in str(value or "").split() if item]


def _parse_onvif_scopes(scopes: list[str]) -> tuple[str, str]:
    name = ""
    hardware = ""
    for scope in scopes:
        s = str(scope or "").strip()
        if not s:
            continue
        if s.startswith("onvif://www.onvif.org/name/"):
            name = urllib.parse.unquote(s.split("/name/", 1)[1]).strip()
            continue
        if s.startswith("onvif://www.onvif.org/hardware/"):
            hardware = urllib.parse.unquote(s.split("/hardware/", 1)[1]).strip()
            continue
    return name, hardware


def _env_bool(name: str, *, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _append_target(
    out: list[OnvifDiscoveryTarget],
    seen: set[tuple[str, int]],
    host: str,
    *,
    port: int = MULTICAST_PORT,
    source: str,
) -> None:
    normalized_host = str(host or "").strip()
    if not normalized_host:
        return
    try:
        normalized_port = int(port)
    except Exception:
        normalized_port = MULTICAST_PORT
    if normalized_port <= 0 or normalized_port > 65535:
        normalized_port = MULTICAST_PORT
    key = (normalized_host, normalized_port)
    if key in seen:
        return
    seen.add(key)
    out.append(OnvifDiscoveryTarget(host=normalized_host, port=normalized_port, source=source))


def _parse_configured_targets(raw: str | None) -> list[OnvifDiscoveryTarget]:
    out: list[OnvifDiscoveryTarget] = []
    seen: set[tuple[str, int]] = set()
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        host = value
        port = MULTICAST_PORT
        if ":" in value and not value.startswith("["):
            maybe_host, maybe_port = value.rsplit(":", 1)
            try:
                port = int(maybe_port)
                host = maybe_host
            except ValueError:
                host = value
                port = MULTICAST_PORT
        _append_target(out, seen, host, port=port, source="env")
    return out


def _broadcast_from_cidr(raw: str) -> str:
    try:
        interface = ipaddress.ip_interface(str(raw or "").strip())
    except ValueError:
        return ""
    ip = interface.ip
    network = interface.network
    if ip.version != 4:
        return ""
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return ""
    if network.prefixlen >= 31:
        return ""
    broadcast = network.broadcast_address
    if broadcast.is_unspecified or broadcast.is_loopback or broadcast.is_multicast:
        return ""
    return str(broadcast)


def _iter_network_ip_addresses(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_iter_network_ip_addresses(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("ip_address", "address", "addresses"):
            if key in value:
                out.extend(_iter_network_ip_addresses(value.get(key)))
        ipv4 = value.get("ipv4")
        if isinstance(ipv4, dict):
            out.extend(_iter_network_ip_addresses(ipv4))
        return out
    return []


def _supervisor_network_info() -> dict[str, object]:
    token = str(os.getenv("SUPERVISOR_TOKEN") or "").strip()
    if not token:
        return {}
    supervisor_url = str(os.getenv("SUPERVISOR") or "http://supervisor").strip().rstrip("/")
    if not supervisor_url:
        supervisor_url = "http://supervisor"
    req = urllib.request.Request(
        f"{supervisor_url}/network/info",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=2.0) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _supervisor_broadcast_targets() -> tuple[list[OnvifDiscoveryTarget], list[str]]:
    if not _env_bool("TOPOSYNC_ONVIF_DISCOVERY_SUPERVISOR_NETWORK", default=True):
        return [], []
    if not os.getenv("SUPERVISOR_TOKEN"):
        return [], []

    warnings: list[str] = []
    try:
        info = _supervisor_network_info()
    except Exception as exc:
        return [], [f"Could not read Home Assistant Supervisor network info: {exc}"]

    interfaces_raw = info.get("interfaces")
    if isinstance(interfaces_raw, dict):
        interfaces = list(interfaces_raw.values())
    elif isinstance(interfaces_raw, list):
        interfaces = interfaces_raw
    else:
        interfaces = []

    out: list[OnvifDiscoveryTarget] = []
    seen: set[tuple[str, int]] = set()
    for item in interfaces:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False or item.get("connected") is False:
            continue
        for address in _iter_network_ip_addresses(item):
            broadcast = _broadcast_from_cidr(address)
            if broadcast:
                _append_target(out, seen, broadcast, source="home-assistant")

    if not out:
        warnings.append("Home Assistant Supervisor did not report an IPv4 LAN broadcast address.")
    return out, warnings


def resolve_onvif_discovery_targets() -> tuple[list[OnvifDiscoveryTarget], list[str]]:
    targets: list[OnvifDiscoveryTarget] = []
    seen: set[tuple[str, int]] = set()
    warnings: list[str] = []

    if _env_bool("TOPOSYNC_ONVIF_DISCOVERY_MULTICAST", default=True):
        _append_target(targets, seen, MULTICAST_HOST, source="multicast")
    if _env_bool("TOPOSYNC_ONVIF_DISCOVERY_LIMITED_BROADCAST", default=True):
        _append_target(targets, seen, LIMITED_BROADCAST_HOST, source="broadcast")

    supervisor_targets, supervisor_warnings = _supervisor_broadcast_targets()
    for target in supervisor_targets:
        _append_target(targets, seen, target.host, port=target.port, source=target.source)
    warnings.extend(supervisor_warnings)

    for target in _parse_configured_targets(os.getenv("TOPOSYNC_ONVIF_DISCOVERY_TARGETS")):
        _append_target(targets, seen, target.host, port=target.port, source=target.source)

    if not targets:
        warnings.append("No ONVIF discovery targets are configured.")
    return targets, warnings


def parse_ws_discovery_probe_matches(payload: bytes, *, source_ip: str = "") -> list[OnvifDiscoveredDevice]:
    """Parse a WS-Discovery ProbeMatches SOAP response.

    This intentionally uses wildcard namespaces to work across vendor variations.
    """
    try:
        root = ET.fromstring(payload)
    except Exception:
        return []

    out: list[OnvifDiscoveredDevice] = []
    for match in root.findall(".//{*}ProbeMatch"):
        device_id = _safe_text(match.findtext(".//{*}EndpointReference/{*}Address")) or _safe_text(
            match.findtext(".//{*}Address")
        )
        xaddrs_raw = _safe_text(match.findtext(".//{*}XAddrs"))
        scopes_raw = _safe_text(match.findtext(".//{*}Scopes"))

        xaddrs = [item for item in _split_ws(xaddrs_raw) if item.startswith(("http://", "https://"))]
        scopes = _split_ws(scopes_raw)
        name, hardware = _parse_onvif_scopes(scopes)

        if not device_id and xaddrs:
            device_id = xaddrs[0]
        if not device_id:
            continue

        out.append(
            OnvifDiscoveredDevice(
                device_id=device_id,
                xaddrs=xaddrs,
                scopes=scopes,
                source_ip=str(source_ip or "").strip(),
                name=name,
                hardware=hardware,
            )
        )
    return out


def discover_onvif_devices(
    *,
    timeout_s: float = 1.2,
    attempts: int = 2,
    max_results: int = 64,
    targets: list[OnvifDiscoveryTarget] | None = None,
) -> list[OnvifDiscoveredDevice]:
    """Broadcast WS-Discovery probe and collect ONVIF devices.

    Responses are typically unicast back to the source port of the probe.
    """
    timeout_s = max(0.2, float(timeout_s))
    attempts = max(1, min(6, int(attempts)))
    max_results = max(1, min(512, int(max_results)))
    if targets is None:
        targets, _ = resolve_onvif_discovery_targets()
    targets = list(targets or [])
    if not targets:
        return []

    message_id = f"uuid:{uuid.uuid4()}"
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "<e:Header>"
        f"<w:MessageID>{message_id}</w:MessageID>"
        "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header>"
        "<e:Body>"
        "<d:Probe>"
        "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe>"
        "</e:Body>"
        "</e:Envelope>"
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.18)
        sock.bind(("", 0))

        for _ in range(attempts):
            for target in targets:
                try:
                    sock.sendto(probe, (target.host, target.port))
                except Exception:
                    pass

        results_by_key: dict[str, OnvifDiscoveredDevice] = {}
        deadline = time.time() + timeout_s
        while time.time() < deadline and len(results_by_key) < max_results:
            try:
                data, addr = sock.recvfrom(1024 * 64)
            except socket.timeout:
                continue
            except Exception:
                break

            ip = addr[0] if isinstance(addr, tuple) and addr else ""
            for item in parse_ws_discovery_probe_matches(data, source_ip=ip):
                key = item.device_id or item.xaddr or item.source_ip
                if not key:
                    continue
                existing = results_by_key.get(key)
                if existing is None:
                    results_by_key[key] = item
                    continue

                # Best-effort merge: keep richer fields and union addresses.
                merged_xaddrs = list(dict.fromkeys([*existing.xaddrs, *item.xaddrs]))
                merged_scopes = list(dict.fromkeys([*existing.scopes, *item.scopes]))
                name = existing.name or item.name
                hardware = existing.hardware or item.hardware
                source_ip = existing.source_ip or item.source_ip
                results_by_key[key] = OnvifDiscoveredDevice(
                    device_id=existing.device_id or item.device_id,
                    xaddrs=merged_xaddrs,
                    scopes=merged_scopes,
                    source_ip=source_ip,
                    name=name,
                    hardware=hardware,
                )

        return list(results_by_key.values())
    finally:
        try:
            sock.close()
        except Exception:
            pass
