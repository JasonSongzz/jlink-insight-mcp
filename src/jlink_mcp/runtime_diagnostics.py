"""Evidence-based Cortex-M and scheduling diagnostics / 基于证据的故障及调度诊断."""

from __future__ import annotations

from typing import Any

FAULT_BITS = {
    0: "IACCVIOL",
    1: "DACCVIOL",
    3: "MUNSTKERR",
    4: "MSTKERR",
    5: "MLSPERR",
    8: "IBUSERR",
    9: "PRECISERR",
    10: "IMPRECISERR",
    11: "UNSTKERR",
    12: "STKERR",
    13: "LSPERR",
    16: "UNDEFINSTR",
    17: "INVSTATE",
    18: "INVPC",
    19: "NOCP",
    24: "UNALIGNED",
    25: "DIVBYZERO",
}
SUPPORTED_FAULT_PARTS = {0xC23, 0xC24, 0xC27, 0xD21, 0xD31, 0xD22, 0xD23}


def _registers(link: Any) -> dict[str, int]:
    result = {}
    for index in link.register_list():
        name = link.register_name(index).upper().split(" ")[0]
        if name in {"PC", "R15", "LR", "R14", "SP", "R13", "MSP", "PSP", "XPSR", "PSR"}:
            result[name] = int(link.register_read(index))
    return result


def cpu_snapshot(link: Any, halt_if_running: bool = False, symbols: dict[str, int] | None = None) -> dict[str, Any]:
    """Sample fault evidence, restore prior run state / 采样故障并恢复原运行状态."""
    was_halted = bool(link.halted())
    data: dict[str, Any] = {
        "was_halted": was_halted,
        "intrusive": not was_halted and halt_if_running,
        "registers": {},
        "fault_registers": {},
        "findings": [],
        "errors": [],
    }
    if not was_halted and not halt_if_running:
        data["assessment"] = "unknown"
        data["limitations"] = ["CPU is running; no intrusive register/memory snapshot requested"]
        return data
    requested_halt = not was_halted
    try:
        if requested_halt:
            link.halt()
        if not link.halted():
            raise RuntimeError("CPU did not halt for snapshot")
        regs = data["registers"] = _registers(link)
        exception = regs.get("XPSR", regs.get("PSR", 0)) & 0x1FF
        data["active_exception"] = exception
        if exception in (3, 4, 5, 6):
            data["findings"].append(
                {
                    "code": "ACTIVE_FAULT_EXCEPTION",
                    "severity": "error",
                    "evidence": {"exception": exception},
                    "message": "CPU sampled inside a fault exception",
                }
            )
        pc = regs.get("PC", regs.get("R15"))
        if pc is not None and symbols:
            data["pc_symbols"] = [name for name, value in symbols.items() if (value & ~1) == (pc & ~1)]
        cpuid = int(link.memory_read32(0xE000ED00, 1)[0])
        part = (cpuid >> 4) & 0xFFF
        data["cpuid"] = cpuid
        if part in SUPPORTED_FAULT_PARTS:
            cfsr, hfsr, dfsr, mmfar, bfar = [int(value) for value in link.memory_read32(0xE000ED28, 5)]
            data["fault_registers"] = {"CFSR": cfsr, "HFSR": hfsr, "DFSR": dfsr}
            if cfsr & (1 << 7):
                data["fault_registers"]["MMFAR"] = mmfar
            if cfsr & (1 << 15):
                data["fault_registers"]["BFAR"] = bfar
            if cfsr or hfsr:
                data["findings"].append(
                    {
                        "code": "FAULT_STATUS_LATCHED",
                        "severity": "warning",
                        "message": "Fault status bits are latched; may describe an earlier handled fault",
                        "evidence": {
                            "CFSR": cfsr,
                            "HFSR": hfsr,
                            "flags": [name for bit, name in FAULT_BITS.items() if cfsr & (1 << bit)],
                        },
                    }
                )
        else:
            data["limitations"] = [f"Extended fault registers not read for CPUID part {part:#x}"]
        data["assessment"] = (
            "fault_observed"
            if any(f["severity"] == "error" for f in data["findings"])
            else ("warning" if data["findings"] else "no_fault_evidence_in_snapshot")
        )
    except Exception as exc:
        data["assessment"] = "unknown"
        data["errors"].append(str(exc))
    finally:
        if requested_halt:
            try:
                if link.halted():
                    link.restart(skip_breakpoints=False)
                if link.halted():
                    raise RuntimeError("CPU remained halted after snapshot")
            except Exception as exc:
                data["assessment"] = "unknown"
                data["errors"].append(f"Failed to restore running state: {exc}")
    return data


def scheduling_findings(
    analysis: dict[str, Any], ready_latency_us: float = 10000, isr_budget_percent: float = 30.0
) -> dict[str, Any]:
    """Produce scoped scheduling findings / 产生受观测范围约束的调度反馈."""
    findings = []
    quality = analysis.get("quality", {})
    unreliable = any(quality.get(key, 0) for key in ("lost_events", "decode_errors", "discarded_bytes"))
    quality_warnings = quality.get("warnings", [])
    if quality_warnings:
        findings.append({"code": "TRACE_QUALITY", "severity": "warning", "evidence": quality})
    events = analysis.get("events", [])
    ready = {}
    latencies = []
    current_segment = None
    hz = analysis.get("timestamp_hz")
    for event in events:
        segment = event.get("segment")
        if segment != current_segment or event["name"] in ("sync", "overflow", "trace_start", "trace_stop"):
            ready.clear()
            current_segment = segment
        values = event.get("values", [])
        name = event["name"]
        if name == "task_start_ready":
            ready.setdefault(values[0], event["ticks"])
        elif name == "task_stop_ready":
            ready.pop(values[0], None)
        elif name == "task_start_exec" and values[0] in ready:
            since = ready.pop(values[0])
            elapsed = (event["ticks"] - since) * 1e6 / hz if hz else None
            latencies.append(
                {"task_wire_id": values[0], "latency_us": elapsed, "latency_ticks": event["ticks"] - since}
            )
    for latency in latencies:
        if latency["latency_us"] is not None and latency["latency_us"] > ready_latency_us:
            findings.append(
                {
                    "code": "READY_LATENCY_BUDGET_EXCEEDED",
                    "severity": "warning",
                    "evidence": dict(latency, budget_us=ready_latency_us),
                    "message": "Observed ready-to-running delay exceeds configured budget; cause unproven",
                }
            )
    irq_percent = sum(
        value["percent_of_window"] for key, value in analysis.get("contexts", {}).items() if key.startswith("isr:")
    )
    if irq_percent > isr_budget_percent:
        findings.append(
            {
                "code": "ISR_BUDGET_EXCEEDED",
                "severity": "warning",
                "evidence": {"observed_percent": irq_percent, "budget_percent": isr_budget_percent},
                "message": "ISR occupancy exceeds configured budget",
            }
        )
    enough = (
        analysis.get("event_count", 0) > 1
        and bool(analysis.get("window_ticks"))
        and bool(analysis.get("event_counts", {}).get("task_start_exec"))
    )
    return {
        "assessment": (
            "insufficient_data"
            if unreliable or not enough
            else ("risk_observed" if findings else "no_anomaly_observed_in_window")
        ),
        "findings": findings,
        "ready_latency_samples": len(latencies),
        "max_ready_latency_us": max((v["latency_us"] for v in latencies if v["latency_us"] is not None), default=None),
        "limitations": [
            "No proof of absence of deadlock or starvation",
            "Budgets are configurable heuristics, not RTOS correctness proofs",
            "Ready-to-running intervals need matching events within this observation window",
        ],
    }
