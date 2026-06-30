from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from rich.console import Console

from synode.config import Settings
from synode.persistence.database import Database
from synode.persistence.urls import to_sync_database_url
from synode.runtime.service import create_service
from synode.schemas import RunMode

app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(help="Database commands")
agents_app = typer.Typer(help="Agent registry commands")
tools_app = typer.Typer(help="Tool registry commands")
mcp_app = typer.Typer(help="MCP commands")
models_app = typer.Typer(help="Model provider commands")
app.add_typer(db_app, name="db")
app.add_typer(agents_app, name="agents")
app.add_typer(tools_app, name="tools")
app.add_typer(mcp_app, name="mcp")
app.add_typer(models_app, name="models")
console = Console()


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
    console.print("database upgraded")


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


def main() -> None:
    app()
