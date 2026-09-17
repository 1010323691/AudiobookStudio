"""Book chunker core — ported (behavior-preserving) from BookChunker/chunker.js.

Pipeline: ``decode_buffer`` -> ``analyze_text`` (count + ``detect_chapters``) ->
``make_chapter_filenames`` / ``chapter_content`` (one file per chapter;
``make_whole_book_filename`` for the explicit whole-book fallback).

The JS core was written to be Python-compatible (see BookChunker/CLAUDE.md):
character count == Unicode code points excluding line breaks (matches Python
``len()`` on BMP text). Python strings are already code-point sequences (no
surrogate halves), so the counting is a direct, simpler port — a single sorted
list of line-break positions answered by binary search, exactly as the JS does.

Invariants: chapters tile the whole text; each chapter is written as exactly one
file (never split); chapters are never renumbered (the 分册NN index is a
positional sequence number, 第XXX章 carries the original number); no chapters ->
stop (only an explicit ``whole_book`` opt-in writes the single 全书 file);
concatenating all per-chapter files (or the single 全书 file) reproduces the
original exactly.
"""
from __future__ import annotations

import bisect
import re
import zipfile
from pathlib import Path
from typing import Callable, Optional


# ============================ Encoding ============================

def _is_plausible_unit(cp: int) -> bool:
    """Is a code point 'plausible' as real (mostly CJK) text?

    Mirrors the JS ``isPlausibleUnit`` (which inspected UTF-16 units); in Python
    a non-BMP code point (>= 0x10000) is the equivalent of the surrogate check.
    """
    if 0x20 <= cp <= 0x7E:  # ASCII printable
        return True
    if cp in (0x09, 0x0A, 0x0D):  # tab / newlines
        return True
    if 0xA0 <= cp <= 0x24F:  # Latin-1 supplement
        return True
    if 0x3000 <= cp <= 0x303F:  # CJK punctuation
        return True
    if 0x3400 <= cp <= 0x4DBF:  # CJK Extension A
        return True
    if 0x4E00 <= cp <= 0x9FFF:  # CJK unified ideographs
        return True
    if 0xF900 <= cp <= 0xFAFF:  # CJK compatibility
        return True
    if 0xFF00 <= cp <= 0xFFEF:  # fullwidth forms
        return True
    if cp >= 0x10000:  # non-BMP
        return True
    return False


def plausible_ratio(text: str) -> float:
    n = min(len(text), 4000)
    if n == 0:
        return 0
    good = sum(1 for ch in text[:n] if _is_plausible_unit(ord(ch)))
    return good / n


def _try_decode(label: str, data: bytes) -> Optional[str]:
    try:
        return data.decode(label)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_buffer(data: bytes) -> tuple[str, str]:
    """Detect the encoding of a raw buffer and decode it.

    Order: BOM -> strict UTF-8 (with a plausibility cross-check) -> GB18030 -> GBK.
    Returns ``(text, encoding_label)`` or raises ``ValueError``.
    """
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8"), "UTF-8（含 BOM）"
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le"), "UTF-16 LE（含 BOM）"
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be"), "UTF-16 BE（含 BOM）"

    # No BOM: try strict UTF-8 first.
    try:
        utf8 = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        utf8 = None

    if utf8 is not None:
        r = plausible_ratio(utf8)
        if r > 0.9:
            return utf8, "UTF-8"
        # Borderline: a GBK file can occasionally be valid UTF-8 — compare plausibility.
        gb = _try_decode("gb18030", data)
        if gb:
            r_gb = plausible_ratio(gb)
            if r_gb > r + 0.05:
                return gb, "GB18030 / GBK"
        return utf8, "UTF-8"

    # Not valid UTF-8: fall back to GB18030 (a superset of GBK).
    gb = _try_decode("gb18030", data)
    if gb and plausible_ratio(gb) > 0.5:
        return gb, "GB18030 / GBK"
    gbk = _try_decode("gbk", data)
    if gbk:
        return gbk, "GBK"

    raise ValueError("无法识别文件编码，请确认它是一份有效的文本（TXT）文件。")


# ======================= Character counting =======================

