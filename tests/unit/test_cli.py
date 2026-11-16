"""CLI 单元测试 / Unit tests for the CI-facing CLI.

只验证参数解析、退出码与输出契约，不触碰真实工具链或硬件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jlink_mcp import cli
from jlink_mcp.tools import build_workflow as bw


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "jlink-project.json").write_text(
        json.dumps({"chip_name": "STM32F407VG", "firmware": "build/Debug/demo.elf"}),
        encoding="utf-8",
    )
    fw = tmp_path / "build" / "Debug" / "demo.elf"
    fw.parent.mkdir(parents=True)
    fw.write_bytes(b"\x7fELF" + b"\x00" * 60)
    return tmp_path


def _ok(data: dict) -> dict:
    return {"schema_version": 1, "success": True, "data": data}


def _fail(msg: str) -> dict:
    return {"schema_version": 1, "success": False, "error": {"description": msg}}


def test_parser_requires_project() -> None:
    with pytest.raises(SystemExit):
        cli.main(["status"])


def test_flash_requires_confirm(project: Path, capsys) -> None:
    """没有 --confirm 必须拒绝写 Flash，并以用法错误退出（2）。"""
    assert cli.main(["flash", "--project", str(project)]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "refusing to write Flash" in err


def test_pipeline_flash_requires_confirm(project: Path, capsys) -> None:
    assert cli.main(["pipeline", "--project", str(project), "--flash"]) == cli.EXIT_USAGE
    assert "refusing to write Flash" in capsys.readouterr().err


def test_status_ok(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bw, "build_status",
        lambda *a, **k: _ok({
            "toolchain": {"build_ready": True, "debug_ready": True, "missing": []},
            "host_test": {"ready": True, "test_count": 3},
            "firmware": {"relative": "build/Debug/demo.elf", "sha256": "ab" * 32},
        }),
    )
    assert cli.main(["status", "--project", str(project)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "build_ready=True" in out
    assert "host_test_ready=True" in out


def test_status_not_ready_exits_failure(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        bw, "build_status",
        lambda *a, **k: _ok({"toolchain": {"build_ready": False, "missing": ["cmake"]},
                             "host_test": {}, "firmware": None}),
    )
    assert cli.main(["status", "--project", str(project)]) == cli.EXIT_FAIL


def test_status_json_mode(project: Path, monkeypatch, capsys) -> None:
    payload = {"toolchain": {"build_ready": True, "debug_ready": True, "missing": []},
               "host_test": {"ready": True, "test_count": 0}, "firmware": None}
    monkeypatch.setattr(bw, "build_status", lambda *a, **k: _ok(payload))
    assert cli.main(["status", "--project", str(project), "--json"]) == cli.EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["schema_version"] == 1


def test_build_failure_exit_code(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: _ok({
            "ok": False, "message": "build failed",
            "stages": [{"name": "build", "returncode": 2,
                        "stderr": "error: expected ';'", "elapsed_s": 0.1}],
        }),
    )
    assert cli.main(["build", "--project", str(project)]) == cli.EXIT_FAIL
    captured = capsys.readouterr()
    assert "build failed" in captured.err
    # 关键日志尾必须透出，CI 控制台才能看到编译错误
    assert "expected ';'" in captured.err


def test_build_ok_exit_code(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bw, "build_project",
        lambda *a, **k: _ok({
            "ok": True,
            "firmware": {"relative": "build/Debug/demo.elf", "sha256": "cd" * 32},
            "warning_count": 0, "error_count": 0,
            "stages": [{"name": "build", "returncode": 0, "elapsed_s": 0.2}],
        }),
    )
    assert cli.main(["build", "--project", str(project)]) == cli.EXIT_OK
    assert "build ok" in capsys.readouterr().out


def test_test_failure_reports_failing_cases(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bw, "host_test",
        lambda *a, **k: _ok({"ok": False, "failing_tests": ["gap_zero"],
                             "runs": [{"opt": "0", "returncode": 1,
                                       "stderr": "Assertion failed: x", "stdout": ""}]}),
    )
    assert cli.main(["test", "--project", str(project)]) == cli.EXIT_FAIL
    captured = capsys.readouterr()
    assert "gap_zero" in captured.err
    assert "Assertion failed" in captured.err


def test_verify_runs_build_then_test(project: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_build(*a, **k):
        calls.append("build")
        return _ok({"ok": True, "firmware": {"relative": "build/Debug/demo.elf",
                                             "sha256": "ef" * 32},
                    "warning_count": 0, "error_count": 0,
                    "stages": [{"name": "build", "returncode": 0, "elapsed_s": 0.1}]})

    def fake_test(*a, **k):
        calls.append("test")
        return _ok({"ok": True, "test_names": ["can"], "opts": ["0", "3"], "elapsed_s": 1.0})

    monkeypatch.setattr(bw, "build_project", fake_build)
    monkeypatch.setattr(bw, "host_test", fake_test)
    assert cli.main(["verify", "--project", str(project)]) == cli.EXIT_OK
    assert calls == ["build", "test"]


def test_verify_stops_when_build_fails(project: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_build(*a, **k):
        calls.append("build")
        return _ok({"ok": False, "message": "build failed", "stages": []})

    def fake_test(*a, **k):  # pragma: no cover - 不应被调用
        calls.append("test")
        return _ok({"ok": True})

    monkeypatch.setattr(bw, "build_project", fake_build)
    monkeypatch.setattr(bw, "host_test", fake_test)
    assert cli.main(["verify", "--project", str(project)]) == cli.EXIT_FAIL
    assert calls == ["build"]


def test_value_error_maps_to_usage_exit(project: Path, monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise ValueError("toolchain incomplete")

    monkeypatch.setattr(bw, "build_project", boom)
    assert cli.main(["build", "--project", str(project)]) == cli.EXIT_USAGE
    assert "toolchain incomplete" in capsys.readouterr().err


def test_pipeline_prints_stage_summary(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        bw, "workflow_pipeline",
        lambda *a, **k: _ok({
            "ok": True, "firmware": "build/Debug/demo.elf", "firmware_sha256": "12" * 32,
            "stages": [
                {"name": "build", "result": {"data": {"ok": True, "warning_count": 0,
                                                      "error_count": 0,
                                                      "firmware": {"relative": "build/Debug/demo.elf"}}}},
                {"name": "host_test", "result": {"data": {"ok": True, "failing_tests": []}}},
                {"name": "flash", "skipped": True, "reason": "flash=False (dry run)"},
            ],
        }),
    )
    assert cli.main(["pipeline", "--project", str(project)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "build: ok=True" in out
    assert "host_test: ok=True" in out
    assert "flash: skipped" in out
