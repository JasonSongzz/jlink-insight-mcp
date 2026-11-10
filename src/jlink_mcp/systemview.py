"""SystemView wire decoding and single-core accounting / SystemView 解码及单核运行统计.

Protocol reference: SEGGERMicro/SystemView SYSVIEW/SEGGER_SYSVIEW.c.
OS-specific payloads remain opaque; no SEGGER source is bundled.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

SYNC = b"\x00" * 10
NAMES = (
    "nop",
    "overflow",
    "isr_enter",
    "isr_exit",
    "task_start_exec",
    "task_stop_exec",
    "task_start_ready",
    "task_stop_ready",
    "task_create",
    "task_info",
    "trace_start",
    "trace_stop",
    "systime_cycles",
    "systime_us",
    "sysdesc",
    "mark_start",
    "mark_stop",
    "idle",
    "isr_to_scheduler",
    "timer_enter",
    "timer_exit",
    "stack_info",
    "moduledesc",
    "data_sample",
    "init",
    "name_resource",
    "print_formatted",
    "nummodules",
    "end_call",
    "task_terminate",
)
# u=unsigned varint (also float bit patterns), s=SystemView string.
SCHEMAS = (
    "",
    "u",
    "u",
    "",
    "u",
    "",
    "u",
    "uu",
    "u",
    "uus",
    "",
    "",
    "u",
    "uu",
    "s",
    "u",
    "u",
    "",
    "",
    "u",
    "",
    "uuuu",
    "uus",
    "uu",
)


class IncompletePacketError(Exception):
    """More input is needed / 需要更多输入."""


def _uint(data: bytes | bytearray, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if pos >= len(data):
            raise IncompletePacketError
        byte = data[pos]
        pos += 1
        if shift == 28 and byte > 15:
            raise ValueError("Invalid SystemView U32 varint")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, pos
    raise ValueError("Invalid SystemView U32 varint")


def _fields(data: bytes | bytearray, pos: int, schema: str) -> tuple[list[Any], int]:
    values = []
    for kind in schema:
        if kind == "u":
            value, pos = _uint(data, pos)
        else:
            if kind == "s":
                if pos >= len(data):
                    raise IncompletePacketError
                size = data[pos]
                pos += 1
                if size == 255:
                    if pos + 2 > len(data):
                        raise IncompletePacketError
                    size = int.from_bytes(data[pos : pos + 2], "big")
                    pos += 2
            else:
                size = 4
            if pos + size > len(data):
                raise IncompletePacketError
            raw = bytes(data[pos : pos + size])
            value = raw.decode("utf-8", errors="replace") if kind == "s" else raw.hex()
            pos += size
        values.append(value)
    return values, pos


class SystemViewDecoder:
    """Incremental decoder with explicit sync requirement / 需要同步标记的增量解码器."""

    def __init__(self) -> None:
        self.pending = bytearray()
        self.synced = False
        self.discarded = 0
        self.ticks = 0
        self.segment = 0
        self.errors = 0

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Decode complete packets, retain split packets / 解码完整包并保留分片."""
        self.pending.extend(data)
        events = []
        while self.pending:
            if len(events) >= 100000:
                raise ValueError("Trace exceeds 100000 decoded events; use shorter capture windows")
            if not self.synced:
                offset = self.pending.find(SYNC)
                if offset < 0:
                    count = max(0, len(self.pending) - 9)
                    self.discarded += count
                    del self.pending[:count]
                    break
                self.discarded += offset
                del self.pending[: offset + 10]
                self.synced = True
                self.ticks = 0
                self.segment += 1
                events.append({"name": "sync", "ticks": 0, "segment": self.segment})
                continue
            # Zero is reserved for synchronization. Wait for the full marker.
            if self.pending[0] == 0:
                if len(self.pending) < 10:
                    break
                if self.pending[:10] == SYNC:
                    self.synced = False
                    continue
            try:
                event_id, pos = _uint(self.pending, 0)
                payload = None
                if event_id < 24:
                    values, pos = _fields(self.pending, pos, SCHEMAS[event_id])
                else:
                    size, pos = _uint(self.pending, pos)
                    if size > 65535:
                        raise ValueError("SystemView packet exceeds 65535 bytes")
                    end = pos + size
                    if end > len(self.pending):
                        raise IncompletePacketError
                    payload = bytes(self.pending[pos:end])
                    values = []
                    if event_id == 24:
                        try:
                            values, _ = _fields(payload, 0, "uuuu")
                        except IncompletePacketError as exc:
                            raise ValueError("Invalid INIT packet") from exc
                    pos = end
                delta, pos = _uint(self.pending, pos)
            except IncompletePacketError:
                break
            except ValueError:
                self.errors += 1
                self.synced = False
                # Fail closed: only resume at an actual sync marker.
                self.discarded += 1
                del self.pending[:1]
                continue
            self.ticks += delta
            event = {
                "id": event_id,
                "name": NAMES[event_id] if event_id < len(NAMES) else "opaque",
                "ticks": self.ticks,
                "delta_ticks": delta,
                "values": values,
                "segment": self.segment,
            }
            if payload is not None:
                event["payload_hex"] = payload.hex()
            events.append(event)
            del self.pending[:pos]
        return events