def build_newline_positions(text: str) -> list[int]:
    """Sorted positions of every line break (\\n or \\r) in the text."""
    return [i for i, ch in enumerate(text) if ch in "\r\n"]


def range_char_count(newline_positions: list[int], a: int, b: int) -> int:
    """Code points (minus line breaks) in the half-open range [a, b)."""
    if b <= a:
        return 0
    nl = newline_positions
    return (b - a) - (bisect.bisect_left(nl, b) - bisect.bisect_left(nl, a))


# ======================= Chapter detection =======================

# A chapter header line: 第<number>章 followed by a title (<= 50 chars), separated
# by space/full-width space/colon/... or attached directly. MULTILINE so ^ / $
# match at line boundaries, mirroring the JS /gm flag.
BOOK_CHAPTER_RE = re.compile(
    r"^[ \t　]*第([0-9]+|[0-9零〇一二三四五六七八九十百千两]+)章"
    r"[ \t　：:、·—\-–]*([^\r\n]{0,50})[ \t　\r]*$",
    re.MULTILINE,
)

# The only chapter-header shape the splitter recognizes (see BOOK_CHAPTER_RE:
# 「第N章」+ optional <=50-char title, N = Arabic or Chinese numerals). Single
# source of truth, kept next to the regex so the user-facing text can't drift.
# Shipped with the analyze response so the UI shows the real recognition rule.
EXPECTED_CHAPTER_FORMAT = "第N章（如 第1章、第2章；N 为阿拉伯数字或中文数字）"


def detect_chapters(text: str) -> list[dict]:
    """Return contiguous chapter ranges. Chapters tile the whole text: chapter i
    spans ``[start_i, start_{i+1})``; the first starts at 0, the last runs to EOF."""
    found = []
    for m in BOOK_CHAPTER_RE.finditer(text):
        title = m.group(2)
        title = re.sub(r"[ \t　]+$", "", title) if title else ""
        found.append({"index": m.start(), "numStr": m.group(1), "title": title})

    kept = filter_spurious_chapters(found)

    chapters = []
    for i, k in enumerate(kept):
        start = 0 if i == 0 else k["index"]
        end = kept[i + 1]["index"] if i < len(kept) - 1 else len(text)
        chapters.append(
            {
                "seq": i + 1,
                "start": start,
                "end": end,
                "numStr": k["numStr"],
                "num": parse_chapter_number(k["numStr"]),
                "title": k["title"],
                "chars": 0,
            }
        )
    return chapters


def filter_spurious_chapters(found: list[dict]) -> list[dict]:
    """Drop candidates that are isolated dips in the number sequence of their
    neighbours; keeps legitimate renumbering and boundary chapters intact."""
    n = len(found)
    if n < 3:
        return found
    keep = [True] * n
    for i in range(1, n - 1):
        prev = parse_chapter_number(found[i - 1]["numStr"])
        cur = parse_chapter_number(found[i]["numStr"])
        nxt = parse_chapter_number(found[i + 1]["numStr"])
        if prev is None or cur is None or nxt is None:
            continue
        if cur <= prev and nxt > cur and nxt >= prev:  # isolated dip
            keep[i] = False
    return [f for i, f in enumerate(found) if keep[i]]


# ======================= Chapter-number checks =======================

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def parse_cn_number(s: str) -> Optional[int]:
    """Chinese numeral -> int (一…九, 十/百/千, 零/〇, 两; e.g. 一千零一=1001)."""
    total = 0
    num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            u = _CN_UNITS[ch]
            if num == 0:
                if u == 10:
                    total += 10
                else:
                    return None
            else:
                total += num * u
                num = 0
        else:
            return None
    total += num
    return total if total > 0 else None


def parse_chapter_number(num_str: Optional[str]) -> Optional[int]:
    if not num_str:
        return None
    if re.fullmatch(r"[0-9]+", num_str):
        return int(num_str)
    return parse_cn_number(num_str)


