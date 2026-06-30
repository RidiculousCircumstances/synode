from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        output_limit = self.settings.sandbox_output_max_bytes
        after_size = _directory_size(cwd)
        disk_limit = self.settings.sandbox_disk_mb * 1024 * 1024
        if after_size > disk_limit:
            return SandboxResult(
                ok=False,
                argv=argv,
                returncode=process.returncode,
                stdout=_decode_tail(stdout, output_limit),
                stderr=_decode_tail(stderr, output_limit),
                error=f"workspace exceeds sandbox disk limit of {self.settings.sandbox_disk_mb} MiB",
            )
        if after_size - before_size > disk_limit:
            return SandboxResult(
                ok=False,
                argv=argv,
                returncode=process.returncode,
                stdout=_decode_tail(stdout, output_limit),
                stderr=_decode_tail(stderr, output_limit),
                error=f"command wrote more than sandbox disk limit of {self.settings.sandbox_disk_mb} MiB",
            )
        return SandboxResult(
            ok=process.returncode == 0,
            argv=argv,
            returncode=process.returncode,
            stdout=_decode_tail(stdout, output_limit),
            stderr=_decode_tail(stderr, output_limit),
        )

    async def run_python(self, code: str, *, cwd: Path, timeout: float) -> SandboxResult:
        return await self.run_command(
            ["python", "-I", "-c", code],
            cwd=cwd,
            timeout=timeout,
            env={"PYTHONNOUSERSITE": "1"},
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
