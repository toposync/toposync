from __future__ import annotations

from importlib import metadata as importlib_metadata

__all__ = ["__version__"]

try:
    __version__ = importlib_metadata.version("toposync-core")
except importlib_metadata.PackageNotFoundError:
    __version__ = "0.4.9"
