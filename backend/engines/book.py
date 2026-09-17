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
import hashlib
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


# ======================= Smart repair (智能识别) =======================
#
# Mechanical chapter-structure repair for unproofed novels. Physical order is
# the ground truth; chapter numbers are data to be corrected. Abnormally long
# chapters (suspected of swallowing missing chapters) are split at inferred
# length positions snapped to paragraph boundaries — a gap-filling internal
# title line can never be found by rescan (the top-level spurious filter only
# drops isolated dips, which never fill a gap), so internal title-like lines
# are recorded as evidence only. Duplicate-number chapters with identical
# content are dropped (copy-paste error); all others are kept. The final
# structure is renumbered 1..N in physical order.
#
# This stage ALWAYS produces output (never blocks): every inferred action is
# reported with a confidence level for manual review. It never rewrites file
# content (title lines keep their original numbers) and never mutates inputs.

# chars >= median * LONG_CHAPTER_RATIO -> "abnormally long" (suspected merged
# chapters). A chapter that swallowed ONE missing chapter is ~2x the median,
# so the threshold is 2.0 to flag that primary case; a false positive costs at
# most one reported, low-confidence split when a gap exists (when no gap
# exists — consecutive/last chapter — the long chapter is simply kept
# SILENTLY: older novels legitimately have long chapters and a length flag
# alone is not an anomaly, 2026-09 user refinement) — the user requires
# always-split-and-report for missing numbers, never block.
LONG_CHAPTER_RATIO = 2.0
# Length-anomaly detection needs a stable baseline: fewer chapters than this,
# or a median below MIN_BASELINE_CHARS, disables it (short books / test books).
MIN_LENGTH_SAMPLES = 5
MIN_BASELINE_CHARS = 200
# Inferred split cut points search for the nearest paragraph boundary within
# +/- this fraction of the chapter's per-segment raw span.
INFER_SNAP_TOLERANCE = 0.5
# Smart filename number width: N <= 1000 -> 3 digits (001...), else 4 (0001...).
SMART_FILENAME_WIDTH_THRESHOLD = 1000


def _line_start_of(text: str, pos: int) -> int:
    """Index of the start of the line containing ``pos`` (skips back over the
    \\r\\n sequence preceding it)."""
    i = pos
    while i > 0 and text[i - 1] in "\r\n":
        i -= 1
    return i


def _normalized_fingerprint(content: str) -> str:
    """sha256 of the content with ALL whitespace removed (Python ``\\s`` on str
    is Unicode-aware, so 　/\\r/\\n/\\t all count). Binary same/different test
    only — no similarity thresholds."""
    return hashlib.sha256(re.sub(r"\s+", "", content).encode("utf-8")).hexdigest()


def _index_at_char_count(text: str, nl: list[int], start: int, n_chars: int) -> int:
    """Smallest raw index >= ``start`` whose range char count (line breaks
    excluded) reaches ``n_chars``."""
    if n_chars <= 0:
        return start
    lo, hi = start, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if range_char_count(nl, start, mid) >= n_chars:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _paragraph_boundaries(text: str, a: int, b: int) -> list[int]:
    """Cut points p in [a, b) where text[p] starts a paragraph gap (``\\n\\n`` or
    ``\\r\\n\\r\\n``). Cutting at p keeps every paragraph intact on one side."""
    return [p for p in range(a, b) if text.startswith("\n\n", p) or text.startswith("\r\n\r\n", p)]


