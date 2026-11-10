"""Runtime fault, scheduling and lifecycle tests / 运行故障、调度和生命周期测试."""

from __future__ import annotations

import importlib
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jlink_mcp.hardware_access import claim_hardware, hardware_access, release_hardware
from jlink_mcp.runtime_diagnostics import cpu_snapshot, scheduling_findings
from jlink_mcp.runtime_monitor import RuntimeMonitor
from jlink_mcp.systemview import SystemViewDecoder, analyze_systemview
from jlink_mcp.tools import debug, rtt
from tests.unit.test_systemview import SYNC, packet, trace

pytestmark = pytest.mark.unit
monitor_module = importlib.import_module("jlink_mcp.runtime_monitor")


def cpu_link(exception=0, cpuid=0x410FC241):
    link = Mock()
    link.halted.return_value = True
    names = {13: "R13 (SP)", 14: "R14", 15: "R15 (PC)", 16: "XPSR"}
    link.register_list.return_value = list(names)
    link.register_name.side_effect = names.__getitem__
    link.register_read.side_effect = {13: 0x20002000, 14: 0xFFFFFFF9, 15: 0x08000100, 16: exception}.__getitem__
    link.memory_read32.side_effect = lambda address, count: [cpuid] if count == 1 else [1 << 25, 1 << 30, 0, 0, 0]
    return link


def test_active_fault_and_sticky_status_are_distinct():
    report = cpu_snapshot(cpu_link(exception=3))
    assert report["assessment"] == "fault_observed"
    assert report["active_exception"] == 3
    assert report["findings"][1]["severity"] == "warning"
    assert report["findings"][1]["evidence"]["flags"] == ["DIVBYZERO"]


def test_fault_status_in_thread_mode_does_not_prove_active_fault():
    assert cpu_snapshot(cpu_link())["assessment"] == "warning"


def test_m0_does_not_read_unimplemented_fault_registers():
    link = cpu_link(cpuid=0x410CC200)
    report = cpu_snapshot(link)
    assert report["fault_registers"] == {}
    assert link.memory_read32.call_count == 1


def test_running_cpu_untouched_without_sampling():
    link = Mock()
    link.halted.return_value = False
    assert cpu_snapshot(link)["assessment"] == "unknown"
    link.halt.assert_not_called()
    link.register_read.assert_not_called()
    link.memory_read32.assert_not_called()


def test_intrusive_snapshot_restores_running_cpu_on_read_failure():
    link = cpu_link()
    link.halted.side_effect = [False, True, True, False]
    link.memory_read32.side_effect = RuntimeError("read failed")
    report = cpu_snapshot(link, True)
    assert report["intrusive"]
    assert report["assessment"] == "unknown"
    link.restart.assert_called_once_with(skip_breakpoints=False)


def test_ready_latency_and_irq_budget_are_scoped_warnings():
    raw = SYNC + packet(24, (1000000, 1000000, 0, 0)) + packet(6, (1,)) + packet(4, (1,), 20000)
    analysis = analyze_systemview(raw)
    result = scheduling_findings(analysis, ready_latency_us=10000)
    assert result["findings"][0]["code"] == "READY_LATENCY_BUDGET_EXCEEDED"
    assert result["max_ready_latency_us"] == 20000
    assert scheduling_findings(analyze_systemview(trace()), isr_budget_percent=1)["assessment"] == "risk_observed"


def test_overflow_and_absent_task_events_prevent_healthy_claim():
    assert scheduling_findings(analyze_systemview(SYNC + packet(1, (5,), 100)))["assessment"] == "insufficient_data"
    assert scheduling_findings(analyze_systemview(SYNC + packet(17, delta=100)))["assessment"] == "insufficient_data"


def test_hardware_lease_blocks_debug_operations():
    claim_hardware("session-test")
    try:
        assert not debug.reset_target()["success"]
        with pytest.raises(RuntimeError, match="reserved"):
            with hardware_access():
                pass
        with hardware_access("session-test"):
            pass
    finally:
        release_hardware("session-test")


