from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Go2RtcResolvedConfig:
    api_bind_host: str
    api_port: int
    streams: dict[str, str]


def _yaml_single_quote(value: str) -> str:
    text = str(value or "")
    return "'" + text.replace("'", "''") + "'"


def _yaml_inline_list(values: list[str]) -> str:
    return "[" + ", ".join(_yaml_single_quote(item) for item in values) + "]"


def render_go2rtc_config(config: Go2RtcResolvedConfig) -> str:
    streams = {
        str(key or "").strip(): str(value or "").strip()
        for key, value in dict(config.streams or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    listen = f"{str(config.api_bind_host or '127.0.0.1').strip()}:{int(config.api_port)}"

    lines: list[str] = []
    lines.append("app:")
    lines.append(f"  modules: {_yaml_inline_list(['api', 'streams', 'rtsp', 'mp4', 'ws'])}")
    lines.append("")
    lines.append("log:")
    lines.append("  level: info")
    lines.append("  format: text")
    lines.append("")
    lines.append("api:")
    lines.append(f"  listen: {_yaml_single_quote(listen)}")
    lines.append("  origin: '*'")
    lines.append("")
    lines.append("rtsp:")
    lines.append("  listen: ''")
    lines.append("")
    lines.append("webrtc:")
    lines.append("  listen: ''")
    lines.append("")
    lines.append("streams:")
    if not streams:
        lines.append("  {}")
    else:
        for name in sorted(streams):
            lines.append(f"  {name}: {_yaml_single_quote(streams[name])}")
    lines.append("")
    return "\n".join(lines)
