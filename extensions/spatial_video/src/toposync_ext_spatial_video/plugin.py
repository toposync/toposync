from __future__ import annotations

from toposync.extensions import BaseExtension


class SpatialVideoExtension(BaseExtension):
    def __init__(self) -> None:
        super().__init__(package="toposync_ext_spatial_video")
