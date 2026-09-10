"""Offline tests for the script-generation engine (``backend/engines/script.py``).

These pin the faithful port of the source ``generate_script.py`` JSON pipeline —
chunking, response cleaning, array repair, and regex salvage — against known inputs,
with no network access. The LLM transport (streaming + non-streaming) is unit-tested
here against a mocked ``urllib.request.urlopen`` (no real HTTP). Mirrors the style and
focus of ``test_text.py``.
"""
from __future__ import annotations

import json
import re
import urllib.request

import pytest

from backend.core.config import LLMConfig
from backend.core.tasks import TaskCancelled
from backend.engines.script import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    _llm_chat_completion,
    _llm_chat_completion_stream,
    clean_json_string,
    fix_mojibake,
    process_chunk,
    repair_json_array,
    salvage_json_entries,
    split_into_chunks,
)

BS = chr(92)  # backslash — built via chr() so no literal backslashes live in this file


def _entry(speaker: str, text: str, instruct: str) -> dict:
    return {"speaker": speaker, "text": text, "instruct": instruct}


# --------------------------------------------------------------------------- #
# split_into_chunks
# --------------------------------------------------------------------------- #

def test_split_by_paragraphs():
    assert split_into_chunks("para one\n\npara two\n\npara three", max_size=10) == [
        "para one", "para two", "para three",
    ]


def test_split_packs_small_paragraphs():
    assert split_into_chunks("aa\n\nbb\n\ncc", max_size=100) == ["aa\n\nbb\n\ncc"]


def test_split_long_paragraph_by_sentences():
    assert split_into_chunks("aaaa. bbbb. cccc.", max_size=8) == ["aaaa.", "bbbb.", "cccc."]


def test_split_empty():
    assert split_into_chunks("") == []
    assert split_into_chunks("   \n\n  ", max_size=10) == []


def test_split_preserves_content():
    samples = [
        "para one\n\npara two\n\npara three",
        "aaaa. bbbb. cccc.",
        "第一段。\n\n第二段。句子一。句子二。\n\n第三段。",
    ]
    for src in samples:
        chunks = split_into_chunks(src, max_size=20)
        assert re.sub(r"\s", "", src) == re.sub(r"\s", "", "\n\n".join(chunks)), repr(src)


# --------------------------------------------------------------------------- #
# clean_json_string
# --------------------------------------------------------------------------- #

def test_clean_strips_code_fence():
    assert clean_json_string('```json\n[{"a":1}]\n```') == '[{"a":1}]'


def test_clean_strips_thinking_tags():
    assert clean_json_string('<thinking>blah</thinking>[{"a":1}] tail') == '[{"a":1}]'


def test_clean_bracket_counter_with_noise():
    assert clean_json_string('x [{"a":1},{"b":2}] y') == '[{"a":1},{"b":2}]'


def test_clean_salvages_unclosed_array():
    assert clean_json_string('noise [{"a":1},{"b":2}') == '[{"a":1}]'


def test_clean_returns_none_without_array():
    assert clean_json_string("no brackets here") is None


def test_clean_escapes_control_chars_in_strings():
    raw = '[{"speaker":"N","text":"a\nb","instruct":"i"}]'  # \n is a real newline
    cleaned = clean_json_string(raw)
    assert json.loads(cleaned)[0]["text"] == "a\nb"


# --------------------------------------------------------------------------- #
# repair_json_array
# --------------------------------------------------------------------------- #

def test_repair_valid_array():
    assert repair_json_array('[{"speaker":"N","text":"a","instruct":"b"}]') == [_entry("N", "a", "b")]


def test_repair_missing_comma():
    got = repair_json_array(
        '[{"speaker":"A","text":"1","instruct":"i"}'
        '{"speaker":"B","text":"2","instruct":"j"}]'
    )
    assert [e["speaker"] for e in got] == ["A", "B"]


def test_repair_trailing_comma():
    assert repair_json_array('[{"speaker":"A","text":"1","instruct":"i"},]') == [_entry("A", "1", "i")]


def test_repair_drops_non_dict_and_logs():
    logs = []
    got = repair_json_array('[{"speaker":"A","text":"1","instruct":"i"}, "stray"]', log=logs.append)
    assert got == [_entry("A", "1", "i")]
    assert logs == ["Dropped 1 non-object entries from LLM JSON array"]


def test_repair_drops_non_dict_without_log():
    # No ``log`` callback: the drop path must not crash (the ``and log`` guard).
    assert repair_json_array('[{"speaker":"A","text":"1","instruct":"i"}, "stray"]') == [_entry("A", "1", "i")]


def test_repair_unparseable():
    assert repair_json_array("not json at all") is None
    assert repair_json_array("") is None


# --------------------------------------------------------------------------- #
# salvage_json_entries
# --------------------------------------------------------------------------- #

def test_salvage_single_entry():
    got = salvage_json_entries('noise [{"speaker":"NARRATOR","text":"他说","instruct":"calm"}, garbage')
    assert got == [_entry("NARRATOR", "他说", "calm")]


def test_salvage_unescapes_text():
    raw = '{"speaker":"N","text":"a' + BS + 'nb","instruct":"i"}'  # text value is a\nb (escaped)
    got = salvage_json_entries(raw)
    assert got[0]["text"] == "a\nb"


def test_salvage_none_when_no_match():
    assert salvage_json_entries("no objects here") is None


# --------------------------------------------------------------------------- #
# fix_mojibake
# --------------------------------------------------------------------------- #

def test_fix_mojibake_noop_on_clean_text():
    assert fix_mojibake("plain text 123 中文") == "plain text 123 中文"


