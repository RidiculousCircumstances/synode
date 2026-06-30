from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from synode.config import Settings


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    argv: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


@dataclass(frozen=True)
class SandboxStatus:
    backend: str
    available: bool
    detail: str | None
    cpu_seconds: int
    memory_mb: int
    disk_mb: int
    output_max_bytes: int


class SandboxUnavailable(RuntimeError):
    pass


class SandboxRunner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def status(self) -> SandboxStatus:
        if self.settings.sandbox_backend == "none":
            return SandboxStatus(
                backend="none",
                available=False,
                detail="sandbox backend is disabled",
                cpu_seconds=self.settings.sandbox_cpu_seconds,
                memory_mb=self.settings.sandbox_memory_mb,
                disk_mb=self.settings.sandbox_disk_mb,
                output_max_bytes=self.settings.sandbox_output_max_bytes,
            )
        if self.settings.sandbox_backend == "docker":
            available, socket_detail = _docker_socket_status(self.settings.sandbox_docker_socket)
            detail = socket_detail
            if available:
                detail = (
                    "docker backend with container isolation, "
                    f"image={self.settings.sandbox_docker_image}, "
                    f"network={self.settings.sandbox_docker_network}"
                )
            return SandboxStatus(
                backend="docker",
                available=available,
                detail=detail,
                cpu_seconds=self.settings.sandbox_cpu_seconds,
                memory_mb=self.settings.sandbox_memory_mb,
                disk_mb=self.settings.sandbox_disk_mb,
                output_max_bytes=self.settings.sandbox_output_max_bytes,
            )
        return SandboxStatus(
            backend=self.settings.sandbox_backend,
            available=True,
            detail="process backend with workspace, timeout, output, CPU, RAM, and file-size limits",
            cpu_seconds=self.settings.sandbox_cpu_seconds,
            memory_mb=self.settings.sandbox_memory_mb,
            disk_mb=self.settings.sandbox_disk_mb,
            output_max_bytes=self.settings.sandbox_output_max_bytes,
        )

    def ensure_available(self) -> None:
        status = self.status()
        if not status.available:
            raise SandboxUnavailable(status.detail or "sandbox backend is unavailable")

    async def run_command(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        self.ensure_available()
        if not argv:
            return SandboxResult(ok=False, argv=argv, returncode=None, stdout="", stderr="", error="argv is required")
        before_size = _directory_size(cwd)
        if self.settings.sandbox_backend == "docker":
            return await self._run_docker_command(
                argv,
                cwd=cwd,
                timeout=timeout,
                env=env,
                before_size=before_size,
            )
        return await self._run_process_command(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            before_size=before_size,
        )

    async def _run_process_command(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None,
        before_size: int,
    ) -> SandboxResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limit_process(self.settings) if os.name == "posix" else None,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            _kill_process_tree(process)
            await process.wait()
            return SandboxResult(
                ok=False,
                argv=argv,
                returncode=None,
                stdout="",
                stderr="",
                error=f"command timed out after {timeout}s",
            )
        return self._finalize_command_result(
            argv=argv,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            before_size=before_size,
        )

    async def _run_docker_command(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None,
        before_size: int,
    ) -> SandboxResult:
        container_id: str | None = None
        container_name = f"synode-sandbox-{uuid.uuid4().hex}"
        config = _docker_container_config(
            self.settings,
            argv,
            cwd=cwd,
            container_name=container_name,
            env=env,
        )
        try:
            async with _docker_client(self.settings) as client:
                try:
                    created = await client.post(
                        "/containers/create",
                        params={"name": container_name},
                        json=config,
                    )
                except httpx.HTTPError as exc:
                    return SandboxResult(
                        ok=False,
                        argv=argv,
                        returncode=None,
                        stdout="",
                        stderr="",
                        error=f"docker engine unavailable: {exc}",
                    )
                if created.status_code >= 400:
                    return SandboxResult(
                        ok=False,
                        argv=argv,
                        returncode=None,
                        stdout="",
                        stderr="",
                        error=_docker_response_error(created),
                    )

                container_id = str(created.json().get("Id") or "")
                if not container_id:
                    return SandboxResult(
                        ok=False,
                        argv=argv,
                        returncode=None,
                        stdout="",
                        stderr="",
                        error="docker engine did not return a container id",
                    )

                started = await client.post(f"/containers/{container_id}/start")
                if started.status_code >= 400:
                    stdout, stderr = await _docker_logs(client, container_id)
                    return SandboxResult(
                        ok=False,
                        argv=argv,
                        returncode=None,
                        stdout=_decode_tail(stdout, self.settings.sandbox_output_max_bytes),
                        stderr=_decode_tail(stderr, self.settings.sandbox_output_max_bytes),
                        error=_docker_response_error(started),
                    )

                try:
                    waited = await asyncio.wait_for(
                        client.post(f"/containers/{container_id}/wait"),
                        timeout=timeout,
                    )
                except TimeoutError:
                    await _kill_docker_container(client, container_id)
                    stdout, stderr = await _docker_logs(client, container_id)
                    return SandboxResult(
                        ok=False,
                        argv=argv,
                        returncode=None,
                        stdout=_decode_tail(stdout, self.settings.sandbox_output_max_bytes),
                        stderr=_decode_tail(stderr, self.settings.sandbox_output_max_bytes),
                        error=f"command timed out after {timeout}s",
                    )

                stdout, stderr = await _docker_logs(client, container_id)
                if waited.status_code >= 400:
                    return SandboxResult(
                        ok=False,
                        argv=argv,
                        returncode=None,
                        stdout=_decode_tail(stdout, self.settings.sandbox_output_max_bytes),
                        stderr=_decode_tail(stderr, self.settings.sandbox_output_max_bytes),
                        error=_docker_response_error(waited),
                    )
                wait_payload = waited.json()
                returncode = int(wait_payload.get("StatusCode", 1))
                return self._finalize_command_result(
                    argv=argv,
                    returncode=returncode,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=cwd,
                    before_size=before_size,
                )
        except httpx.HTTPError as exc:
            return SandboxResult(
                ok=False,
                argv=argv,
                returncode=None,
                stdout="",
                stderr="",
                error=f"docker engine unavailable: {exc}",
            )
        finally:
            if container_id is not None:
                async with _docker_client(self.settings) as cleanup_client:
                    await _remove_docker_container(cleanup_client, container_id)

    def _finalize_command_result(
        self,
        *,
        argv: list[str],
        returncode: int | None,
        stdout: bytes,
        stderr: bytes,
        cwd: Path,
        before_size: int,
    ) -> SandboxResult:
        output_limit = self.settings.sandbox_output_max_bytes
        after_size = _directory_size(cwd)
        disk_limit = self.settings.sandbox_disk_mb * 1024 * 1024
        if after_size > disk_limit:
            return SandboxResult(
                ok=False,
                argv=argv,
                returncode=returncode,
                stdout=_decode_tail(stdout, output_limit),
                stderr=_decode_tail(stderr, output_limit),
                error=f"workspace exceeds sandbox disk limit of {self.settings.sandbox_disk_mb} MiB",
            )
        if after_size - before_size > disk_limit:
            return SandboxResult(
                ok=False,
                argv=argv,
                returncode=returncode,
                stdout=_decode_tail(stdout, output_limit),
                stderr=_decode_tail(stderr, output_limit),
                error=f"command wrote more than sandbox disk limit of {self.settings.sandbox_disk_mb} MiB",
            )
        return SandboxResult(
            ok=returncode == 0,
            argv=argv,
            returncode=returncode,
            stdout=_decode_tail(stdout, output_limit),
            stderr=_decode_tail(stderr, output_limit),
        )

    async def run_python(
        self,
        code: str,
        *,
        cwd: Path,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        return await self.run_command(
            ["python", "-I", "-c", code],
            cwd=cwd,
            timeout=timeout,
            env={**(env or {}), "PYTHONNOUSERSITE": "1"},
        )


def _limit_process(settings: Settings) -> Any:
    def apply_limits() -> None:
        try:
            import resource
        except ImportError:
            return
        cpu_seconds = max(1, int(settings.sandbox_cpu_seconds))
        memory_bytes = max(32, int(settings.sandbox_memory_mb)) * 1024 * 1024
        file_bytes = max(1, int(settings.sandbox_disk_mb)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))

    return apply_limits


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def _docker_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=_docker_socket_path(settings.sandbox_docker_socket)),
        base_url="http://docker",
        timeout=httpx.Timeout(None, connect=5.0),
    )


