"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError

from app.settings import Settings
from app.routes.api import router
from app.services.cleanup import cleanup_expired
from app.state import RuntimeState


logger = logging.getLogger("shrinkly.app")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct the application with isolated runtime state."""
    runtime_settings = settings or Settings.from_environment()
    runtime_settings.data_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeState(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime.redis is not None:
            await asyncio.to_thread(runtime.redis.ping)

        async def cleanup_loop() -> None:
            while True:
                try:
                    await asyncio.to_thread(cleanup_expired, runtime)
                except RedisError as exc:
                    logger.error("Redis cleanup scan failed (%s).", type(exc).__name__)
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
            if runtime.redis is not None:
                await asyncio.to_thread(runtime.redis.close)

    application = FastAPI(title="Shrinkly", version="0.1.0", lifespan=lifespan)
    application.state.runtime = runtime

    @application.exception_handler(RedisError)
    async def redis_unavailable(_, exc: RedisError) -> JSONResponse:
        logger.error("Redis request failed (%s).", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"detail": "The job service is temporarily unavailable."},
        )

    application.include_router(router)

    frontend_directory = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_directory.is_dir():
        application.mount(
            "/", StaticFiles(directory=frontend_directory, html=True), name="frontend"
        )
    return application


app = create_app()
