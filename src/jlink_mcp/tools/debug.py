"""Debug Control Tool Functions / 调试控制工具函数."""

from typing import Dict, Any, List, Optional

import pylink

from ..jlink_manager import jlink_manager
from ..hardware_access import hardware_tool
from ..pylink_compat import register_index, require_halted
from ..exceptions import JLinkMCPError, JLinkErrorCode
from ..models.operations import DebugBreakpoint, CPUState
from ..utils import logger


@hardware_tool
def reset_target(reset_type: str = "normal") -> Dict[str, Any]:
    """复位目标芯片.

    Args:
        reset_type: 复位类型
            - normal: 普通复位
            - halt: 复位后暂停
            - core: 内核复位

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - reset_type: 复位类型
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        if reset_type not in ("normal", "halt", "core"):
            raise ValueError("reset_type must be normal/halt/core")
        if reset_type == "core":
            raise ValueError("Core-only reset is not supported by this tool; use normal or halt")
        jlink.reset(halt=(reset_type == "halt"))

        return {
            "success": True,
            "reset_type": reset_type,
            "message": f"目标已复位（{reset_type}）"
        }
    except JLinkMCPError as e:
        logger.error(f"复位失败: {e}")
        return {
            "success": False,
            "reset_type": reset_type,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"复位失败: {e}")
        return {
            "success": False,
            "reset_type": reset_type,
            "error": {
                "code": JLinkErrorCode.RESET_FAILED.value[0],
                "description": str(e),
                "suggestion": "请检查目标芯片连接和供电状态"
            }
        }


@hardware_tool
def halt_cpu() -> Dict[str, Any]:
    """暂停 CPU.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - pc: 程序计数器值
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()
        jlink.halt()

        pc = jlink.register_read(register_index(jlink, "R15 (PC)"))
        logger.info(f"CPU 已暂停，PC = {pc:#x}")

        return {
            "success": True,
            "pc": pc,
            "message": f"CPU 已暂停，PC = {pc:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"暂停 CPU 失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"暂停 CPU 失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查目标是否正在运行"
            }
        }


@hardware_tool
def run_cpu() -> Dict[str, Any]:
    """运行 CPU.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        try:
            if hasattr(jlink, "halted") and not jlink.halted():
                logger.info("CPU 已在运行，无需恢复")
                return {
                    "success": True,
                    "message": "CPU 已在运行"
                }
        except Exception:
            logger.debug("无法预先判断 CPU 是否已暂停，继续尝试恢复运行")

        if hasattr(jlink, "restart"):
            restarted = jlink.restart(skip_breakpoints=True)
            if restarted:
                logger.info("CPU 已恢复运行")
                return {
                    "success": True,
                    "message": "CPU 已恢复运行"
                }

            if hasattr(jlink, "halted") and not jlink.halted():
                logger.info("CPU 已恢复运行")
                return {
                    "success": True,
                    "message": "CPU 已恢复运行"
                }

        raise RuntimeError(
            "当前 pylink 版本不支持在不复位的情况下继续运行目标，请升级 pylink-square 或改用 reset_target"
        )
    except JLinkMCPError as e:
        logger.error(f"运行 CPU 失败: {e}")
        return {
            "success": False,
            "error": e.to_dict()
        }
    except Exception as e:
        error_msg = str(e)
        logger.error(f"运行 CPU 失败: {error_msg}")
        return {
            "success": False,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": error_msg,
                "suggestion": "请检查目标芯片状态；如需重新启动程序请使用 reset_target，而非 run_cpu"
            }
        }


@hardware_tool
def step_instruction() -> Dict[str, Any]:
    """单步执行一条指令.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - pc: 程序计数器值
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()
        require_halted(jlink)
        jlink.step()

        pc = jlink.register_read(register_index(jlink, "R15 (PC)"))
        logger.info(f"单步执行，PC = {pc:#x}")

        return {
            "success": True,
            "pc": pc,
            "message": f"单步执行完成，PC = {pc:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"单步执行失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"单步执行失败: {e}")
        return {
            "success": False,
            "pc": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请确保目标已暂停"
            }
        }


@hardware_tool
def get_cpu_state() -> Dict[str, Any]:
    """获取 CPU 状态.

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - running: 是否正在运行
        - halted: 是否已暂停
        - pc: 程序计数器
        - lr: 链接寄存器
        - sp: 堆栈指针
    """
    try:
        jlink = jlink_manager.get_jlink()

        # 检查运行状态
        halted = jlink.halted()
        running = not halted

        pc = lr = sp = None
        if halted:
            try:
                pc = jlink.register_read(register_index(jlink, "R15 (PC)"))
                lr = jlink.register_read(register_index(jlink, "R14"))  # LR 寄存器名称是 R14，不是 R14 (LR)
                sp = jlink.register_read(register_index(jlink, "R13 (SP)"))
            except Exception as e:
                logger.warning(f"读取寄存器失败: {e}")

        logger.info(f"CPU 状态: {'运行' if running else '暂停'}")
        return {
            "success": True,
            "running": running,
            "halted": halted,
            "pc": pc,
            "lr": lr,
            "sp": sp,
            "message": f"CPU 状态: {'运行中' if running else '已暂停'}"
        }
    except JLinkMCPError as e:
        logger.error(f"获取 CPU 状态失败: {e}")
        return {
            "success": False,
            "running": False,
            "halted": False,
            "pc": None,
            "lr": None,
            "sp": None,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"获取 CPU 状态失败: {e}")
        return {
            "success": False,
            "running": False,
            "halted": False,
            "pc": None,
            "lr": None,
            "sp": None,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查设备连接状态"
            }
        }


@hardware_tool
def set_breakpoint(address: int) -> Dict[str, Any]:
    """设置断点.

    Args:
        address: 断点地址

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - address: 断点地址
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        # 确保目标已暂停
        if not jlink.halted():
            raise JLinkMCPError(
                JLinkErrorCode.TARGET_RUNNING,
                "目标正在运行，无法设置断点",
                "请先调用 halt_cpu 暂停目标"
            )

        # 设置断点
        handle = jlink.breakpoint_set(address, thumb=True)

        logger.info(f"断点已设置: {address:#x}")
        return {
            "success": True,
            "address": address,
            "handle": handle,
            "message": f"断点已设置: {address:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"设置断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"设置断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否有效，是否已达到断点数量限制"
            }
        }


@hardware_tool
def clear_breakpoint(address: int) -> Dict[str, Any]:
    """清除断点.

    Args:
        address: 断点地址

    Returns:
        包含以下字段的字典:
        - success: 是否成功
        - address: 断点地址
        - message: 状态信息
    """
    try:
        jlink = jlink_manager.get_jlink()

        # 清除断点
        handle = jlink.breakpoint_find(address)
        if handle == 0:
            raise ValueError("No breakpoint found at this address")
        jlink.breakpoint_clear(handle)

        logger.info(f"断点已清除: {address:#x}")
        return {
            "success": True,
            "address": address,
            "message": f"断点已清除: {address:#x}"
        }
    except JLinkMCPError as e:
        logger.error(f"清除断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": e.to_dict()
        }
    except Exception as e:
        logger.error(f"清除断点失败: {e}")
        return {
            "success": False,
            "address": address,
            "error": {
                "code": JLinkErrorCode.UNKNOWN_ERROR.value[0],
                "description": str(e),
                "suggestion": "请检查地址是否正确"
            }
        }