def _docker_container_config(
    settings: Settings,
    argv: list[str],
    *,
    cwd: Path,
    container_name: str,
    env: dict[str, str] | None,
) -> dict[str, Any]:
    memory_bytes = max(32, int(settings.sandbox_memory_mb)) * 1024 * 1024
    file_bytes = max(1, int(settings.sandbox_disk_mb)) * 1024 * 1024
    cpu_seconds = max(1, int(settings.sandbox_cpu_seconds))
    mount_source = _docker_mount_source(cwd, settings)
    network = settings.sandbox_docker_network.strip()
    return {
        "Image": settings.sandbox_docker_image,
        "Cmd": argv,
        "WorkingDir": settings.sandbox_docker_workdir,
        "User": settings.sandbox_docker_user,
        "Env": [f"{key}={value}" for key, value in sorted((env or {}).items())],
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,
        "OpenStdin": False,
        "NetworkDisabled": network == "none",
        "Labels": {"synode.sandbox": "true", "synode.sandbox.name": container_name},
        "HostConfig": {
            "AutoRemove": False,
            "Binds": [f"{mount_source}:{settings.sandbox_docker_workdir}:rw"],
            "CapDrop": ["ALL"],
            "Memory": memory_bytes,
            "MemorySwap": memory_bytes,
            "NanoCpus": int(max(0.1, float(settings.sandbox_docker_cpus)) * 1_000_000_000),
            "NetworkMode": network,
            "PidsLimit": max(1, int(settings.sandbox_docker_pids_limit)),
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": f"rw,noexec,nosuid,size={settings.sandbox_docker_tmpfs_mb}m"},
            "Ulimits": [
                {"Name": "cpu", "Soft": cpu_seconds, "Hard": cpu_seconds + 1},
                {"Name": "fsize", "Soft": file_bytes, "Hard": file_bytes},
                {"Name": "nofile", "Soft": 128, "Hard": 128},
            ],
        },
    }


