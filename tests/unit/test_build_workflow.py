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

