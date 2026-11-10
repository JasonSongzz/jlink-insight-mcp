"""Project-aware deployment tests without hardware / 无硬件的工程部署测试."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jlink_mcp.firmware import flash_verified, load_image
from jlink_mcp.project_inspector import inspect_project_data, read_elf
from jlink_mcp.tools import workflow

pytestmark = pytest.mark.unit


def make_elf(
    path: Path, symbols: dict[str, int] | None = None, payload: bytes = b"\x00\x20\x00\x20\x09\x00\x00\x08"
) -> None:
    """Construct a real ELF32 fixture / 构造真实 ELF32 文件格式测试数据."""
    symbols = symbols or {}
    strings = bytearray(b"\0")
    entries = [b"\0" * 16]
    for name, value in symbols.items():
        offset = len(strings)
        strings.extend(name.encode() + b"\0")
        entries.append(struct.pack("<IIIBBH", offset, value, 2, 0x12, 0, 1))
    symtab = b"".join(entries)
    names = b"\0.text\0.symtab\0.strtab\0.shstrtab\0"
    text_offset = 0x100
    sym_offset = text_offset + len(payload)
    str_offset = sym_offset + len(symtab)
    names_offset = str_offset + len(strings)
    sh_offset = (names_offset + len(names) + 3) & ~3
    header = struct.pack(
        "<16sHHIIIIIHHHHHH",
        b"\x7fELF\x01\x01\x01" + b"\0" * 9,
        2,
        40,
        1,
        0x08000009,
        52,
        sh_offset,
        0x05000000,
        52,
        32,
        1,
        40,
        5,
        4,
    )
    program = struct.pack("<IIIIIIII", 1, text_offset, 0x08000000, 0x08000000, len(payload), len(payload), 5, 4)
    raw = bytearray(header + program)
    raw.extend(b"\0" * (text_offset - len(raw)))
    raw.extend(payload + symtab + strings + names)
    raw.extend(b"\0" * (sh_offset - len(raw)))
    sections = [
        b"\0" * 40,
        struct.pack("<IIIIIIIIII", 1, 1, 6, 0x08000000, text_offset, len(payload), 0, 0, 4, 0),
        struct.pack("<IIIIIIIIII", 7, 2, 0, 0, sym_offset, len(symtab), 3, 1, 4, 16),
        struct.pack("<IIIIIIIIII", 15, 3, 0, 0, str_offset, len(strings), 0, 0, 1, 0),
        struct.pack("<IIIIIIIIII", 23, 3, 0, 0, names_offset, len(names), 0, 0, 1, 0),
    ]
    raw.extend(b"".join(sections))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "demo.ioc").write_text("Mcu.Name=STM32F407VGTx\nMcu.CPN=STM32F407VGT6\n")
    (tmp_path / "task.h").write_text('#define tskKERNEL_VERSION_NUMBER "V10.4.6"\n')
    (tmp_path / "SEGGER_SYSVIEW.c").write_text("void SEGGER_SYSVIEW_Init(void) {}")
    (tmp_path / "SEGGER_RTT.c").write_text("int RTT;")
    (tmp_path / "main.c").write_text("int main(void) { SEGGER_SYSVIEW_Conf(); }")
    (tmp_path / "FreeRTOSConfig.h").write_text('#include "SEGGER_SYSVIEW_FreeRTOS.h"')
    make_elf(
        tmp_path / "build" / "demo.elf",
        {
            "vTaskStartScheduler": 0x08000101,
            "_SEGGER_RTT": 0x20000100,
            "SEGGER_SYSVIEW_Init": 0x08000201,
            "SEGGER_SYSVIEW_Start": 0x08000301,
            "SEGGER_SYSVIEW_OnTaskStartExec": 0x08000401,
        },
    )
    return tmp_path


def test_real_elf_and_project_detection(project):
    report = inspect_project_data(str(project))
    assert report["chip_name"] == "STM32F407VG"
    assert report["rtos"]["version"] == "V10.4.6"
    assert report["rtos"]["linked"]
    assert report["capabilities"]["systemview_candidate"]
    assert report["evidence"]["linked_symbols"]["_SEGGER_RTT"] == 0x20000100
    assert read_elf(project / "build/demo.elf")["load_ranges"][0]["address"] == 0x08000000


def test_dead_sources_do_not_enable_capability(project):
    make_elf(project / "build/demo.elf")
    report = inspect_project_data(str(project))
    assert report["evidence"]["sources"]["systemview"]
    assert not report["capabilities"]["systemview_candidate"]
    assert not report["capabilities"]["rtt_candidate"]


def test_multiple_artifacts_and_ioc_are_not_guessed(project):
    make_elf(project / "build/other.elf")
    (project / "other.ioc").write_text("Mcu.Name=STM32F103C8Tx")
    report = inspect_project_data(str(project))
    assert report["firmware"] is None
    assert report["chip_name"] is None
    selected = inspect_project_data(str(project), "build/demo.elf", "demo.ioc")
    assert selected["chip_name"] == "STM32F407VG"


def test_manifest_resolves_ambiguity(project):
    (project / "jlink-project.json").write_text(json.dumps({"chip_name": "STM32F407VG", "firmware": "build/demo.elf"}))
    assert inspect_project_data(str(project))["firmware"]["relative_path"] == "build/demo.elf"


def test_path_escape_and_missing_firmware_are_rejected(project):
    with pytest.raises(ValueError, match="outside"):
        inspect_project_data(str(project), "../other.elf")
    with pytest.raises(ValueError, match="does not exist"):
        inspect_project_data(str(project), "missing.elf")


def test_bin_requires_address_and_internal_flash(tmp_path):
    path = tmp_path / "demo.bin"
    path.write_bytes(b"\x01\x02")
    with pytest.raises(ValueError, match="explicit"):
        load_image(str(path))
    with pytest.raises(ValueError, match="outside"):
        load_image(str(path), 0x20000000)
    assert load_image(str(path), 0x08000000)[0] == [(0x08000000, b"\x01\x02")]


def test_hex_and_elf_load_addresses(project):
    from intelhex import IntelHex

    hexfile = project / "firmware.hex"
    image = IntelHex()
    image[0x08000100] = 42
    image.write_hex_file(str(hexfile))
    assert load_image(str(hexfile))[0] == [(0x08000100, b"*")]
    segments, _ = load_image(str(project / "build/demo.elf"))
    assert segments[0][0] == 0x08000000
    assert len(segments[0][1]) == 8


def test_verification_failure_is_not_success():
    link = Mock()
    link.halted.return_value = True
    link.memory_read8.return_value = [0, 2]
    with pytest.raises(RuntimeError, match="verification failed"):
        flash_verified(link, [(0x08000000, b"\x01\x02")])
    link.reset.assert_not_called()
    link.restart.assert_not_called()


def test_deploy_verifies_then_runs(project, monkeypatch):
    link = Mock()
    link.halted.side_effect = [True, True, False]
    link.memory_read8.return_value = list(b"\x00\x20\x00\x20\x09\x00\x00\x08")
    manager = SimpleNamespace(
        is_connected=True, requested_chip_name="STM32F407VG", connection_mode="native", get_jlink=lambda: link
    )
    monkeypatch.setattr(workflow, "jlink_manager", manager)
    result = workflow.deploy_project(str(project), monitor=False)
    assert result["success"]
    assert result["data"]["flash"]["verified"]
    link.reset.assert_called_once_with(halt=True)
    link.restart.assert_called_once_with(skip_breakpoints=False)
    assert [call[0] for call in link.method_calls].index("memory_read8") < [
        call[0] for call in link.method_calls
    ].index("reset")


def test_deploy_refuses_generic_core_and_wrong_target(project, monkeypatch):
    manager = SimpleNamespace(is_connected=True, requested_chip_name="STM32F407VG", connection_mode="generic")
    monkeypatch.setattr(workflow, "jlink_manager", manager)
    assert not workflow.deploy_project(str(project), monitor=False)["success"]
    manager.requested_chip_name = "STM32F103C8"
    assert not workflow.deploy_project(str(project), monitor=False)["success"]


def test_deploy_does_not_choose_first_probe(project, monkeypatch):
    manager = SimpleNamespace(is_connected=False, enumerate_devices=lambda: [Mock(), Mock()])
    monkeypatch.setattr(workflow, "jlink_manager", manager)
    result = workflow.deploy_project(str(project), monitor=False)
    assert not result["success"]
    assert "provide serial_number" in result["error"]["description"]


def test_corrupt_image_rejected_before_hardware(project, monkeypatch):
    (project / "build/demo.elf").write_bytes(b"corrupt")
    manager = Mock()
    monkeypatch.setattr(workflow, "jlink_manager", manager)
    assert not workflow.deploy_project(str(project))["success"]
    manager.get_jlink.assert_not_called()


def test_all_segments_are_verified_after_all_downloads():
    link = Mock()
    link.halted.return_value = True
    segments = [(0x08000000, b"first"), (0x08000020, b"second")]
    link.memory_read8.side_effect = [list(b"first"), list(b"second")]
    assert flash_verified(link, segments)["verified"]
    calls = [call[0] for call in link.method_calls]
    assert max(i for i, name in enumerate(calls) if name == "flash") < min(
        i for i, name in enumerate(calls) if name == "memory_read8"
    )
