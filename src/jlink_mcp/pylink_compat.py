"""Resolve public register names to pylink indices / 将寄存器名称解析为 pylink 索引."""

from __future__ import annotations

from typing import Any


def register_index(link: Any, name: str) -> int:
    """Resolve PC/SP/LR aliases using DLL register metadata / 通过 DLL 元数据解析别名."""
    aliases = {"PC": "R15", "SP": "R13", "LR": "R14"}
    wanted = name.upper().split(" ")[0]
    wanted = aliases.get(wanted, wanted)
    for index in link.register_list():
        actual = link.register_name(index).upper().split(" ")[0]
        if aliases.get(actual, actual) == wanted:
            return int(index)
    raise ValueError(f"Register unavailable: {name}")


def require_halted(link: Any) -> None:
    """Require a halted target for an explicit debug operation / 显式调试操作要求目标暂停."""
    if not link.halted():
        raise RuntimeError("Target running; call halt_cpu before register/memory debug access")
