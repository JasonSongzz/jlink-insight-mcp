"""Inspect CubeMX sources and linked firmware / 检查 CubeMX 工程及已链接固件.

Detection reports evidence and uncertainty. No build scripts are executed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".cache", "__pycache__"}
MAX_FILE = 2 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024


def _inside(root: Path, path: str | Path) -> Path:
    candidate = Path(path)
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path outside project: {path}")
    return candidate


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_FILE:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _files(root: Path) -> list[Path]:
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if not path.is_symlink():
                result.append(path)
                if len(result) > 30000:
                    raise ValueError("Project exceeds 30000 files; select a smaller project root")
    return result


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)


def read_elf(path: Path) -> dict[str, Any]:
    """Read ARM ELF symbols and load ranges / 读取 ARM ELF 符号和加载范围."""
    if path.stat().st_size > MAX_IMAGE:
        raise ValueError("Firmware exceeds 32 MiB")
    symbols: dict[str, int] = {}
    original = path.read_bytes()
    with BytesIO(original) as stream:
        elf = ELFFile(stream)
        if elf["e_machine"] != "EM_ARM" or not elf.little_endian or elf.elfclass != 32:
            raise ValueError("Expected 32-bit little-endian ARM ELF")
        sections = []
        for section_name in (".symtab", ".dynsym"):
            section = elf.get_section_by_name(section_name)
            if section is not None:
                for symbol in section.iter_symbols():
                    if symbol["st_shndx"] not in ("SHN_UNDEF", "SHN_COMMON") and symbol.name:
                        symbols[symbol.name] = int(symbol["st_value"])
        for segment in elf.iter_segments():
            if segment["p_type"] == "PT_LOAD" and segment["p_filesz"]:
                sections.append({"address": int(segment["p_paddr"]), "size": int(segment["p_filesz"])})
        entry = int(elf["e_entry"])
    return {"entry": entry, "load_ranges": sections, "symbols": symbols, "sha256": hashlib.sha256(original).hexdigest()}


def inspect_project_data(
    project_path: str, firmware_path: str | None = None, ioc_path: str | None = None
) -> dict[str, Any]:
    """Inspect project without altering it / 只读检查工程并返回能力证据."""
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("project_path must be an existing directory on the MCP host")
    files = _files(root)

    def rel(p: Path) -> str:
        return str(p.relative_to(root)).replace("\\", "/")

    warnings = []
    manifest_file = root / "jlink-project.json"
    manifest = json.loads(_read(manifest_file)) if manifest_file.exists() else {}
    if not isinstance(manifest, dict):
        raise ValueError("jlink-project.json must be an object")
    iocs = [p for p in files if p.suffix.lower() == ".ioc"]
    selected_ioc = (
        _inside(root, ioc_path or manifest["ioc"])
        if (ioc_path or manifest.get("ioc"))
        else (iocs[0] if len(iocs) == 1 else None)
    )
    if len(iocs) > 1 and selected_ioc is None:
        warnings.append("Multiple .ioc files; select ioc_path or configure manifest.ioc")
    config = {}
    if selected_ioc:
        for line in _read(selected_ioc).splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    raw_mcu = config.get("Mcu.CPN") or config.get("Mcu.Name")
    name = config.get("Mcu.Name", "")
    chip = manifest.get("chip_name")
    if not chip:
        if re.fullmatch(r"STM32[A-Z0-9]+[TUVYH]x", name):
            chip = name[:-2]
        elif re.fullmatch(r"STM32[A-Z0-9]+[A-Z][0-9]", config.get("Mcu.CPN", "")):
            chip = config["Mcu.CPN"][:-2]
    candidates = [p for p in files if p.suffix.lower() in (".elf", ".axf", ".hex", ".bin")]
    preferred = [p for p in candidates if p.suffix.lower() in (".elf", ".axf")]
    artifact = firmware_path or manifest.get("firmware")
    firmware = (
        _inside(root, artifact)
        if artifact
        else (preferred[0] if len(preferred) == 1 else (candidates[0] if len(candidates) == 1 else None))
    )
    if firmware is None:
        warnings.append("No unique firmware artifact; pass firmware_path or configure manifest.firmware")
    elif not firmware.is_file():
        raise ValueError(f"Firmware does not exist: {firmware}")
    symbols = {}
    elf_info = None
    if firmware and firmware.suffix.lower() in (".elf", ".axf"):
        elf_info = read_elf(firmware)
        symbols = elf_info["symbols"]
    compile_files = set()
    for db in [p for p in files if p.name == "compile_commands.json"]:
        if db.stat().st_size > 16 * 1024 * 1024:
            warnings.append(f"Skipped oversized compile database: {rel(db)}")
            continue
        for entry in json.loads(db.read_text(encoding="utf-8")):
            source = Path(entry.get("file", ""))
            if not source.is_absolute():
                source = Path(entry.get("directory", str(db.parent))) / source
            compile_files.add(str(source.resolve()))
    source_evidence = {"rtos": [], "rtt": [], "systemview": [], "init_calls": [], "trace_hooks": []}
    versions = set()
    for path in files:
        if path.suffix.lower() not in (".c", ".h", ".cpp", ".hpp"):
            continue
        filename = path.name.lower()
        if filename in ("tasks.c", "freertos.h", "freertosconfig.h", "task.h"):
            source_evidence["rtos"].append(rel(path))
        if filename == "segger_rtt.c":
            source_evidence["rtt"].append(rel(path))
        if filename == "segger_sysview.c":
            source_evidence["systemview"].append(rel(path))
        # Limit reads to relevant kernel/config and application sources.
        if filename in ("task.h", "freertos.h", "freertosconfig.h") or filename.endswith(".c"):
            text = _strip_comments(_read(path))
            version = re.search(r'#\s*define\s+tskKERNEL_VERSION_NUMBER\s+"([^"]+)"', text)
            if version:
                versions.add(version[1])
            if filename != "segger_sysview.c" and re.search(r"\bSEGGER_SYSVIEW_Conf\s*\(\s*\)\s*;", text):
                source_evidence["init_calls"].append(rel(path))
            if re.search(r"#\s*define\s+traceTASK_SWITCHED_IN\b|#\s*include\s+[\"<]SEGGER_SYSVIEW_FreeRTOS.h", text):
                source_evidence["trace_hooks"].append(rel(path))
    rtos_linked = any(k in symbols for k in ("vTaskStartScheduler", "xTaskCreate", "vTaskSwitchContext"))
    rtt_linked = "_SEGGER_RTT" in symbols
    sv_linked = all(k in symbols for k in ("SEGGER_SYSVIEW_Init", "SEGGER_SYSVIEW_Start"))
    hooks_linked = "SEGGER_SYSVIEW_OnTaskStartExec" in symbols
    version = next(iter(versions)) if len(versions) == 1 else None
    if len(versions) > 1:
        warnings.append("Multiple FreeRTOS versions in sources; linked kernel version is uncertain")
    source_presence = {k: bool(v) for k, v in source_evidence.items()}
    # A candidate still requires runtime SysView channel + synchronization validation.
    sv_candidate = rtos_linked and rtt_linked and sv_linked and hooks_linked
    if not symbols:
        warnings.append("ELF symbols unavailable; source presence does not prove linked/runtime capability")
    if not chip:
        warnings.append("J-Link chip name unresolved; explicitly configure chip_name before deployment")
    if compile_files:
        built_sv = [p for p in source_evidence["systemview"] if str((root / p).resolve()) in compile_files]
    else:
        built_sv = []
    firmware_info = None
    if firmware:
        if firmware.stat().st_size > MAX_IMAGE:
            raise ValueError("Firmware exceeds 32 MiB")
        firmware_info = {
            "path": str(firmware),
            "relative_path": rel(firmware),
            "sha256": elf_info["sha256"] if elf_info else hashlib.sha256(firmware.read_bytes()).hexdigest(),
            "size": firmware.stat().st_size,
            "format": firmware.suffix.lower()[1:],
        }
        if elf_info:
            firmware_info.update(entry=elf_info["entry"], load_ranges=elf_info["load_ranges"])
    return {
        "project_path": str(root),
        "ioc": rel(selected_ioc) if selected_ioc else None,
        "ioc_candidates": [rel(p) for p in iocs],
        "mcu": raw_mcu,
        "chip_name": chip,
        "firmware": firmware_info,
        "firmware_candidates": [rel(p) for p in candidates],
        "rtos": {
            "name": "FreeRTOS" if source_presence["rtos"] or rtos_linked else None,
            "version": version,
            "version_source": "source_headers",
            "version_verified_from_firmware": False,
            "linked": rtos_linked,
            "cmsis_wrapper": (
                "v2"
                if any(p.name == "cmsis_os2.c" for p in files)
                else ("v1" if any(p.name == "cmsis_os.c" for p in files) else None)
            ),
        },
        "capabilities": {
            "basic_debug": True,
            "rtt_candidate": rtt_linked,
            "systemview_candidate": sv_candidate,
            "systemview_requires_runtime_validation": True,
        },
        "evidence": {
            "sources": source_evidence,
            "systemview_compilation_units": built_sv,
            "linked_symbols": {
                key: symbols[key]
                for key in (
                    "_SEGGER_RTT",
                    "vTaskStartScheduler",
                    "SEGGER_SYSVIEW_Init",
                    "SEGGER_SYSVIEW_Start",
                    "SEGGER_SYSVIEW_OnTaskStartExec",
                    "HardFault_Handler",
                    "vApplicationStackOverflowHook",
                    "vApplicationMallocFailedHook",
                )
                if key in symbols
            },
        },
        "warnings": warnings,
        "manifest": manifest,
    }
