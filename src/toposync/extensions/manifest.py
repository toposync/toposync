from __future__ import annotations

from pydantic import BaseModel, Field


class FrontendManifest(BaseModel):
    kind: str = Field(default="module-federation")
    remote_entry: str
    scope: str
    module: str = Field(description="Exposed module to load, e.g. './activate'")


class ExtensionManifest(BaseModel):
    schema_version: int = Field(default=1, ge=1)

    id: str
    name: str
    version: str

    requires_core_version: str | None = None
    requires_extensions: list[str] = Field(default_factory=list)

    frontend: FrontendManifest | None = None
