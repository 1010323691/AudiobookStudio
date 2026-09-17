"""Book-splitting endpoints (module: 分册, the split half of 排版与分册).

``POST /api/book/analyze`` previews the split (chapters + per-chapter filenames,
no files written); ``POST /api/book/split`` writes ONE file per chapter to the
workspace's ``02_split_text/`` (or a single ``<base> 全书.txt`` when
``whole_book=True``), optionally a STORE zip as well. Both honour the "no
chapters -> stop, never force-split" invariant — only an explicit ``whole_book``
request bypasses it. ``POST /api/book/smart-split`` (智能识别) mechanically
repairs the chapter structure and writes renumbered ``第 NNN 章 标题.txt``
files into the same directory, always reporting every inferred action.
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
    smart: bool = False  # split by the SMART-REPAIRED structure (智能识别结果)


class SmartSplitRequest(BaseModel):
    path: str
    as_zip: bool = False


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
        if req.smart:
            # 按智能识别结果分册：重跑确定性修复（同输入 → 同结果），写出与
            # /smart-split 完全相同的「第 NNN 章 标题.txt」文件——不产生第二套
            # 命名，02_split_text/ 里只有一套章节文件。
            repair = B.smart_repair(prep["text"], chapters)
            if repair["status"] == "error":
                raise HTTPException(400, repair.get("error") or "智能识别失败，请检查原文。")
            chapters = repair["chapters"]
            filenames = B.make_smart_filenames(chapters)
        else:
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
        if not req.whole_book and req.smart:
            # Same content as /smart-split → same zip name, so 02_split_text/
            # keeps a single smart zip (no second, conflicting archive).
            zip_name = B.sanitize_file_name(f"{base} 智能识别.zip")
        else:
            zip_name = f"{base}.zip"
        zip_path = layout.split_text / zip_name
        B.build_zip(zip_entries, zip_path)
        result["zip_path"] = str(zip_path)
    return result


@router.post("/smart-split")
def smart_split(req: SmartSplitRequest) -> dict:
    """智能识别：mechanically repair the chapter structure (renumber 1..N in
    physical order, split abnormally long chapters, drop exact-duplicate
    chapters) and write one file per repaired chapter as ``第 NNN 章 标题.txt``
    into ``02_split_text/``. Always produces output when chapters exist —
    every inferred action is reported with a confidence level. Never rewrites
    file content; existing split files are left untouched."""
    _common.require_workspace()
    prep = _prepare(req.path)
    chapters, analysis = prep["chapters"], prep["analysis"]
    if not chapters:
        raise HTTPException(
            400, "未检测到章节，无法智能识别。请先排版并确认系统识别到章节，或重新上传原文。"
        )

    repair = B.smart_repair(prep["text"], chapters)
    if repair["status"] == "error":
        raise HTTPException(400, repair.get("error") or "智能识别失败，请检查原文。")

    layout = get_layout()
    base = prep["base"]
    filenames = B.make_smart_filenames(repair["chapters"])
    written = []
    zip_entries = []
    for ch, fname in zip(repair["chapters"], filenames):
        data = B.chapter_content(analysis, ch).encode("utf-8")
        out_path = layout.split_text / fname
        out_path.write_bytes(data)
        written.append({"name": fname, "path": str(out_path), "chars": ch["chars"]})
        zip_entries.append((fname, data))

    result: dict = {
        "status": repair["status"],
        "output_dir": str(layout.split_text),
        "file_count": len(written),
        "files": written,
        "chapters": [
            {
                "seq": c["seq"],
                "orig_num": c["repair"]["orig_num"],
                "orig_numStr": c["repair"]["orig_numStr"],
                "final_num": c["final_num"],
                "title": c["title"],
                "chars": c["chars"],
                "actions": c["repair"]["actions"],
                "confidence": c["repair"]["confidence"],
            }
            for c in repair["chapters"]
        ],
        "report": repair["report"],
        "baseline_chars": repair["baseline_chars"],
        "original_count": repair["original_count"],
        "expected_format": B.EXPECTED_CHAPTER_FORMAT,
    }
    if req.as_zip:
        zip_path = layout.split_text / B.sanitize_file_name(f"{base} 智能识别.zip")
        B.build_zip(zip_entries, zip_path)
        result["zip_path"] = str(zip_path)
    return result