def check_chapter_sequence(chapters: list[dict]) -> dict:
    """Report gaps / duplicates / disorder in the detected numbers. Informational
    only — it never changes how the text is split."""
    report = {
        "count": len(chapters),
        "parseable": 0,
        "unparseable": 0,
        "first": None,
        "last": None,
        "gaps": [],
        "duplicates": [],
        "disorder": [],
        "hasIssues": False,
    }
    prev = None
    seen: set[int] = set()
    for c in chapters:
        num = parse_chapter_number(c["numStr"])
        if num is None:
            report["unparseable"] += 1
            continue
        report["parseable"] += 1
        if report["first"] is None:
            report["first"] = num
        report["last"] = num
        if num in seen:
            report["duplicates"].append({"seq": c["seq"], "num": num})
        else:
            seen.add(num)
        if prev is not None:
            if num < prev:
                report["disorder"].append({"seq": c["seq"], "num": num, "prevNum": prev})
            elif num > prev + 1:
                report["gaps"].append({"after": prev, "missing": list(range(prev + 1, num))})
        prev = num
    report["hasIssues"] = bool(report["gaps"] or report["duplicates"] or report["disorder"])
    return report


# ======================= Filenames =======================

def chapter_number(ch: dict):
    """Parsed int when available, else the raw number string (never renumber)."""
    return ch["num"] if ch["num"] is not None else ch["numStr"]


def pad_chapter_number(n, w: int) -> str:
    return str(n).zfill(w) if isinstance(n, int) else str(n)


def chapter_number_width(chapters: list[dict]) -> int:
    max_n = 0
    for c in chapters:
        n = c["num"]
        if n is not None and n > max_n:
            max_n = n
    return max(3, len(str(max_n)))


def base_name(file_name: str) -> str:
    b = file_name.replace("\\", "/").split("/")[-1]
    dot = b.rfind(".")
    if dot > 0:
        b = b[:dot]
    return b or file_name


def sanitize_file_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def make_chapter_filenames(base: str, chapters: list[dict]) -> list[str]:
    """``<base> 分册NN 第XXX章.txt`` — one file per chapter. ``NN`` is the 1-based
    sequential volume index (width max(2, digits of the chapter count); it also
    disambiguates duplicated chapter numbers); ``XXX`` is the chapter's ORIGINAL
    number (width max(3, digits of the largest number), never renumbered); an
    unparseable number keeps its raw ``numStr`` unpadded."""
    chap_width = chapter_number_width(chapters)
    nn_width = max(2, len(str(len(chapters))))
    out = []
    for i, ch in enumerate(chapters):
        name = (
            f"{base} 分册{str(i + 1).zfill(nn_width)} "
            f"第{pad_chapter_number(chapter_number(ch), chap_width)}章.txt"
        )
        out.append(sanitize_file_name(name))
    return out


def make_whole_book_filename(base: str) -> str:
    """Whole-book fallback name (zero chapters detected, user chose to continue):
    ``<base> 全书.txt`` (sanitized like chapter names)."""
    return sanitize_file_name(f"{base} 全书.txt")


# ======================= Analysis =======================

def analyze_text(
    text: str, on_progress: Optional[Callable[[float], None]] = None
) -> dict:
    """Decoded text -> ``{text, newline_positions, chapters, totalChars}``."""
    newline_positions = build_newline_positions(text)
    if on_progress:
        on_progress(0.6)

    def char_count(a: int, b: int) -> int:
        return range_char_count(newline_positions, a, b)

    chapters = detect_chapters(text)
    for c in chapters:
        c["chars"] = char_count(c["start"], c["end"])
    total_chars = char_count(0, len(text))
    return {
        "text": text,
        "newline_positions": newline_positions,
        "chapters": chapters,
        "totalChars": total_chars,
    }


def chapter_content(analysis: dict, chapter: dict) -> str:
    """One chapter's content = one slice of the original text. Concatenating the
    per-chapter slices in order reproduces the original exactly."""
    return analysis["text"][chapter["start"]: chapter["end"]]


# ======================= ZIP (STORE) =======================

def build_zip(files: list[tuple[str, bytes]], out_path: Path) -> None:
    """Write an uncompressed (STORE) ZIP with UTF-8 filenames (replaces the hand
    -written JS writer; stdlib ``zipfile`` is equivalent)."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for name, data in files:
            zf.writestr(name, data)
