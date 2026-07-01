from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from rich.console import Console

from synode.config import Settings
from synode.persistence.database import Database
from synode.persistence.urls import to_sync_database_url
from synode.runtime.queue import build_run_queue_transport
from synode.runtime.service import create_service
from synode.runtime.worker import RunWorker
from synode.schemas import RunMode, RuntimeBackend

app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(help="Database commands")
agents_app = typer.Typer(help="Agent registry commands")
tools_app = typer.Typer(help="Tool registry commands")
mcp_app = typer.Typer(help="MCP commands")
models_app = typer.Typer(help="Model provider commands")
worker_app = typer.Typer(help="Worker commands")
queue_app = typer.Typer(help="Run queue commands")
runtime_app = typer.Typer(help="Runtime diagnostics")
maintenance_app = typer.Typer(help="Maintenance commands")
eval_app = typer.Typer(help="Evaluation commands")
app.add_typer(db_app, name="db")
app.add_typer(agents_app, name="agents")
app.add_typer(tools_app, name="tools")
app.add_typer(mcp_app, name="mcp")
app.add_typer(models_app, name="models")
app.add_typer(worker_app, name="worker")
app.add_typer(queue_app, name="queue")
app.add_typer(runtime_app, name="runtime")
app.add_typer(maintenance_app, name="maintenance")
app.add_typer(eval_app, name="eval")
console = Console()


@app.command(
    "fabricator",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def fabricator_command(ctx: typer.Context) -> None:
    """Forward to the Synode Fabricator workflow CLI."""

    from synode.fabricator.cli import main as fabricator_main

    raise typer.Exit(fabricator_main(list(ctx.args)))


@app.command()
def run(
    task: str = typer.Argument(..., help="Task to run"),
    workspace: str | None = typer.Option(None, "--workspace", "-w"),
    model_provider: str | None = typer.Option(None, "--model-provider"),
    mode: RunMode = typer.Option(RunMode.GENERAL, "--mode"),
) -> None:
    async def _run() -> None:
        settings = Settings()
        service = await create_service(settings)
        try:
            result = await service.run_task(task, workspace, model_provider, mode)
            console.print(f"[bold]Run:[/bold] {result.id}")
            console.print(f"[bold]Status:[/bold] {result.status}")
            console.print(result.final_answer or "", markup=False)
        finally:
            await service.database.close()

    asyncio.run(_run())


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    uvicorn.run("synode.api:create_app", host=host, port=port, factory=True)


@db_app.command("upgrade")
def db_upgrade() -> None:
    async def _sqlite_upgrade(settings: Settings) -> None:
        database = Database(settings)
        try:
            await database.create_schema()
        finally:
            await database.close()

    settings = Settings()
    if settings.database_url.startswith("sqlite"):
        asyncio.run(_sqlite_upgrade(settings))
    else:
        cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", to_sync_database_url(settings.database_url))
        command.upgrade(cfg, "head")
    if settings.run_queue_transport == "procrastinate":
        applied = asyncio.run(_queue_upgrade(settings))
        console.print("queue schema applied" if applied else "queue schema already present")
    console.print("database upgraded")


@queue_app.command("upgrade")
def queue_upgrade() -> None:
    settings = Settings()
    applied = asyncio.run(_queue_upgrade(settings))
    console.print("queue schema applied" if applied else "queue schema already present")


async def _queue_upgrade(settings: Settings) -> bool:
    queue = build_run_queue_transport(settings)
    try:
        return await queue.apply_schema()
    finally:
        await queue.close()


@agents_app.command("list")
def agents_list() -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            for role in service.roles.as_public():
                console.print(role)
        finally:
            await service.database.close()

    asyncio.run(_run())


@tools_app.command("list")
def tools_list() -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            for tool in service.tools.list_names():
                console.print(tool)
        finally:
            await service.database.close()

    asyncio.run(_run())


@mcp_app.command("list")
def mcp_list() -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=True)
        try:
            for tool in service.tools.list_names():
                if tool.startswith("mcp."):
                    console.print(tool)
        finally:
            await service.database.close()

    asyncio.run(_run())


@app.command()
def events(run_id: str, after_id: int = 0) -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            for event in await service.list_events(run_id, after_id=after_id):
                console.print(event)
        finally:
            await service.database.close()

    asyncio.run(_run())


