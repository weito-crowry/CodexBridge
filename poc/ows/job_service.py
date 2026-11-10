from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from job_adapter import DurableJobAdapter, _append_service_event, process_one_job
from process_identity import get_process_identity, process_is_active

OWS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OWS_DIR.parents[1]


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary_path, path)


@dataclass
class MockJobServiceProcess:
    process: subprocess.Popen[bytes]
    stop_path: Path
    stdout_file: TextIO
    stderr_file: TextIO
    service_pid: int | None = None
    service_creation_time_100ns: int | None = None
    service_image_path: str | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    def is_running(self) -> bool:
        if self.service_pid is None:
            return self.process.poll() is None
        if self.service_creation_time_100ns is None or self.service_image_path is None:
            raise RuntimeError("mock-job service runtime identity is incomplete")
        return process_is_active(
            self.service_pid,
            expected_creation_time_100ns=self.service_creation_time_100ns,
        )

    def stop(self, timeout_seconds: float = 5.0) -> int:
        if not self.stop_path.exists():
            self.stop_path.write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + timeout_seconds
        while self.is_running() and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.is_running():
            raise TimeoutError(
                f"mock-job service PID {self.service_pid} did not stop after its stop marker; "
                "the service was left running"
            )
        while self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.process.poll() is None:
            raise TimeoutError("mock-job service launcher did not exit after service shutdown")
        self.stdout_file.close()
        self.stderr_file.close()
        return int(self.process.returncode or 0)


def start_mock_job_service(
    run_dir: Path,
    *,
    poll_interval_seconds: float = 0.02,
    startup_timeout_seconds: float = 15.0,
) -> MockJobServiceProcess:
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    service_id = uuid.uuid4().hex
    ready_path = run_dir / f"job-service-{service_id}-ready.json"
    stop_path = run_dir / f"job-service-{service_id}.stop"
    stdout_path = run_dir / f"job-service-{service_id}.stdout.log"
    stderr_path = run_dir / f"job-service-{service_id}.stderr.log"
    stdout_file = stdout_path.open("w", encoding="utf-8", newline="\n")
    stderr_file = stderr_path.open("w", encoding="utf-8", newline="\n")
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--run-dir",
            str(run_dir),
            "--ready-file",
            str(ready_path),
            "--stop-file",
            str(stop_path),
            "--poll-interval-seconds",
            str(poll_interval_seconds),
        ],
        cwd=REPO_ROOT,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    service = MockJobServiceProcess(process, stop_path, stdout_file, stderr_file)
    expected_argv = [
        str(Path(__file__).resolve()),
        "--run-dir",
        str(run_dir),
        "--ready-file",
        str(ready_path),
        "--stop-file",
        str(stop_path),
        "--poll-interval-seconds",
        str(poll_interval_seconds),
    ]
    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        if ready_path.is_file():
            try:
                ready = json.loads(ready_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                service.stop()
                raise RuntimeError(
                    f"mock-job service readiness record is invalid: {error}"
                ) from error
            service_pid = ready.get("pid")
            service_parent_pid = ready.get("parent_pid")
            identity = get_process_identity(service_pid) if isinstance(service_pid, int) else None
            parent_matches = (
                service_pid == process.pid
                or service_parent_pid == process.pid
                and identity is not None
                and identity["parent_pid"] == process.pid
            )
            if (
                ready.get("run_dir") != str(run_dir)
                or ready.get("argv") != expected_argv
                or not isinstance(service_pid, int)
                or not parent_matches
                or identity is None
                or not identity["active"]
                or identity["creation_time_100ns"] != ready.get("creation_time_100ns")
                or identity["image_path"] != ready.get("image_path")
                or str(identity["image_path"]).rsplit("\\", 1)[-1].casefold() != "python.exe"
            ):
                stop_path.write_text("stop\n", encoding="utf-8")
                service.stop()
                raise RuntimeError(
                    "mock-job service identity, argv, run-dir, or Popen parent "
                    "relationship mismatched"
                )
            service.service_pid = service_pid
            service.service_creation_time_100ns = int(ready["creation_time_100ns"])
            service.service_image_path = str(ready["image_path"])
            return service
        if process.poll() is not None:
            service.stop()
            raise RuntimeError(
                f"mock-job service exited during startup ({process.returncode}); "
                f"logs: {stdout_path}, {stderr_path}"
            )
        time.sleep(0.02)
    service.stop()
    raise TimeoutError(f"mock-job service did not become ready; logs: {stdout_path}, {stderr_path}")


def _append_service_heartbeat(log_path: Path) -> None:
    _append_service_event(log_path, "service_heartbeat")


def run_service(
    run_dir: Path, ready_path: Path, stop_path: Path, poll_interval_seconds: float
) -> int:
    if not 0 < poll_interval_seconds <= 1:
        raise ValueError("poll interval must be greater than 0 and at most 1 second")
    run_dir = run_dir.resolve()
    database_path = run_dir / "jobs.sqlite"
    log_path = run_dir / "job-service.jsonl"
    DurableJobAdapter(database_path)
    started_at = datetime.now(UTC).isoformat()
    identity = get_process_identity(os.getpid())
    if identity is None or not identity["active"]:
        raise RuntimeError("mock-job service could not confirm its Windows process identity")
    _write_json(
        ready_path,
        {
            "pid": os.getpid(),
            "parent_pid": identity["parent_pid"],
            "run_dir": str(run_dir),
            "argv": sys.argv,
            "creation_time_100ns": identity["creation_time_100ns"],
            "image_path": identity["image_path"],
            "database_path": str(database_path),
            "started_at": started_at,
            "python_version": sys.version.split()[0],
        },
    )
    _append_service_event(log_path, "service_started")
    heartbeat_at = time.monotonic()
    while not stop_path.exists():
        processed = process_one_job(database_path, log_path)
        if processed is None:
            time.sleep(poll_interval_seconds)
        if time.monotonic() - heartbeat_at >= 0.5:
            _append_service_heartbeat(log_path)
            heartbeat_at = time.monotonic()
    _append_service_event(log_path, "service_stopped")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fixed mock-job SQLite service")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.02)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return run_service(args.run_dir, args.ready_file, args.stop_file, args.poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
