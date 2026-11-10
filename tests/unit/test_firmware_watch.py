"""Build-artifact watch lifecycle tests / 编译产物监听生命周期测试."""

import time
from unittest.mock import Mock

import pytest

from jlink_mcp.firmware_watch import FirmwareWatch
from jlink_mcp.tools import workflow

pytestmark = pytest.mark.unit


def test_watch_deploys_stable_artifact_once_and_stops(tmp_path, monkeypatch):
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"firmware")
    deploy = Mock(return_value={"success": True, "data": {"runtime": None}})
    monkeypatch.setattr(workflow, "deploy_project", deploy)
    watch = FirmwareWatch()
    watch.start(str(tmp_path), "firmware.bin", duration_s=10, deploy_initial=True)
    try:
        deadline = time.monotonic() + 3
        while not watch.status()["deployments"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert watch.status()["deployments"] == 1
        # Same SHA remains stable across further polls: no repeated programming.
        time.sleep(0.6)
        assert deploy.call_count == 1
    finally:
        assert watch.stop()["state"] == "stopped"


def test_watcher_refuses_outside_project_and_invalid_suffix(tmp_path):
    watch = FirmwareWatch()
    with pytest.raises(ValueError):
        watch.start(str(tmp_path), "../firmware.bin")
    with pytest.raises(ValueError):
        watch.start(str(tmp_path), "main.c")


def test_watcher_start_without_initial_deployment(tmp_path, monkeypatch):
    (tmp_path / "firmware.bin").write_bytes(b"v1")
    deploy = Mock()
    monkeypatch.setattr(workflow, "deploy_project", deploy)
    watch = FirmwareWatch()
    watch.start(str(tmp_path), "firmware.bin", duration_s=10)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            watch.start(str(tmp_path), "firmware.bin")
    finally:
        watch.stop()
    deploy.assert_not_called()
