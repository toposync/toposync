from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MediaMTXResolvedPorts:
    rtsp: int
    hls: int
    api: int
    webrtc: int | None = None
    # Used only when RTSP over UDP is enabled.
    # MediaMTX requires RTP/RTCP ports to be consecutive.
    rtp: int = 8000
    rtcp: int = 8001


@dataclass(frozen=True, slots=True)
class MediaMTXPathAuth:
    path: str
    read_username: str | None = None
    read_password: str | None = None
    publish_username: str | None = None
    publish_password: str | None = None


def normalize_path_slug(value: str, *, fallback: str = "test") -> str:
    fallback_value = str(fallback)
    raw = str(value or "").strip().lower()
    if not raw:
        return fallback_value

    filtered = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "-" for ch in raw)
    cleaned = filtered.strip("-_")
    if cleaned:
        return cleaned
    return fallback_value


def _address(bind_host: str, port: int) -> str:
    host = (bind_host or "").strip()
    if host in {"0.0.0.0", "::", ""}:
        return f":{port}"
    return f"{host}:{port}"


def _yaml_single_quote(value: str) -> str:
    text = str(value or "")
    return "'" + text.replace("'", "''") + "'"


def _as_yaml_list(values: list[str]) -> str:
    normalized = [str(item or "").strip() for item in values if str(item or "").strip()]
    if not normalized:
        return "[]"
    return "[" + ", ".join(_yaml_single_quote(item) for item in normalized) + "]"


