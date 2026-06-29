from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


DirectorMode = Literal["no_demand", "idle", "event", "handoff", "fallback"]
EventLifecycle = Literal["open", "update", "close"]
EventPriority = Literal["silent", "low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class CameraCandidate:
    camera_id: str
    source_id: str = ""
    name: str = ""
    source_role: str = "auto"
    available: bool = True
    manual_priority: int = 0
    last_seen_at: float = 0.0


@dataclass(frozen=True, slots=True)
class EventCandidate:
    key: str
    source_kind: str
    priority: EventPriority
    lifecycle: EventLifecycle
    pipeline_name: str = ""
    notification_id: str = ""
    event_id: str = ""
    subject: dict[str, Any] = field(default_factory=dict)
    camera_id: str = ""
    source_id: str = ""
    area_label: str = ""
    confidence: float | None = None
    opened_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float | None = None


@dataclass(frozen=True, slots=True)
class CutPolicy:
    idle_dwell_seconds: float = 8.0
    event_min_seconds: float = 10.0
    cut_cooldown_seconds: float = 1.5
    close_hold_seconds: float = 3.0
    current_camera_sticky_seconds: float = 4.0
    max_event_hold_seconds: float = 60.0
    max_cuts_per_minute: int = 12

    @classmethod
    def from_config(cls, config: Any) -> "CutPolicy":
        return cls(
            idle_dwell_seconds=float(getattr(config, "idle_dwell_seconds", cls.idle_dwell_seconds)),
            event_min_seconds=float(getattr(config, "event_min_seconds", cls.event_min_seconds)),
            cut_cooldown_seconds=float(getattr(config, "cut_cooldown_seconds", cls.cut_cooldown_seconds)),
            close_hold_seconds=float(getattr(config, "close_hold_seconds", cls.close_hold_seconds)),
            current_camera_sticky_seconds=float(
                getattr(config, "current_camera_sticky_seconds", cls.current_camera_sticky_seconds)
            ),
            max_event_hold_seconds=float(getattr(config, "max_event_hold_seconds", cls.max_event_hold_seconds)),
            max_cuts_per_minute=int(getattr(config, "max_cuts_per_minute", cls.max_cuts_per_minute)),
        )


@dataclass(slots=True)
class DirectorState:
    demand_active: bool = False
    mode: DirectorMode = "no_demand"
    active_camera_id: str = ""
    active_source_id: str = ""
    pending_camera_id: str = ""
    active_event_key: str = ""
    shot_started_at: float = 0.0
    last_cut_at: float = 0.0
    hold_until: float = 0.0
    interruptible_after: float = 0.0
    camera_health_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_events_by_key: dict[str, EventCandidate] = field(default_factory=dict)
    last_seen_by_camera_id: dict[str, float] = field(default_factory=dict)
    recent_cut_timestamps: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ShotDecision:
    camera_id: str
    source_id: str = ""
    mode: DirectorMode = "idle"
    reason: str = ""
    event_key: str = ""
    score: float = 0.0
    hold_until: float = 0.0
    interruptible_after: float = 0.0
    framing_hint: dict[str, Any] = field(default_factory=dict)
