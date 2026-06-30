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
        while not self._stopping:
            did_work = await self.run_once()
            if not did_work:
                await self._heartbeat("idle", None)
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
        await self._execute_claimed_run(run.id)
        return True

    async def _execute_claimed_run(self, run_id: str) -> None:
        try:
            if self.service.database.engine.dialect.name == "sqlite":
                await self._heartbeat("running", run_id)
                await self.service.heartbeat_run(run_id, self.worker_id)
                await self.service.execute_run(run_id)
                return
            task = asyncio.create_task(self.service.execute_run(run_id))
            cancelled_for_run = False
            while not task.done():
                await self._heartbeat("running", run_id)
                await self.service.heartbeat_run(run_id, self.worker_id)
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
            logger.exception("worker run execution failed", extra={"run_id": run_id, "worker_id": self.worker_id})
        finally:
            await self._heartbeat("idle", None)

    async def _heartbeat(self, status: str, current_run_id: str | None) -> None:
        await self.service.record_worker_heartbeat(
            worker_id=self.worker_id,
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
                worker_id=self.worker_id,
                run_id=current_run_id,
                status=status,
            )


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
