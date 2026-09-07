"""File endpoints — list a module's output directory and serve files for
download / preview (the desktop shell prefers "open folder" via Tauri; this is
the browser fallback and the source of preview text)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..core.paths import get_layout

router = APIRouter(prefix="/api/files", tags=["files"])

_MODULES = {"text", "books", "tts", "audio"}


def _module_dir(module: str) -> Path:
    layout = get_layout()
    d = getattr(layout, f"output_{module}", None)
    if d is None:
        raise HTTPException(404, "未知模块")
    return d


@router.get("/list/{module}")
def list_module(module: str) -> dict:
    if module not in _MODULES:
        raise HTTPException(404, "未知模块")
    d = _module_dir(module)
    if not d.exists():
        return {"path": str(d), "items": []}
    items = [
        {
            "name": p.name,
            "is_dir": p.is_dir(),
            "size": (p.stat().st_size if p.is_file() else None),
        }
        for p in sorted(d.iterdir())
    ]
    return {"path": str(d), "items": items}


@router.get("/download/{module}/{name:path}")
def download_file(module: str, name: str):
    if module not in _MODULES:
        raise HTTPException(404, "未知模块")
    d = _module_dir(module).resolve()
    p = (d / name).resolve()
    if not str(p).startswith(str(d)) or not p.is_file():
        raise HTTPException(400, "非法路径")
    return FileResponse(p, filename=p.name)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), filename: str | None = Form(None)) -> dict:
    """Browser fallback for file selection: save the uploaded file into
    ``input/`` and return its path. The Tauri desktop shell instead uses the
    native dialog to get a path directly, so this endpoint is the browser path
    into the same ``input/`` directory."""
    layout = get_layout()
    layout.input.mkdir(parents=True, exist_ok=True)
    name = Path(filename or file.filename or "upload.bin").name  # strip any directory
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传内容为空。")
    dest = layout.input / name
    dest.write_bytes(data)
    return {"path": str(dest), "name": name, "size": len(data)}
