from __future__ import annotations

import socket
import time
import uuid
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass


MULTICAST_HOST = "239.255.255.250"
MULTICAST_PORT = 3702


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
) -> list[OnvifDiscoveredDevice]:
    """Broadcast WS-Discovery probe and collect ONVIF devices.

    Responses are typically unicast back to the source port of the probe.
    """
    timeout_s = max(0.2, float(timeout_s))
    attempts = max(1, min(6, int(attempts)))
    max_results = max(1, min(512, int(max_results)))

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
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.18)
        sock.bind(("", 0))

        for _ in range(attempts):
            try:
                sock.sendto(probe, (MULTICAST_HOST, MULTICAST_PORT))
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

