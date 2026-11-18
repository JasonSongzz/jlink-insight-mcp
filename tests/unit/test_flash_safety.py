"""Flash erase safety gates / Flash 擦除安全闸测试."""

import pytest

from jlink_mcp.exceptions import JLinkMCPError
from jlink_mcp.tools.flash import erase_flash

pytestmark = pytest.mark.unit


def test_chip_erase_requires_confirm():
    """整片擦除会连 Bootloader 一起抹掉，必须显式确认。"""
    with pytest.raises(JLinkMCPError) as exc:
        erase_flash(chip_erase=True)
    msg = str(exc.value)
    assert "confirm" in msg.lower()
    assert "bootloader" in msg.lower()


def test_range_erase_requires_confirm():
    """范围擦除同样不可逆，必须显式确认。"""
    with pytest.raises(JLinkMCPError) as exc:
        erase_flash(start_address=0x08020000, end_address=0x08022000)
    assert "confirm" in str(exc.value).lower()


def test_erase_without_arguments_requires_confirm():
    """没有任何参数时也不能悄悄进入擦除流程。"""
    with pytest.raises(JLinkMCPError):
        erase_flash()
