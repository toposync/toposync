"""Whole-frame presence uses the same evidence clock and lifecycle as area presence."""

import math
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal

from pydantic import Field

from ..area_events import AreaOccurrenceRuntime, OccurrenceConfig


class FrameOccurrenceConfig(OccurrenceConfig):
    mode: Literal["presence"] = "presence"
    categories: list[str] = Field(default_factory=lambda: ["person"])


class FrameOccurrenceRuntime(AreaOccurrenceRuntime):
    def __init__(self, config, dependencies=None):
        super().__init__({}, dependencies)
        self.config = FrameOccurrenceConfig.model_validate(config)
        self.area = SimpleNamespace(id=None, name="frame")

    async def _refresh_area(self, context):
        return []

    def _classify(self, packet, subject):
        box = subject.get("bbox01")
        if (
            subject.get("category") not in self.config.categories
            or not isinstance(box, (tuple, list))
            or len(box) != 4
        ):
            return None
        if not all(type(value) in (int, float) and math.isfinite(value) for value in box):
            return None
        left, top, right, bottom = box
        if left >= right or top >= bottom:
            return None
        return right > 0 and left < 1 and bottom > 0 and top < 1

    def _event(self, key, state, lifecycle, status):
        packet = super()._event(key, state, lifecycle, status)
        payload = dict(packet.payload)
        payload["frame_occurrence"] = payload.pop("area_occurrence")
        payload.pop("area_label", None)
        return replace(packet, payload=payload)


def register_frame_occurrence_operator(registry):
    registry.register_operator(
        operator_id="camera.frame_occurrence",
        description="Observed whole-frame presence; time without evidence never confirms duration.",
        config_model=FrameOccurrenceConfig,
        defaults=FrameOccurrenceConfig().model_dump(),
        inputs=[{"name": "in", "required": True}],
        outputs=[{"name": "out"}],
        requires_payload_keys=["subject"],
        produces_payload_keys=["subject", "frame_occurrence"],
        capabilities=["camera", "tracking"],
        state_kind="stateful_per_stream",
        ordering="strict",
        share_strategy="never",
        owner="com.toposync.cameras",
        runtime_factory=FrameOccurrenceRuntime,
    )
