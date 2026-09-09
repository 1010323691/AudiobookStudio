"""Invariant tests for the book-chunking engine (``backend/engines/book.py``).

These are the Python port of BookChunker's behavioural contract (its CLAUDE.md
invariants): chapters tile the whole text; cuts fall only on chapter boundaries;
chapters are never renumbered; no chapters -> stop (no forced split); and
concatenating all volumes reproduces the original exactly. Plus unit checks for
encoding detection, character counting, volume-count choice, Chinese numerals and
the exact output-file naming.
"""
from __future__ import annotations

import re

from backend.engines import book as B


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_novel(num_chapters: int = 20, body_repeats: int = 40) -> str:
    """Build a synthetic novel: a preamble, then ``num_chapters`` numbered
    chapters with bodies long enough to force several volumes at a modest target."""
    lines = ["这是一部用于测试的分册小说。", "前言内容，若干行。", ""]
    for i in range(1, num_chapters + 1):
        lines.append(f"第{i}章 标题{i}")
        # A body of fixed length with embedded newlines, so the newline-exclusion
        # counting path is exercised (each "段。" line ends in a newline).
        for _ in range(body_repeats):
            lines.append(f"这是第{i}章的一段正文内容。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Character counting
# --------------------------------------------------------------------------- #

def test_range_char_count_excludes_newlines():
    text = "abc\ndef\n"  # positions: a0 b1 c2 \n3 d4 e5 f6 \n7
    nl = B.build_newline_positions(text)
    assert nl == [3, 7]
    assert B.range_char_count(nl, 0, 8) == 6  # a b c d e f
    assert B.range_char_count(nl, 0, 3) == 3  # a b c
    assert B.range_char_count(nl, 4, 7) == 3  # d e f
    assert B.range_char_count(nl, 3, 3) == 0  # empty range
    # The whole-text count equals non-newline code points.
    assert sum(1 for c in text if c not in "\r\n") == B.range_char_count(nl, 0, len(text))


def test_range_char_count_multibyte_code_points():
    # Emoji (non-BMP) count as one code point each, matching Python len().
    text = "你好🌍🔥"  # 6 code points, 2 of them non-BMP
    nl = B.build_newline_positions(text)
    assert B.range_char_count(nl, 0, len(text)) == len(text)


# --------------------------------------------------------------------------- #
# Chapter detection + tiling
# --------------------------------------------------------------------------- #

def test_chapters_tile_the_whole_text():
    text = make_novel(20)
    analysis = B.analyze_text(text)
    chs = analysis["chapters"]
    assert len(chs) == 20

    # Preamble is absorbed into the first chapter: it starts at 0.
    assert chs[0]["start"] == 0
    # The last chapter runs to the end of the file.
    assert chs[-1]["end"] == len(text)
    # Contiguous tiling: chapter i ends where chapter i+1 begins.
    for i in range(len(chs) - 1):
        assert chs[i]["end"] == chs[i + 1]["start"]
    # Each chapter has a real (non-negative) length.
    for c in chs:
        assert c["end"] > c["start"]
        assert c["chars"] >= 0
    # Chapter numbers parsed as 1..20.
    assert [c["num"] for c in chs] == list(range(1, 21))


def test_chapter_header_positions_are_real_headers():
    text = make_novel(10)
    analysis = B.analyze_text(text)
    chs = analysis["chapters"]
    # From chapter 2 onward, the slice begins at its "第N章" header.
    for i in range(1, len(chs)):
        assert text[chs[i]["start"]].startswith("第")


def test_no_chapters_yields_no_volumes():
    text = "这是一段没有任何章节标记的普通文本。\n它只是正文，没有第几章。"
    analysis = B.analyze_text(text)
    assert analysis["chapters"] == []
    assert B.compute_volumes(analysis["chapters"], 1000) == []  # stop, never force-split


# --------------------------------------------------------------------------- #
# Volume computation
# --------------------------------------------------------------------------- #

def test_choose_volume_count():
    # exact single volume
    assert B.choose_volume_count(100, 100, 10) == 1
    # total/target = 1.67 -> closer to 2
    assert B.choose_volume_count(100, 60, 10) == 2
    # total/target = 2.5 -> closer to 3
    assert B.choose_volume_count(100, 40, 10) == 3
    # clamped to N (can't exceed chapter count)
    assert B.choose_volume_count(100, 30, 3) == 3
    # tiny file -> one volume (kLo clamps to 1, kHi == 1)
    assert B.choose_volume_count(50, 100, 10) == 1


def test_volumes_partition_chapters_contiguously():
    text = make_novel(30)
    analysis = B.analyze_text(text)
    vols = B.compute_volumes(analysis["chapters"], 1500)
    N = len(analysis["chapters"])
    assert len(vols) >= 2

    assert vols[0]["firstChapter"] == 0
    assert vols[-1]["lastChapter"] == N - 1
    for a, b in zip(vols, vols[1:]):
        assert a["lastChapter"] + 1 == b["firstChapter"]
    for v in vols:
        assert v["firstChapter"] <= v["lastChapter"]


# --------------------------------------------------------------------------- #
# The core invariant: round-trip
# --------------------------------------------------------------------------- #

def test_round_trip_concatenation_equals_original():
    text = make_novel(40)
    analysis = B.analyze_text(text)
    vols = B.compute_volumes(analysis["chapters"], 1200)
    assert len(vols) >= 2

    joined = "".join(B.volume_content(analysis, v) for v in vols)
    assert joined == text  # no character lost, added, or reordered


def test_round_trip_single_volume():
    text = make_novel(5)
    analysis = B.analyze_text(text)
    vols = B.compute_volumes(analysis["chapters"], 10_000_000)  # huge target -> 1 volume
    assert len(vols) == 1
    assert B.volume_content(analysis, vols[0]) == text


# --------------------------------------------------------------------------- #
# Output-file naming (exact contract)
# --------------------------------------------------------------------------- #

def test_volume_filenames_format_and_padding():
    # Chapters numbered 1..5 -> width max(3, 1) = 3; two volumes -> width 2.
    chapters = [
        {"num": 1, "numStr": "1"},
        {"num": 2, "numStr": "2"},
        {"num": 3, "numStr": "3"},
        {"num": 4, "numStr": "4"},
        {"num": 5, "numStr": "5"},
    ]
    volumes = [
        {"firstChapter": 0, "lastChapter": 1},  # 第1章 ~ 第2章
        {"firstChapter": 2, "lastChapter": 4},  # 第3章 ~ 第5章
    ]
    names = B.make_volume_filenames("测试小说", volumes, chapters)
    assert names == [
        "测试小说 分册01 第001章 ~ 第002章.txt",
        "测试小说 分册02 第003章 ~ 第005章.txt",
    ]


def test_volume_filenames_widen_with_largest_number():
    # Largest chapter number is 1234 -> width 4 (>= 3).
    chapters = [{"num": 1, "numStr": "1"}, {"num": 1234, "numStr": "1234"}]
    volumes = [{"firstChapter": 0, "lastChapter": 1}]
    names = B.make_volume_filenames("书", volumes, chapters)
    assert names == ["书 分册01 第0001章 ~ 第1234章.txt"]


def test_volume_filenames_keep_original_non_numeric_labels():
    # A chapter whose number can't be parsed keeps its raw label, unpadded.
    chapters = [
        {"num": None, "numStr": "楔子"},
        {"num": 1, "numStr": "1"},
    ]
    volumes = [{"firstChapter": 0, "lastChapter": 1}]
    names = B.make_volume_filenames("书", volumes, chapters)
    # 楔子 is unpadded; chapter 1 padded to width 3.
    assert names == ["书 分册01 第楔子章 ~ 第001章.txt"]


def test_volume_filenames_never_renumber():
    # A gap (1, 2, 5) must be preserved, not renumbered to 1,2,3.
    chapters = [
        {"num": 1, "numStr": "1"},
        {"num": 2, "numStr": "2"},
        {"num": 5, "numStr": "5"},
    ]
    volumes = [{"firstChapter": 0, "lastChapter": 2}]
    names = B.make_volume_filenames("书", volumes, chapters)
    assert names == ["书 分册01 第001章 ~ 第005章.txt"]


def test_base_name_and_sanitizing():
    assert B.base_name("novel.txt") == "novel"
    assert B.base_name(r"C:\books\novel.txt") == "novel"
    assert B.base_name("noext") == "noext"
    assert B.sanitize_file_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


# --------------------------------------------------------------------------- #
# Encoding detection
# --------------------------------------------------------------------------- #

def test_decode_utf8_bom():
    data = b"\xef\xbb\xbf" + "你好".encode("utf-8")
    text, enc = B.decode_buffer(data)
    assert text == "你好"
    assert enc == "UTF-8（含 BOM）"


def test_decode_utf16_le_bom():
    data = b"\xff\xfe" + "你好".encode("utf-16-le")
    text, enc = B.decode_buffer(data)
    assert text == "你好"
    assert enc == "UTF-16 LE（含 BOM）"


def test_decode_plain_utf8_cjk():
    text = "你好世界，这是一段中文文本。" * 20
    text, enc = B.decode_buffer(text.encode("utf-8"))
    assert enc == "UTF-8"


def test_decode_gbk_fallback():
    original = "你好世界，测试文本，中文内容。" * 50
    data = original.encode("gbk")  # not valid UTF-8 -> falls back to GB18030
    text, enc = B.decode_buffer(data)
    assert enc == "GB18030 / GBK"
    assert text == original


# --------------------------------------------------------------------------- #
# Chinese-numeral parsing
# --------------------------------------------------------------------------- #

def test_parse_cn_number():
    cases = {
        "一": 1, "二": 2, "两": 2, "九": 9,
        "十": 10, "十一": 11, "二十": 20, "九十九": 99,
        "一百": 100, "一千": 1000, "一千零一": 1001,
    }
    for s, expected in cases.items():
        assert B.parse_cn_number(s) == expected, s


def test_parse_cn_number_invalid():
    assert B.parse_cn_number("") is None
    assert B.parse_cn_number("〇") is None  # total 0 -> None
    assert B.parse_cn_number("abc") is None
    assert B.parse_cn_number("千") is None  # leading 千 with no preceding digit


def test_parse_chapter_number():
    assert B.parse_chapter_number("12") == 12
    assert B.parse_chapter_number("二十一") == 21
    assert B.parse_chapter_number("") is None
    assert B.parse_chapter_number(None) is None


# --------------------------------------------------------------------------- #
# Chapter-sequence checks (informational)
# --------------------------------------------------------------------------- #

def _chs(*numstrs):
    return [{"seq": i + 1, "numStr": s} for i, s in enumerate(numstrs)]


def test_sequence_gap():
    rep = B.check_chapter_sequence(_chs("1", "2", "3", "5"))
    assert rep["hasIssues"]
    assert rep["gaps"] == [{"after": 3, "missing": [4]}]


def test_sequence_duplicate():
    rep = B.check_chapter_sequence(_chs("1", "2", "2", "3"))
    assert rep["hasIssues"]
    assert rep["duplicates"] == [{"seq": 3, "num": 2}]


def test_sequence_disorder():
    rep = B.check_chapter_sequence(_chs("3", "1", "2"))
    assert rep["hasIssues"]
    assert any(d["num"] == 1 for d in rep["disorder"])


def test_sequence_clean():
    rep = B.check_chapter_sequence(_chs("1", "2", "3", "4"))
    assert not rep["hasIssues"]
    assert rep["first"] == 1 and rep["last"] == 4


# --------------------------------------------------------------------------- #
# ZIP
# --------------------------------------------------------------------------- #

def test_build_zip_roundtrip(tmp_path):
    out = tmp_path / "out.zip"
    B.build_zip(
        [("分册01.txt", "第一章内容".encode("utf-8")),
         ("分册02.txt", "第二章内容".encode("utf-8"))],
        out,
    )
    import zipfile

    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["分册01.txt", "分册02.txt"]
        assert zf.read("分册01.txt") == "第一章内容".encode("utf-8")
        # STORED (no compression) -> compress_type is ZIP_STORED
        assert zf.infolist()[0].compress_type == zipfile.ZIP_STORED
