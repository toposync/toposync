from __future__ import annotations

from toposync.runtime.pipelines.distributed.processing_server import create_processing_app


def create_app():
    return create_processing_app()

