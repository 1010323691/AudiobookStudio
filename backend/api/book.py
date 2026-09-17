"""Book-splitting endpoints (module: 分册, the split half of 排版与分册).

``POST /api/book/analyze`` previews the split (chapters + per-chapter filenames,
no files written); ``POST /api/book/split`` writes ONE file per chapter to the
workspace's ``02_split_text/`` (or a single ``<base> 全书.txt`` when
``whole_book=True``), optionally a STORE zip as well. Both honour the "no
chapters -> stop, never force-split" invariant — only an explicit ``whole_book``
request bypasses it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.paths import get_layout
from ..engines import book as B
from . import _common

router = APIRouter(prefix="/api/book", tags=["book"])


class AnalyzeRequest(BaseModel):
    path: str


class SplitRequest(BaseModel):
    path: str
    base: str | None = None  # override the base (file) name
    as_zip: bool = False
    whole_book: bool = False  # write the entire text as one ``<base> 全书.txt``


def _prepare(path: str) -> dict:
    text, enc, src = _common.read_decoded_file(path)
    analysis = B.analyze_text(text)
    chapters = analysis["chapters"]
    base = B.base_name(src.name)
    return {
        "text": text,
        "analysis": analysis,
        "encoding": enc,
        "src": src,
        "chapters": chapters,
        "base": base,
        "filenames": B.make_chapter_filenames(base, chapters),
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
    prep = _prepare(req.path)
    chapters = prep["chapters"]
    result = {
        "source": str(prep["src"]),
        "encoding": prep["encoding"],
        "base": prep["base"],
        "total_chars": prep["analysis"]["totalChars"],
        "chapters": _chapters_payload(chapters),
        "chapter_count": len(chapters),
        "filenames": prep["filenames"],
        "expected_format": B.EXPECTED_CHAPTER_FORMAT,
        "sequence": B.check_chapter_sequence(chapters),
        "error": None,
    }
    if not chapters:
        result["error"] = (
            f"未检测到章节（系统识别的格式：{B.EXPECTED_CHAPTER_FORMAT}）。"
            "可「不处理，按整本继续」（整本输出为单个文件），或重新上传原文。"
        )
    return result


@router.post("/split")
def split(req: SplitRequest) -> dict:
    _common.require_workspace()
    prep = _prepare(req.path)
    chapters, analysis = prep["chapters"], prep["analysis"]

    base = (req.base or prep["base"]).strip() or prep["base"]
    layout = get_layout()

    if req.whole_book:
        # Explicit opt-in: write the whole text as a single file (no chapters
        # detected, user chose to continue) — the parse stage chunks it.
        name = B.make_whole_book_filename(base)
        data = prep["text"].encode("utf-8")
        out_path = layout.split_text / name
        # Write raw bytes (no newline translation) so the file reproduces the
        # source's line endings exactly — Path.write_text would turn \n into
        # \r\n on Windows and corrupt the round-trip guarantee.
        out_path.write_bytes(data)
        written = [{"name": name, "path": str(out_path), "chars": analysis["totalChars"]}]
        zip_entries = [(name, data)]
    else:
        if not chapters:
            raise HTTPException(
                400, "未检测到章节，无法分册。请先在页面选择「不处理，按整本继续」或重新上传原文。"
            )
        filenames = B.make_chapter_filenames(base, chapters)
        written = []
        zip_entries = []
        for ch, fname in zip(chapters, filenames):
            data = B.chapter_content(analysis, ch).encode("utf-8")
            out_path = layout.split_text / fname
            out_path.write_bytes(data)
            written.append({"name": fname, "path": str(out_path), "chars": ch["chars"]})
            zip_entries.append((fname, data))

    result: dict = {
        "output_dir": str(layout.split_text),
        "file_count": len(written),
        "files": written,
    }
    if req.as_zip:
        zip_path = layout.split_text / f"{base}.zip"
        B.build_zip(zip_entries, zip_path)
        result["zip_path"] = str(zip_path)
    return result
