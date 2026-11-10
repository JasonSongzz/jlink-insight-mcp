"""Project-aware deployment and diagnostics tools / 工程感知的烧录与诊断工具."""

from __future__ import annotations

import atexit
import functools
from collections.abc import Callable
from typing import Any

from ..firmware import flash_verified, load_image
from ..hardware_access import hardware_access
from ..jlink_manager import jlink_manager
from ..project_inspector import inspect_project_data, read_elf
from ..runtime_diagnostics import cpu_snapshot
from ..runtime_monitor import runtime_monitor
from ..svd_manager import svd_manager
from .connection import connect_device


def _response(func: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"success": True, "data": func(*args, **kwargs)}
        except Exception as exc:
            return {"success": False, "error": {"description": str(exc)}}

    return wrapped


def _symbols(project: dict[str, Any]) -> dict[str, int]:
    artifact = project.get("firmware")
    if artifact and artifact["format"] in ("elf", "axf"):
        from pathlib import Path

        metadata = read_elf(Path(artifact["path"]))
        if metadata["sha256"] != artifact["sha256"]:
            raise ValueError("ELF changed during inspection; wait for build completion")
        return metadata["symbols"]
    return {}


@_response
def inspect_project(project_path: str, firmware_path: str | None = None, ioc_path: str | None = None) -> dict[str, Any]:
    """Inspect CubeMX project and linked capabilities / 检测工程及链接能力."""
    return inspect_project_data(project_path, firmware_path, ioc_path)


@_response
def plan_project_debug(
    project_path: str, firmware_path: str | None = None, chip_name: str | None = None
) -> dict[str, Any]:
    """Create a read-only deployment plan / 生成不访问硬件的执行计划."""
    project = inspect_project_data(project_path, firmware_path)
    chip = chip_name or project["chip_name"]
    caps = project["capabilities"]
    mode = "systemview" if caps["systemview_candidate"] else ("rtt" if caps["rtt_candidate"] else "basic")
    return {
        "project": project,
        "chip_name": chip,
        "suggested_mode": mode,
        "ready_to_deploy": bool(chip and project["firmware"]),
        "stages": [
            "parse_image",
            "select_probe",
            "connect_exact_target",
            "halt",
            "program",
            "verify_readback",
            "reset_and_run",
            "validate_runtime_capabilities",
            "monitor",
        ],
        "svd_candidates": [
            name
            for name in svd_manager.device_names
            if chip and (name.lower() == chip.lower() or name.lower().startswith(chip.lower()))
        ],
        "limitations": ["Static evidence is not runtime confirmation", "No project build scripts executed"],
    }


@_response
def deploy_project(
    project_path: str,
    firmware_path: str | None = None,
    chip_name: str | None = None,
    serial_number: str | None = None,
    flash_address: int | None = None,
    monitor: bool = True,
    duration_s: int = 60,
    sample_cpu: bool = False,
) -> dict[str, Any]:
    """Flash compiled firmware, verify, run and monitor / 烧录已编译固件并校验运行监控."""
    project = inspect_project_data(project_path, firmware_path)
    chip = chip_name or project["chip_name"]
    if not chip or not chip.upper().startswith("STM32"):
        raise ValueError("An exact STM32 J-Link chip_name is required; generic core fallback is refused")
    if not project["firmware"]:
        raise ValueError("No unique firmware; pass firmware_path")
    if not 1 <= duration_s <= 3600:
        raise ValueError("duration_s must be 1..3600")
    manifest = project["manifest"]
    address = flash_address if flash_address is not None else manifest.get("flash_address")
    if isinstance(address, str):
        address = int(address, 0)
    segments, digest = load_image(project["firmware"]["path"], address)
    if digest != project["firmware"]["sha256"]:
        raise ValueError("Firmware changed during inspection; finish compilation before deploying")
    symbols = _symbols(project)
    caps = project["capabilities"]
    requested_mode = "auto" if (caps["rtt_candidate"] or project["evidence"]["sources"]["rtt"]) else "basic"
    stages = []
    with hardware_access():
        from ..gdb_server import gdb_server_manager

        if gdb_server_manager.is_running:
            raise RuntimeError("Stop GDB Server before firmware deployment")
        if jlink_manager.is_connected:
            # Never silently reuse a connection to a different target or probe.
            if jlink_manager.requested_chip_name != chip:
                raise ValueError("Connected target differs; disconnect_device before deploying this project")
            if serial_number and str(jlink_manager.get_connection_status().device_serial) != serial_number:
                raise ValueError("Connected probe differs from serial_number; disconnect first")
        else:
            probes = jlink_manager.enumerate_devices()
            if serial_number is None:
                if len(probes) != 1:
                    raise ValueError("Expected one connected probe; provide serial_number explicitly")
                serial_number = str(probes[0].serial_number)
            connection = connect_device(serial_number=serial_number, interface="SWD", chip_name=chip)
            if not connection.get("success"):
                raise RuntimeError(f"Connect failed: {connection.get('error')}")
        if jlink_manager.connection_mode != "native":
            raise RuntimeError("Exact device connection required for flash; generic core mode is refused")
        link = jlink_manager.get_jlink()
        stages.append("connected")
        try:
            programmed = flash_verified(link, segments)
            stages.append("programmed_and_verified")
            link.reset(halt=True)
            if not link.halted():
                raise RuntimeError("Reset did not halt target")
            link.restart(skip_breakpoints=False)
            if link.halted():
                raise RuntimeError("Target remained halted after resume")
            stages.append("running")
        except Exception as exc:
            halt_error = None
            try:
                link.halt()
            except Exception as halt_exc:
                halt_error = str(halt_exc)
            raise RuntimeError(f"Deployment failed after {stages}: {exc}; halt_cleanup_error={halt_error}") from exc
    # Initialization occurs in firmware after reset; RTT discovery retries separately.
    runtime = None
    if monitor:
        runtime = runtime_monitor.start(
            mode=requested_mode,
            duration_s=duration_s,
            sample_cpu=sample_cpu,
            block_address=symbols.get("_SEGGER_RTT"),
            symbols=symbols,
        )
    return {
        "project_path": project["project_path"],
        "chip_name": chip,
        "firmware": project["firmware"],
        "stages": stages,
        "flash": programmed,
        "runtime": runtime,
        "runtime_monitor_started": runtime is not None and runtime.get("state") in ("starting", "running"),
        "capabilities_expected": caps,
        "warnings": project["warnings"],
        "message": "Firmware verified and started; inspect runtime state separately",
    }


