"""Invariant tests for the book-chunking engine (``backend/engines/book.py``).

These are the Python port of BookChunker's behavioural contract (its CLAUDE.md
invariants): chapters tile the whole text; each chapter is written as exactly one
file (never split); chapters are never renumbered; no chapters -> stop (no forced
split); and concatenating all per-chapter files reproduces the original exactly.
Plus unit checks for encoding detection, character counting, Chinese numerals,
the chapter-sequence report and the exact output-file naming.
"""
from __future__ import annotations

from backend.engines import book as B


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_novel(num_chapters: int = 20, body_repeats: int = 40) -> str:
    """Build a synthetic novel: a preamble, then ``num_chapters`` numbered
    chapters with fixed-length bodies (each "段。" line ends in a newline, so the
    newline-exclusion counting path is exercised)."""
    lines = ["这是一部用于测试的分册小说。", "前言内容，若干行。", ""]
    for i in range(1, num_chapters + 1):
        lines.append(f"第{i}章 标题{i}")
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


def test_no_chapters_yields_no_files():
    text = "这是一段没有任何章节标记的普通文本。\n它只是正文，没有第几章。"
    analysis = B.analyze_text(text)
    assert analysis["chapters"] == []
    assert B.make_chapter_filenames("书", analysis["chapters"]) == []  # stop, never force-split


# --------------------------------------------------------------------------- #
# The core invariant: round-trip
# --------------------------------------------------------------------------- #

def test_round_trip_concatenation_equals_original():
    text = make_novel(40)
    analysis = B.analyze_text(text)
    # One file per chapter: concatenating the per-chapter slices reproduces the
    # original exactly (no character lost, added, or reordered).
    joined = "".join(B.chapter_content(analysis, ch) for ch in analysis["chapters"])
    assert joined == text


# --------------------------------------------------------------------------- #
# Output-file naming (exact contract)
# --------------------------------------------------------------------------- #

def test_chapter_filenames_format_and_padding():
    # Chapters numbered 1..5 -> NN width max(2, 1) = 2; chapter width max(3, 1) = 3.
    chapters = [
        {"num": 1, "numStr": "1"},
        {"num": 2, "numStr": "2"},
        {"num": 3, "numStr": "3"},
        {"num": 4, "numStr": "4"},
        {"num": 5, "numStr": "5"},
    ]
    names = B.make_chapter_filenames("测试小说", chapters)
    assert names == [
        "测试小说 分册01 第001章.txt",
        "测试小说 分册02 第002章.txt",
        "测试小说 分册03 第003章.txt",
        "测试小说 分册04 第004章.txt",
        "测试小说 分册05 第005章.txt",
    ]


def test_chapter_filenames_widen_with_largest_number():
    # Largest chapter number is 1234 -> width 4 (>= 3).
    chapters = [{"num": 1, "numStr": "1"}, {"num": 1234, "numStr": "1234"}]
    names = B.make_chapter_filenames("书", chapters)
    assert names == ["书 分册01 第0001章.txt", "书 分册02 第1234章.txt"]


def test_chapter_filenames_keep_original_non_numeric_labels():
    # A chapter whose number can't be parsed keeps its raw label, unpadded.
    chapters = [
        {"num": None, "numStr": "楔子"},
        {"num": 1, "numStr": "1"},
    ]
    names = B.make_chapter_filenames("书", chapters)
    # 楔子 is unpadded; chapter 1 padded to width 3.
    assert names == ["书 分册01 第楔子章.txt", "书 分册02 第001章.txt"]


def test_chapter_filenames_never_renumber():
    # A gap (1, 2, 5) must be preserved, not renumbered to 1,2,3.
    chapters = [
        {"num": 1, "numStr": "1"},
        {"num": 2, "numStr": "2"},
        {"num": 5, "numStr": "5"},
    ]
    names = B.make_chapter_filenames("书", chapters)
    assert names == [
        "书 分册01 第001章.txt",
        "书 分册02 第002章.txt",
        "书 分册03 第005章.txt",
    ]


def test_chapter_filenames_widen_with_count():
    # 120 chapters -> NN width max(2, digits of 120) = 3; chapter-number width
    # is driven by the largest NUMBER (120 -> 3), not by the count.
    chapters = [{"num": i, "numStr": str(i)} for i in range(1, 121)]
    names = B.make_chapter_filenames("书", chapters)
    assert names[0] == "书 分册001 第001章.txt"
    assert names[-1] == "书 分册120 第120章.txt"


def test_chapter_filenames_unique_for_duplicate_numbers():
    # Duplicated chapter numbers must not collide: the positional 分册NN disambiguates.
    chapters = [{"num": 5, "numStr": "5"}, {"num": 5, "numStr": "5"}]
    names = B.make_chapter_filenames("书", chapters)
    assert names == ["书 分册01 第005章.txt", "书 分册02 第005章.txt"]


def test_whole_book_filename():
    assert B.make_whole_book_filename("书") == "书 全书.txt"
    # Same sanitizing rules as chapter names.
    assert B.make_whole_book_filename("a/b*c") == "a_b_c 全书.txt"


def test_expected_format_string():
    # The user-visible recognition rule, shipped with the analyze response.
    fmt = B.EXPECTED_CHAPTER_FORMAT
    assert "第N章" in fmt
    assert "阿拉伯数字" in fmt
    assert "中文数字" in fmt


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
