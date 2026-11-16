"""Headless CLI for CI/CD / 面向 CI 的命令行入口.

为什么需要 / Why
----------------
CI runner 里没有 LLM、没有 MCP 客户端。要让流水线在提交时自动跑
「编译 → host 测试 →（可选）烧录 → 取证」，就需要一个确定性的命令行入口，
用**退出码**表达结果。

用法 / Usage::

    jlink-ci status  --project D:/fw
    jlink-ci build   --project D:/fw --preset Debug
    jlink-ci test    --project D:/fw --opt both
    jlink-ci verify  --project D:/fw          # build + host test，无硬件
    jlink-ci flash   --project D:/fw --chip STM32U575RG --confirm
    jlink-ci pipeline --project D:/fw --flash --confirm --observe 30
    jlink-ci debug   --project D:/fw --cmd bt --cmd "info registers"

退出码 / Exit codes:
    0  成功
    1  执行失败（编译错误、测试失败、烧录失败……）
    2  用法或配置错误（缺参数、缺工程、拒绝写 Flash 等）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# Output helpers / 输出
# ---------------------------------------------------------------------------


def _tail(text: str | None, lines: int = 25) -> str:
    if not text:
        return ""
    body = text.strip().splitlines()
    return "\n".join(body[-lines:])


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _stage_text(stage: dict[str, Any]) -> str:
    """One-line summary for a pipeline stage / 单行汇总一个阶段."""
    name = stage.get("name", "?")
    if stage.get("skipped"):
        return f"{name}: skipped ({stage.get('reason', '')})"
    inner = (stage.get("result") or {}).get("data", {})
    if name == "build":
        fw = (inner.get("firmware") or {}).get("relative", "?")
        return (f"{name}: ok={inner.get('ok')} firmware={fw} "
                f"warn={inner.get('warning_count')} err={inner.get('error_count')}")
    if name == "host_test":
        return (f"{name}: ok={inner.get('ok')} "
                f"failing={inner.get('failing_tests') or []}")
    return f"{name}: ok={inner.get('ok')}"


def _print_failure_logs(result: dict[str, Any]) -> None:
    """把关键日志尾部打到 stderr，CI 控制台能直接看到编译/测试错误.

    兼容三种结果形态：
    * build_project: data.stages 是原始运行记录（returncode/stdout/stderr）
    * host_test:     data.runs 是每个优化档的运行记录
    * pipeline:      data.stages[i].result.data 再嵌套上述结构
    """

    def runs_of(data: dict[str, Any]):
        for stage in data.get("stages") or []:
            if "result" in stage:
                inner = (stage.get("result") or {}).get("data") or {}
                yield from runs_of(inner)
            else:
                yield stage.get("name", "?"), stage
        for run in data.get("runs") or []:
            yield f"opt -O{run.get('opt')}", run

    data = result.get("data") or {}
    emitted = False
    for name, run in runs_of(data):
        body = (run.get("stderr") or "") + "\n" + (run.get("stdout") or "")
        tail = _tail(body)
        if tail:
            print(f"--- {name} ---\n{tail}", file=sys.stderr)
            emitted = True
    if not emitted:
        tail = _tail((data.get("stderr") or "") + "\n" + (data.get("stdout") or ""))
        if tail:
            print(tail, file=sys.stderr)


def _ok(result: dict[str, Any]) -> bool:
    if not result.get("success"):
        return False
    data = result.get("data")
    if isinstance(data, dict):
        return bool(data.get("ok", True))
    return True


def _error_text(result: dict[str, Any]) -> str:
    err = result.get("error") or {}
    return str(err.get("description") or err or "unknown error")


# ---------------------------------------------------------------------------
# Commands / 子命令
# ---------------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    from .tools import build_workflow as bw

    result = bw.build_status(args.project, args.preset)
    _emit(result, args.json)
    if not result.get("success"):
        print(f"status failed: {_error_text(result)}", file=sys.stderr)
        return EXIT_FAIL
    data = result["data"]
    tc = data.get("toolchain", {})
    ht = data.get("host_test", {})
    fw = data.get("firmware") or {}
    if not args.json:
        print(f"build_ready={tc.get('build_ready')} debug_ready={tc.get('debug_ready')} "
              f"missing={tc.get('missing')}")
        print(f"host_test_ready={ht.get('ready')} tests={ht.get('test_count')}")
        if fw:
            print(f"firmware={fw.get('relative')} sha256={str(fw.get('sha256'))[:16]}")
    return EXIT_OK if tc.get("build_ready") else EXIT_FAIL


def _cmd_build(args: argparse.Namespace) -> int:
    from .tools import build_workflow as bw

    result = bw.build_project(
        args.project, preset=args.preset, clean=args.clean, timeout_s=args.timeout
    )
    _emit(result, args.json)
    if not _ok(result):
        data = (result.get("data") or {})
        if data.get("message"):
            print(f"build failed: {data['message']}", file=sys.stderr)
        else:
            print(f"build failed: {_error_text(result)}", file=sys.stderr)
        _print_failure_logs(result)
        if not args.json:
            for stage in data.get("stages", []):
                print(f"  {stage.get('name')}: rc={stage.get('returncode')} "
                      f"{stage.get('elapsed_s')}s")
        return EXIT_FAIL
    data = result["data"]
    fw = data.get("firmware") or {}
    if not args.json:
        print(f"build ok: {fw.get('relative')} "
              f"sha256={str(fw.get('sha256'))[:16]} "
              f"warn={data.get('warning_count')} err={data.get('error_count')} "
              f"({data.get('stages', [{}])[-1].get('elapsed_s')}s)")
    return EXIT_OK


def _cmd_test(args: argparse.Namespace) -> int:
    from .tools import build_workflow as bw

    result = bw.host_test(
        args.project, script=getattr(args, "script", None), cc=getattr(args, "cc", None),
        opt=getattr(args, "opt", "both"), only=getattr(args, "only", None),
        timeout_s=args.timeout,
    )
    _emit(result, args.json)
    if not _ok(result):
        data = result.get("data") or {}
        if data.get("failing_tests"):
            print(f"host tests failed: {', '.join(data['failing_tests'])}", file=sys.stderr)
        else:
            print(f"host tests failed: {_error_text(result)}", file=sys.stderr)
        _print_failure_logs(result)
        return EXIT_FAIL
    data = result["data"]
    if not args.json:
        print(f"host tests ok: {len(data.get('test_names') or [])} cases, "
              f"opts={data.get('opts')} ({data.get('elapsed_s')}s)")
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    """CI 门禁：编译 + host 测试，不碰硬件 / build + host test, no hardware."""
    code = _cmd_build(args)
    if code != EXIT_OK:
        return code
    return _cmd_test(args)


def _cmd_flash(args: argparse.Namespace) -> int:
    from .tools import build_workflow as bw

    if not args.confirm:
        print("refusing to write Flash: pass --confirm (real hardware write)", file=sys.stderr)
        return EXIT_USAGE
    result = bw.workflow_pipeline(
        args.project, preset=args.preset, flash=True, confirm=True,
        observe_s=args.observe, chip_name=args.chip,
        serial_number=args.serial, timeout_s=args.timeout,
        host_test_enabled=not args.no_host_test, host_test_opt=args.opt,
    )
    _emit(result, args.json)
    if not _ok(result):
        data = result.get("data") or {}
        print(f"flash failed: {data.get('message') or _error_text(result)}", file=sys.stderr)
        _print_failure_logs(result)
        return EXIT_FAIL
    data = result["data"]
    if not args.json:
        print(f"flash ok: {data.get('firmware')} sha256={str(data.get('firmware_sha256'))[:16]}")
        for stage in data.get("stages", []):
            print(f"  {_stage_text(stage)}")
    return EXIT_OK


def _cmd_pipeline(args: argparse.Namespace) -> int:
    from .tools import build_workflow as bw

    if args.flash and not args.confirm:
        print("refusing to write Flash: pass --confirm with --flash", file=sys.stderr)
        return EXIT_USAGE
    result = bw.workflow_pipeline(
        args.project, preset=args.preset, flash=args.flash, confirm=args.confirm,
        observe_s=args.observe, chip_name=args.chip,
        serial_number=args.serial, timeout_s=args.timeout,
        host_test_enabled=not args.no_host_test, host_test_opt=args.opt,
    )
    _emit(result, args.json)
    data = result.get("data") or {}
    ok = _ok(result)
    if not args.json:
        for stage in data.get("stages", []):
            print(f"  {_stage_text(stage)}")
    if not ok:
        print(f"pipeline failed: {data.get('message') or _error_text(result)}", file=sys.stderr)
        _print_failure_logs(result)
        return EXIT_FAIL
    if not args.json:
        print(f"pipeline ok: {data.get('firmware')} "
              f"sha256={str(data.get('firmware_sha256'))[:16]}")
    return EXIT_OK


def _cmd_debug(args: argparse.Namespace) -> int:
    from .tools import build_workflow as bw

    result = bw.gdb_batch(
        args.project, commands=args.cmd, elf_path=args.elf,
        chip_name=args.chip, port=args.port, timeout_s=args.timeout,
    )
    _emit(result, args.json)
    if not result.get("success"):
        print(f"debug failed: {_error_text(result)}", file=sys.stderr)
        return EXIT_FAIL
    data = result["data"]
    gdb = data.get("gdb") or {}
    if not args.json:
        print(f"gdb rc={gdb.get('returncode')} elapsed={gdb.get('elapsed_s')}s")
        print(_tail((gdb.get("stdout") or "") + "\n" + (gdb.get("stderr") or ""), 40))
    return EXIT_OK if gdb.get("returncode") == 0 else EXIT_FAIL


# ---------------------------------------------------------------------------
# Parser / 参数解析
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser, *, preset: bool = True, host_test: bool = True) -> None:
    p.add_argument("--project", required=True, help="firmware project root")
    if preset:
        p.add_argument("--preset", default="Debug", help="CMake preset (default: Debug)")
    p.add_argument("--timeout", type=int, default=600, help="per-step timeout seconds")
    p.add_argument("--json", action="store_true", help="print raw JSON result")
    p.add_argument("-v", "--verbose", action="store_true", help="show INFO-level logs")
    if host_test:
        p.add_argument("--opt", choices=("0", "3", "both"), default="both",
                       help="host test optimisation level")
        p.add_argument("--no-host-test", action="store_true",
                       help="skip the host test stage")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jlink-ci",
        description="Headless build/flash/debug workflow for CI/CD",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="report toolchain/firmware/host-test readiness")
    _add_common(p, host_test=False)

    p = sub.add_parser("build", help="configure + build")
    _add_common(p, host_test=False)
    p.add_argument("--clean", action="store_true")

    p = sub.add_parser("test", help="run host unit tests (no hardware)")
    _add_common(p, preset=False)
    p.add_argument("--script", help="host test driver path (relative to project)")
    p.add_argument("--cc", help="host C compiler")
    p.add_argument("--only", help="run a single named test")

    p = sub.add_parser("verify", help="build + host tests (CI gate, no hardware)")
    _add_common(p)
    p.add_argument("--clean", action="store_true")
    p.add_argument("--script", help="host test driver path")

    p = sub.add_parser("flash", help="build + host tests + flash (writes Flash)")
    _add_common(p)
    p.add_argument("--chip", help="exact J-Link chip name, e.g. STM32U575RG")
    p.add_argument("--serial", help="probe serial number")
    p.add_argument("--observe", type=int, default=0, help="seconds of runtime evidence")
    p.add_argument("--confirm", action="store_true", help="required: real Flash write")

    p = sub.add_parser("pipeline", help="build → host tests → optional flash → evidence")
    _add_common(p)
    p.add_argument("--flash", action="store_true", help="enable Flash write")
    p.add_argument("--confirm", action="store_true", help="required with --flash")
    p.add_argument("--chip")
    p.add_argument("--serial")
    p.add_argument("--observe", type=int, default=0)

    p = sub.add_parser("debug", help="one-shot GDB batch via J-Link GDB Server")
    _add_common(p, preset=False)
    p.add_argument("--cmd", action="append", help="GDB command (-ex); repeatable")
    p.add_argument("--elf", help="ELF path (default: manifest firmware)")
    p.add_argument("--chip")
    p.add_argument("--port", type=int, default=2331)

    return parser


_DISPATCH = {
    "status": _cmd_status,
    "build": _cmd_build,
    "test": _cmd_test,
    "verify": _cmd_verify,
    "flash": _cmd_flash,
    "pipeline": _cmd_pipeline,
    "debug": _cmd_debug,
}


def main(argv: list[str] | None = None) -> int:
    # CI 输出保持干净：默认只显示 WARNING 及以上。
    # 注意：包导入阶段已经打印的 INFO 无法回收，CI 里应在作业环境里
    # 预先设置 JLINK_LOG_LEVEL=WARNING（在 Python 启动前生效）。
    if not os.environ.get("JLINK_LOG_LEVEL"):
        import logging

        logging.getLogger("jlink_mcp").setLevel(logging.WARNING)
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verbose", False):
        os.environ["JLINK_LOG_LEVEL"] = "INFO"
    handler = _DISPATCH[args.command]
    try:
        return handler(args)
    except ValueError as exc:  # 配置/参数问题
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