def analyze_systemview(raw: bytes, timestamp_hz: int | None = None, event_limit: int = 100) -> dict[str, Any]:
    """Summarize observed exclusive runtime / 汇总观测到的独占运行时间.

    No time beyond the last event is extrapolated. Lost or unknown contexts are
    accounted as unknown, and percentages describe the captured event window.
    """
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Trace exceeds 4 MiB")
    if timestamp_hz is not None and timestamp_hz <= 0:
        raise ValueError("timestamp_hz must be positive")
    if not 0 <= event_limit <= 1000:
        raise ValueError("event_limit must be between 0 and 1000")
    decoder = SystemViewDecoder()
    events = decoder.feed(raw)
    return summarize_events(events, decoder, len(raw), timestamp_hz, event_limit)


def summarize_events(
    events: list[dict[str, Any]],
    decoder: SystemViewDecoder,
    byte_count: int = 0,
    timestamp_hz: int | None = None,
    event_limit: int = 100,
) -> dict[str, Any]:
    """Summarize an already decoded observation window / 汇总已解码的观测窗口."""
    tasks: dict[int, dict[str, Any]] = {}
    runtime: Counter = Counter()
    counts: Counter = Counter()
    current: int | str | None = None
    interrupts: list[int] = []
    previous = events[0]["ticks"] if events else 0
    total = 0
    lost = 0
    hz = timestamp_hz
    frequencies = set()
    warnings = []
    for event in events:
        name, ticks = event["name"], event["ticks"]
        counts[name] += 1
        if name == "sync":
            current, interrupts, previous = None, [], 0
            continue
        values = event["values"]
        duration = ticks - previous
        # Overflow interval cannot be attributed to the previously running context.
        context = (
            "unknown"
            if name == "overflow"
            else (
                f"isr:{interrupts[-1]}"
                if interrupts
                else (f"task:{current}" if isinstance(current, int) else current or "unknown")
            )
        )
        runtime[context] += duration
        total += duration
        previous = ticks
        if name == "init":
            frequencies.add(values[0])
            if timestamp_hz is None:
                hz = values[0] or None
        elif name == "overflow":
            lost += values[0]
            current, interrupts = None, []
        elif name in ("task_create", "task_info", "task_start_exec"):
            task = values[0]
            tasks.setdefault(task, {"wire_id": task, "name": None, "priority": None})
            if name == "task_info":
                tasks[task].update(name=values[2], priority=values[1])
            elif name == "task_start_exec":
                current = task
        elif name == "task_stop_exec":
            current = None
        elif name == "idle":
            current = "idle"
        elif name == "isr_enter":
            interrupts.append(values[0])
        elif name in ("isr_exit", "isr_to_scheduler"):
            if interrupts:
                interrupts.pop()
            else:
                warnings.append("Unmatched ISR exit")
                current = None
            if name == "isr_to_scheduler" and not interrupts:
                current = None
        elif name in ("trace_start", "trace_stop"):
            current, interrupts = None, []
    if len(frequencies) > 1:
        hz = None
        warnings.append("Timestamp frequency changed; time conversion disabled")
    if lost:
        warnings.append("Target reported dropped events; runtime attribution is incomplete")
    if decoder.discarded or decoder.errors:
        warnings.append("Discarded unsynchronized or malformed data")
    if decoder.pending:
        warnings.append("Incomplete trailing packet or missing sync")
    if not decoder.segment:
        warnings.append("No SystemView sync marker found")
    if hz is None:
        warnings.append("Timestamp frequency unavailable; only ticks are reported")
    if interrupts:
        warnings.append("Capture ended inside an ISR")

    def row(key: str) -> dict[str, Any]:
        ticks = runtime[key]
        return {
            "ticks": ticks,
            "duration_us": ticks * 1e6 / hz if hz else None,
            "percent_of_window": ticks * 100 / total if total else 0.0,
        }

    return {
        "bytes": byte_count,
        "event_count": len(events),
        "event_counts": dict(counts),
        "timestamp_hz": hz,
        "window_ticks": total,
        "window_us": total * 1e6 / hz if hz else None,
        "tasks": [dict(info, **row(f"task:{task}")) for task, info in tasks.items()],
        "contexts": {key: row(key) for key in runtime},
        "quality": {
            "lost_events": lost,
            "discarded_bytes": decoder.discarded,
            "decode_errors": decoder.errors,
            "pending_bytes": len(decoder.pending),
            "segments": decoder.segment,
            "warnings": sorted(set(warnings)),
        },
        "events": events[:event_limit],
        "events_omitted": max(0, len(events) - event_limit),
        "scope": "single-core; exclusive runtime within observed event window; wire task IDs",
    }
