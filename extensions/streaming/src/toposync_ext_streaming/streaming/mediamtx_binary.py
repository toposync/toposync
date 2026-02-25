from __future__ import annotations

import os
import shutil
import stat
from importlib import resources
from pathlib import Path
from typing import Final

from . import MEDIAMTX_VERSION
from .platform import MediaMTXPlatform


PACKAGE_NAME: Final[str] = "toposync_ext_streaming"


def packaged_mediamtx_binary(platform: MediaMTXPlatform) -> resources.abc.Traversable:
    root = resources.files(PACKAGE_NAME)
    path = root.joinpath("bin", "mediamtx", platform.key, platform.exe_name)
    if not path.is_file():
        raise FileNotFoundError(f"Packaged MediaMTX binary not found: {platform.key}/{platform.exe_name}")
    return path


def extract_mediamtx_binary(
    *,
    data_dir: Path,
    platform: MediaMTXPlatform,
    version: str = MEDIAMTX_VERSION,
) -> Path:
    """Extrai o binário embarcado para um diretório gravável.

    Comentário: arquivos do pacote podem ser read-only; copiar para data_dir permite chmod e execução.
    """
    runtime_dir = data_dir / "runtime" / "streaming" / "mediamtx" / version / platform.key
    runtime_dir.mkdir(parents=True, exist_ok=True)

    source = packaged_mediamtx_binary(platform)
    target = runtime_dir / platform.exe_name

    # Atualiza apenas quando necessário (evita I/O e evita perder permissões customizadas).
    needs_copy = True
    if target.is_file():
        try:
            needs_copy = source.stat().st_size != target.stat().st_size
        except Exception:
            needs_copy = True

    if needs_copy:
        temp = runtime_dir / f".{platform.exe_name}.tmp"
        if temp.exists():
            try:
                temp.unlink()
            except Exception:
                pass
        with source.open("rb") as reader, temp.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
        os.replace(temp, target)

    if platform.os != "windows":
        # Garante permissão de execução no Unix.
        try:
            st_mode = target.stat().st_mode
            os.chmod(target, st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            # Falhar aqui não deve derrubar a API; o start do engine reporta o erro.
            pass

    return target

