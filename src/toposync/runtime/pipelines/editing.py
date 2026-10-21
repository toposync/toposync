from __future__ import annotations

import hashlib
import json

from pydantic import Field

from ..config_store import Pipeline


def pipeline_revision(pipeline: Pipeline) -> str:
    content = json.dumps(pipeline.model_dump(mode="json", exclude={"revision"}),
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


class PipelineEditRequest(Pipeline):
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class PipelineRead(Pipeline):
    revision: str

    @classmethod
    def from_pipeline(cls, pipeline: Pipeline) -> PipelineRead:
        return cls(**pipeline.model_dump(), revision=pipeline_revision(pipeline))