def _internal_title_scan(text: str, ch: dict, own_header_line: int) -> list[dict]:
    """Rescan a chapter slice for chapter-title lines (same BOOK_CHAPTER_RE),
    skipping the chapter's own header line. Note the only lines that end up
    INSIDE a detected chapter's slice are the ones ``filter_spurious_chapters``
    dropped at the top level (isolated number dips: number <= previous
    candidate) — a line whose number fills the gap is never dropped, so an
    exact gap-filling title can never be found here; these candidates are
    evidence for the report (suspected copy-paste / renumbering residue), and
    the split itself is always inferred from length. No spurious-filter here —
    it is a whole-sequence heuristic that misfires on a single chapter slice.
    Returns candidates with absolute positions, sorted by position."""
    body = text[ch["start"]: ch["end"]]
    out = []
    for m in BOOK_CHAPTER_RE.finditer(body):
        abs_pos = ch["start"] + m.start()
        if _line_start_of(text, abs_pos) == own_header_line:
            continue  # the chapter's own header line
        title = m.group(2)
        title = re.sub(r"[ \t　]+$", "", title) if title else ""
        out.append(
            {
                "line_start": _line_start_of(text, abs_pos),
                "numStr": m.group(1),
                "num": parse_chapter_number(m.group(1)),
                "title": title,
            }
        )
    out.sort(key=lambda x: x["line_start"])
    return out


def _inferred_cut_points(
    text: str, nl: list[int], ch: dict, k: int, baseline: float
) -> tuple[list[int], bool]:
    """k inferred cut positions (raw indices) inside ``ch`` for splitting it
    into k+1 segments. The model: missing chapters' content was appended
    after the original chapter, so each cut sits ~``baseline`` chars before the
    next. Every cut is snapped to the nearest paragraph boundary (within
    +/- INFER_SNAP_TOLERANCE of the per-segment span, else the nearest one in
    the chapter, else the exact position) and kept strictly monotone. Returns
    (cuts, used_exact) — used_exact means at least one cut had no blank line
    to snap to (the cut is mid-paragraph, flagged by the caller)."""
    start, end = ch["start"], ch["end"]
    seg_span = (end - start) / (k + 1)
    # The trailing \n\n (separator before the next chapter header) is a
    # paragraph boundary at the very end of the slice — snapping a cut there
    # would leave a char-empty final segment, so it (and any boundary with no
    # non-newline content left of the chapter end) is excluded.
    boundaries = [
        b for b in _paragraph_boundaries(text, start, end)
        if range_char_count(nl, b, end) > 0
    ]
    cuts: list[int] = []
    used_exact = False
    prev = start
    for m in range(1, k + 1):
        t = _index_at_char_count(text, nl, start, int(baseline * m))
        t = max(t, prev + 1)
        lo = int(t - INFER_SNAP_TOLERANCE * seg_span)
        hi = int(t + INFER_SNAP_TOLERANCE * seg_span)
        window = [b for b in boundaries if lo <= b <= hi and prev < b < end]
        if window:
            pick = min(window, key=lambda b: (abs(b - t), b))
        else:
            allc = [b for b in boundaries if prev < b < end]
            if allc:
                pick = min(allc, key=lambda b: (abs(b - t), b))
            else:
                pick = min(t, end - 1)
                used_exact = True
        if pick <= prev:
            continue  # cannot keep monotone -> drop this cut (fewer segments)
        cuts.append(pick)
        prev = pick
    return cuts, used_exact


