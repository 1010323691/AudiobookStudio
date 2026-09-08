"""AudiobookStudio backend — FastAPI application entrypoint.

Run (API only):      ``python -m backend.main``  →  http://127.0.0.1:8642
Run (whole app):     ``npm run build`` then the same command, and open
                     http://127.0.0.1:8642 — the backend also serves the built
                     frontend, so the full console runs with no Rust and no Vite.
The Tauri shell (when built) launches this process and polls ``/api/health``.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from .api import audio as api_audio
from .api import book as api_book
from .api import config as api_config
from .api import files as api_files
from .api import script as api_script
from .api import tasks as api_tasks
from .api import text as api_text
from .api import tts as api_tts
from .core import config as core_config
from .core import logging_setup
from .core.paths import get_layout

PORT = 8642

# All module routers (tts drives the isolated local engine; script drives the
# LLM → JSON pipeline) plus the cross-cutting task / config / files routers.
ROUTERS = [
    api_tasks.router,
    api_config.router,
    api_files.router,
    api_text.router,
    api_book.router,
    api_audio.router,
    api_tts.router,
    api_script.router,
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    layout = get_layout()
    logging_setup.setup_logging(layout.logs, core_config.get_config().log.level)
    yield


app = FastAPI(title="AudiobookStudio Backend", version="0.1.0", lifespan=lifespan)

# Local desktop client; origins are loopback / the Tauri webview.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in ROUTERS:
    app.include_router(r)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "audiobookstudio-backend", "port": PORT}


# ---------------------------------------------------------------------------
# Static frontend (optional fallback).
#
# Serves the Vite build (``dist/``) so the entire console can run from the
# backend alone — no Rust toolchain and no Vite dev server required:
#
#     npm run build            #  →  dist/
#     python -m backend.main   #  →  open http://127.0.0.1:8642
#
# Registered LAST, so every ``/api/...`` route (added above) still wins. Real
# assets resolve from ``dist/``; any other path falls back to ``index.html``
# so the client-side router can handle deep links (``/text``, ``/book`` …).
# When ``dist/`` is absent (e.g. during ``tauri dev``, where Vite serves the UI)
# these routes simply return 503 and the API is unaffected.
# ---------------------------------------------------------------------------
DIST_DIR = Path(__file__).resolve().parent.parent / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str) -> Response:
    # Keep API 404s honest: an unknown /api/... is a real error, not the SPA shell.
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if not DIST_DIR.is_dir():
        return Response(
            status_code=503,
            media_type="text/plain; charset=utf-8",
            content="Frontend build not found. Run `npm run build` first, then reload.",
        )

    candidate = (DIST_DIR / full_path).resolve() if full_path else DIST_DIR
    # Serve a real file (e.g. /assets/index-*.js) when it lives inside dist/.
    if full_path and candidate.is_file() and candidate.is_relative_to(DIST_DIR.resolve()):
        return FileResponse(candidate)

    # Otherwise return the SPA entry and let the client router take over.
    index = DIST_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return Response(
        status_code=503,
        media_type="text/plain; charset=utf-8",
        content="index.html missing in dist/. Run `npm run build`.",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=PORT, log_level="warning")
