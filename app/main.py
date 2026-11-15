from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .abuse import Guard
from .api import v1
from .cache import Cache
from .clients.itunes import ITunes
from .clients.listenbrainz import ListenBrainz
from .clients.musicbrainz import MusicBrainz
from .clients.spotify import Spotify
from .config import Settings
from .errors import AppError, ClientGone
from .i18n import message

STATIC = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,  # tests inject a mock here
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cache = Cache(settings.cache_dir, settings.cache_size_bytes)
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=10,
            follow_redirects=True,
            transport=transport,
        ) as http:
            app.state.mb = MusicBrainz(http, cache, settings.mb_interval)
            app.state.lb = ListenBrainz(http, cache)
            app.state.it = ITunes(http, cache)
            app.state.sp = (
                Spotify(http, cache, settings.spotify_id, settings.spotify_secret)
                if settings.spotify_id and settings.spotify_secret else None
            )
            yield
        cache.close()

    app = FastAPI(title="Sounre", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)  # no public API docs
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # index.html is ~30 KB of text; JSON answers are small
    app.add_middleware(Guard, budget=settings.ip_budget, proxy=settings.proxy, hops=settings.proxy_hops)  # added last = outermost

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        lang = request.query_params.get("lang")
        return JSONResponse({"detail": message(exc.key, lang)}, status_code=exc.status)

    @app.exception_handler(ClientGone)
    async def _client_gone(request: Request, exc: ClientGone):
        return Response(status_code=499)  # nobody is listening; only shows up in access logs

    @app.get("/healthz", include_in_schema=False)
    async def _health():
        return {"ok": True}

    app.include_router(v1.router)
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")  # must stay last
    return app