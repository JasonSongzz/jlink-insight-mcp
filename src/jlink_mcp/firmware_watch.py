"""Watch stable build artifacts and redeploy on change / 监听稳定的编译产物并重新部署."""

from __future__ import annotations

import copy
import hashlib
import threading
from pathlib import Path
from typing import Any

from .runtime_monitor import runtime_monitor


class FirmwareWatch:
    """One explicit, local build-artifact watcher / 一个显式启动的本地编译产物监听器."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] = {"state": "idle"}

    def start(
        self,
        project_path: str,
        firmware_path: str,
        chip_name: str | None = None,
        serial_number: str | None = None,
        duration_s: int = 3600,
        deploy_initial: bool = False,
    ) -> dict[str, Any]:
        """Watch one selected artifact; do not run build commands / 监听选定产物，不执行构建命令."""
        root = Path(project_path).expanduser().resolve()
        path = Path(firmware_path)
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not root.is_dir() or not path.is_relative_to(root):
            raise ValueError("Select a firmware path inside an existing project")
        if path.suffix.lower() not in {".elf", ".axf", ".hex", ".bin"}:
            raise ValueError("Expected ELF/AXF/HEX/BIN artifact")
        if not 1 <= duration_s <= 86400:
            raise ValueError("duration_s must be 1..86400")
        with self._mutex:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("A firmware watcher is already active")
            self._stop.clear()
            self._status = {
                "state": "watching",
                "project_path": str(root),
                "firmware_path": str(path),
                "deployments": 0,
                "last_result": None,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(root, path, chip_name, serial_number, duration_s, deploy_initial),
                name="jlink-firmware-watch",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def _run(
        self, root: Path, path: Path, chip: str | None, serial: str | None, duration: int, deploy_initial: bool
    ) -> None:
        import time

        from .tools.workflow import deploy_project

        deadline = time.monotonic() + duration
        last_processed = None
        candidate = None
        stable_since = 0.0
        initialized = deploy_initial
        owned_monitor = None
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                if not path.is_file():
                    candidate = None
                    self._stop.wait(0.5)
                    continue
                stat = path.stat()
                if stat.st_size > 32 * 1024 * 1024:
                    raise ValueError("Artifact exceeds 32 MiB")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                fingerprint = (stat.st_mtime_ns, stat.st_size, digest)
                if fingerprint != candidate:
                    candidate = fingerprint
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 1.0:
                    if not initialized:
                        last_processed, initialized = digest, True
                    elif digest != last_processed:
                        # One attempt per hash: failed flash is never retried blindly.
                        last_processed = digest
                        active = runtime_monitor.status()
                        if active.get("state") in {"running", "starting"}:
                            if active.get("session_id") != owned_monitor:
                                raise RuntimeError("Another monitor owns hardware; new firmware not deployed")
                            stopped = runtime_monitor.stop()
                            if stopped.get("stop_pending"):
                                raise RuntimeError("Previous monitoring session is still stopping")
                        if self._stop.is_set():
                            break
                        result = deploy_project(str(root), str(path), chip_name=chip, serial_number=serial)
                        session = (result.get("data") or {}).get("runtime") or {}
                        owned_monitor = session.get("session_id")
                        with self._mutex:
                            self._status.update(
                                last_result=result,
                                last_sha256=digest,
                                deployments=self._status["deployments"] + int(result["success"]),
                            )
                with self._mutex:
                    self._status.pop("error", None)
            except Exception as exc:
                with self._mutex:
                    self._status["error"] = str(exc)
            self._stop.wait(0.5)
        with self._mutex:
            self._status["state"] = "stopped"

    def status(self) -> dict[str, Any]:
        """Read watcher state / 查询监听状态."""
        with self._mutex:
            return copy.deepcopy(self._status)

    def stop(self) -> dict[str, Any]:
        """Stop future redeployments; running firmware is left running / 停止后续部署，固件继续运行."""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                return dict(self.status(), stop_pending=True)
        return self.status()


firmware_watch = FirmwareWatch()
