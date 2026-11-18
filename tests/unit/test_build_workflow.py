"""build_workflow 单元测试 / Unit tests for the portable build workflow tools.

不依赖真实工具链：用临时目录 + 假 manifest 验证纯逻辑部分。
真实工具链行为由 tests/integration 或手工验收覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jlink_mcp.tools import build_workflow as bw


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """最小工程：manifest + presets + 假 ELF。"""
    (tmp_path / "jlink-project.json").write_text(
        json.dumps({
            "ioc": "demo.ioc",
            "chip_name": "STM32F407VG",
            "firmware": "build/Debug/demo.elf",
            "build": {"preset": "Debug", "firmware": "build/Debug/demo.elf"},
        }),
        encoding="utf-8",
    )
    (tmp_path / "CMakePresets.json").write_text(
        json.dumps({
            "version": 3,
            "configurePresets": [{"name": "default", "hidden": True}, {"name": "Debug"}],
            "buildPresets": [{"name": "Debug", "configurePreset": "Debug"}],
        }),
        encoding="utf-8",
    )
    fw = tmp_path / "build" / "Debug" / "demo.elf"
    fw.parent.mkdir(parents=True)
    fw.write_bytes(b"\x7fELF" + b"\x00" * 60)
    (tmp_path / "cmake").mkdir()
    (tmp_path / "cmake" / "gcc-arm-none-eabi.cmake").write_text("# toolchain\n", encoding="utf-8")
    return tmp_path


def test_read_manifest_parses(project: Path) -> None:
    m = bw._read_manifest(project)
    assert m["chip_name"] == "STM32F407VG"
    assert m["build"]["preset"] == "Debug"


def test_read_manifest_rejects_non_object(tmp_path: Path) -> None:
    (tmp_path / "jlink-project.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        bw._read_manifest(tmp_path)


def test_find_firmware_from_manifest(project: Path) -> None:
    fw = bw._find_firmware(project, {"firmware": "build/Debug/demo.elf"}, "Debug")
    assert fw.name == "demo.elf"
    assert fw.is_file()


def test_find_firmware_autodetect(project: Path) -> None:
    fw = bw._find_firmware(project, {}, "Debug")
    assert fw.name == "demo.elf"


def test_find_firmware_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Firmware not found"):
        bw._find_firmware(tmp_path, {}, "Debug")


def test_inside_rejects_escape(project: Path) -> None:
    with pytest.raises(ValueError, match="outside project"):
        bw._inside(project, "../../evil.elf")


def test_inside_accepts_relative(project: Path) -> None:
    p = bw._inside(project, "build/Debug/demo.elf")
    assert p.is_relative_to(project)


def test_find_toolchain_file_skips_snapshot(project: Path) -> None:
    # .public_snapshot 里的同名文件不应被选中
    snap = project / ".public_snapshot" / "cmake"
    snap.mkdir(parents=True)
    (snap / "gcc-arm-none-eabi.cmake").write_text("# snap\n", encoding="utf-8")
    found = bw._find_toolchain_file(project)
    assert found == project / "cmake" / "gcc-arm-none-eabi.cmake"


def test_sha256_stable(project: Path) -> None:
    fw = project / "build" / "Debug" / "demo.elf"
    assert bw._sha256(fw) == bw._sha256(fw)
    assert len(bw._sha256(fw)) == 64


def test_preset_names(project: Path) -> None:
    presets = bw._presets(project)
    assert bw._preset_names(presets, "configurePresets") == ["default", "Debug"]
    assert bw._preset_names(presets, "buildPresets") == ["Debug"]
    assert bw._preset_names(None, "buildPresets") == []


def test_build_status_error_is_structured(project: Path) -> None:
    """_response 必须把异常转成结构化结果，而不是让 MCP 报协议错误。"""
    r = bw.build_status(str(project / "does-not-exist"))
    assert r["success"] is False
    assert "description" in r["error"]


def test_pipeline_requires_confirm_for_flash(project: Path) -> None:
    """安全闸：flash=True 但没 confirm 必须拒绝且不触碰硬件。"""
    r = bw.workflow_pipeline(str(project), flash=True, confirm=False)
    assert r["success"] is False
    assert "confirm" in r["error"]["description"].lower()


def test_pipeline_rejects_bad_observe(project: Path) -> None:
    r = bw.workflow_pipeline(str(project), observe_s=-1)
    assert r["success"] is False


def test_dry_run_pipeline_never_flashes(project: Path, monkeypatch) -> None:
    """dry run 下必须跳过 flash 阶段（用 stub 替换 build 以免真调编译）。"""
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: {
            "success": True,
            "data": {"ok": True, "firmware": {"relative": "build/Debug/demo.elf", "sha256": "ab" * 32}},
        },
    )
    called = {"flash": False}

    from jlink_mcp.tools import workflow as wf

    def fake_deploy(**kwargs):  # pragma: no cover - 不应被调用
        called["flash"] = True
        return {"success": True, "data": {}}

    monkeypatch.setattr(wf, "deploy_project", fake_deploy)
    r = bw.workflow_pipeline(str(project), flash=False)
    assert r["success"] is True
    data = r["data"]
    assert data["ok"] is True
    assert called["flash"] is False
    flash_stage = next(s for s in data["stages"] if s["name"] == "flash")
    assert flash_stage["skipped"] is True


def test_toolchain_detect_reports_shape() -> None:
    r = bw.toolchain_detect()
    assert r["success"] is True
    d = r["data"]
    for key in ("tools", "missing", "build_ready", "debug_ready", "env_override_names"):
        assert key in d
    assert isinstance(d["tools"], dict)
    # 每个工具项要么为 None，要么含 path/version/source
    for item in d["tools"].values():
        if item is not None:
            assert {"path", "version", "source"} <= set(item)


def test_env_override_names_avoid_scrubbed_prefixes() -> None:
    """环境变量名不能落在 MCP 客户端的清洗规则里。"""
    names = [s["env"] for s in bw._TOOL_SPECS.values()]
    for name in names:
        assert name.startswith("EMBEDDED_"), name
        assert not name.startswith("DSH_"), name
        for banned in ("KEY", "PASSWORD", "SECRET", "TOKEN"):
            assert banned not in name.upper().replace("EMBEDDED_", ""), name


# ---------------------------------------------------------------------------
# Host test integration / Host 单元测试集成
# ---------------------------------------------------------------------------

HOST_DRIVER = '''
import argparse
ROOT = "."
WRAP = "03_Platform/Wrapper/"
TESTS = {
    "can": ["a.c"],
    "factory_protocol": ["b.c", WRAP + "c.c"],
    "queue_stress": ["d.c"],
}
'''


def _write_host_driver(root: Path, body: str = HOST_DRIVER) -> Path:
    tests = root / "Tests"
    tests.mkdir()
    script = tests / "run_host_tests.py"
    script.write_text(body, encoding="utf-8")
    return script


def test_host_test_names_parses_dict_keys(project: Path) -> None:
    script = _write_host_driver(project)
    assert bw._host_test_names(script) == ["can", "factory_protocol", "queue_stress"]


def test_host_test_names_tolerates_missing_dict(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text("TESTS = build_tests()\n", encoding="utf-8")
    assert bw._host_test_names(script) == []


def test_host_test_names_tolerates_syntax_error(tmp_path: Path) -> None:
    script = tmp_path / "broken.py"
    script.write_text("def f(:\n", encoding="utf-8")
    assert bw._host_test_names(script) == []


def test_find_host_test_script_autodetect(project: Path) -> None:
    script = _write_host_driver(project)
    assert bw._find_host_test_script(project, {}) == script


def test_find_host_test_script_manifest_override(project: Path) -> None:
    script = project / "custom" / "driver.py"
    script.parent.mkdir()
    script.write_text("TESTS = {}\n", encoding="utf-8")
    assert bw._find_host_test_script(project, {"host_test_script": "custom/driver.py"}) == script


def test_find_host_test_script_missing_returns_none(project: Path) -> None:
    assert bw._find_host_test_script(project, {}) is None


def test_resolve_host_cc_explicit_path(tmp_path: Path) -> None:
    """显式传入的编译器路径优先；配置了但不存在时应报错（不静默回退）。"""
    fake = tmp_path / "mycc"
    fake.write_text("", encoding="utf-8")
    assert bw._resolve_host_cc({"host_cc": str(fake)})["path"] == str(fake.resolve())
    with pytest.raises(ValueError, match="not found"):
        bw._resolve_host_cc({"host_cc": str(tmp_path / "nope")})


def test_pipeline_skips_host_test_when_disabled(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: {"success": True,
                         "data": {"ok": True, "firmware": {"relative": "build/Debug/demo.elf",
                                                           "sha256": "cd" * 32}}},
    )
    called = {"host_test": False}
    monkeypatch.setattr(bw, "host_test", lambda *a, **k: called.__setitem__("host_test", True) or {})
    r = bw.workflow_pipeline(str(project), flash=False, host_test_enabled=False)
    assert r["success"] is True
    assert called["host_test"] is False
    stage = next(s for s in r["data"]["stages"] if s["name"] == "host_test")
    assert stage["skipped"] is True


def test_pipeline_skips_host_test_without_driver(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: {"success": True,
                         "data": {"ok": True, "firmware": {"relative": "build/Debug/demo.elf",
                                                           "sha256": "ef" * 32}}},
    )
    r = bw.workflow_pipeline(str(project), flash=False, host_test_enabled=True)
    assert r["success"] is True
    stage = next(s for s in r["data"]["stages"] if s["name"] == "host_test")
    assert stage["skipped"] is True
    assert "driver" in stage["reason"]


def test_pipeline_blocks_flash_when_host_test_fails(project: Path, monkeypatch) -> None:
    """核心安全语义：host 测试失败时绝不烧录。"""
    _write_host_driver(project)
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: {"success": True,
                         "data": {"ok": True, "firmware": {"relative": "build/Debug/demo.elf",
                                                           "sha256": "12" * 32}}},
    )
    monkeypatch.setattr(
        bw, "host_test",
        lambda *a, **k: {"success": True, "data": {"ok": False, "failing_tests": ["gap_zero"]}},
    )
    monkeypatch.setattr(bw, "_resolve_host_cc",
                        lambda cfg: {"path": "gcc", "source": "stub", "version": "stub"})

    from jlink_mcp.tools import workflow as wf

    deployed = {"called": False}

    def fake_deploy(**kwargs):  # pragma: no cover - 不应被调用
        deployed["called"] = True
        return {"success": True, "data": {}}

    monkeypatch.setattr(wf, "deploy_project", fake_deploy)

    r = bw.workflow_pipeline(str(project), flash=True, confirm=True)
    data = r["data"]
    assert data["ok"] is False
    assert deployed["called"] is False
    assert "NOT flashed" in data["message"]
    assert "gap_zero" in data["message"]


def test_pipeline_proceeds_when_host_test_passes(project: Path, monkeypatch) -> None:
    _write_host_driver(project)
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: {"success": True,
                         "data": {"ok": True, "firmware": {"relative": "build/Debug/demo.elf",
                                                           "sha256": "34" * 32}}},
    )
    monkeypatch.setattr(bw, "host_test",
                        lambda *a, **k: {"success": True, "data": {"ok": True, "runs": []}})
    monkeypatch.setattr(bw, "_resolve_host_cc",
                        lambda cfg: {"path": "gcc", "source": "stub", "version": "stub"})
    r = bw.workflow_pipeline(str(project), flash=False)
    data = r["data"]
    assert data["ok"] is True
    stage = next(s for s in data["stages"] if s["name"] == "host_test")
    assert stage["result"]["data"]["ok"] is True


# ---------------------------------------------------------------------------
# Hardening / 加固行为
# ---------------------------------------------------------------------------


def test_response_carries_schema_version() -> None:
    """所有工具响应带 schema_version，CI 侧可稳定解析。"""
    ok = bw.toolchain_detect()
    assert ok["schema_version"] == bw.SCHEMA_VERSION
    bad = bw.build_status("/definitely/not/a/project")
    assert bad["schema_version"] == bw.SCHEMA_VERSION
    assert bad["success"] is False
    assert bad["error"]["type"] == "ValueError"


def test_build_env_strips_secret_variables(project: Path, monkeypatch) -> None:
    """构建子进程不应继承 CI 密钥。"""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("MY_API_KEY", "k")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("DSH_INTERNAL", "x")
    monkeypatch.setenv("HARMLESS_VAR", "ok")
    env = bw._build_env({"tools": {}}, {})
    assert "GITHUB_TOKEN" not in env
    assert "MY_API_KEY" not in env
    assert "DB_PASSWORD" not in env
    assert "DSH_INTERNAL" not in env
    assert env["HARMLESS_VAR"] == "ok"


def test_build_env_manifest_cannot_allowlist_secret(project: Path, monkeypatch) -> None:
    """A committed manifest must not be able to forward credentials.

    env_allow names the variable, but only the operator can opt in via
    JLINK_MCP_ALLOW_SECRET_ENV=1.
    """
    monkeypatch.delenv("JLINK_MCP_ALLOW_SECRET_ENV", raising=False)
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "v")
    env = bw._build_env({"tools": {}}, {"env_allow": ["MY_CUSTOM_TOKEN"]})
    assert "MY_CUSTOM_TOKEN" not in env


def test_build_env_operator_can_opt_in_to_secret(project: Path, monkeypatch) -> None:
    """With explicit operator opt-in the credential is forwarded again."""
    monkeypatch.setenv("JLINK_MCP_ALLOW_SECRET_ENV", "1")
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "v")
    env = bw._build_env({"tools": {}}, {"env_allow": ["MY_CUSTOM_TOKEN"]})
    assert env["MY_CUSTOM_TOKEN"] == "v"


def test_build_env_inline_value_cannot_inject_secret(project: Path, monkeypatch) -> None:
    """build.env values are likewise blocked from naming a credential."""
    monkeypatch.delenv("JLINK_MCP_ALLOW_SECRET_ENV", raising=False)
    env = bw._build_env({"tools": {}}, {"env": {"SOME_API_KEY": "leak"}})
    assert "SOME_API_KEY" not in env


def test_build_env_manifest_values_override(project: Path, monkeypatch) -> None:
    monkeypatch.setenv("CC", "arm-none-eabi-gcc")
    env = bw._build_env({"tools": {}}, {"env": {"CC": "custom-cc"}})
    assert env["CC"] == "custom-cc"


def _gdb_project(tmp_path: Path) -> Path:
    (tmp_path / "jlink-project.json").write_text(
        json.dumps({"chip_name": "STM32F407VG", "firmware": "build/Debug/demo.elf"}),
        encoding="utf-8",
    )
    fw = tmp_path / "build" / "Debug" / "demo.elf"
    fw.parent.mkdir(parents=True)
    fw.write_bytes(b"\x7fELF" + b"\x00" * 60)
    return tmp_path


def _fake_discovery(*_a, **_k) -> dict:
    return {"tools": {"arm_gdb": {"path": "gdb"}, "jlink_gdbserver": {"path": "gdbserver"}}}


def test_gdb_batch_rejects_dangerous_commands(tmp_path: Path, monkeypatch) -> None:
    """能在宿主机执行代码的 GDB 命令必须被拒绝，而不是静默丢弃。"""
    root = _gdb_project(tmp_path)
    monkeypatch.setattr(bw, "_discover", _fake_discovery)
    for bad in ("python import os", "shell dir", "source evil.gdb", "restore x.bin"):
        r = bw.gdb_batch(str(root), commands=[bad])
        assert r["success"] is False
        assert "not allowed" in r["error"]["description"], bad


def test_serialized_lock_blocks_concurrent_builds(tmp_path: Path) -> None:
    """同一工程的构建串行化：两个线程不会同时进入临界区。"""
    import threading
    import time

    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    @bw._serialized
    def critical(project_path: str) -> None:
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with guard:
            active["now"] -= 1

    threads = [threading.Thread(target=critical, args=(str(tmp_path),)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert active["max"] == 1


@pytest.mark.parametrize(
    "command",
    [
        "bt",
        "info registers",
        "set pagination off",
        "target remote :2331",
        "monitor reset halt",
        "x/16wx 0x20000000",
        "x 0x20000000",
        "disassemble /m main",
        "print $pc",
        "p/x $lr",
        "thread apply all bt",
        "break main",
        "continue",
        "finish",
    ],
)
def test_gdb_command_allowed(command: str) -> None:
    """正常调试命令必须放行 / Normal debugging commands must pass."""
    assert bw._gdb_command_allowed(command) is True
    assert bw._gdb_has_function_call(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "shell rm -rf /",
        'eval "shell id"',
        "compile code int main(){} end",
        'interpreter-exec console "shell id"',
        "pipe date | shell cat",
        'call system("id")',
        'p (int)system("id")',
        "def foo",
        "source /etc/passwd",
        "dump binary memory /tmp/x 0 1",
        "restore /tmp/x binary 0",
        "set logging on",
        "python print(1)",
        "!ls",
        "d",
        "sh",
        "py",
        "define hook-run",
        "while 1",
    ],
)
def test_gdb_command_denied(command: str) -> None:
    """主机命令执行 / 文件读写 / 表达式函数调用必须拒绝。

    Allow-list (not deny-list) because GDB's surface is too large to enumerate:
    eval / compile / interpreter-exec / pipe / call all escaped the old list.
    """
    assert bw._gdb_command_allowed(command) is False or bw._gdb_has_function_call(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "set may-call-functions on",
        "set may-call-functions off",
        "set may-call-functions",
    ],
)
def test_user_cannot_set_may_call_functions(command: str) -> None:
    """用户不得自行开关函数调用；该命令只能由可信前置注入。

    If it were allow-listed, a caller could pass "set may-call-functions on" and
    re-enable the host-call primitive we just closed.
    """
    assert bw._gdb_command_allowed(command) is False


def test_trusted_prelude_is_injected_after_validation() -> None:
    """可信前置命令必须在用户命令校验之后注入，否则会自我拦截。

    Regression: the prelude used to be prepended before validation and every
    gdb_batch call failed with "GDB command not allowed: 'set may-call-functions off'".
    """
    import inspect

    source = inspect.getsource(bw.gdb_batch)
    validate_at = source.find("for c in cmds:")
    prelude_at = source.find('cmds = ["set may-call-functions off"] + cmds')
    assert validate_at != -1, "validation loop missing"
    assert prelude_at != -1, "trusted prelude missing"
    assert prelude_at > validate_at, "prelude must be injected after validation"


def test_gdb_argv_skips_user_init_files() -> None:
    """-nx / -nh 必须存在：工作目录下的 .gdbinit 就是任意主机代码执行。"""
    assert "-nx" in bw._GDB_SAFE_FLAGS
    assert "-nh" in bw._GDB_SAFE_FLAGS