def _docker_mount_source(cwd: Path, settings: Settings) -> Path:
    resolved_cwd = cwd.resolve()
    if not settings.sandbox_docker_host_workspace or not settings.sandbox_docker_container_workspace:
        return resolved_cwd
    container_root = Path(settings.sandbox_docker_container_workspace).resolve(strict=False)
    try:
        relative = resolved_cwd.relative_to(container_root)
    except ValueError:
        return resolved_cwd
    return Path(settings.sandbox_docker_host_workspace).expanduser().resolve(strict=False) / relative


def _docker_socket_status(raw_socket: str) -> tuple[bool, str]:
    socket_path = _docker_socket_path(raw_socket)
    if not Path(socket_path).exists():
        return False, f"docker socket not found at {socket_path}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as docker_socket:
            docker_socket.settimeout(1.0)
            docker_socket.connect(socket_path)
    except OSError as exc:
        detail = exc.strerror or str(exc)
        return False, f"docker socket is not reachable at {socket_path}: {detail}"
    return True, f"docker socket is reachable at {socket_path}"


def _docker_socket_path(raw_socket: str) -> str:
    return raw_socket.removeprefix("unix://")


async def _docker_logs(client: httpx.AsyncClient, container_id: str) -> tuple[bytes, bytes]:
    try:
        response = await client.get(
            f"/containers/{container_id}/logs",
            params={"stdout": "1", "stderr": "1"},
        )
    except httpx.HTTPError as exc:
        return b"", f"failed to read docker logs: {exc}".encode()
    if response.status_code >= 400:
        return b"", _docker_response_error(response).encode()
    return _split_docker_logs(response.content)


async def _kill_docker_container(client: httpx.AsyncClient, container_id: str) -> None:
    try:
        await client.post(f"/containers/{container_id}/kill")
    except httpx.HTTPError:
        return


async def _remove_docker_container(client: httpx.AsyncClient, container_id: str) -> None:
    try:
        await client.delete(f"/containers/{container_id}", params={"force": "1", "v": "1"})
    except httpx.HTTPError:
        return


def _docker_response_error(response: httpx.Response) -> str:
    message = response.text
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        message = str(payload["message"])
    return f"docker engine returned HTTP {response.status_code}: {message}"


def _split_docker_logs(data: bytes) -> tuple[bytes, bytes]:
    stdout = bytearray()
    stderr = bytearray()
    index = 0
    while index + 8 <= len(data):
        stream_type = data[index]
        size = int.from_bytes(data[index + 4 : index + 8], "big")
        start = index + 8
        end = start + size
        if end > len(data):
            break
        chunk = data[start:end]
        if stream_type == 1:
            stdout.extend(chunk)
        elif stream_type == 2:
            stderr.extend(chunk)
        else:
            stderr.extend(chunk)
        index = end
    if index < len(data):
        stdout.extend(data[index:])
    return bytes(stdout), bytes(stderr)


def _decode_tail(data: bytes, limit: int) -> str:
    return data[-limit:].decode("utf-8", errors="replace")


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total
