"""Portable build / flash / debug workflow tools / 可移植的编译-烧录-调试工作流工具.

Design goals / 设计目标
----------------------
* **No machine-specific paths in code** / 代码里不写死任何机器路径：
  工具链按「manifest 覆盖 → 环境变量 → PATH(which) → 兄弟目录推导 →
  常见安装目录 glob」的顺序自动发现。
* **Manifest driven** / 以工程根目录的 ``jlink-project.json`` 为唯一工程描述，
  换机器只需改 manifest 或设 ``EMBEDDED_*`` 环境变量。
* **Evidence first** / 每一步都产出可判读证据：完整编译器输出、ELF SHA256、
  Flash/RAM 占用、compile_commands.json、烧录校验结果、RTT/故障快照。
* **Safe by default** / 默认不碰硬件：``workflow_pipeline`` 的 ``flash`` 默认关闭，
  且打开时必须 ``confirm=True``。stdout 只留给 MCP 协议，子进程一律
  ``capture_output``，日志走 stderr。
* **No shell** / 全部用 argv 列表调用，``shell=False``。
"""

from __future__ import annotations

import ast
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from ..project_inspector import inspect_project_data
from ..utils import logger

# 响应结构版本：CI 侧可据此做稳定解析；结构变更时递增。
SCHEMA_VERSION = 1

# 环境变量中属于「凭据」的名字特征（与 dsh-mcp-client 的清洗规则保持一致）。
_SECRET_NAME = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)

# 同一个工程目录的构建/测试必须串行，否则 ninja 会在同一 build 目录互相踩踏。
_PROJECT_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()

# GDB command allow-list / GDB 命令白名单
#
# WHY ALLOW-LIST INSTEAD OF DENY-LIST / 为什么用白名单而不是黑名单
# ------------------------------------------------------------------
# GDB's command surface is far too large to enumerate safely. A deny-list only
# blocks the commands someone thought of. These all escaped the previous
# deny-list and each one runs host code or touches arbitrary files:
#   eval "shell ..."            -> eval forwards to any command
#   compile code ... end        -> compiles and runs arbitrary C on the HOST
#   interpreter-exec console .. -> MI bridge to any CLI command
#   pipe <cmd> | shell ...      -> GDB supports pipes
#   call system("...")          -> calls a host/target function
#   p (int)system("...")        -> expression evaluation can invoke calls
#   def / d e f i n e           -> GDB accepts unambiguous abbreviations
#
# So this list enumerates what IS allowed; anything else is rejected.
# Match rule: exact, or "<entry> " prefix, or "<entry>(" prefix.
_ALLOWED_GDB_COMMANDS = (
    # connection / session setup (used by the generated default sequence)
    "set pagination",
    "set confirm",
    "set print",
    "set width",
    "set height",
    "set listsize",
    "set disassembly-flavor",
    "target remote",
    "target extended-remote",
    "monitor reset",
    "monitor halt",
    "monitor go",
    "monitor sleep",
    "monitor speed",
    # read-only inspection
    "bt",
    "backtrace",
    "where",
    "info",
    "frame",
    "thread",
    "disassemble",
    "list",
    "print",
    "ptype",
    "whatis",
    "show",
    "help",
    "maintenance info",
    # controlled execution / breakpoints
    "break",
    "tbreak",
    "delete breakpoints",
    "disable",
    "enable",
    "clear",
    "continue",
    "next",
    "step",
    "finish",
    "until",
    "detach",
    "quit",
)

# Memory/examine forms are allowed but need their own handling because they start
# with "x" followed by a count/format specifier (x/16wx, x 0x20000000).
# Leading command names derived from the allow-list / 由白名单派生的命令名集合
_ALLOWED_GDB_NAMES = {
    e.split(" ", 1)[0] for e in _ALLOWED_GDB_COMMANDS
} | {"x", "p"}

_ALLOWED_GDB_EXAMINE_PREFIX = "x"

# GDB options that make it load or execute something we did not ask for.
# -nx / -nh are mandatory: a .gdbinit in the working directory is arbitrary
# host code execution.
_GDB_FORBIDDEN_FLAGS = ("-x", "-iex", "-ex-script", "--command", "--init-command")

# Flags this tool sets itself.
_GDB_SAFE_FLAGS = ("-q", "-batch", "-nx", "-nh")


