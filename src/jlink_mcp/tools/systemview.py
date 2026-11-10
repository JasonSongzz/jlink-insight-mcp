"""SystemView MCP tools / SystemView MCP 工具.

Bounded foreground recording uses the existing J-Link connection, without halt
or reset. The normal RTT tools must be stopped before capturing.
"""

from __future__ import annotations

import base64
import time
from typing import Any

from ..jlink_manager import jlink_manager
from ..hardware_access import hardware_tool
from ..systemview import analyze_systemview
from . import rtt


def systemview_decode(data_base64: str, timestamp_hz: int | None = None, event_limit: int = 100) -> dict[str, Any]:
    """Decode raw RTT bytes (not a .SVDat container) / 解码原始 RTT 数据."""
    try:
        if len(data_base64) > 5592408:
            raise ValueError("Encoded trace exceeds 4 MiB")
        raw = base64.b64decode(data_base64, validate=True)
        return {"success": True, "data": analyze_systemview(raw, timestamp_hz, event_limit)}
    except (ValueError, TypeError) as exc:
        return {"success": False, "error": {"description": str(exc)}}


def _name(descriptor: Any) -> str:
    name = descriptor.acName
    return name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)


@hardware_tool
@rtt.exclusive_rtt
def systemview_capture(
    duration_ms: int = 1000,
    channel: int | None = None,
    block_address: int | None = None,
    max_bytes: int = 65536,
    event_limit: int = 100,
) -> dict[str, Any]:
    """Record a bounded single-core SystemView window / 限时采集单核 SystemView.

    Requires initialized firmware, a running target and exclusive RTT ownership.
    Returns raw Base64 plus analysis. START/STOP are requests, not acknowledgments.
    """
    started = False
    start_sent = False
    link = None
    down = None
    raw = bytearray()
    cleanup_errors = []
    result: dict[str, Any]
    try:
        if rtt._rtt_started:
            raise ValueError("Stop normal RTT before SystemView capture")
        if not 1 <= duration_ms <= 10000 or not 1024 <= max_bytes <= 262144:
            raise ValueError("duration_ms must be 1..10000; max_bytes must be 1024..262144")
        if not 0 <= event_limit <= 1000 or (channel is not None and channel < 0):
            raise ValueError("Invalid channel or event_limit")
        if block_address is not None and not 0 <= block_address <= 0xFFFFFFFF:
            raise ValueError("Invalid RTT control block address")
        link = jlink_manager.get_jlink()
        if link.halted():
            raise ValueError("Target is halted; resume it before runtime capture")
        link.rtt_start(block_address=block_address)
        started = True
        # RTT discovery is asynchronous in the J-Link DLL.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                ups = [link.rtt_get_buf_descriptor(i, True) for i in range(link.rtt_get_num_up_buffers())]
                downs = [link.rtt_get_buf_descriptor(i, False) for i in range(link.rtt_get_num_down_buffers())]
                matches = [d for d in ups if _name(d) == "SysView" and (channel is None or d.BufferIndex == channel)]
                if len(matches) != 1:
                    raise ValueError("Expected one SysView up channel; initialize firmware or select channel")
                up = matches[0].BufferIndex
                down_matches = [d.BufferIndex for d in downs if _name(d) == "SysView"]
                if len(down_matches) != 1:
                    raise ValueError("Expected one SysView down channel; multicore is not supported")
                down = down_matches[0]
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        # Discard stale recording bytes before asking for a new sync/start sequence.
        discarded = 0
        for _ in range(64):
            stale = link.rtt_read(up, 4096)
            discarded += len(stale)
            if not stale:
                break
        else:
            raise ValueError("SysView stream is already active; stop the other recorder first")
        # Set before write: a failing DLL call might still have delivered START.
        start_sent = True
        if link.rtt_write(down, [1]) != 1:
            raise ValueError("SystemView START command was not accepted by RTT")
        deadline = time.monotonic() + duration_ms / 1000
        while time.monotonic() < deadline and len(raw) < max_bytes:
            chunk = bytes(link.rtt_read(up, min(4096, max_bytes - len(raw))))
            raw.extend(chunk)
            if not chunk:
                time.sleep(0.001)
        reason = "size_limit" if len(raw) == max_bytes else "duration"
        result = {
            "success": True,
            "data": {
                "channel": up,
                "down_channel": down,
                "stop_reason": reason,
                "stale_bytes_discarded": discarded,
                "raw_base64": base64.b64encode(raw).decode("ascii"),
                "analysis": analyze_systemview(bytes(raw), event_limit=event_limit),
                "control": "START/STOP requested; target processing is asynchronous",
            },
        }
    except Exception as exc:
        result = {
            "success": False,
            "error": {"description": str(exc)},
            "partial_raw_base64": base64.b64encode(raw).decode("ascii"),
        }
    finally:
        if start_sent:
            try:
                if link.rtt_write(down, [2]) != 1:
                    cleanup_errors.append("STOP was not accepted by RTT")
            except Exception as exc:
                cleanup_errors.append(f"STOP: {exc}")
        if started:
            try:
                link.rtt_stop()
            except Exception as exc:
                cleanup_errors.append(f"RTT stop: {exc}")
    if cleanup_errors:
        result["cleanup_warnings"] = cleanup_errors
    return result
