from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import UTC, datetime

from synode.logging import log_event
from synode.runtime.service import OrchestrationService
from synode.schemas import EventType, RunStatus

logger = logging.getLogger(__name__)


class RunWorker:
    def __init__(self, service: OrchestrationService, worker_id: str | None = None):
        self.service = service
        self.worker_id = worker_id or service.settings.worker_id or _default_worker_id()
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.started_at = datetime.now(UTC)
        self._stopping = False

    async def serve_forever(self) -> None:
        slot_ids = self._slot_ids()
        active: dict[str, asyncio.Task[None]] = {}
        while not self._stopping or active:
            if not self._stopping:
                await self.service.recover_stale_runs()
                for slot_id in slot_ids:
                    if slot_id in active:
                        continue
                    run = await self.service.claim_next_queued_run(slot_id)
                    if run is None:
                        continue
                    log_event(
                        logger,
                        "worker_claimed_run",
                        worker_id=slot_id,
                        run_id=run.id,
                        thread_id=run.thread_id,
                        trace_id=run.observability_trace_id,
                        provider=run.model_provider,
                    )
                    active[slot_id] = asyncio.create_task(self._execute_claimed_run(run.id, slot_id))

            completed_slots = [slot_id for slot_id, task in active.items() if task.done()]
            for slot_id in completed_slots:
                task = active.pop(slot_id)
                try:
                    await task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("worker slot failed", extra={"worker_id": slot_id})

            if active:
                await asyncio.wait(
                    set(active.values()),
                    timeout=self.service.settings.worker_poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue

            if self._stopping:
                break
            for slot_id in slot_ids:
                await self._heartbeat("idle", None, slot_id)
            await asyncio.sleep(self.service.settings.worker_poll_interval_seconds)

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> bool:
        await self.service.recover_stale_runs()
        run = await self.service.claim_next_queued_run(self.worker_id)
        if run is None:
            return False
        log_event(
            logger,
            "worker_claimed_run",
            worker_id=self.worker_id,
            run_id=run.id,
            thread_id=run.thread_id,
            trace_id=run.observability_trace_id,
            provider=run.model_provider,
        )
        await self._execute_claimed_run(run.id, self.worker_id)
        return True

    async def _execute_claimed_run(self, run_id: str, worker_id: str) -> None:
        try:
            if self.service.database.engine.dialect.name == "sqlite":
                await self._heartbeat("running", run_id, worker_id)
                await self.service.heartbeat_run(run_id, worker_id)
                await self.service.execute_run(run_id)
                return
            task = asyncio.create_task(self.service.execute_run(run_id))
            cancelled_for_run = False
            while not task.done():
                await self._heartbeat("running", run_id, worker_id)
                await self.service.heartbeat_run(run_id, worker_id)
                run = await self.service.get_run(run_id)
                if run.status == RunStatus.CANCELLING:
                    cancelled_for_run = True
                    task.cancel()
                    break
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=self.service.settings.worker_heartbeat_interval_seconds,
                )
                if done:
                    break
            try:
                await task
            except asyncio.CancelledError:
                if not cancelled_for_run:
                    raise
        except asyncio.CancelledError:
            if "task" in locals() and not task.done():
                task.cancel()
            raise
        except Exception:
            logger.exception("worker run execution failed", extra={"run_id": run_id, "worker_id": worker_id})
        finally:
            await self._heartbeat("idle", None, worker_id)

    async def _heartbeat(self, status: str, current_run_id: str | None, worker_id: str | None = None) -> None:
        effective_worker_id = worker_id or self.worker_id
        await self.service.record_worker_heartbeat(
            worker_id=effective_worker_id,
            hostname=self.hostname,
            pid=self.pid,
            status=status,
            current_run_id=current_run_id,
            started_at=self.started_at,
        )
        if current_run_id:
            log_event(
                logger,
                EventType.WORKER_HEARTBEAT.value,
                worker_id=effective_worker_id,
                run_id=current_run_id,
                status=status,
            )

    def _slot_ids(self) -> list[str]:
        concurrency = max(1, int(self.service.settings.worker_concurrency))
        if concurrency == 1:
            return [self.worker_id]
        return [f"{self.worker_id}:slot-{index}" for index in range(1, concurrency + 1)]


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
