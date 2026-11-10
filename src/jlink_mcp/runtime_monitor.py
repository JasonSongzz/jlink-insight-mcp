"""Bounded background runtime monitoring / 有界后台运行状态监控.

One worker owns J-Link. MCP clients poll snapshots; no unsolicited messages.
"""

from __future__ import annotations

import codecs
import copy
import json
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from .hardware_access import claim_hardware, hardware_access, release_hardware
from .jlink_manager import jlink_manager
from .runtime_diagnostics import cpu_snapshot, scheduling_findings
from .systemview import SystemViewDecoder, summarize_events

LOG_PATTERN = re.compile(r"\b(assert(?:ion)?(?: failed)?|hardfault|stack overflow|malloc failed|fatal|panic)\b", re.I)


def _rtt_name(descriptor: Any) -> str:
    value = descriptor.acName
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


class RuntimeMonitor:
    """Own one monitored hardware connection / 管理一个硬件监控会话."""

    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._session: dict[str, Any] = {}
        self._raw = bytearray()
        self._events: deque[dict[str, Any]] = deque(maxlen=20000)
        self._messages: deque[dict[str, Any]] = deque(maxlen=200)
        self._seq = 0

    def _message(self, code: str, detail: Any) -> None:
        with self._mutex:
            self._seq += 1
            self._messages.append({"sequence": self._seq, "host_time": time.time(), "code": code, "detail": detail})

    def start(
        self,
        mode: str = "auto",
        duration_s: int = 60,
        sample_cpu: bool = False,
        poll_ms: int = 10,
        rtt_channel: int | None = None,
        block_address: int | None = None,
        ready_latency_us: float = 10000,
        isr_budget_percent: float = 30.0,
        symbols: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Start monitoring and wait for initialization / 启动监控并等待初始化结果."""
        if mode not in {"auto", "basic", "rtt", "systemview"}:
            raise ValueError("mode must be auto/basic/rtt/systemview")
        if not 1 <= duration_s <= 3600 or not 10 <= poll_ms <= 2000:
            raise ValueError("duration_s must be 1..3600 and poll_ms 10..2000")
        if not 0 < ready_latency_us < 1e12 or not 0 <= isr_budget_percent <= 100:
            raise ValueError("Invalid diagnostic thresholds")
        if rtt_channel is not None and rtt_channel < 0:
            raise ValueError("Invalid RTT channel")
        with self._mutex:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Monitor already active; stop it first")
            token = uuid.uuid4().hex
            claim_hardware(token)
            self._stop.clear()
            self._ready.clear()
            self._raw.clear()
            self._events.clear()
            self._messages.clear()
            self._seq = 0
            self._session = {
                "session_id": token,
                "state": "starting",
                "requested_mode": mode,
                "mode": "basic",
                "started_at": time.time(),
                "sample_cpu": sample_cpu,
                "poll_ms": poll_ms,
                "duration_s": duration_s,
                "report": {},
                "errors": [],
                "trace_bytes": 0,
                "events_evicted": 0,
            }
            self._thread = threading.Thread(
                target=self._worker,
                args=(
                    token,
                    mode,
                    duration_s,
                    sample_cpu,
                    poll_ms,
                    rtt_channel,
                    block_address,
                    ready_latency_us,
                    isr_budget_percent,
                    symbols or {},
                ),
                name="jlink-runtime-monitor",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                release_hardware(token)
                raise
        self._ready.wait(timeout=5)
        return self.status()

    def _worker(
        self,
        token: str,
        requested_mode: str,
        duration_s: int,
        sample_cpu: bool,
        poll_ms: int,
        rtt_channel: int | None,
        block_address: int | None,
        ready_budget: float,
        isr_budget: float,
        symbols: dict[str, int],
    ) -> None:
        link = None
        rtt_started = False
        sv_started = False
        up = down = terminal = None
        decoder = SystemViewDecoder()
        text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        log_pending = ""
        task_info = {}
        frequency = None
        last_report = 0.0
        logs: deque[str] = deque(maxlen=100)
        try:
            with hardware_access(token):
                from .gdb_server import gdb_server_manager
                from .tools import rtt

                if gdb_server_manager.is_running:
                    raise RuntimeError("Stop GDB Server before runtime monitoring")
                if rtt._rtt_started:
                    raise RuntimeError("Stop ordinary RTT before starting monitoring")
                link = jlink_manager.get_jlink()
                if link.halted():
                    raise RuntimeError("Target halted; run it before starting monitoring")
                mode = "basic"
                if requested_mode != "basic":
                    try:
                        link.rtt_start(block_address=block_address)
                        rtt_started = True
                    except Exception as exc:
                        if requested_mode != "auto":
                            raise
                        self._message("CAPABILITY_FALLBACK", f"RTT initialization failed: {exc}")
            if rtt_started:
                deadline = time.monotonic() + 2
                while not self._stop.is_set():
                    try:
                        with hardware_access(token):
                            ups = [link.rtt_get_buf_descriptor(i, True) for i in range(link.rtt_get_num_up_buffers())]
                            downs = [
                                link.rtt_get_buf_descriptor(i, False) for i in range(link.rtt_get_num_down_buffers())
                            ]
                        sysups = [d for d in ups if _rtt_name(d) == "SysView"]
                        sysdowns = [d for d in downs if _rtt_name(d) == "SysView"]
                        if len(sysups) == len(sysdowns) == 1 and requested_mode in {"auto", "systemview"}:
                            up, down = sysups[0].BufferIndex, sysdowns[0].BufferIndex
                            mode = "systemview"
                        elif requested_mode == "systemview":
                            raise RuntimeError("Unique SysView up/down channels not found")
                        ordinary = [
                            d
                            for d in ups
                            if _rtt_name(d)
                            and _rtt_name(d) != "SysView"
                            and (rtt_channel is None or d.BufferIndex == rtt_channel)
                        ]
                        # Explicit selection wins; otherwise only default channel zero is consumed.
                        selected = ordinary if rtt_channel is not None else [d for d in ordinary if d.BufferIndex == 0]
                        if selected:
                            terminal = selected[0].BufferIndex
                            if mode != "systemview":
                                mode = "rtt"
                        if requested_mode == "rtt" and terminal is None:
                            raise RuntimeError("Requested ordinary RTT channel not found")
                        if requested_mode == "auto" and mode != "systemview" and time.monotonic() < deadline:
                            # Ordinary RTT may initialize before SystemView after a reset.
                            self._stop.wait(0.02)
                            continue
                        if mode == "basic":
                            self._message("CAPABILITY_FALLBACK", "No usable RTT channels initialized")
                        break
                    except Exception as exc:
                        if time.monotonic() >= deadline:
                            if requested_mode != "auto":
                                raise
                            self._message("CAPABILITY_FALLBACK", str(exc))
                            mode = "basic"
                            break
                        self._stop.wait(0.02)
                if mode == "systemview":
                    with hardware_access(token):
                        for _ in range(64):
                            if not link.rtt_read(up, 4096):
                                break
                        else:
                            raise RuntimeError("SysView is already streaming; stop the other recorder")
                        sv_started = True
                        if link.rtt_write(down, [1]) != 1:
                            raise RuntimeError("RTT rejected SystemView START")
            with self._mutex:
                self._session.update(state="running", mode=mode)
            self._message("MONITOR_STARTED", {"mode": mode, "intrusive_sampling": sample_cpu})
            self._ready.set()
            deadline = time.monotonic() + duration_s
            while not self._stop.is_set() and time.monotonic() < deadline:
                with hardware_access(token):
                    if mode == "systemview":
                        # Drain a bounded burst per poll to avoid starving control/status.
                        for _ in range(16):
                            chunk = bytes(link.rtt_read(up, 4096))
                            if not chunk:
                                break
                            with self._mutex:
                                if len(self._raw) + len(chunk) > 4 * 1024 * 1024:
                                    self._session["stop_reason"] = "trace_size_limit"
                                    self._stop.set()
                                    break
                                self._raw.extend(chunk)
                            decoded = decoder.feed(chunk)
                            for event in decoded:
                                if event["name"] == "task_start_exec":
                                    with self._mutex:
                                        self._session["task_events_observed"] = True
                                if event["name"] == "init":
                                    frequency = event["values"][0] or None
                                if event["name"] == "task_info":
                                    values = event["values"]
                                    task_info[values[0]] = {"name": values[2], "priority": values[1]}
                            with self._mutex:
                                self._session["events_evicted"] += max(0, len(self._events) + len(decoded) - 20000)
                                self._events.extend(decoded)
                    if terminal is not None:
                        chunk = bytes(link.rtt_read(terminal, 4096))
                        log_pending += text_decoder.decode(chunk)
                        lines = log_pending.split("\n")
                        log_pending = lines.pop()[-4096:]
                        for line in lines:
                            line = line[-4096:]
                            logs.append(line)
                            if LOG_PATTERN.search(line):
                                self._message(
                                    "ERROR_PATTERN_IN_RTT_LOG",
                                    {
                                        "text": line,
                                        "interpretation": "Log pattern is evidence to inspect, not a confirmed fault",
                                    },
                                )
                    now = time.monotonic()
                    if now - last_report >= 0.5:
                        snapshot = cpu_snapshot(link, halt_if_running=sample_cpu, symbols=symbols)
                        if snapshot["was_halted"]:
                            self._message("TARGET_HALTED", snapshot)
                        if snapshot.get("errors"):
                            self._message("SNAPSHOT_ERROR", snapshot["errors"])
                        if snapshot.get("assessment") == "fault_observed":
                            self._message("CPU_FAULT", snapshot["findings"])
                        last_report = now
                    else:
                        snapshot = None
                if snapshot is not None:
                    self._publish(
                        decoder, frequency, task_info, logs, log_pending, snapshot, ready_budget, isr_budget, sample_cpu
                    )
                self._stop.wait(poll_ms / 1000)
            with hardware_access(token):
                final_snapshot = cpu_snapshot(link, halt_if_running=False, symbols=symbols)
            self._publish(
                decoder, frequency, task_info, logs, log_pending, final_snapshot, ready_budget, isr_budget, sample_cpu
            )
            with self._mutex:
                self._session.setdefault("stop_reason", "requested" if self._stop.is_set() else "duration")
        except Exception as exc:
            with self._mutex:
                self._session["state"] = "failed"
                self._session["errors"].append(str(exc))
            self._message("MONITOR_FAILED", str(exc))
        finally:
            if link is not None:
                with hardware_access(token):
                    if sv_started:
                        try:
                            if link.rtt_write(down, [2]) != 1:
                                raise RuntimeError("SystemView STOP not accepted")
                        except Exception as exc:
                            self._message("CLEANUP_ERROR", str(exc))
                    if rtt_started:
                        try:
                            link.rtt_stop()
                        except Exception as exc:
                            self._message("CLEANUP_ERROR", str(exc))
            release_hardware(token)
            with self._mutex:
                if self._session.get("state") != "failed":
                    self._session["state"] = "stopped"
                self._session["ended_at"] = time.time()
                self._session["trace_bytes"] = len(self._raw)
            self._ready.set()

    def _publish(
        self,
        decoder: SystemViewDecoder,
        frequency: int | None,
        task_info: dict[int, Any],
        logs: deque[str],
        pending: str,
        snapshot: dict[str, Any],
        ready_budget: float,
        isr_budget: float,
        intrusive: bool,
    ) -> None:
        with self._mutex:
            events = list(self._events)
        analysis = summarize_events(events, decoder, len(self._raw), frequency, event_limit=50)
        for task in analysis["tasks"]:
            if task["wire_id"] in task_info:
                task.update(task_info[task["wire_id"]])
        scheduling = scheduling_findings(dict(analysis, events=events), ready_budget, isr_budget)
        if intrusive and self._session["mode"] == "systemview":
            scheduling["limitations"].append("CPU halt sampling perturbs trace timing")
            scheduling["assessment"] = "insufficient_data"
        with self._mutex:
            self._session["trace_bytes"] = len(self._raw)
            self._session["report"] = {
                "updated_at": time.time(),
                "systemview_validated": bool(
                    decoder.segment and frequency and self._session.get("task_events_observed")
                ),
                "cpu": snapshot,
                "systemview": analysis if self._session["mode"] == "systemview" else None,
                "scheduling": (
                    scheduling
                    if self._session["mode"] == "systemview"
                    else {"assessment": "unavailable", "reason": "No validated SystemView stream"}
                ),
                "rtt_log_tail": list(logs)[-50:],
                "rtt_partial_line": pending,
                "scope": "Last 20000 decoded events; matching ready/run pairs only",
            }

    def status(self, after_sequence: int = 0) -> dict[str, Any]:
        """Return a consistent report without touching J-Link / 返回一致报告，不访问硬件."""
        with self._mutex:
            if not self._session:
                return {"state": "idle", "messages": [], "last_sequence": 0}
            result = copy.deepcopy(self._session)
            result["messages"] = [copy.deepcopy(m) for m in self._messages if m["sequence"] > after_sequence]
            result["last_sequence"] = self._seq
            result["messages_evicted"] = max(0, self._seq - 200 - after_sequence)
            return result or {"state": "idle"}

    def stop(self) -> dict[str, Any]:
        """Request stop, wait for cleanup / 停止并等待资源清理."""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                return dict(self.status(), stop_pending=True)
        return self.status()

    def export(self, output_path: str) -> dict[str, Any]:
        """Export raw RTT recording and report / 导出原始 RTT 流和报告."""
        path = Path(output_path).expanduser().resolve()
        with self._mutex:
            raw = bytes(self._raw)
            report = self.status()
        if not raw:
            raise ValueError("No SystemView bytes captured")
        if path.exists() or path.with_suffix(path.suffix + ".json").exists():
            raise ValueError("Export path already exists; select a new filename")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
        report_path = path.with_suffix(path.suffix + ".json")
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False)
        return {
            "raw_path": str(path),
            "report_path": str(report_path),
            "bytes": len(raw),
            "format": "raw SystemView RTT bytes; not .SVDat",
        }


runtime_monitor = RuntimeMonitor()