class Link:
    def __init__(self):
        self.started = False
        self.sent = False
        self.stopped = False
        self.commands = []
        self.halts = 0

    def halted(self):
        return False

    def halt(self):
        self.halts += 1

    def rtt_start(self, block_address=None):
        self.started = True

    def rtt_stop(self):
        self.stopped = True

    def rtt_get_num_up_buffers(self):
        return 2

    def rtt_get_num_down_buffers(self):
        return 1

    def rtt_get_buf_descriptor(self, index, up):
        return SimpleNamespace(acName=b"Terminal" if up and index == 0 else b"SysView", BufferIndex=index)

    def rtt_write(self, index, data):
        self.commands.extend(data)
        return len(data)

    def rtt_read(self, index, size):
        if index == 1 and 1 in self.commands and not self.sent:
            self.sent = True
            return list(trace())
        return []


def test_background_monitor_reports_and_releases_lease(monkeypatch):
    link = Link()
    monkeypatch.setattr(monitor_module.jlink_manager, "get_jlink", lambda: link)
    monkeypatch.setattr(rtt, "_rtt_started", False)
    monitor = RuntimeMonitor()
    try:
        start = monitor.start(mode="systemview", duration_s=5, poll_ms=10)
        assert start["state"] == "running"
        deadline = time.monotonic() + 2
        while not monitor.status()["report"] and time.monotonic() < deadline:
            time.sleep(0.01)
        report = monitor.status()
        assert report["report"]["systemview"]["window_ticks"] == 50
        assert report["report"]["systemview_validated"]
        assert not debug.reset_target()["success"]
        assert link.halts == 0
        assert monitor.status(report["last_sequence"])["messages"] == []
    finally:
        stopped = monitor.stop()
    assert stopped["state"] == "stopped"
    assert link.commands == [1, 2]
    assert link.stopped
    with hardware_access():
        pass


def test_monitor_failure_cleans_up_rtt(monkeypatch):
    link = Link()

    def broken_read(index, size):
        raise RuntimeError("USB lost")

    link.rtt_read = broken_read
    monkeypatch.setattr(monitor_module.jlink_manager, "get_jlink", lambda: link)
    monkeypatch.setattr(rtt, "_rtt_started", False)
    monitor = RuntimeMonitor()
    result = monitor.start(mode="systemview", duration_s=1)
    assert result["state"] == "failed"
    assert link.stopped
    assert "USB lost" in result["errors"][0]
    with hardware_access():
        pass


def test_empty_monitor_status():
    assert RuntimeMonitor().status()["state"] == "idle"


def test_rolling_window_does_not_charge_time_before_first_event():
    from jlink_mcp.systemview import summarize_events

    decoder = SystemViewDecoder()
    events = decoder.feed(trace())
    cropped = events[-2:]
    assert summarize_events(cropped, decoder, timestamp_hz=1000000)["window_ticks"] == 20


def test_auto_waits_for_systemview_after_terminal_initializes(monkeypatch):
    link = Link()
    calls = 0

    def num_up():
        nonlocal calls
        calls += 1
        return 1 if calls == 1 else 2

    link.rtt_get_num_up_buffers = num_up
    monkeypatch.setattr(monitor_module.jlink_manager, "get_jlink", lambda: link)
    monkeypatch.setattr(rtt, "_rtt_started", False)
    monitor = RuntimeMonitor()
    try:
        result = monitor.start(mode="auto", duration_s=5)
        assert result["mode"] == "systemview"
        assert calls >= 2
    finally:
        monitor.stop()


def test_auto_falls_back_when_rtt_initialization_unavailable(monkeypatch):
    link = Link()

    def unavailable(**kwargs):
        raise RuntimeError("RTT unsupported")

    link.rtt_start = unavailable
    monkeypatch.setattr(monitor_module.jlink_manager, "get_jlink", lambda: link)
    monkeypatch.setattr(rtt, "_rtt_started", False)
    monitor = RuntimeMonitor()
    try:
        result = monitor.start(mode="auto", duration_s=5)
        assert result["mode"] == "basic"
        assert any(m["code"] == "CAPABILITY_FALLBACK" for m in result["messages"])
    finally:
        monitor.stop()


def test_export_preserves_raw_bytes_and_report(tmp_path):
    import json

    monitor = RuntimeMonitor()
    monitor._raw.extend(trace())
    output = monitor.export(str(tmp_path / "capture.svraw"))
    assert (tmp_path / "capture.svraw").read_bytes() == trace()
    assert json.loads((tmp_path / "capture.svraw.json").read_text())["state"] == "idle"
    assert output["bytes"] == len(trace())
    with pytest.raises(ValueError, match="already exists"):
        monitor.export(str(tmp_path / "capture.svraw"))