def render_mediamtx_config(
    *,
    bind_host: str,
    ports: MediaMTXResolvedPorts,
    paths: list[str],
    enable_webrtc: bool = False,
    webrtc_ice_servers: list[str] | None = None,
    path_auth: list[MediaMTXPathAuth] | None = None,
    api_allow_origins: list[str] | None = None,
    hls_allow_origins: list[str] | None = None,
    webrtc_allow_origins: list[str] | None = None,
    webrtc_local_udp_address: str = ":0",
    webrtc_local_tcp_address: str = "",
) -> str:
    """Generate a MediaMTX YAML config with per-path auth and internal publish credentials.

    We build YAML manually to avoid adding a PyYAML dependency.
    """
    unique_paths: list[str] = []
    seen_paths: set[str] = set()
    for path in paths:
        key = str(path or "").strip()
        if not key or key in seen_paths:
            continue
        seen_paths.add(key)
        unique_paths.append(key)

    normalized_auth_by_path: dict[str, MediaMTXPathAuth] = {}
    for item in path_auth or []:
        path_key = str(getattr(item, "path", "") or "").strip()
        if not path_key:
            continue
        normalized_auth_by_path[path_key] = MediaMTXPathAuth(
            path=path_key,
            read_username=str(item.read_username or "").strip() or None,
            read_password=str(item.read_password or "").strip() or None,
            publish_username=str(item.publish_username or "").strip() or None,
            publish_password=str(item.publish_password or "").strip() or None,
        )

    default_api_origins = list(api_allow_origins or ["*"])
    default_hls_origins = list(hls_allow_origins or ["*"])
    default_webrtc_origins = list(webrtc_allow_origins or ["*"])
    localhost_ips = ["127.0.0.1", "::1"]

    users_by_key: dict[tuple[str, str, tuple[str, ...]], list[tuple[str, str]]] = {}

    def add_permission(*, user: str, password: str, ips: list[str], action: str, path: str = "") -> None:
        key = (str(user), str(password), tuple(str(ip) for ip in ips))
        permissions = users_by_key.setdefault(key, [])
        permission = (str(action), str(path))
        if permission not in permissions:
            permissions.append(permission)

    # Restrict API/metrics/pprof to localhost only.
    add_permission(user="any", password="", ips=localhost_ips, action="api")
    add_permission(user="any", password="", ips=localhost_ips, action="metrics")
    add_permission(user="any", password="", ips=localhost_ips, action="pprof")

    for path in unique_paths:
        auth = normalized_auth_by_path.get(path)
        read_username = str(getattr(auth, "read_username", "") or "").strip()
        read_password = str(getattr(auth, "read_password", "") or "").strip()
        publish_username = str(getattr(auth, "publish_username", "") or "").strip()
        publish_password = str(getattr(auth, "publish_password", "") or "").strip()

        if read_username and read_password:
            add_permission(user=read_username, password=read_password, ips=[], action="read", path=path)
            add_permission(user=read_username, password=read_password, ips=[], action="playback", path=path)
        else:
            add_permission(user="any", password="", ips=[], action="read", path=path)
            add_permission(user="any", password="", ips=[], action="playback", path=path)

        if publish_username and publish_password:
            add_permission(user=publish_username, password=publish_password, ips=localhost_ips, action="publish", path=path)
        else:
            add_permission(user="any", password="", ips=localhost_ips, action="publish", path=path)

    lines: list[str] = []
    lines.append("logLevel: info")
    lines.append("logDestinations: [stdout]")
    lines.append("")
    lines.append("authMethod: internal")
    lines.append("authInternalUsers:")

    for key in sorted(users_by_key.keys()):
        user, password, ips = key
        permissions = users_by_key[key]
        lines.append(f"- user: {_yaml_single_quote(user)}")
        lines.append(f"  pass: {_yaml_single_quote(password)}")
        lines.append(f"  ips: {_as_yaml_list(list(ips))}")
        lines.append("  permissions:")
        for action, path in permissions:
            lines.append(f"  - action: {action}")
            lines.append(f"    path: {_yaml_single_quote(path)}")

    lines.append("")
    lines.append("api: true")
    lines.append(f"apiAddress: {_address(bind_host, ports.api)}")
    lines.append(f"apiAllowOrigins: {_as_yaml_list(default_api_origins)}")
    lines.append("")
    lines.append("metrics: false")
    lines.append("pprof: false")
    lines.append("")
    lines.append("rtsp: true")
    lines.append(f"rtspAddress: {_address(bind_host, ports.rtsp)}")
    rtp_port = int(getattr(ports, "rtp", 8000))
    rtcp_port = int(getattr(ports, "rtcp", rtp_port + 1))
    if rtcp_port != (rtp_port + 1):
        raise ValueError("RTP and RTCP ports must be consecutive")
    lines.append(f"rtpAddress: {_address(bind_host, rtp_port)}")
    lines.append(f"rtcpAddress: {_address(bind_host, rtcp_port)}")
    # Enabling UDP+TCP improves compatibility (ffplay/VLC default to UDP).
    # In more restricted networks, TCP tends to work better and can be forced by the client.
    lines.append("rtspTransports: [udp, tcp]")
    lines.append("")
    lines.append("rtmp: false")
    lines.append("srt: false")
    lines.append("playback: false")
    lines.append("")
    lines.append("hls: true")
    lines.append(f"hlsAddress: {_address(bind_host, ports.hls)}")
    lines.append(f"hlsAllowOrigins: {_as_yaml_list(default_hls_origins)}")
    # Avoid LL-HLS by default (Safari may require TLS).
    lines.append("hlsVariant: mpegts")
    lines.append("")
    lines.append(f"webrtc: {'true' if enable_webrtc else 'false'}")
    if enable_webrtc and ports.webrtc is not None:
        lines.append(f"webrtcAddress: {_address(bind_host, ports.webrtc)}")
        lines.append(f"webrtcAllowOrigins: {_as_yaml_list(default_webrtc_origins)}")
        lines.append(f"webrtcLocalUDPAddress: {_yaml_single_quote(str(webrtc_local_udp_address or ':0'))}")
        local_tcp = str(webrtc_local_tcp_address or "").strip()
        if local_tcp:
            lines.append(f"webrtcLocalTCPAddress: {_yaml_single_quote(local_tcp)}")
        normalized_ice_servers = [str(item or "").strip() for item in (webrtc_ice_servers or []) if str(item or "").strip()]
        if normalized_ice_servers:
            lines.append("webrtcICEServers2:")
            for item in normalized_ice_servers:
                lines.append(f"  - url: {_yaml_single_quote(item)}")
    lines.append("")
    lines.append("paths:")
    if not unique_paths:
        lines.append("  all_others: {}")
    else:
        for p in unique_paths:
            lines.append(f"  {p}: {{}}")
        lines.append("  all_others: {}")

    lines.append("")
    return "\n".join(lines)
