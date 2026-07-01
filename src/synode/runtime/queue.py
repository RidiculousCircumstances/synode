from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import procrastinate
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.job_context import JobContext

from synode.config import Settings
from synode.logging import log_event

logger = logging.getLogger(__name__)

RUN_EXECUTION_TASK = "synode.execute_run"
RunQueueHandler = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class RunQueueStatus:
    backend: str
    available: bool
    detail: str | None = None
    queue_name: str | None = None
    pending_jobs: int | None = None
    running_jobs: int | None = None
    failed_jobs: int | None = None


class RunQueueTransport(Protocol):
    backend: str

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def apply_schema(self) -> bool: ...

    async def enqueue_run(self, run_id: str) -> None: ...

    async def reconcile_runs(self, run_ids: Iterable[str]) -> int: ...

    async def run_worker(
        self,
        *,
        worker_id: str,
        concurrency: int,
        wait: bool,
        handler: RunQueueHandler,
    ) -> bool: ...

    async def status(self) -> RunQueueStatus: ...


class RunQueueError(RuntimeError):
    pass


class MissingRunQueueTransport:
    backend = "missing"

    async def open(self) -> None:
        raise RunQueueError("run queue transport is not configured")

    async def close(self) -> None:
        return None

    async def apply_schema(self) -> bool:
        raise RunQueueError("run queue transport is not configured")

    async def enqueue_run(self, run_id: str) -> None:
        raise RunQueueError(f"run queue transport is not configured; cannot enqueue {run_id}")

    async def reconcile_runs(self, run_ids: Iterable[str]) -> int:
        raise RunQueueError("run queue transport is not configured")

    async def run_worker(
        self,
        *,
        worker_id: str,
        concurrency: int,
        wait: bool,
        handler: RunQueueHandler,
    ) -> bool:
        raise RunQueueError("run queue transport is not configured")

    async def status(self) -> RunQueueStatus:
        return RunQueueStatus(
            backend=self.backend,
            available=False,
            detail="run queue transport is not configured",
        )


class ProcrastinateRunQueueTransport:
    backend = "procrastinate"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue_name = settings.queue_name
        self._opened = False
        self._handler: RunQueueHandler | None = None
        connector = procrastinate.PsycopgConnector(
            conninfo=_procrastinate_conninfo(settings.resolved_queue_database_url)
        )
        self.app = procrastinate.App(
            connector=connector,
            worker_defaults={
                "queues": [self.queue_name],
                "delete_jobs": "successful",
                "fetch_job_polling_interval": settings.worker_poll_interval_seconds,
                "update_heartbeat_interval": settings.worker_heartbeat_interval_seconds,
                "stalled_worker_timeout": settings.worker_stale_after_seconds,
            },
        )
        self._task = self.app.task(
            name=RUN_EXECUTION_TASK,
            pass_context=True,
            queue=self.queue_name,
        )(self._execute_run_job)

    async def _execute_run_job(self, context: JobContext, run_id: str) -> bool:
        if self._handler is None:
            raise RunQueueError("run queue worker started without a run handler")
        worker_id = _context_worker_id(context)
        return await self._handler(run_id, worker_id)

    async def open(self) -> None:
        if self._opened:
            return
        await self.app.open_async()
        self._opened = True

    async def close(self) -> None:
        if not self._opened:
            return
        await self.app.close_async()
        self._opened = False

    async def apply_schema(self) -> bool:
        async def _apply() -> bool:
            if await self.app.check_connection_async():
                return False
            await self.app.schema_manager.apply_schema_async()
            return True

        return await self._with_opened(_apply)

    async def enqueue_run(self, run_id: str) -> None:
        async def _enqueue() -> None:
            try:
                await self._task.configure(
                    queue=self.queue_name,
                    queueing_lock=_queueing_lock(run_id),
                ).defer_async(run_id=run_id)
            except procrastinate_exceptions.AlreadyEnqueued:
                log_event(
                    logger,
                    "run_queue_already_enqueued",
                    run_id=run_id,
                    queue_backend=self.backend,
                    queue_name=self.queue_name,
                )

        await self._with_opened(_enqueue)

    async def reconcile_runs(self, run_ids: Iterable[str]) -> int:
        enqueued = 0
        for run_id in run_ids:
            await self.enqueue_run(run_id)
            enqueued += 1
        return enqueued

    async def run_worker(
        self,
        *,
        worker_id: str,
        concurrency: int,
        wait: bool,
        handler: RunQueueHandler,
    ) -> bool:
        self._handler = handler
        await self._with_opened(
            lambda: self.app.run_worker_async(
                queues=[self.queue_name],
                name=worker_id,
                concurrency=concurrency,
                wait=wait,
                fetch_job_polling_interval=self.settings.worker_poll_interval_seconds,
                abort_job_polling_interval=self.settings.worker_poll_interval_seconds,
                shutdown_graceful_timeout=self.settings.worker_heartbeat_interval_seconds,
                listen_notify=True,
                install_signal_handlers=True,
                update_heartbeat_interval=self.settings.worker_heartbeat_interval_seconds,
                stalled_worker_timeout=self.settings.worker_stale_after_seconds,
                delete_jobs="successful",
            )
        )
        return True

    async def status(self) -> RunQueueStatus:
        async def _status() -> RunQueueStatus:
            try:
                available = await self.app.check_connection_async()
                if not available:
                    return RunQueueStatus(
                        backend=self.backend,
                        available=False,
                        detail="Procrastinate schema is not applied",
                        queue_name=self.queue_name,
                    )
                queues = await self.app.job_manager.list_queues_async(queue=self.queue_name)
                stats = next((queue for queue in queues if queue["name"] == self.queue_name), None)
                return RunQueueStatus(
                    backend=self.backend,
                    available=True,
                    detail="Procrastinate queue is reachable",
                    queue_name=self.queue_name,
                    pending_jobs=int(stats["todo"]) if stats else 0,
                    running_jobs=int(stats["doing"]) if stats else 0,
                    failed_jobs=int(stats["failed"]) if stats else 0,
                )
            except Exception as exc:
                return RunQueueStatus(
                    backend=self.backend,
                    available=False,
                    detail=str(exc),
                    queue_name=self.queue_name,
                )

        return await self._with_opened(_status)

    async def _with_opened(self, action: Callable[[], Awaitable[Any]]) -> Any:
        if self._opened:
            return await action()
        async with self.app.open_async():
            return await action()