# Expressions may not invoke functions. GDB's `set may-call-functions` is forced
# off below, but reject call syntax as well so an injection attempt is reported
# explicitly instead of silently evaluating to an error inside GDB.
_CALL_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\(")


def _gdb_has_function_call(command: str) -> bool:
    """True when an expression command tries to call a function / 是否含函数调用."""
    low = " ".join(command.strip().lower().split())
    head = low.split("/", 1)[0]
    name = head.split(" ", 1)[0].strip()
    # Only expression/evaluate commands can embed calls worth blocking; the
    # allow-list already rejects shell/eval/compile/python.
    if name not in {"p", "print", "x", "call", "whatis", "ptype"}:
        return False
    # Allow the harmless cast form "(type)" but block "ident(" calls.
    body = low.split(" ", 1)[1] if " " in low else ""
    # strip format suffix remnants like "/x"
    if "/" in body:
        body = body.split("/", 1)[-1]
    return bool(_CALL_PATTERN.search(body))


def _gdb_command_allowed(command: str) -> bool:
    """Return True when a GDB command is on the allow-list / 命令是否在白名单内.

    Matching strategy: take the command's leading NAME (text before the first
    "/" format suffix and before the first space), then require that name to be
    one of the allow-listed names. This keeps "p/x $lr", "print/x $pc" and
    "x/16wx addr" working while rejecting "py", "sh", "!" and friends.
    """
    low = " ".join(command.strip().lower().split())
    if not low:
        return False

    # GDB command name = leading token, minus any /format suffix.
    #   "p/x $lr"          -> "p"
    #   "print/x $pc"      -> "print"
    #   "x/16wx addr"      -> "x"
    #   "info registers"   -> "info"
    #   "monitor reset halt" -> "monitor"
    head = low.split("/", 1)[0]
    name = head.split(" ", 1)[0].strip()
    if not name:
        return False

    if name not in _ALLOWED_GDB_NAMES:
        return False

    # Multi-word entries ("set pagination", "target remote", "monitor reset")
    # are matched as a prefix of the whole command so "set logging" stays denied
    # even though "set" itself is allowed.
    for entry in _ALLOWED_GDB_COMMANDS:
        if " " not in entry:
            continue
        if low == entry or low.startswith(entry + " ") or low.startswith(entry + "("):
            return True
        # allow format suffix on multi-word entries, e.g. "disassemble/m main"
        if low.startswith(entry.replace(" ", "/", 1)):
            return True

    # If the name is allow-listed and no multi-word constraint applies to it,
    # accept it (covers "bt", "p", "print", "x", "info", ...).
    constrained = {e.split(" ", 1)[0] for e in _ALLOWED_GDB_COMMANDS if " " in e}
    if name in constrained:
        # The name only appears in the list with a specific argument form, and
        # none of those matched, so reject (e.g. "set logging" vs "set").
        return False
    return True

# ---------------------------------------------------------------------------
# Response wrapper / 统一响应包装
# ---------------------------------------------------------------------------


def _response(func: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"schema_version": SCHEMA_VERSION, "success": True, "data": func(*args, **kwargs)}
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成结构化结果
            logger.exception("build workflow error in %s", func.__name__)
            return {
                "schema_version": SCHEMA_VERSION,
                "success": False,
                "error": {"description": str(exc), "type": type(exc).__name__},
            }

    return wrapped


def _serialized(func: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize per-project build/test operations / 同一工程串行执行.

    ninja/cmake 共享同一个 build 目录；两个 Agent 会话并发调用会互相破坏
    build graph。锁的粒度是工程根目录，不同工程仍可并行。
    """

    @wraps(func)
    def wrapped(project_path: str, *args: Any, **kwargs: Any) -> Any:
        root = Path(project_path).expanduser().resolve()
        key = str(root)
        with _PROJECT_LOCKS_GUARD:
            lock = _PROJECT_LOCKS.setdefault(key, threading.Lock())
        with lock:
            return func(project_path, *args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# Toolchain discovery / 工具链自动发现
# ---------------------------------------------------------------------------

# 环境变量前缀用 EMBEDDED_，避开 dsh-mcp-client 会清洗的 DSH_* 与
# KEY/PASSWORD/SECRET/TOKEN 关键字。
_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "cmake": {
        "names": ["cmake"],
        "env": "EMBEDDED_CMAKE",
        "version_args": ["--version"],
        "globs": [
            "C:/Program Files/CMake/bin/cmake.exe",
            "C:/Program Files (x86)/CMake/bin/cmake.exe",
            "/usr/bin/cmake",
            "/usr/local/bin/cmake",
            "/opt/homebrew/bin/cmake",
        ],
    },
    "ninja": {
        "names": ["ninja", "ninja.exe"],
        "env": "EMBEDDED_NINJA",
        "version_args": ["--version"],
        "globs": [
            "C:/Windows/ninja.exe",
            "C:/Program Files/CMake/bin/ninja.exe",
            "/usr/bin/ninja",
            "/usr/local/bin/ninja",
            "/opt/homebrew/bin/ninja",
            "C:/ST/STM32CubeCLT_*/Ninja/bin/ninja.exe",
            "C:/ST/STM32CubeCLT_*/CMake/bin/ninja.exe",
        ],
    },
    "arm_gcc": {
        "names": ["arm-none-eabi-gcc", "arm-none-eabi-gcc.exe"],
        "env": "EMBEDDED_ARM_GCC",
        "version_args": ["--version"],
        "globs": [
            "C:/Program Files (x86)/Arm/GNU Toolchain */bin/arm-none-eabi-gcc.exe",
            "C:/Program Files/Arm/GNU Toolchain */bin/arm-none-eabi-gcc.exe",
            "C:/Program Files (x86)/GNU Arm Embedded Toolchain/*/bin/arm-none-eabi-gcc.exe",
            "C:/ST/STM32CubeCLT_*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc.exe",
            "/usr/bin/arm-none-eabi-gcc",
            "/opt/*/bin/arm-none-eabi-gcc",
            "/opt/st/stm32cubeclt*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc",
            "/Applications/ArmGNUToolchain/*/bin/arm-none-eabi-gcc",
        ],
    },
    "arm_gdb": {
        "names": ["arm-none-eabi-gdb", "arm-none-eabi-gdb.exe", "arm-none-eabi-gdb-py.exe"],
        "env": "EMBEDDED_ARM_GDB",
        "version_args": ["--version"],
        "globs": [
            "C:/Program Files (x86)/Arm/GNU Toolchain */bin/arm-none-eabi-gdb.exe",
            "C:/Program Files/Arm/GNU Toolchain */bin/arm-none-eabi-gdb.exe",
            "C:/ST/STM32CubeCLT_*/GNU-tools-for-STM32/bin/arm-none-eabi-gdb.exe",
            "/usr/bin/arm-none-eabi-gdb",
            "/opt/st/stm32cubeclt*/GNU-tools-for-STM32/bin/arm-none-eabi-gdb",
        ],
    },
    "arm_size": {
        "names": ["arm-none-eabi-size", "arm-none-eabi-size.exe"],
        "env": "EMBEDDED_ARM_SIZE",
        "version_args": ["--version"],
        "globs": [],
    },
    "arm_objcopy": {
        "names": ["arm-none-eabi-objcopy", "arm-none-eabi-objcopy.exe"],
        "env": "EMBEDDED_ARM_OBJCOPY",
        "version_args": ["--version"],
        "globs": [],
    },
    "jlink_gdbserver": {
        "names": ["JLinkGDBServer", "JLinkGDBServer.exe", "JLinkGDBServerCL.exe"],
        "env": "EMBEDDED_JLINK_GDBSERVER",
        "version_args": None,  # J-Link GDB Server 没有稳定的 -version，跳过探测
        "globs": [
            "D:/SEGGER/JLink_*/JLinkGDBServer.exe",
            "C:/Program Files/SEGGER/JLink*/JLinkGDBServer.exe",
            "C:/Program Files (x86)/SEGGER/JLink*/JLinkGDBServer.exe",
            "/opt/SEGGER/JLink*/JLinkGDBServer",
            "/usr/bin/JLinkGDBServer",
        ],
    },
}

# arm_* 工具可以从已发现的 arm-none-eabi-gcc 同目录推导，跨平台一致性最好。
_ARM_SIBLINGS = {
    "arm_gdb": ["arm-none-eabi-gdb", "arm-none-eabi-gdb-py"],
    "arm_size": ["arm-none-eabi-size"],
    "arm_objcopy": ["arm-none-eabi-objcopy"],
}


def _exe_candidates(name: str) -> list[str]:
    """Windows 下补全 .exe 后缀 / Add .exe variants on Windows."""
    if os.name == "nt" and not name.lower().endswith((".exe", ".cmd", ".bat")):
        return [name, name + ".exe"]
    return [name]


def _glob_first(patterns: list[str], basename: str | None = None) -> str | None:
    """Expand glob patterns and return the first existing file."""
    for pattern in patterns:
        try:
            for match in sorted(glob.glob(pattern)):
                if basename and Path(match).name.lower() != basename.lower():
                    continue
                if Path(match).is_file():
                    return str(Path(match).resolve())
        except (OSError, ValueError):
            continue
    return None


def _version(exe: str, args: list[str] | None) -> str:
    if not args:
        return "unknown"
    try:
        p = subprocess.run(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        text = (p.stdout or p.stderr).strip().splitlines()
        return text[0][:200] if text else f"exit={p.returncode}"
    except Exception as exc:  # noqa: BLE001
        return f"probe failed: {exc}"


def _resolve_tool(key: str, overrides: dict[str, str], found: dict[str, str]) -> dict[str, Any] | None:
    """Resolve one tool / 解析单个工具路径.

    Order / 顺序:
      1. manifest build.toolchain[key]
      2. EMBEDDED_* 环境变量
      3. PATH (shutil.which)
      4. 已发现的 arm-none-eabi-gcc 兄弟目录（仅 arm_* 工具）
      5. 常见安装目录 glob
    """
    spec = _TOOL_SPECS[key]

    def make(path: str | None, source: str) -> dict[str, Any] | None:
        if not path:
            return None
        p = Path(path).expanduser()
        if not p.is_absolute():
            resolved = shutil.which(str(p))
            p = Path(resolved) if resolved else p
        if not p.is_file():
            return None
        return {"path": str(p.resolve()), "source": source, "version": _version(str(p.resolve()), spec["version_args"])}

    # 1. manifest override
    if key in overrides:
        r = make(str(overrides[key]), "manifest")
        if r:
            return r

    # 2. env override
    env_val = os.environ.get(spec["env"])
    if env_val:
        r = make(env_val, f"env:{spec['env']}")
        if r:
            return r

    # 3. PATH
    for name in spec["names"]:
        w = shutil.which(name)
        if w:
            r = make(w, "PATH")
            if r:
                return r

    # 4. sibling of arm-none-eabi-gcc
    if key in _ARM_SIBLINGS and "arm_gcc" in found:
        parent = Path(found["arm_gcc"]).parent
        for sibling in _ARM_SIBLINGS[key]:
            for candidate in _exe_candidates(sibling):
                r = make(str(parent / candidate), "sibling")
                if r:
                    return r

    # 5. globs
    patterns = list(spec["globs"])
    expanded: list[str] = []
    for pattern in patterns:
        expanded.append(pattern)
        if "*" in pattern and not pattern.lower().endswith((".exe", "/ninja", "/cmake", "/JLinkGDBServer")):
            expanded.append(pattern.rstrip("/\\") + "/*")
    r = make(_glob_first(expanded), "glob")
    if r:
        return r
    return None


def _discover(overrides: dict[str, str]) -> dict[str, Any]:
    """Discover all tools / 发现全部工具."""
    found: dict[str, str] = {}
    tools: dict[str, Any] = {}
    # arm_gcc 先解析，供兄弟目录推导
    order = ["arm_gcc", "cmake", "ninja", "arm_gdb", "arm_size", "arm_objcopy", "jlink_gdbserver"]
    for key in order:
        item = _resolve_tool(key, overrides, found)
        tools[key] = item
        if item:
            found[key] = item["path"]
    missing = [k for k, v in tools.items() if v is None]
    return {
        "platform": platform.platform(),
        "tools": tools,
        "missing": missing,
        # 必需：cmake + ninja + gcc；gdb / gdbserver 只影响符号级调试
        "build_ready": all(tools[k] for k in ("cmake", "ninja", "arm_gcc")),
        "debug_ready": bool(tools["arm_gdb"] and tools["jlink_gdbserver"]),
    }


# ---------------------------------------------------------------------------
# Manifest helpers / manifest 读取
# ---------------------------------------------------------------------------


def _root(project_path: str) -> Path:
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project_path is not an existing directory: {root}")
    return root


def _read_manifest(root: Path) -> dict[str, Any]:
    f = root / "jlink-project.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid jlink-project.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("jlink-project.json must be a JSON object")
    return data


def _build_cfg(root: Path) -> dict[str, Any]:
    cfg = _read_manifest(root).get("build", {})
    if not isinstance(cfg, dict):
        raise ValueError("jlink-project.json: 'build' must be an object")
    return cfg


def _inside(root: Path, path: str | Path) -> Path:
    p = Path(path)
    p = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"Path outside project root: {path}")
    return p


def _find_firmware(root: Path, build_cfg: dict[str, Any], preset: str) -> Path:
    """Locate ELF / 定位 ELF：manifest 优先，其次 build/<preset>/*.elf。"""
    candidates: list[str] = []
    if build_cfg.get("firmware"):
        candidates.append(str(build_cfg["firmware"]))
    manifest_fw = _read_manifest(root).get("firmware")
    if manifest_fw:
        candidates.append(str(manifest_fw))
    for c in candidates:
        p = _inside(root, c)
        if p.is_file():
            return p
    patterns = [
        f"build/{preset}/*.elf",
        f"build/{preset}/*.axf",
        f"build/{preset}/**/*.elf",
        "build/*.elf",
    ]
    for pattern in patterns:
        for match in sorted(glob.glob(str(root / pattern), recursive=True)):
            if Path(match).is_file():
                return Path(match).resolve()
    raise ValueError(
        "Firmware not found; set build.firmware (or firmware) in jlink-project.json"
    )


def _find_toolchain_file(root: Path) -> Path | None:
    """CubeMX 生成的 CMake 工具链文件 / CubeMX-generated toolchain file."""
    for pattern in ("cmake/*arm-none-eabi*.cmake", "cmake/*gcc*.cmake", "**/gcc-arm-none-eabi.cmake"):
        for match in sorted(glob.glob(str(root / pattern), recursive=True)):
            p = Path(match)
            if p.is_file() and ".public_snapshot" not in p.parts:
                return p.resolve()
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the whole process tree / 终止整棵进程树.

    ``subprocess.run(timeout=)`` 只杀直接子进程；ninja/cmake 会派发
    arm-none-eabi-gcc 等孙进程，超时后它们会变成孤儿并锁住 build 目录。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=20,
                shell=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - 清理路径必须尽力而为
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run a subprocess safely / 安全执行子进程.

    三条硬约束：
    1. ``stdin=DEVNULL``：MCP 的 stdin 是常开的 JSON-RPC 管道，子进程继承后
       cmake configure 里由 ``execute_process`` 派生的孙进程会永久阻塞。
    2. stdout/stderr 全部 capture，绝不打印到协议通道。
    3. 超时按**进程树**终止，避免孤儿进程锁住 build 目录。
    """
    t0 = time.monotonic()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # 便于 killpg 整棵树
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        return {
            "argv": argv,
            "returncode": None,
            "stdout": "",
            "stderr": f"SPAWN FAILED: {exc}",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "timed_out": False,
        }

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    return {
        "argv": argv,
        "returncode": proc.returncode,
        "stdout": (out or "")[-12000:],
        "stderr": (err or "")[-12000:] if not timed_out else f"TIMEOUT after {timeout}s (process tree killed)",
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "elapsed_s": round(time.monotonic() - t0, 2),
        "timed_out": timed_out,
    }


def _build_env(discovery: dict[str, Any], build_cfg: dict[str, Any]) -> dict[str, str]:
    """Compose child env / 组装子进程环境.

    默认从父环境继承，但会**剔除以凭据命名的变量**（KEY/PASSWORD/SECRET/TOKEN）
    与 DSH_* —— 构建脚本、CMake 的 execute_process、测试程序都不应该看到 CI 密钥。
    需要显式传回时用 manifest ``build.env_allow`` 列名单，或 ``build.env`` 赋值。
    """
    allow = build_cfg.get("env_allow", [])
    allow_set = {str(x) for x in allow} if isinstance(allow, list) else set()

    # Credentials can be re-enabled ONLY by the operator, never by a manifest.
    # A committed jlink-project.json must not be able to forward OPENAI_API_KEY,
    # GITHUB_TOKEN etc. to cmake/ninja/test subprocesses.
    allow_secret_env = os.environ.get("JLINK_MCP_ALLOW_SECRET_ENV", "").strip() == "1"

    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith("DSH_"):
            continue
        if _SECRET_NAME.search(k):
            if not allow_secret_env:
                continue
            if k not in allow_set:
                continue
        env[k] = v
    extra = build_cfg.get("env", {})
    if isinstance(extra, dict):
        for k, v in extra.items():
            if not isinstance(k, str) or not isinstance(v, (str, int, float)):
                continue
            # Same rule for inline values: a manifest cannot name a credential.
            if _SECRET_NAME.search(k):
                if not allow_secret_env:
                    continue
                if k not in allow_set:
                    continue
            env[k] = str(v)
    arm_gcc = discovery["tools"].get("arm_gcc")
    if arm_gcc:
        bindir = str(Path(arm_gcc["path"]).parent)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def _presets(root: Path) -> dict[str, Any] | None:
    f = root / "CMakePresets.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _preset_names(presets: dict[str, Any] | None, kind: str) -> list[str]:
    if not presets:
        return []
    return [p.get("name", "") for p in presets.get(kind, []) if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# Host test integration / Host 单元测试集成
# ---------------------------------------------------------------------------

# 常见 host 测试驱动脚本位置（相对工程根目录）。
# 最后一项覆盖 Tests/host/ 布局（工程级测试目录独立于 Application 的工程）。
_HOST_TEST_SCRIPTS = (
    "Tests/run_host_tests.py",
    "Tests/host/run_host_tests.py",
    "tests/run_host_tests.py",
    "test/run_host_tests.py",
    "scripts/run_host_tests.py",
    "run_host_tests.py",
)

_HOST_CC_CANDIDATES = ("gcc", "clang", "cc")


def _python_exe() -> str:
    """当前 MCP 服务所用的 Python / Python running this MCP server."""
    return sys.executable or shutil.which("python") or "python"


def _host_test_root(root: Path, build_cfg: dict[str, Any]) -> Path:
    """Directory to search for the host test driver / host 测试搜索根目录.

    默认与工程根相同。``build.host_test_project_root`` 允许指向父目录，用于
    ``04_Software/{Application,Bootloader,Tests}`` 这类测试目录独立于 Application
    的布局。
    """
    configured = build_cfg.get("host_test_project_root")
    if not configured:
        return root
    candidate = (root / str(configured)).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"build.host_test_project_root not found: {candidate}")
    return candidate


def _find_host_test_script(root: Path, build_cfg: dict[str, Any]) -> Path | None:
    """Locate the host test driver script / 定位 host 测试驱动脚本."""
    configured = build_cfg.get("host_test_script")
    if configured:
        p = _inside(root, str(configured))
        if p.is_file():
            return p
        raise ValueError(f"build.host_test_script not found: {p}")
    for rel in _HOST_TEST_SCRIPTS:
        p = root / rel
        if p.is_file():
            return p
    return None


def _resolve_host_cc(build_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a usable host C compiler / 解析可用的宿主编译器.

    显式指定（manifest / 环境变量）但找不到时**报错而不回退**——静默换用 PATH 里
    另一个编译器会让「用错工具链跑测试」这种问题难以发现。
    """
    explicit: str | None = None
    source = ""
    if build_cfg.get("host_cc"):
        explicit, source = str(build_cfg["host_cc"]), "manifest"
    elif os.environ.get("EMBEDDED_HOST_CC"):
        explicit, source = os.environ["EMBEDDED_HOST_CC"], "env:EMBEDDED_HOST_CC"

    if explicit:
        p = Path(explicit).expanduser()
        if p.is_absolute() or os.sep in explicit or "/" in explicit:
            if p.is_file():
                return {"path": str(p.resolve()), "source": source,
                        "version": _version(str(p.resolve()), ["--version"])}
            raise ValueError(f"Configured host_cc not found: {explicit}")
        found = shutil.which(explicit)
        if found:
            return {"path": found, "source": source, "version": _version(found, ["--version"])}
        raise ValueError(f"Configured host_cc not on PATH: {explicit}")

    for cand in _HOST_CC_CANDIDATES:
        found = shutil.which(cand)
        if found:
            return {"path": found, "source": "PATH", "version": _version(found, ["--version"])}
    return None


def _host_test_names(script: Path) -> list[str]:
    """Parse test names from the driver's ``TESTS = {...}`` dict / 解析测试名.

    只读 AST，不执行脚本。键必须是字符串常量，值的形式不限。
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "TESTS" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        names: list[str] = []
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.append(key.value)
        return names
    return []


@_response
@_serialized
def host_test(
    project_path: str,
    script: str | None = None,
    cc: str | None = None,
    opt: str = "both",
    only: str | None = None,
    isolate_failures: bool = True,
    timeout_s: int = 900,
) -> dict[str, Any]:
    """Compile and run host (native) unit tests / 在主机上编译并运行单元测试.

    Requires a host C compiler (gcc/clang/cc) and a driver script that accepts
    ``--cc`` / ``--opt`` / ``--only``. Never touches hardware, so it can gate a
    flash step. With ``opt="both"`` the suite runs at -O0 and -O3 to catch
    optimization-sensitive bugs (missing volatile, uninitialised reads).
    On failure it re-runs per-test with ``--only`` to name the failing cases.
    """
    root = _root(project_path)
    cfg = _build_cfg(root)
    if opt not in ("0", "3", "both"):
        raise ValueError("opt must be '0', '3' or 'both'")

    # Host 测试可以有自己的根目录：有些工程把 Tests/ 放在 Application 的父目录
    # （例如 04_Software/{Application,Bootloader,Tests}），此时 build 根是
    # Application，而测试根是 04_Software。用 build.host_test_project_root 指定。
    host_root = _host_test_root(root, cfg)

    driver = _inside(host_root, script) if script else _find_host_test_script(host_root, cfg)
    if driver is None:
        raise ValueError(
            "Host test driver not found; set build.host_test_script or place "
            f"one of {list(_HOST_TEST_SCRIPTS)} under {host_root}"
        )
    if not driver.is_file():
        raise ValueError(f"Host test driver not found: {driver}")

    compiler = {"path": cc, "source": "explicit", "version": _version(cc, ["--version"])} if cc else _resolve_host_cc(cfg)
    if not compiler:
        raise ValueError(
            "Host C compiler not found; install gcc/clang or set build.host_cc / EMBEDDED_HOST_CC"
        )

    opts = ["0", "3"] if opt == "both" else [opt]
    runs: list[dict[str, Any]] = []
    t0 = time.monotonic()
    for level in opts:
        argv = [_python_exe(), str(driver), "--cc", compiler["path"], "--opt", level]
        if only:
            argv += ["--only", only]
        r = _run(argv, host_root, dict(os.environ), timeout_s)
        runs.append({
            "opt": level,
            "returncode": r["returncode"],
            "ok": r["returncode"] == 0,
            "elapsed_s": r["elapsed_s"],
            "stdout": r["stdout"],
            "stderr": r["stderr"],
        })
        if r["returncode"] != 0:
            break  # 第一档就失败，无需再跑第二档

    failed = [r for r in runs if not r["ok"]]
    result: dict[str, Any] = {
        "ok": not failed,
        "project_path": str(root),
        "driver": str(driver),
        "host_cc": compiler,
        "opts": opts,
        "only": only,
        "runs": runs,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "test_names": _host_test_names(driver),
    }

    # 失败定位：逐个 --only 重跑，指出是哪个用例挂了
    if failed and isolate_failures and not only:
        names = result["test_names"]
        isolation: list[dict[str, Any]] = []
        for name in names:
            argv = [_python_exe(), str(driver), "--cc", compiler["path"], "--opt", opts[0], "--only", name]
            r = _run(argv, host_root, dict(os.environ), timeout_s)
            isolation.append({
                "name": name,
                "ok": r["returncode"] == 0,
                "returncode": r["returncode"],
                "stderr_tail": (r["stderr"] or r["stdout"])[-800:],
            })
        result["isolation"] = isolation
        result["failing_tests"] = [i["name"] for i in isolation if not i["ok"]]

    result["note"] = (
        "Host tests cover hardware-independent logic only; "
        "timing/peripheral/DMA behaviour still needs target hardware"
    )
    return result


# ---------------------------------------------------------------------------
# Tools / 工具实现
# ---------------------------------------------------------------------------


@_response
def toolchain_detect(project_path: str | None = None) -> dict[str, Any]:
    """Discover CMake/Ninja/arm-none-eabi/J-Link tools and report versions / 探测工具链.

    Resolution order: jlink-project.json build.toolchain → EMBEDDED_* env vars →
    PATH → sibling of arm-none-eabi-gcc → common install locations.
    Returns paths, version strings and readiness flags. Read-only; no hardware access.
    """
    overrides: dict[str, str] = {}
    root = None
    if project_path:
        root = _root(project_path)
        tc = _build_cfg(root).get("toolchain", {})
        if isinstance(tc, dict):
            overrides = {k: str(v) for k, v in tc.items() if isinstance(v, (str, int))}
    result = _discover(overrides)
    result["project_path"] = str(root) if root else None
    result["toolchain_overrides"] = overrides
    result["env_override_names"] = [s["env"] for s in _TOOL_SPECS.values()]
    return result


@_response
def build_status(project_path: str, preset: str = "Debug") -> dict[str, Any]:
    """Report manifest, toolchain, firmware hash and build evidence / 工程与产物状态盘点.

    Read-only. Useful as the first call before any build/flash in a headless session.
    """
    root = _root(project_path)
    cfg = _build_cfg(root)
    disco = _discover(cfg.get("toolchain", {}) or {})
    presets = _presets(root)
    fw = None
    fw_error = None
    try:
        fw_path = _find_firmware(root, cfg, preset)
        fw = {
            "path": str(fw_path),
            "relative": str(fw_path.relative_to(root)).replace("\\", "/"),
            "sha256": _sha256(fw_path),
            "size_bytes": fw_path.stat().st_size,
            "mtime": fw_path.stat().st_mtime,
        }
        size_tool = disco["tools"].get("arm_size")
        if size_tool:
            r = _run([size_tool["path"], "-B", str(fw_path)], root, dict(os.environ), 30)
            fw["size_output"] = (r["stdout"] or r["stderr"]).strip()
    except ValueError as exc:
        fw_error = str(exc)
    compile_db = root / "build" / preset / "compile_commands.json"
    toolchain_file = _find_toolchain_file(root)
    # Host 测试可用性（只读探测，不执行）
    host_driver = None
    host_names: list[str] = []
    try:
        host_driver = _find_host_test_script(_host_test_root(root, cfg), cfg)
        if host_driver:
            host_names = _host_test_names(host_driver)
    except ValueError:
        host_driver = None
    host_cc = _resolve_host_cc(cfg)
    return {
        "project_path": str(root),
        "preset": preset,
        "manifest": _read_manifest(root),
        "build_config": cfg,
        "toolchain": disco,
        "presets": {
            "configure": _preset_names(presets, "configurePresets"),
            "build": _preset_names(presets, "buildPresets"),
        },
        "toolchain_file": str(toolchain_file) if toolchain_file else None,
        "compile_commands": str(compile_db) if compile_db.is_file() else None,
        "firmware": fw,
        "firmware_error": fw_error,
        "host_test": {
            "driver": str(host_driver) if host_driver else None,
            "test_names": host_names,
            "test_count": len(host_names),
            "host_cc": host_cc,
            "ready": bool(host_driver and host_cc),
        },
    }


@_response
@_serialized
def build_project(
    project_path: str,
    preset: str = "Debug",
    clean: bool = False,
    target: str | None = None,
    configure_args: list[str] | None = None,
    build_args: list[str] | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Configure and build a CMake/Ninja/arm-none-eabi project / 编译工程并产出证据.

    Uses CMakePresets.json when available (``cmake --preset`` / ``--build --preset``),
    otherwise falls back to an explicit Ninja configure with the CubeMX toolchain file.
    Returns raw compiler output, ELF SHA256, size output and warning count.
    Never touches hardware. Does not modify source files.
    """
    root = _root(project_path)
    cfg = _build_cfg(root)
    preset = preset or cfg.get("preset", "Debug")
    disco = _discover(cfg.get("toolchain", {}) or {})
    if not disco["build_ready"]:
        raise ValueError(f"Toolchain incomplete, missing: {disco['missing']}")

    cmake = disco["tools"]["cmake"]["path"]
    ninja = disco["tools"]["ninja"]["path"]
    env = _build_env(disco, cfg)
    env["CMAKE_GENERATOR"] = "Ninja"
    env["CMAKE_MAKE_PROGRAM"] = ninja

    presets = _presets(root)
    cfg_names = _preset_names(presets, "configurePresets")
    use_presets = preset in cfg_names

    stages: list[dict[str, Any]] = []
    binary_dir = root / "build" / preset
    if use_presets:
        configure_argv = [cmake, "--preset", preset, *(configure_args or [])]
    else:
        tc_file = _find_toolchain_file(root)
        configure_argv = [
            cmake,
            "-S", str(root),
            "-B", str(binary_dir),
            "-G", "Ninja",
            f"-DCMAKE_BUILD_TYPE={preset}",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            f"-DCMAKE_MAKE_PROGRAM={ninja}",
        ]
        if tc_file:
            configure_argv.append(f"-DCMAKE_TOOLCHAIN_FILE={tc_file}")
        configure_argv += list(configure_args or [])

    r = _run(configure_argv, root, env, min(timeout_s, 300))
    stages.append({"name": "configure", **r})
    if r["returncode"] != 0:
        return _build_failure(stages, "configure failed")

    if use_presets:
        build_argv = [cmake, "--build", "--preset", preset]
    else:
        build_argv = [cmake, "--build", str(binary_dir)]
    if clean:
        r = _run([*build_argv, "--target", "clean"], root, env, min(timeout_s, 300))
        stages.append({"name": "clean", **r})
        if r["returncode"] != 0:
            return _build_failure(stages, "clean failed")
    build_argv += list(build_args or [])
    if target:
        build_argv += ["--target", target]
    r = _run(build_argv, root, env, timeout_s)
    stages.append({"name": "build", **r})
    if r["returncode"] != 0:
        return _build_failure(stages, "build failed")

    fw_path = _find_firmware(root, cfg, preset)
    fw = {
        "path": str(fw_path),
        "relative": str(fw_path.relative_to(root)).replace("\\", "/"),
        "sha256": _sha256(fw_path),
        "size_bytes": fw_path.stat().st_size,
    }
    size_tool = disco["tools"].get("arm_size")
    if size_tool:
        sr = _run([size_tool["path"], "-B", str(fw_path)], root, env, 30)
        fw["size_output"] = (sr["stdout"] or sr["stderr"]).strip()
    combined = "\n".join(s.get("stdout", "") + "\n" + s.get("stderr", "") for s in stages)
    compile_db = binary_dir / "compile_commands.json"
    return {
        "ok": True,
        "project_path": str(root),
        "preset": preset,
        "used_cmake_presets": use_presets,
        "stages": stages,
        "firmware": fw,
        "compile_commands": str(compile_db) if compile_db.is_file() else None,
        "warning_count": combined.count("warning:"),
        "error_count": combined.count("error:"),
        "toolchain": {k: v["path"] if v else None for k, v in disco["tools"].items()},
        "note": "ELF SHA256 must be carried into flash/debug to guarantee symbol consistency",
    }


def _build_failure(stages: list[dict[str, Any]], message: str) -> dict[str, Any]:
    return {"ok": False, "message": message, "stages": stages}


@_response
def workflow_pipeline(
    project_path: str,
    preset: str = "Debug",
    flash: bool = False,
    confirm: bool = False,
    observe_s: int = 0,
    chip_name: str | None = None,
    serial_number: str | None = None,
    sample_cpu: bool = False,
    timeout_s: int = 600,
    host_test_enabled: bool = True,
    host_test_opt: str = "both",
    host_cc: str | None = None,
) -> dict[str, Any]:
    """One-call build → host tests → (optional) flash/verify/run → (optional) evidence / 一体化流水线.

    Safe by default: ``flash`` is False. When ``flash=True``, ``confirm=True`` is
    required because this performs a real Flash write.

    ``host_test_enabled=True`` runs the host (native) unit tests **before** any
    Flash write, so a logic regression never reaches the board. The stage is
    skipped (not failed) when the project has no host test driver or no host
    compiler. ``host_test_opt="both"`` runs -O0 and -O3.

    ``observe_s>0`` starts a short runtime monitor, captures one report, then
    releases the hardware. For long observations use deploy_project +
    get_runtime_report polling instead.
    """
    root = _root(project_path)
    if observe_s < 0 or observe_s > 3600:
        raise ValueError("observe_s must be 0..3600")
    if flash and not confirm:
        raise ValueError("flash=True requires confirm=True (real Flash write)")

    from . import workflow as _wf  # 延迟导入，避免模块加载顺序问题

    stages: list[dict[str, Any]] = []

    built = build_project(str(root), preset=preset, timeout_s=timeout_s)
    stages.append({"name": "build", "result": built})
    if not built.get("success") or not built["data"].get("ok"):
        return {"ok": False, "message": "build failed; pipeline stopped", "stages": stages}

    fw_rel = built["data"]["firmware"]["relative"]
    fw_hash = built["data"]["firmware"]["sha256"]

    # ---- Host 测试门禁：烧录前先拦截逻辑回归 ----
    if host_test_enabled:
        cfg = _build_cfg(root)
        driver = None
        try:
            driver = _find_host_test_script(_host_test_root(root, cfg), cfg)
        except ValueError as exc:
            stages.append({"name": "host_test", "skipped": True, "reason": str(exc)})
        if driver is None:
            stages.append({
                "name": "host_test",
                "skipped": True,
                "reason": "no host test driver found (set build.host_test_script to enable)",
            })
        elif _resolve_host_cc(cfg) is None:
            stages.append({
                "name": "host_test",
                "skipped": True,
                "reason": "no host C compiler found (install gcc/clang or set build.host_cc)",
            })
        else:
            tested = host_test(
                str(root), opt=host_test_opt, cc=host_cc, timeout_s=max(120, timeout_s * 2)
            )
            stages.append({"name": "host_test", "result": tested})
            if not tested.get("success") or not tested["data"].get("ok"):
                detail = ""
                failing = (tested.get("data") or {}).get("failing_tests")
                if failing:
                    detail = f"; failing: {', '.join(failing)}"
                return {
                    "ok": False,
                    "message": f"host tests failed; firmware NOT flashed{detail}",
                    "firmware": fw_rel,
                    "firmware_sha256": fw_hash,
                    "stages": stages,
                }
    else:
        stages.append({"name": "host_test", "skipped": True, "reason": "host_test_enabled=False"})

    if flash:
        deployed = _wf.deploy_project(
            project_path=str(root),
            firmware_path=fw_rel,
            chip_name=chip_name,
            serial_number=serial_number,
            monitor=False,
            duration_s=max(1, observe_s or 10),
            sample_cpu=sample_cpu,
        )
        stages.append({"name": "flash", "result": deployed})
        if not deployed.get("success"):
            return {"ok": False, "message": "flash failed; pipeline stopped", "stages": stages}
    else:
        stages.append({"name": "flash", "skipped": True, "reason": "flash=False (dry run)"})

    if observe_s > 0:
        if not flash:
            stages.append({
                "name": "observe",
                "skipped": True,
                "reason": "observe requires flash=True (firmware must be running on target)",
            })
        else:
            started = _wf.start_runtime_monitor(
                mode="auto", duration_s=observe_s, sample_cpu=sample_cpu
            )
            report = _wf.get_runtime_report(0)
            stopped = _wf.stop_runtime_monitor()
            stages.append({
                "name": "observe",
                "started": started,
                "report": report,
                "stopped": stopped,
                "note": "Hardware released; for longer evidence call deploy_project + get_runtime_report",
            })

    return {
        "ok": True,
        "project_path": str(root),
        "preset": preset,
        "firmware": fw_rel,
        "firmware_sha256": fw_hash,
        "stages": stages,
        "note": "Each stage keeps raw evidence; absence of findings is not proof of correctness",
    }


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@_response
def gdb_batch(
    project_path: str,
    commands: list[str] | None = None,
    elf_path: str | None = None,
    chip_name: str | None = None,
    port: int = 2331,
    interface: str = "SWD",
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Run one-shot GDB batch commands via J-Link GDB Server / 一次性符号级调试.

    Spawns JLinkGDBServer if the port is free, waits for it, runs
    ``arm-none-eabi-gdb -batch -ex ... <elf>``, then terminates the server it started.

    Safety / 安全约束:
    * 持有 ``hardware_access`` 独占租约，避免与 runtime monitor / deploy 抢硬件。
    * 拒绝可在宿主机执行代码或读写文件的 GDB 命令（shell / python / source /
      dump / restore / define / while 等）。
    """
    root = _root(project_path)
    cfg = _build_cfg(root)
    disco = _discover(cfg.get("toolchain", {}) or {})
    gdb = disco["tools"].get("arm_gdb")
    server = disco["tools"].get("jlink_gdbserver")
    if not gdb or not server:
        raise ValueError(
            "GDB tooling unavailable; missing: "
            + str([k for k in ("arm_gdb", "jlink_gdbserver") if not disco["tools"].get(k)])
        )

    if elf_path:
        elf = _inside(root, elf_path)
    else:
        elf = _find_firmware(root, cfg, cfg.get("preset", "Debug"))
    if not elf.is_file():
        raise ValueError(f"ELF not found: {elf}")

    chip = chip_name or _read_manifest(root).get("chip_name")
    if not chip:
        inspected = inspect_project_data(str(root))
        chip = inspected.get("chip_name")
    if not chip:
        raise ValueError("chip_name required (manifest key 'chip_name' or explicit argument)")

    cmds = list(commands or [
        "set pagination off",
        "set confirm off",
        f"target remote :{port}",
        "monitor reset halt",
        "bt",
        "info registers",
    ])
    # 拒绝而非静默丢弃：调用方必须知道某条命令没有执行
    # 拒绝任何不在白名单内的命令：未知即拒绝，而不是静默跳过
    for c in cmds:
        if _gdb_has_function_call(c):
            raise ValueError(
                "GDB expression may not call functions (may-call-functions is off): "
                + repr(c)
            )
        if not _gdb_command_allowed(c):
            raise ValueError(
                "GDB command not allowed (host code execution / file IO): " + repr(c)
            )

    # Trusted prelude, added AFTER validation: these are our own commands, not
    # user input, and "set may-call-functions" is deliberately absent from the
    # allow-list so a user cannot re-enable function calls.
    cmds = ["set may-call-functions off"] + cmds

    # 与其它硬件操作互斥
    from ..hardware_access import hardware_access
    from ..runtime_monitor import runtime_monitor

    if runtime_monitor.status(0).get("state") in ("starting", "running"):
        raise RuntimeError("Runtime monitor is active; call stop_runtime_monitor first")

    with hardware_access():
        started_server = False
        proc: subprocess.Popen | None = None
        if not _port_open(port):
            argv = [
                server["path"],
                "-device", str(chip),
                "-if", interface,
                "-speed", "4000",
                "-port", str(port),
                "-singlerun",
            ]
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                )
                started_server = True
            except OSError as exc:
                raise RuntimeError(f"Failed to start J-Link GDB Server: {exc}") from exc
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not _port_open(port):
                if proc.poll() is not None:
                    raise RuntimeError("J-Link GDB Server exited before listening")
                time.sleep(0.3)
            if not _port_open(port):
                raise RuntimeError("J-Link GDB Server did not open the port in time")

        # -nx / -nh keep GDB from reading .gdbinit / .gdb_history, which would be
        # arbitrary host code execution from the working directory.
        gdb_argv = [gdb["path"], *_GDB_SAFE_FLAGS]
        for c in cmds:
            gdb_argv += ["-ex", c]
        gdb_argv.append(str(elf))
        env = _build_env(disco, cfg)
        try:
            result = _run(gdb_argv, root, env, timeout_s)
        finally:
            if started_server and proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass

    return {
        "elf": str(elf),
        "elf_sha256": _sha256(elf),
        "chip_name": chip,
        "port": port,
        "server_started_here": started_server,
        "commands": cmds,
        "gdb": result,
    }