@app.command()
def resume(run_id: str) -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            await service.resume_run(run_id)
            result = await service.get_run(run_id)
            console.print(f"[bold]Run:[/bold] {result.id}")
            console.print(f"[bold]Status:[/bold] {result.status}")
            console.print(result.final_answer or "", markup=False)
        finally:
            await service.database.close()

    asyncio.run(_run())


@app.command()
def approve(
    approval_id: str,
    decision: str = typer.Option(..., "--decision", help="approve or reject"),
    reason: str | None = None,
) -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            if decision == "approve":
                await service.approve(approval_id, reason)
            elif decision == "reject":
                await service.reject(approval_id, reason)
            else:
                raise typer.BadParameter("decision must be approve or reject")
        finally:
            await service.database.close()

    asyncio.run(_run())
    console.print(f"approval {decision}: {approval_id}")


@models_app.command("health")
def models_health() -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            for health in await service.model_health():
                console.print(health)
        finally:
            await service.database.close()

    asyncio.run(_run())


@worker_app.command("run")
def worker_run(worker_id: str | None = typer.Option(None, "--worker-id")) -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=True)
        worker = RunWorker(service, worker_id=worker_id)
        try:
            await worker.serve_forever()
        finally:
            await service.close()

    asyncio.run(_run())


@worker_app.command("once")
def worker_once(worker_id: str | None = typer.Option(None, "--worker-id")) -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=True)
        try:
            worker = RunWorker(service, worker_id=worker_id)
            did_work = await worker.run_once()
            console.print(json.dumps({"did_work": did_work, "worker_id": worker.worker_id}))
        finally:
            await service.close()

    asyncio.run(_run())


@runtime_app.command("status")
def runtime_status(check: bool = typer.Option(False, "--check")) -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            status = await service.runtime_status()
            if check and not status.workers:
                raise typer.Exit(1)
            if check and not status.queue.available:
                raise typer.Exit(1)
            openhands = status.execution_backends.get(RuntimeBackend.OPENHANDS.value)
            if check and service.settings.openhands_enabled and (openhands is None or not openhands.available):
                raise typer.Exit(1)
            console.print_json(data=status.model_dump(mode="json"))
        finally:
            await service.close()

    asyncio.run(_run())


@runtime_app.command("sandbox")
def runtime_sandbox() -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            status = service.sandbox_status()
            if not status.available:
                raise typer.Exit(1)
            console.print_json(data=status.model_dump(mode="json"))
        finally:
            await service.close()

    asyncio.run(_run())


@maintenance_app.command("cleanup")
def maintenance_cleanup() -> None:
    async def _run() -> None:
        service = await create_service(Settings(), include_mcp=False)
        try:
            result = await service.cleanup_retention()
            console.print_json(data=result)
        finally:
            await service.close()

    asyncio.run(_run())


@eval_app.command("coding")
def eval_coding(
    api_url: str = typer.Option("http://127.0.0.1:8787", "--api-url"),
    model: str = typer.Option("llama3.1:8b", "--model"),
    output_dir: Path = typer.Option(Path("var/evals/coding"), "--output-dir"),
    tasks: str | None = typer.Option(None, "--tasks", help="Comma-separated task ids"),
    ollama_base_url: str = typer.Option("http://127.0.0.1:11434", "--ollama-base-url"),
    timeout_seconds: float = typer.Option(1200.0, "--timeout-seconds"),
    list_tasks: bool = typer.Option(False, "--list-tasks"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    from synode.evals.coding import load_tasks, materialize_task, run_coding_eval

    available = load_tasks()
    selected_ids = [item.strip() for item in tasks.split(",") if item.strip()] if tasks else None
    if list_tasks:
        for task in available:
            console.print(f"{task.id}\t{task.title}")
        return
    if dry_run:
        root = output_dir / "dry-run"
        selected = available if selected_ids is None else [task for task in available if task.id in selected_ids]
        for task in selected:
            workspace = materialize_task(task, root)
            console.print(f"{task.id}: {workspace}")
        return
    report = run_coding_eval(
        api_url=api_url,
        model=model,
        output_root=output_dir / datetime_stamp(),
        task_ids=selected_ids,
        ollama_base_url=ollama_base_url,
        timeout_seconds=timeout_seconds,
    )
    console.print_json(data=report)


def datetime_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def main() -> None:
    app()
