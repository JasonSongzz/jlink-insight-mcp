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

import glob
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from ..project_inspector import inspect_project_data
from ..utils import logger

# ---------------------------------------------------------------------------
# Response wrapper / 统一响应包装
# ---------------------------------------------------------------------------


def _response(func: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    @wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"success": True, "data": func(*args, **kwargs)}
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成结构化结果
            logger.exception("build workflow error in %s", func.__name__)
            return {"success": False, "error": {"description": str(exc)}}

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


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict[str, Any]:
    """Run a subprocess safely / 安全执行子进程.

    MCP 服务的 stdin 是 JSON-RPC 管道且常开；子进程必须显式 ``stdin=DEVNULL``，
    否则 cmake configure 里由 ``execute_process`` 派生的孙进程可能继承该管道并阻塞。
    stdout/stderr 全部 capture，绝不打印到协议通道。
    """
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "argv": argv,
            "returncode": p.returncode,
            "stdout": (p.stdout or "")[-12000:],
            "stderr": (p.stderr or "")[-12000:],
            "elapsed_s": round(time.monotonic() - t0, 2),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {
            "argv": argv,
            "returncode": None,
            "stdout": (out or "")[-4000:],
            "stderr": f"TIMEOUT after {timeout}s",
            "elapsed_s": round(time.monotonic() - t0, 2),
        }


def _build_env(discovery: dict[str, Any], build_cfg: dict[str, Any]) -> dict[str, str]:
    """Compose child env / 组装子进程环境：把工具链目录加进 PATH。"""
    env = dict(os.environ)
    extra = build_cfg.get("env", {})
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and isinstance(v, (str, int, float)):
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
    }


@_response
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
) -> dict[str, Any]:
    """One-call build → (optional) flash/verify/run → (optional) runtime evidence / 一体化流水线.

    Safe by default: ``flash`` is False. When ``flash=True``, ``confirm=True`` is
    required because this performs a real Flash write. ``observe_s>0`` starts a
    short runtime monitor, captures one report, then releases the hardware.
    For long observations use deploy_project + get_runtime_report polling instead.
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
    Use for backtrace / registers / memory / disassembly without VSCode.
    Stop any runtime monitor first (hardware is exclusive).
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
    # 禁止通过 GDB 拉起宿主 shell（避免命令注入面）
    cmds = [c for c in cmds if not c.strip().lower().startswith("shell ")]

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

    gdb_argv = [gdb["path"], "-q", "-batch"]
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
            except Exception:
                try:
                    proc.kill()
                except Exception:
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
