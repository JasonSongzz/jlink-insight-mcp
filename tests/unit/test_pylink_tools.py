"""Verify public tools against actual pylink API shapes / 验证真实 pylink API 调用约定."""

from types import SimpleNamespace
from unittest.mock import Mock

import pylink
import pytest

from jlink_mcp.tools import debug, flash, memory, svd

pytestmark = pytest.mark.unit


@pytest.fixture
def link(monkeypatch):
    link = Mock(spec=pylink.JLink)
    link.halted.return_value = True
    names = {0: "R0", 13: "R13 (SP)", 14: "R14", 15: "R15 (PC)"}
    link.register_list.return_value = list(names)
    link.register_name.side_effect = names.__getitem__
    link.register_read.return_value = 0x08000100
    for module in (debug, flash, memory, svd):
        monkeypatch.setattr(module.jlink_manager, "get_jlink", lambda: link)
    return link


def test_register_name_is_resolved_to_integer_index(link):
    assert memory.read_registers(["PC"])["success"]
    link.register_read.assert_called_with(15)
    assert memory.write_register("SP", 0x20001000)["success"]
    link.register_write.assert_called_once_with(13, 0x20001000)


def test_memory_width_and_little_endian_bytes(link):
    link.memory_read.return_value = [0x04030201]
    assert memory.read_memory(0x20000000, 4, 32)["data"] == [1, 2, 3, 4]
    link.memory_read.assert_called_once_with(0x20000000, 1, nbits=32)
    link.memory_write.return_value = 1
    assert memory.write_memory(0x20000000, b"\x01\x02\x03\x04", 32)["success"]
    link.memory_write.assert_called_once_with(0x20000000, [0x04030201], nbits=32)


def test_debug_register_access_refuses_running_target(link):
    link.halted.return_value = False
    assert not memory.read_memory(0x20000000, 4)["success"]
    assert not memory.write_register("PC", 0x08000000)["success"]
    link.register_write.assert_not_called()
    link.halt.assert_not_called()


def test_breakpoint_and_reset_use_supported_api(link):
    link.breakpoint_set.return_value = 1
    assert debug.set_breakpoint(0x08000100)["handle"] == 1
    link.breakpoint_set.assert_called_once_with(0x08000100, thumb=True)
    link.breakpoint_find.return_value = 1
    assert debug.clear_breakpoint(0x08000100)["success"]
    link.breakpoint_clear.assert_called_once_with(1)
    assert debug.reset_target("halt")["success"]
    link.reset.assert_called_once_with(halt=True)


def test_flash_verify_failure_is_reported(link):
    link.memory_read8.return_value = [0, 0, 0, 0]
    assert not flash.program_flash(0x08000000, b"1234")["success"]
    link.flash.assert_called_once_with(b"1234", 0x08000000)


def test_svd_respects_register_access_width(link, monkeypatch):
    register = SimpleNamespace(
        size=32, access="read-only", address_offset=0x10, fields=[], description="status", reset_value=0
    )
    manager = SimpleNamespace(
        is_available=lambda: True,
        get_register=lambda *args: register,
        get_peripheral=lambda *args: SimpleNamespace(base_address=0x40000000),
    )
    monkeypatch.setattr(svd, "svd_manager", manager)
    link.memory_read.return_value = [42]
    assert svd.read_register_with_fields("STM32", "GPIO", "STATUS")["raw_value"] == 42
    link.memory_read.assert_called_once_with(0x40000010, 1, nbits=32)
