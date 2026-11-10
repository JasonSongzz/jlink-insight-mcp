"""Serialize hardware access and reserve monitoring sessions / 硬件互斥及会话独占."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

lock = threading.RLock()
_local = threading.local()
_lease: str | None = None


@contextmanager
def hardware_access(token: str | None = None) -> Iterator[None]:
    """Enter one atomic hardware transaction / 执行一个原子硬件事务."""
    with lock:
        if _lease is not None and token != _lease and getattr(_local, "token", None) != _lease:
            raise RuntimeError("Hardware reserved by runtime monitor; stop_runtime_monitor first")
        previous = getattr(_local, "token", None)
        _local.token = token or previous
        try:
            yield
        finally:
            _local.token = previous


def claim_hardware(token: str) -> None:
    """Reserve device until monitor ends / 监控结束前独占设备."""
    global _lease
    with lock:
        if _lease is not None:
            raise RuntimeError("A hardware monitoring session is already active")
        _lease = token


def release_hardware(token: str) -> None:
    """Release only the matching lease / 仅释放对应会话的独占权."""
    global _lease
    with lock:
        if _lease == token:
            _lease = None


def hardware_tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Protect a whole tool operation / 保护整个工具操作，阻止监控期间的复位等操作."""

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            with hardware_access():
                return func(*args, **kwargs)
        except RuntimeError as exc:
            return {"success": False, "error": {"description": str(exc)}}

    return wrapped