def smart_repair(text: str, chapters: list[dict], on_progress: Optional[Callable[[float], None]] = None) -> dict:
    """Mechanically repair the chapter structure of ``chapters`` (as produced
    by ``analyze_text``) and renumber 1..N in physical order. Pure function:
    inputs are not mutated.

    Returns ``{status: "ok"|"clean", chapters (with start/end/final_num/
    repair), final_numbers, report {actions, warnings, removed},
    baseline_chars, original_count}``. A self-check (lossless concatenation,
    tiling, non-empty segments, unique 1..N numbering) runs last; on failure
    ``status="error"`` with a diagnostic ``error`` message and no output."""
    if on_progress:
        on_progress(0.1)
    warnings: list[dict] = []
    removed: list[dict] = []
    removed_spans: list[tuple[int, int]] = []  # raw ranges deleted from the text
    if not chapters:
        return {
            "status": "ok",
            "chapters": [],
            "final_numbers": [],
            "report": {"actions": [], "warnings": [], "removed": []},
            "baseline_chars": None,
            "original_count": 0,
        }

    nl = build_newline_positions(text)

    def char_count(a: int, b: int) -> int:
        return range_char_count(nl, a, b)

    work = [dict(c) for c in chapters]
    for c in work:
        c["chars"] = char_count(c["start"], c["end"])

    # -- step 1: baseline (median) + abnormally-long flags -----------------
    baseline: Optional[float] = None
    if len(work) >= MIN_LENGTH_SAMPLES:
        s = sorted(c["chars"] for c in work)
        mid = len(s) // 2
        med = float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        if med >= MIN_BASELINE_CHARS:
            baseline = med
    if baseline is not None:
        for c in work:
            c["_long"] = c["chars"] >= baseline * LONG_CHAPTER_RATIO
    else:
        for c in work:
            c["_long"] = False
        reason = (
            "章节数少于 5 章，" if len(work) < MIN_LENGTH_SAMPLES else "章节字数中位数过低（<200 字），"
        )
        warnings.append(
            {"type": "length_disabled", "detail": reason + "长度异常检测已禁用"}
        )

    # Each chapter's own header line (chapter 1's slice starts at 0 and
    # contains the preamble, so its header is the first top-level match —
    # filter_spurious_chapters never removes the first candidate).
    first_m = next((m for m in BOOK_CHAPTER_RE.finditer(text)), None)
    own_header_lines = []
    for i, c in enumerate(work):
        # Chapter 1's slice starts at 0 and contains the preamble, so its
        # header is the first top-level match (filter_spurious_chapters never
        # removes the first candidate). Every other chapter's slice begins at
        # its own header line.
        own_header_lines.append(
            _line_start_of(text, first_m.start()) if i == 0 and first_m
            else _line_start_of(text, c["start"])
        )

    # -- steps 2+3: handle absorbed duplicates + abnormally long chapters --
    # The rescan can only find the isolated dips filter_spurious_chapters
    # dropped (number <= previous candidate) — a gap-filling number is never
    # dropped, so an exact gap-filling title can never appear inside a slice.
    # The two forms of findable lines:
    #   * same number as its own chapter = absorbed duplicate (the spurious
    #     filter dropped the second copy, which this chapter swallowed):
    #     fingerprint head vs tail — truncate if identical, else keep both
    #     as a two-chapter split. Runs on EVERY parseable chapter, not only
    #     long ones: a chapter that swallowed one copy is ~2x the baseline,
    #     below the 3x "long" threshold;
    #   * any other number = evidence only; the gap split itself is always
    #     inferred from length (gated on _long).
    inferred_splits: dict[int, list[int]] = {}  # work index -> raw cut points
    split_nums: dict[int, list[tuple[str, Optional[int], str]]] = {}  # work index -> per-segment (numStr, num, title)
    if on_progress:
        on_progress(0.4)
    for i, c in enumerate(work):
        own = c["num"]
        nxt = work[i + 1]["num"] if i + 1 < len(work) else None
        if own is None and c["_long"]:
            warnings.append(
                {
                    "type": "long_kept",
                    "detail": (
                        f"第{c['numStr']}章异常长（{c['chars']}字）且章号无法解析，"
                        "无法校验内部结构，原样保留"
                    ),
                }
            )
            continue
        if own is not None:
            cands = _internal_title_scan(text, c, own_header_lines[i])
            dup_cands = [x for x in cands if x["num"] == own]
            if dup_cands:
                # Absorbed duplicate: its own copy dropped by the spurious
                # filter and swallowed into this chapter (appended at the tail).
                last = dup_cands[-1]
                head_fp = _normalized_fingerprint(text[c["start"]: last["line_start"]])
                tail_fp = _normalized_fingerprint(text[last["line_start"]: c["end"]])
                if head_fp == tail_fp:
                    # Identical content twice -> drop the duplicate (keep the first).
                    old_end = c["end"]
                    c["end"] = last["line_start"]
                    removed_spans.append((last["line_start"], old_end))
                    c["chars"] = char_count(c["start"], c["end"])
                    c["_truncated"] = True
                    removed.append(
                        {
                            "seq": c["seq"],
                            "num": own,
                            "numStr": last["numStr"],
                            "title": last["title"],
                            "kind": "truncated",
                        }
                    )
                    warnings.append(
                        {
                            "type": "duplicate_truncated",
                            "detail": (
                                f"第{own}章出现两次且正文完全相同（疑似复制错误），已删除重复的一份"
                            ),
                        }
                    )
                else:
                    # Different content -> keep both: split at the duplicated line.
                    inferred_splits[i] = [last["line_start"]]
                    split_nums[i] = [
                        (c["numStr"], c["num"], c["title"]),
                        (last["numStr"], last["num"], last["title"]),
                    ]
                    c["_dup_split"] = True
                    warnings.append(
                        {
                            "type": "duplicate_split_kept",
                            "detail": (
                                f"第{own}章出现两次且正文不同，两份均保留并重新编号，请人工核对"
                            ),
                        }
                    )
                # A gap exists only when nxt > own + 1 (own < nxt also holds
                # for the consecutive case, which must NOT warn).
                if nxt is not None and nxt > own + 1:
                    gap_names = "、".join(str(g) for g in range(own + 1, nxt))
                    warnings.append(
                        {
                            "type": "gap_after_duplicate",
                            "detail": (
                                f"重复的第{own}章之后存在缺号区间（第{gap_names}章），"
                                "已由重编号吸收，请人工核对"
                            ),
                        }
                    )
                continue
            if not c["_long"]:
                continue
            # No missing-number interval to fill (last chapter, or the next
            # number is consecutive / out-of-order / duplicate): there is no
            # evidence of a swallowed chapter — an abnormally long chapter is
            # just a long chapter (older novels legitimately have long
            # chapters). Kept as-is SILENTLY (no warning): the user requires
            # alerts only when a gap could not be resolved. (An unparseable
            # number still warns above, since that anomaly is invisible
            # elsewhere in the report.)
            if nxt is None or nxt <= own + 1:
                continue
            gap = list(range(own + 1, nxt))
            gap_names = "、".join(str(g) for g in gap)
            if cands:
                warnings.append(
                    {
                        "type": "internal_title_evidence",
                        "detail": (
                            f"第{c['num']}章内部发现 {len(cands)} 行似章节标题行（"
                            + "、".join(f"第{x['numStr']}章" for x in cands)
                            + f"），与缺号（第{gap_names}章）不符，已记录供人工核对"
                        ),
                    }
                )
            cuts, used_exact = _inferred_cut_points(text, nl, c, len(gap), baseline)
            inferred_splits[i] = cuts
            split_nums[i] = (
                [(c["numStr"], c["num"], c["title"])]
                + [(str(g), g, "") for g in gap[: len(cuts)]]
            )
            c["_explained"] = True
            warnings.append(
                {
                    "type": "inferred_split",
                    "detail": (
                        f"第{c['num']}章（{c['chars']}字）疑似包含缺失的"
                        f"第{gap_names}章：已按推断长度拆分为 {len(cuts) + 1} 段，请人工核对"
                    ),
                }
            )
            if used_exact:
                warnings.append(
                    {
                        "type": "inferred_split_mid_paragraph",
                        "detail": (
                            f"第{c['num']}章内部没有空行可对齐，推断切点落在段落中间，"
                            "请务必人工核对"
                        ),
                    }
                )

    if on_progress:
        on_progress(0.6)

    # -- step 4: fingerprints + duplicate removal --------------------------
    for c in work:
        c["fingerprint"] = _normalized_fingerprint(text[c["start"]: c["end"]])
    # duplicate groups (same parseable number), physical order
    groups: dict[int, list[dict]] = {}
    for c in work:
        if c["num"] is not None:
            groups.setdefault(c["num"], []).append(c)
    drop: set[int] = set()  # ids() of chapters to drop
    for num, members in groups.items():
        kept_fps: list[str] = [members[0]["fingerprint"]]  # first occurrence always kept
        for c in members[1:]:
            if c["fingerprint"] in kept_fps:
                drop.add(id(c))
                removed_spans.append((c["start"], c["end"]))
                removed.append(
                    {
                        "seq": c["seq"],
                        "num": num,
                        "numStr": c["numStr"],
                        "title": c["title"],
                        "kind": "dropped",
                    }
                )
            else:
                kept_fps.append(c["fingerprint"])
    # cross-number identical content -> warning only, never dropped
    seen_fp: dict[str, dict] = {}
    for c in work:
        fp = c["fingerprint"]
        if fp in seen_fp and id(c) not in drop:
            other = seen_fp[fp]
            if other["num"] != c["num"]:
                warnings.append(
                    {
                        "type": "content_collision",
                        "detail": (
                            f"第{other['numStr']}章与第{c['numStr']}章正文完全相同"
                            "（章号不同），未做处理，请留意"
                        ),
                    }
                )
        else:
            seen_fp[fp] = c

    # gap-before flag (same chain semantics as check_chapter_sequence: the
    # number jumped up from the last parseable predecessor) — report only.
    prev_num: Optional[int] = None
    for c in work:
        if c["num"] is None:
            continue
        if prev_num is not None and c["num"] > prev_num + 1:
            c["_gap_before"] = True
        prev_num = c["num"]

    # -- build the repaired structure (splits + drops) ---------------------
    new_work: list[dict] = []
    for i, c in enumerate(work):
        if id(c) in drop:
            continue
        cuts = inferred_splits.get(i)
        if not cuts:
            new_work.append(c)
            continue
        # Per-segment numbering was fixed up front: inferred splits fill the
        # gap numbers in order, a duplicate split keeps both copies' numbers.
        nums = split_nums[i]
        points = [c["start"]] + list(cuts) + [c["end"]]
        for j in range(len(points) - 1):
            s, e = points[j], points[j + 1]
            if e <= s or char_count(s, e) == 0:
                return {
                    "status": "error",
                    "error": f"拆分产生空段落（第{c['num']}章），边界异常，已放弃输出。",
                    "chapters": [],
                    "final_numbers": [],
                    "report": {"actions": [], "warnings": warnings, "removed": removed},
                    "baseline_chars": baseline,
                    "original_count": len(chapters),
                }
            seg = dict(c)
            seg["start"], seg["end"] = s, e
            seg["chars"] = char_count(s, e)
            seg["numStr"], seg["num"], seg["title"] = nums[j]
            seg["fingerprint"] = _normalized_fingerprint(text[s:e])  # per-segment
            seg["_parent_seq"] = c["seq"]
            seg["_split_kind"] = "duplicate_split" if c.get("_dup_split") else "inferred_split"
            seg["_seg_index"] = j
            new_work.append(seg)

    # -- step 5: renumber 1..N in physical order + repair records ----------
    for pos, c in enumerate(new_work):
        c["seq"] = pos + 1
        c["final_num"] = pos + 1
    if on_progress:
        on_progress(0.8)

    dup_nums = {n for n, ms in groups.items() if len(ms) >= 2}

    _conf_rank = {"high": 2, "medium": 1, "low": 0}  # high > medium > low
    actions: list[dict] = []
    for c in new_work:
        acts: list[str] = []
        levels: list[str] = ["high"]

        def add_action(kind: str, level: str = "high") -> None:
            if kind not in acts:
                acts.append(kind)
                levels.append(level)

        if c.get("_split_kind") == "inferred_split":
            add_action("inferred_split", "low")
        elif c.get("_split_kind") == "duplicate_split":
            add_action("duplicate_kept", "medium")
        if c.get("_truncated"):
            add_action("duplicate_truncated", "medium")
        if c["num"] in dup_nums:
            add_action("duplicate_kept", "medium")
        if c["num"] is not None and c["num"] != c["final_num"]:
            acts.append("renumbered")
        # inferred segments (j>0) exist to fill the missing numbers; unsplit
        # chapters absorb a gap when their number jumped up from the previous
        # parseable one. Duplicate-split segments fill no gap.
        is_inferred_seg = (
            c.get("_split_kind") == "inferred_split" and c.get("_seg_index", 0) > 0
        )
        if is_inferred_seg or c.get("_gap_before"):
            acts.append("gap_absorbed")
        if not acts:
            acts.append("kept")
        conf = min(levels, key=_conf_rank.get)  # lowest confidence level wins
        c["repair"] = {
            "orig_num": c["num"],
            "orig_numStr": c["numStr"],
            "final_num": c["final_num"],
            "actions": acts,
            "confidence": conf,
        }
        actions.append(
            {
                "seq": c["seq"],
                "orig_num": c["num"],
                "orig_numStr": c["numStr"],
                "orig_title": c["title"],
                "final_num": c["final_num"],
                "actions": acts,
                "confidence": conf,
            }
        )

    # -- step 6: safety self-check (never ship a broken structure) ---------
    # Dropped duplicate spans are excised, so the invariant is: the kept
    # chapters + the dropped spans tile [0, len(text)] exactly, and the kept
    # chapters concatenate to the original text with those spans removed
    # (no character lost, added, or reordered within the kept set).
    if on_progress:
        on_progress(0.9)
    all_spans = sorted([(c["start"], c["end"]) for c in new_work] + removed_spans)
    tiling_ok = all(b[0] == a[1] for a, b in zip(all_spans, all_spans[1:]))
    tiling_ok = tiling_ok and bool(all_spans) and all_spans[0][0] == 0 and all_spans[-1][1] == len(text)
    nonempty_ok = all(c["end"] > c["start"] for c in new_work)
    excised = text
    for s, e in sorted(removed_spans, reverse=True):  # reverse keeps indices valid
        excised = excised[:s] + excised[e:]
    joined_ok = "".join(text[c["start"]: c["end"]] for c in new_work) == excised
    numbers = [c["final_num"] for c in new_work]
    numbers_ok = numbers == list(range(1, len(new_work) + 1))
    if not (joined_ok and tiling_ok and nonempty_ok and numbers_ok):
        return {
            "status": "error",
            "error": "结构自检未通过（拼接/边界/编号异常），已放弃输出，请检查原文。",
            "chapters": [],
            "final_numbers": [],
            "report": {"actions": [], "warnings": warnings, "removed": removed},
            "baseline_chars": baseline,
            "original_count": len(chapters),
        }

    # "clean" = nothing found at all (no actions, no warnings); any action or
    # warning makes it "ok" (the report carries the details).
    status = "clean" if all(a["actions"] == ["kept"] for a in actions) and not warnings else "ok"
    out_chapters = []
    for c in new_work:
        out = dict(c)
        for key in (
            "_long",
            "_explained",
            "_parent_seq",
            "_split_kind",
            "_seg_index",
            "_gap_before",
            "_truncated",
            "_dup_split",
        ):
            out.pop(key, None)
        out_chapters.append(out)
    if on_progress:
        on_progress(1.0)
    return {
        "status": status,
        "chapters": out_chapters,
        "final_numbers": numbers,
        "report": {"actions": actions, "warnings": warnings, "removed": removed},
        "baseline_chars": baseline,
        "original_count": len(chapters),
    }


def make_smart_filenames(chapters: list[dict]) -> list[str]:
    """``第 {NNN} 章 {title}.txt`` for smart-repair output. NNN width: N <=
    SMART_FILENAME_WIDTH_THRESHOLD -> 3 digits (001...), else 4 (0001...);
    an empty title yields ``第 {NNN} 章.txt``. Names are sanitized, then
    de-duplicated (a collision appends `` 2``, `` 3``... to the latest
    offender) so the list is always unique. Input chapters must carry
    ``final_num`` (1..N in physical order)."""
    n = len(chapters)
    w = 3 if n <= SMART_FILENAME_WIDTH_THRESHOLD else 4
    used: set[str] = set()
    out: list[str] = []
    for c in chapters:
        num = c.get("final_num", c.get("seq", 1))
        title = (c.get("title") or "").strip()
        base = f"第 {str(num).zfill(w)} 章 {title}.txt" if title else f"第 {str(num).zfill(w)} 章.txt"
        name = sanitize_file_name(base)
        k = 2
        while name in used:
            name = sanitize_file_name(f"{base} {k}")
            k += 1
        used.add(name)
        out.append(name)
    return out
