"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.settings import Settings
from app.routes.api import router
from app.services.cleanup import cleanup_expired
from app.state import RuntimeState


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the application with isolated runtime state."""
    runtime_settings = settings or Settings.from_environment()
    runtime_settings.data_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeState(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def cleanup_loop() -> None:
            while True:
                await asyncio.to_thread(cleanup_expired, runtime)
                await asyncio.sleep(60)

        cleanup_task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    application = FastAPI(title="Shrinkly", version="0.1.0", lifespan=lifespan)
    application.state.runtime = runtime
    application.include_router(router)

    frontend_directory = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_directory.is_dir():
        application.mount(
            "/", StaticFiles(directory=frontend_directory, html=True), name="frontend"
        )
    return application


app = create_app()
