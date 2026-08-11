from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXTENSION_ROOT = Path(__file__).resolve().parents[1]
for path in (REPOSITORY_ROOT / "src", EXTENSION_ROOT / "src"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def pytest_configure(config) -> None:  # noqa: ANN001
    config.addinivalue_line("markers", "asyncio: run an async test with the stdlib event loop")


def pytest_pyfunc_call(pyfuncitem) -> bool | None:  # noqa: ANN001
    test_function = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_function):
        return None
    arguments = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(test_function(**arguments))
    return True
