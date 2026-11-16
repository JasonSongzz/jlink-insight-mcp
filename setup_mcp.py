#!/usr/bin/env python3
"""One-shot bootstrap for jlink-insight-mcp / 一键安装脚本.

用法 / Usage::

    python setup_mcp.py            # 创建 .venv 并安装依赖
    python setup_mcp.py --check    # 只做环境自检，不安装
    python setup_mcp.py --force    # 删除已有 .venv 后重建

设计目标 / Goals:
* 跨平台（Windows / Linux / macOS），只用标准库。
* 幂等：重复运行安全，已存在的 venv 会复用并升级依赖。
* 装完立刻自检：Python 版本、依赖导入、工具链探测、SVD 资源。
* 打印可直接粘贴到 MCP 客户端配置的 JSON 片段。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parent
VENV = REPO / ".venv"
MIN_PY = (3, 10)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def pip_env(mirror: str | None = None) -> dict[str, str]:
    """pip 子进程环境 / Environment for pip.

    默认加超时与重试，避免网络抖动时静默挂起数分钟。
    ``mirror`` 非空时覆盖 PIP_INDEX_URL（不改用户全局配置，只影响本次安装）。
    """
    env = dict(os.environ)
    env.setdefault("PIP_DEFAULT_TIMEOUT", "30")
    env.setdefault("PIP_RETRIES", "2")
    if mirror:
        env["PIP_INDEX_URL"] = mirror
        host = urllib.parse.urlparse(mirror).hostname
        if host:
            env["PIP_TRUSTED_HOST"] = host
    return env


def run(argv: list[str], cwd: Path | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> int:
    print(f"  $ {' '.join(str(a) for a in argv)}")
    p = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if check and p.returncode != 0:
        raise SystemExit(f"命令失败 (exit {p.returncode}): {' '.join(map(str, argv))}")
    return p.returncode


def check_python() -> None:
    if sys.version_info < MIN_PY:
        raise SystemExit(
            f"需要 Python >= {MIN_PY[0]}.{MIN_PY[1]}，当前 {sys.version.split()[0]}。"
            f"\n请安装更新的 Python 后重试。"
        )
    print(f"[OK] Python {sys.version.split()[0]} ({sys.executable})")


def create_venv(force: bool) -> None:
    if VENV.exists() and force:
        print(f"[..] 删除已有 venv: {VENV}")
        shutil.rmtree(VENV, ignore_errors=True)
    if VENV.exists():
        print(f"[OK] 复用已有 venv: {VENV}")
        return
    print(f"[..] 创建 venv: {VENV}")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(VENV)


def install(extra: str = "", mirror: str | None = None, editable: bool = False) -> None:
    py = venv_python()
    if not py.exists():
        raise SystemExit(f"venv 中找不到 python: {py}")
    env = pip_env(mirror)
    # 注意 1：不要跑 `pip install --upgrade pip`。
    # venv 自带的 pip 足够用；自升级在某些网络/代理环境下会挂起数分钟
    # （实测 >180s 无输出、CPU≈0），是首次安装最常见的卡点。
    #
    # 注意 2：默认用**非 editable** 安装。
    # editable 走 `prepare_metadata_for_build_editable`，在部分 hatchling /
    # pip 组合下会失败并长时间挂起（实测 20 分钟无输出）。普通安装只走
    # `build_wheel`，稳定得多。需要改源码即时生效时用 --editable。
    #
    # 注意 3：先装 hatchling 再 `--no-build-isolation`，省掉 build isolation
    # 的一次网络往返，慢网络下更稳；失败则回退默认隔离构建。
    #
    # 注意 4：所有 pip 调用都加 `--no-cache-dir`。
    # 某些受管环境里 pip 的 HTTP 缓存写入会挂起（实测同一命令加与不加：
    # 793s 无输出 vs 58s 成功），禁用缓存是决定性的差异。
    base = [str(py), "-m", "pip", "install", "--no-cache-dir"]
    target = str(REPO)
    flag = "-e" if editable else ""
    if extra:
        target = f"{REPO}{extra}"
    has_hatchling = run([*base, "hatchling", "-q"], check=False, env=env) == 0
    if has_hatchling:
        cmd = [*base, "--no-build-isolation"]
        if flag:
            cmd.append(flag)
        cmd.append(target)
        if run(cmd, check=False, env=env) == 0:
            return
        print("[..] 无隔离安装失败，回退到默认构建方式")
    cmd = list(base)
    if flag:
        cmd.append(flag)
    cmd.append(target)
    run(cmd, env=env)


def verify() -> bool:
    """Import the package and call the toolchain detector / 自检."""
    py = venv_python()
    code = (
        "import json, sys\n"
        "import jlink_mcp\n"
        "from jlink_mcp.tools import build_workflow as bw\n"
        "d = bw.toolchain_detect().get('data', {})\n"
        "print(json.dumps({\n"
        "  'package': jlink_mcp.__file__,\n"
        "  'build_ready': d.get('build_ready'),\n"
        "  'debug_ready': d.get('debug_ready'),\n"
        "  'missing': d.get('missing'),\n"
        "}, ensure_ascii=False))\n"
    )
    p = subprocess.run(
        [str(py), "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    print(p.stdout.strip())
    if p.returncode != 0:
        print(p.stderr[-2000:], file=sys.stderr)
        return False
    try:
        info = json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False
    print(f"[{'OK' if info.get('build_ready') else '!!'}] build_ready = {info.get('build_ready')}")
    print(f"[{'OK' if info.get('debug_ready') else '!!'}] debug_ready = {info.get('debug_ready')}")
    if info.get("missing"):
        print(f"[!!] 缺失工具: {info['missing']}")
        print("     这些工具只影响对应能力；编译需要 cmake+ninja+arm-none-eabi-gcc。")
        print("     若已安装但未发现，设置环境变量 EMBEDDED_CMAKE / EMBEDDED_ARM_GCC 等指向绝对路径。")
    return bool(info.get("build_ready"))


def print_client_config() -> None:
    py = str(venv_python()).replace("\\", "/")
    cfg = {
        "mcpServers": {
            "jlink-insight": {
                "command": py,
                "args": ["-m", "jlink_mcp"],
                "cwd": str(REPO).replace("\\", "/"),
                "env": {"JLINK_DEFAULT_INTERFACE": "SWD", "PYTHONUTF8": "1"},
            }
        }
    }
    print()
    print("=" * 68)
    print("MCP 客户端配置片段（粘贴到 Claude Desktop / Codex / DSH profile）")
    print("=" * 68)
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    print()
    print("DeepSeek Harness 写法（cordis.patch.yml 顶层数组里追加）：")
    print("- insert:")
    print("    - id: mcp-jlink")
    print('      name: "@deepseek-ai/dsh-mcp-client"')
    print("      config:")
    print("        serverName: jlink")
    print("        transport: stdio")
    print(f'        command: "{py}"')
    print("        args: [\"-m\", \"jlink_mcp\"]")
    print(f'        cwd: "{str(REPO).replace(chr(92), "/")}"')
    print("        env:")
    print("          JLINK_DEFAULT_INTERFACE: SWD")
    print("          PYTHONUTF8: \"1\"")
    print("        toolCallTimeoutMs: 600000")
    print("        failOnStartupError: false")


def main() -> int:
    ap = argparse.ArgumentParser(description="jlink-insight-mcp 一键安装")
    ap.add_argument("--check", action="store_true", help="只做自检，不安装")
    ap.add_argument("--force", action="store_true", help="删除已有 .venv 后重建")
    ap.add_argument("--dev", action="store_true", help="同时安装开发依赖（pytest 等）")
    ap.add_argument(
        "--editable", action="store_true",
        help="editable 安装（改源码即时生效；开发用，普通使用不建议）",
    )
    ap.add_argument(
        "--mirror",
        metavar="URL",
        default=None,
        help="PyPI 镜像地址，例如 https://pypi.tuna.tsinghua.edu.cn/simple",
    )
    args = ap.parse_args()

    print("=" * 68)
    print("jlink-insight-mcp bootstrap")
    print(f"仓库目录: {REPO}")
    if args.mirror:
        print(f"PyPI 镜像: {args.mirror}")
    print("=" * 68)

    check_python()

    if not args.check:
        create_venv(args.force)
        print("[..] 安装依赖")
        install("[dev]" if args.dev else "", mirror=args.mirror, editable=args.editable)
        print("[OK] 依赖安装完成")

    print("[..] 环境自检")
    ok = verify()

    print()
    print("前置依赖提醒 / Prerequisites:")
    print("  * SEGGER J-Link Software 必须安装（提供 JLinkARM.dll / JLinkGDBServer）")
    print("  * 编译功能需要 cmake + ninja + arm-none-eabi-gcc；缺失时只影响 build_* 工具")
    print("  * SVD（寄存器解析）可选：设置 JLINK_SVD_DIR 指向芯片厂商 SVD 目录")

    if not args.check:
        print_client_config()

    print()
    print("完成。" if ok else "安装完成但工具链不完整；服务仍可启动，仅 build_* 会报缺失。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
