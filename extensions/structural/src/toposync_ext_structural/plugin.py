from __future__ import annotations

from toposync.extensions import BaseExtension


class StructuralExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_structural")

