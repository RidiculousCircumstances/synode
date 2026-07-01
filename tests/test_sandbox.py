from __future__ import annotations

from pathlib import Path

import pytest

from synode.config import Settings
from synode.tools.sandbox import (
    SandboxRunner,
    SandboxUnavailable,
    _docker_container_config,
    _sandbox_command_env,
    _split_docker_logs,
)


def test_docker_sandbox_fails_closed_when_socket_missing(tmp_path: Path) -> None:
    settings = Settings(
        sandbox_backend="docker",
        sandbox_docker_socket=str(tmp_path / "missing.sock"),
    )

    status = SandboxRunner(settings).status()

    assert status.backend == "docker"
    assert status.available is False
    assert "docker socket not found" in str(status.detail)
    with pytest.raises(SandboxUnavailable, match="docker socket not found"):
        SandboxRunner(settings).ensure_available()


def test_docker_container_config_applies_isolation_and_resource_limits() -> None:
    settings = Settings(
        sandbox_backend="docker",
        sandbox_docker_image="synode-sandbox:test",
        sandbox_docker_host_workspace="/host/workspaces",
        sandbox_docker_container_workspace="/container/workspaces",
        sandbox_cpu_seconds=7,
        sandbox_memory_mb=256,
        sandbox_disk_mb=64,
        sandbox_docker_cpus=0.5,
        sandbox_docker_pids_limit=32,
        sandbox_docker_tmpfs_mb=16,
    )

    config = _docker_container_config(
        settings,
        ["python", "-c", "print('ok')"],
        cwd=Path("/container/workspaces/thread-a"),
        container_name="synode-sandbox-test",
        env={"PYTHONNOUSERSITE": "1"},
    )
    host_config = config["HostConfig"]

    assert config["Image"] == "synode-sandbox:test"
    assert config["Cmd"] == ["python", "-c", "print('ok')"]
    assert config["Env"] == ["PYTHONNOUSERSITE=1"]
    assert config["NetworkDisabled"] is True
    assert host_config["Binds"] == ["/host/workspaces/thread-a:/workspace:rw"]
    assert host_config["CapDrop"] == ["ALL"]
    assert host_config["Memory"] == 256 * 1024 * 1024
    assert host_config["MemorySwap"] == 256 * 1024 * 1024
    assert host_config["NanoCpus"] == 500_000_000
    assert host_config["NetworkMode"] == "none"
    assert host_config["PidsLimit"] == 32
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["SecurityOpt"] == ["no-new-privileges"]
    assert host_config["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=16m"}
    assert {"Name": "cpu", "Soft": 7, "Hard": 8} in host_config["Ulimits"]
    assert {"Name": "nofile", "Soft": 128, "Hard": 128} in host_config["Ulimits"]


def test_sandbox_command_env_adds_workspace_pythonpath(tmp_path: Path) -> None:
    settings = Settings(sandbox_backend="process")

    env = _sandbox_command_env(settings, tmp_path, None)

    assert env["PYTHONPATH"] == str(tmp_path)


def test_sandbox_command_env_uses_container_workspace_for_docker() -> None:
    settings = Settings(sandbox_backend="docker", sandbox_docker_workdir="/workspace")

    env = _sandbox_command_env(settings, Path("/container/workspaces/thread-a"), {"PYTHONPATH": "/extra"})

    assert env["PYTHONPATH"] == "/workspace:/extra"


def test_split_docker_logs_demultiplexes_stdout_and_stderr() -> None:
    def frame(stream_type: int, payload: bytes) -> bytes:
        return bytes([stream_type, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload

    stdout, stderr = _split_docker_logs(frame(1, b"out\n") + frame(2, b"err\n"))

    assert stdout == b"out\n"
    assert stderr == b"err\n"
