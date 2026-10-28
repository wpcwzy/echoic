from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import (
    auth,
    chat,
    dashboard,
    insights,
    live,
    public,
    recordings,
    scenes,
    search,
    settings as settings_api,
    shares,
    system,
    tags,
    transcript,
    users,
    vector,
)
from .config import settings
from .core.errors import ApiError, ProviderError
from .db import SessionLocal, init_db
from .migrations import run_migrations
from .deps import ensure_admin_user
from .services.demo import ensure_mock_demo
from .services.queue import reset_stale_tasks
from .services.settings_store import ensure_default_settings
from .worker import start_embedded_worker, stop_embedded_worker

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _seed_startup_state() -> None:
    init_db()
    run_migrations()
    with SessionLocal() as db:
        ensure_default_settings(db)
        ensure_admin_user(db)
    reset_stale_tasks()
    if settings.demo_seed:
        try:
            with SessionLocal() as db:
                ensure_mock_demo(db)
        except Exception:  # demo data is a convenience, never block boot
            logger.exception("demo seeding failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info("starting %s v%s (data dir: %s)", settings.app_name, settings.version, settings.data_path)
    _seed_startup_state()
    if settings.embedded_worker:
        start_embedded_worker()
    try:
        yield
    finally:
        stop_embedded_worker()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} API",
        description="Self-hosted meeting recording knowledge base",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )

    origins = settings.cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        if not request.url.path.startswith(("/assets/", "/favicon")):
            logger.info(
                "%s %s -> %s (%.1f ms)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
            )
        return response

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code},
            headers=exc.headers,
        )

    @app.exception_handler(ProviderError)
    async def _provider_error_handler(_request: Request, exc: ProviderError):
        return JSONResponse(
            status_code=502,
            content={"detail": exc.message, "code": "PROVIDER_ERROR"},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        message = first.get("msg", "参数校验失败")
        return JSONResponse(
            status_code=422,
            content={"detail": f"参数校验失败：{loc} {message}".strip(), "code": "VALIDATION_ERROR"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_request: Request, exc: StarletteHTTPException):
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
            exc.status_code, "ERROR"
        )
        detail = exc.detail if isinstance(exc.detail, str) else "请求处理失败"
        return JSONResponse(status_code=exc.status_code, content={"detail": detail, "code": code})

    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(recordings.router, prefix="/api/recordings", tags=["recordings"])
    app.include_router(scenes.router, prefix="/api/recordings", tags=["scenes"])
    app.include_router(transcript.router, prefix="/api/recordings", tags=["transcript"])
    app.include_router(insights.router, prefix="/api/recordings", tags=["insights"])
    app.include_router(chat.router, prefix="/api/recordings", tags=["chat"])
    app.include_router(tags.router, prefix="/api/tags", tags=["tags"])
    app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
    app.include_router(search.router, prefix="/api/search", tags=["search"])
    app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
    app.include_router(system.router, prefix="/api/system", tags=["system"])
    app.include_router(users.router, prefix="/api/users", tags=["users"])
    app.include_router(shares.router, prefix="/api/shares", tags=["shares"])
    app.include_router(public.router, prefix="/api/public", tags=["public"])
    app.include_router(live.router, prefix="/api/live", tags=["live"])
    app.include_router(vector.router, prefix="/api/vector", tags=["vector"])

    static_dir = settings.resolved_static_dir
    if static_dir is not None:
        assets = static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def _pwa_manifest():
            file = static_dir / "manifest.webmanifest"
            if not file.is_file():
                return JSONResponse(status_code=404, content={"detail": "manifest 不存在", "code": "NOT_FOUND"})
            return FileResponse(
                file,
                media_type="application/manifest+json",
                headers={"Cache-Control": "no-cache"},
            )

        @app.get("/sw.js", include_in_schema=False)
        async def _service_worker():
            file = static_dir / "sw.js"
            if not file.is_file():
                return JSONResponse(status_code=404, content={"detail": "service worker 不存在", "code": "NOT_FOUND"})
            # no-cache lets browsers discover new app-shell versions promptly;
            # Service-Worker-Allowed keeps the worker scoped to the whole app.
            return FileResponse(
                file,
                media_type="application/javascript",
                headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
            )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"detail": "接口不存在", "code": "NOT_FOUND"})
            candidate = (static_dir / full_path).resolve()
            if full_path and static_dir in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()