@_response
def start_runtime_monitor(
    mode: str = "auto",
    duration_s: int = 60,
    sample_cpu: bool = False,
    poll_ms: int = 10,
    rtt_channel: int | None = None,
    block_address: int | None = None,
    ready_latency_us: float = 10000,
    isr_budget_percent: float = 30.0,
) -> dict[str, Any]:
    """Start asynchronous monitor on connected running target / 对已连接运行目标启动后台监控."""
    return runtime_monitor.start(
        mode, duration_s, sample_cpu, poll_ms, rtt_channel, block_address, ready_latency_us, isr_budget_percent
    )


@_response
def get_runtime_report(after_sequence: int = 0) -> dict[str, Any]:
    """Poll latest report and new messages / 查询最新报告及增量反馈."""
    if after_sequence < 0:
        raise ValueError("after_sequence must be nonnegative")
    return runtime_monitor.status(after_sequence)


@_response
def stop_runtime_monitor() -> dict[str, Any]:
    """Stop and release J-Link/RTT resources / 停止并释放 J-Link/RTT 独占权."""
    return runtime_monitor.stop()


@_response
def runtime_snapshot(halt_if_running: bool = False) -> dict[str, Any]:
    """Inspect Cortex-M fault evidence, optionally briefly halting / 可选短暂停机采样故障证据."""
    with hardware_access():
        return cpu_snapshot(jlink_manager.get_jlink(), halt_if_running)


@_response
def export_runtime_trace(output_path: str) -> dict[str, Any]:
    """Save raw binary recording and JSON report / 保存原始二进制及 JSON 报告."""
    return runtime_monitor.export(output_path)


atexit.register(runtime_monitor.stop)


@_response
def start_firmware_watch(
    project_path: str,
    firmware_path: str,
    chip_name: str | None = None,
    serial_number: str | None = None,
    duration_s: int = 3600,
    deploy_initial: bool = False,
) -> dict[str, Any]:
    """Watch build output and deploy stable changes / 监听编译产物并自动部署稳定版本."""
    from ..firmware_watch import firmware_watch

    return firmware_watch.start(project_path, firmware_path, chip_name, serial_number, duration_s, deploy_initial)


@_response
def get_firmware_watch_status() -> dict[str, Any]:
    """Read local artifact watcher state / 查询本地固件监听状态."""
    from ..firmware_watch import firmware_watch

    return firmware_watch.status()


@_response
def stop_firmware_watch() -> dict[str, Any]:
    """Stop future deployments / 停止后续自动部署."""
    from ..firmware_watch import firmware_watch

    return firmware_watch.stop()


def shutdown_workers() -> None:
    """Stop producer before hardware worker during shutdown / 关闭时先停止部署监听再停止硬件监控."""
    from ..firmware_watch import firmware_watch

    firmware_watch.stop()
    runtime_monitor.stop()


atexit.unregister(runtime_monitor.stop)
atexit.register(shutdown_workers)