def test_fix_mojibake_replaces_ellipsis():
    # The CP1252-as-UTF8 mojibake of "…" (E2 80 A6) is mapped back to the real ellipsis.
    assert fix_mojibake("aâ€¦b") == "a…b"


# --------------------------------------------------------------------------- #
# Bundled default prompts
# --------------------------------------------------------------------------- #

def test_default_prompts_loaded():
    assert DEFAULT_SYSTEM_PROMPT.strip()
    assert "{context}" in DEFAULT_USER_PROMPT
    assert "{chunk}" in DEFAULT_USER_PROMPT


# --------------------------------------------------------------------------- #
# LLM transport — mocked urllib.request.urlopen (no real HTTP)
# --------------------------------------------------------------------------- #

class _StreamResp:
    """Iterable stand-in for a streaming ``chat/completions`` response (SSE lines)."""

    def __init__(self, lines: list):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BodyResp:
    """Stand-in for a non-streaming response: one ``read()`` of the full JSON body."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sse_frame(obj) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class _Handle:
    """Minimal TaskHandle: records llm_chunk / llm_rate calls; ``cancelled`` is fixed per test."""

    def __init__(self, cancelled: bool = False):
        self.cancelled = cancelled
        self.chunks: list = []
        self.rates: list = []

    def llm_chunk(self, text: str) -> None:
        self.chunks.append(text)

    def llm_rate(self, tps: float) -> None:
        self.rates.append(tps)

    def log(self, *a, **k):
        pass

    def check(self):
        pass

    def progress(self, *a, **k):
        pass


def test_stream_llm_accumulates_and_forwards(monkeypatch):
    deltas = ["[",
              '{"speaker":"N","text":"他说","instruct":"calm"},',
              '{"speaker":"B","text":"好","instruct":"loud"}',
              "]"]
    full = "".join(deltas)
    lines = [_sse_frame({"choices": [{"delta": {"role": "assistant", "content": ""}}]})]
    lines += [_sse_frame({"choices": [{"delta": {"content": d}}]}) for d in deltas]
    lines.append(_sse_frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    lines.append(_sse_frame({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}))
    lines.append(b"data: [DONE]\n")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))
    handle = _Handle()

    content, finish_reason, usage = _llm_chat_completion_stream(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        handle=handle,
    )

    # Accumulated deltas, stripped — identical to what the non-streaming call returns.
    assert content == full
    assert finish_reason == "stop"
    assert usage == {"prompt_tokens": 5, "completion_tokens": 7}
    # Coalesced forwards lose nothing (the 「流式反馈」 panel sees the whole stream).
    assert "".join(handle.chunks) == full
    assert handle.chunks  # at least one flush reached the UI
    # The accumulated content still flows through the unchanged JSON pipeline.
    assert clean_json_string(content) == full
    assert repair_json_array(full) == [
        {"speaker": "N", "text": "他说", "instruct": "calm"},
        {"speaker": "B", "text": "好", "instruct": "loud"},
    ]


def test_stream_llm_none_handle_still_works(monkeypatch):
    # ``handle=None`` (tests / no UI) must still accumulate and return the content.
    lines = [
        _sse_frame({"choices": [{"delta": {"content": "hello "}}]}),
        _sse_frame({"choices": [{"delta": {"content": "world"}}]}),
        _sse_frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))

    content, finish_reason, usage = _llm_chat_completion_stream(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        handle=None,
    )
    assert content == "hello world"
    assert finish_reason == "stop"
    assert usage is None  # no usage frame sent


def test_stream_llm_reasoning_shown_but_not_returned(monkeypatch):
    # A reasoning model (e.g. Qwen3 "thinking") streams its working in
    # ``delta.reasoning_content`` before the answer in ``delta.content``: the panel
    # (llm_chunk) must show both, but the returned content is the answer only, so the
    # JSON pipeline never sees the thinking text.
    lines = [
        _sse_frame({"choices": [{"delta": {"reasoning_content": "Thinking hard "}}]}),
        _sse_frame({"choices": [{"delta": {"reasoning_content": "about the answer"}}]}),
        _sse_frame({"choices": [{"delta": {"content": '[{"a":1}]'}}]}),
        _sse_frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))
    handle = _Handle()

    content, finish_reason, _usage = _llm_chat_completion_stream(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        handle=handle,
    )

    # Returned content = the answer only (no thinking) — JSON pipeline unaffected.
    assert content == '[{"a":1}]'
    assert finish_reason == "stop"
    # The panel saw reasoning + answer in arrival order (the live "working" state).
    shown = "".join(handle.chunks)
    assert shown == 'Thinking hard about the answer[{"a":1}]'
    assert "Thinking hard " in shown
    assert "Thinking hard " not in content


def test_nonstream_llm_strips_content(monkeypatch):
    full = '[{"speaker":"N","text":"a","instruct":"b"}]'
    payload = json.dumps({
        "choices": [{"message": {"content": "  " + full + "  "}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _BodyResp(payload))

    content, finish_reason, usage = _llm_chat_completion(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
    )
    # The non-streaming path strips the content; the streaming path matches it.
    assert content == full
    assert finish_reason == "stop"
    assert usage == {"prompt_tokens": 3, "completion_tokens": 4}


def test_stream_cancel_propagates_through_process_chunk(monkeypatch):
    # A cancel raised mid-stream must escape process_chunk (not be swallowed/retried
    # by the generic ``except Exception``).
    lines = [_sse_frame({"choices": [{"delta": {"content": "x"}}]})]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))
    handle = _Handle(cancelled=True)  # already cancelled -> raise on the first frame

    with pytest.raises(TaskCancelled):
        process_chunk(handle, LLMConfig(stream=True), "model", "chunk text", 1, 1)
