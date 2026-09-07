"""Book-splitting endpoints (module: 分册切割).

``POST /api/book/analyze`` previews the split (chapters + volume plan + filenames,
no files written); ``POST /api/book/split`` writes the volumes to
``output/books/`` (optionally a STORE zip as well). Both honour the "no chapters
-> stop, never force-split" invariant.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import get_config
from ..core.paths import get_layout
from ..engines import book as B
from . import _common

router = APIRouter(prefix="/api/book", tags=["book"])


class AnalyzeRequest(BaseModel):
    path: str
    target_chars: int | None = None


class SplitRequest(BaseModel):
    path: str
    target_chars: int | None = None
    base: str | None = None  # override the base (file) name
    as_zip: bool = False


def _target(req) -> int:
    return req.target_chars if req.target_chars else get_config().book.target_chars


def _prepare(path: str, target: int) -> dict:
    text, enc, src = _common.read_decoded_file(path)
    analysis = B.analyze_text(text)
    chapters = analysis["chapters"]
    volumes = B.compute_volumes(chapters, target)
    base = B.base_name(src.name)
    return {
        "text": text,
        "analysis": analysis,
        "encoding": enc,
        "src": src,
        "chapters": chapters,
        "volumes": volumes,
        "base": base,
        "filenames": B.make_volume_filenames(base, volumes, chapters),
        "target": target,
    }


def _chapters_payload(chapters) -> list[dict]:
    return [
        {
            "seq": c["seq"],
            "num": c["num"],
            "numStr": c["numStr"],
            "title": c["title"],
            "chars": c["chars"],
        }
        for c in chapters
    ]


@router.post("/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    prep = _prepare(req.path, _target(req))
    chapters = prep["chapters"]
    result = {
        "source": str(prep["src"]),
        "encoding": prep["encoding"],
        "base": prep["base"],
        "target_chars": prep["target"],
        "total_chars": prep["analysis"]["totalChars"],
        "chapters": _chapters_payload(chapters),
        "chapter_count": len(chapters),
        "volume_count": len(prep["volumes"]),
        "volumes": prep["volumes"],
        "filenames": prep["filenames"],
        "sequence": B.check_chapter_sequence(chapters),
        "error": None,
    }
    if not chapters:
        result["error"] = "未检测到章节（第N章）。请检查文件内容与编码；本工具不会按字数强行切分。"
    return result


@router.post("/split")
def split(req: SplitRequest) -> dict:
    prep = _prepare(req.path, _target(req))
    chapters, volumes = prep["chapters"], prep["volumes"]
    if not chapters:
        raise HTTPException(400, "未检测到章节，无法分册。")

    base = (req.base or prep["base"]).strip() or prep["base"]
    filenames = B.make_volume_filenames(base, volumes, chapters)

    layout = get_layout()
    written = []
    for v, name in zip(volumes, filenames):
        content = B.volume_content(prep["analysis"], v)
        out_path = layout.output_books / name
        # Write raw bytes (no newline translation) so the volume reproduces the
        # source's line endings exactly — Path.write_text would turn \n into \r\n
        # on Windows and corrupt the round-trip guarantee.
        out_path.write_bytes(content.encode("utf-8"))
        written.append({"name": name, "path": str(out_path), "chars": v["chars"]})

    result: dict = {
        "output_dir": str(layout.output_books),
        "file_count": len(written),
        "files": written,
    }
    if req.as_zip:
        zip_path = layout.output_books / f"{base}.zip"
        B.build_zip(
            [(w["name"], B.volume_content(prep["analysis"], v).encode("utf-8"))
             for v, w in zip(volumes, written)],
            zip_path,
        )
        result["zip_path"] = str(zip_path)
    return result