class InMemoryRunQueueTransport:
    """Test transport. Production service construction never selects this backend."""

    backend = "in_memory"

    def __init__(self) -> None:
        self.queue_name = "in-memory"
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._event = asyncio.Event()
        self._stopping = False

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self.stop()

    def stop(self) -> None:
        self._stopping = True
        self._event.set()

    async def apply_schema(self) -> bool:
        return False

    async def enqueue_run(self, run_id: str) -> None:
        if run_id in self._queued:
            return
        self._queue.append(run_id)
        self._queued.add(run_id)
        self._event.set()

    async def reconcile_runs(self, run_ids: Iterable[str]) -> int:
        count = 0
        for run_id in run_ids:
            await self.enqueue_run(run_id)
            count += 1
        return count

    async def run_worker(
        self,
        *,
        worker_id: str,
        concurrency: int,
        wait: bool,
        handler: RunQueueHandler,
    ) -> bool:
        did_work = False
        active: dict[str, asyncio.Task[bool]] = {}
        slot_ids = _slot_ids(worker_id, concurrency)
        try:
            while not self._stopping or active:
                if not self._stopping:
                    for slot_id in slot_ids:
                        if slot_id in active or not self._queue:
                            continue
                        run_id = self._queue.popleft()
                        self._queued.discard(run_id)
                        active[slot_id] = asyncio.create_task(_run_handler(handler, run_id, slot_id))
                        did_work = True

                completed = [slot_id for slot_id, task in active.items() if task.done()]
                for slot_id in completed:
                    task = active.pop(slot_id)
                    await task

                if not wait and not self._queue and not active:
                    break
                if active:
                    await asyncio.wait(
                        set(active.values()),
                        timeout=0.01,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue
                if self._stopping:
                    break
                self._event.clear()
                if self._queue:
                    continue
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=0.05 if wait else 0.0)
                except asyncio.TimeoutError:
                    if not wait:
                        break
        finally:
            for task in active.values():
                task.cancel()
            if active:
                await asyncio.gather(*active.values(), return_exceptions=True)
        return did_work

    async def status(self) -> RunQueueStatus:
        return RunQueueStatus(
            backend=self.backend,
            available=True,
            detail="in-memory test queue",
            queue_name=self.queue_name,
            pending_jobs=len(self._queue),
            running_jobs=0,
            failed_jobs=0,
        )


def build_run_queue_transport(settings: Settings) -> RunQueueTransport:
    if settings.run_queue_transport != "procrastinate":
        raise RunQueueError(f"unsupported run queue transport: {settings.run_queue_transport}")
    return ProcrastinateRunQueueTransport(settings)


def _procrastinate_conninfo(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgresql://"):
        return url
    raise RunQueueError("SYNODE_QUEUE_DATABASE_URL must be a PostgreSQL psycopg-compatible URL")


def _queueing_lock(run_id: str) -> str:
    return f"synode-run:{run_id}"


def _context_worker_id(context: Any) -> str:
    name = str(getattr(context, "worker_name", "") or "worker")
    job = getattr(context, "job", None)
    job_id = getattr(job, "id", None)
    if job_id is None:
        return name
    return f"{name}:job-{job_id}"


async def _run_handler(handler: RunQueueHandler, run_id: str, worker_id: str) -> bool:
    return await handler(run_id, worker_id)


def _slot_ids(worker_id: str, concurrency: int) -> list[str]:
    effective_concurrency = max(1, int(concurrency))
    if effective_concurrency == 1:
        return [worker_id]
    return [f"{worker_id}:slot-{index}" for index in range(1, effective_concurrency + 1)]
