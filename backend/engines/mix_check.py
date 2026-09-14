"""段落混合检查 (mix check) — split multi-subject entries, drop punctuation-only entries.

Runs BETWEEN 文本解析 and 角色匹配检查 on the same ``03_parsed_json/`` files, and is the
producer of the ``<stem>_checked.json`` artifact that both check stages share (the base
``<stem>.json`` stays pristine; 角色匹配检查 later updates the SAME file in place, changing
only ``speaker``).

Two mechanisms, and nothing else:

- **punctuation-only deletion — deterministic, no LLM.** An entry whose text is empty or
  all punctuation (``unicodedata`` ``P*`` + a few symbol-categorized tildes) is dropped
  locally (``is_deletable_text``). Such entries are excluded from the LLM target sets.
- **multi-subject splitting — one LLM verdict per batch + four deterministic gates.** The
  LLM marks each target entry ``keep`` (a single speaking subject — even a mislabelled
  one: fixing attribution is 角色匹配检查's job) or ``split`` (narration mixed with a
  character's line, or lines of 2+ characters) into ``parts``. A split is adopted only if
  it passes ALL gates (``validate_split_parts``): ≥2 well-formed parts; the parts' texts
  (whitespace-stripped) concatenate EXACTLY to the entry's text; every part speaker is
  ``NARRATOR`` or in the file-wide character roster; the parts hold ≥2 distinct speakers.
  Any failure rejects the whole split — no voting, because a split is all-or-nothing and
  the gates make text corruption impossible. Gate-rejected splits get ONE retry pass
  (``group_retry_indices`` + the retry block in ``mix_check_file``): after the batch loop,
  every failed entry (across all batches) is re-asked — in groups of at most the shared
  batch size, each call windowed by the ±n context around the entries' span, with a
  per-entry note naming the gate the previous attempt broke — and re-gated; a re-proposed
  split is adopted only if it passes ALL gates again, and any entry still failing (or
  re-judged keep) is abandoned (kept unchanged). Never retried twice: two consecutive
  failures mean the model cannot split this entry reliably, and the safe keep stands.

  A different, earlier failure is a batch whose LLM call fails or whose reply yields NO
  usable verdict at all: that batch is re-asked ONCE in place, immediately (the same
  window, before the batch's entries are processed) — a second failure keeps the whole
  batch unchanged, and it never enters the end-of-file retry pass (which is reserved for
  gate-rejected splits). Each batch is thus retried at most once, where it failed.

Index strategy: LLM verdicts are keyed by ORIGINAL-array index; ``rebuild_entries`` walks
the original order (keep → copy, deletable → drop, split → parts in order, each with an
empty ``instruct``) so the rebuilt list is renumbered by construction — positions unique
and contiguous, order stable, nothing skipped or overwritten. If the file already has a
synthesis manifest (``05_audio_chunk/<package>/manifest.json``) whose segments no longer
line up (index out of range or text mismatch — a count-only check would miss a shift that
keeps the total), the task log warns to re-run 音频合成/合并; the manifest is never touched.

Batch geometry (``batch_size`` / ``context_window``) is read from the 角色匹配检查 settings
(``SpeakerCheckConfig``) by design — the two stages are batched identically from one config.
Prompts are the mix check's own (``config.mix_check``, empty → bundled defaults).

``mix_check_file`` is a Task worker (first arg is the :class:`TaskHandle`): one shared-gate
slot for the whole file (serial LLM calls within, parallel across files), ONE write of the
``_checked`` artifact after the whole loop — nothing is written on cancel, so a re-run
always re-derives from the pristine base (breakpoint resume = re-run from base).
"""
from __future__ import annotations

import json
import time
import unicodedata
from pathlib import Path

from ..core.config import GenerationConfig, LLMConfig, MixCheckConfig, SpeakerCheckConfig
from ..core.concurrency import gate
from ..core.paths import get_layout
from ..core.tasks import TaskCancelled
from . import mix_check_prompts
from .speaker_check import _as_int, _clean_reply, _extract_json, _llm_call, build_batch_window, target_indices
from .tts_batch import package_for


# Tilde-ish symbols whose Unicode category is NOT P* but which count as punctuation here.
PUNCT_EXTRA = "~〜～"  # ~ 〜 ～


def _no_ws(text: str) -> str:
    """All whitespace characters removed (basis for deletability + exact-partition checks)."""
    return "".join(ch for ch in text if not ch.isspace())


def is_deletable_text(text: str | None) -> bool:
    """Whether an entry carries no real content: empty, or punctuation-only.

    Deterministic (no LLM): with all whitespace removed, the entry is deletable iff the
    remainder is empty or every remaining character is punctuation — ``unicodedata``
    category ``P*`` (covers ``。！？，…—`` and the full-width ASCII forms) plus the symbol-
    categorized tildes in :data:`PUNCT_EXTRA`. Any letter/digit/symbol content keeps the
    entry (a lone "嗯" or "2024" is content, not punctuation).
    """
    if text is None:
        return True
    rem = _no_ws(text)
    if not rem:
        return True
    return all(unicodedata.category(ch)[0] == "P" or ch in PUNCT_EXTRA for ch in rem)


def build_roster(entries: list) -> list[str]:
    """The book-wide character roster: sorted, de-duplicated, non-empty, non-NARRATOR
    ``speaker`` values over the WHOLE input file.

    A split part's speaker may name any roster character — not just those present in the
    local batch window (which would wrongly reject a character whose first appearance sits
    inside the mixed entry). Computed deterministically from the input, never from the LLM;
    mirrors the 解析 stage's roster injection.
    """
    roster = set()
    for e in entries:
        sp = (e.get("speaker") or "").strip()
        if sp and sp != "NARRATOR":
            roster.add(sp)
    return sorted(roster)


def _verdict_from_item(x) -> dict | None:
    """Normalize one reply item into a verdict ``{"action": "keep"}`` /
    ``{"action": "split", "parts": [...]}``, or ``None`` when it isn't a usable verdict
    (the caller then keeps the entry — never guess)."""
    if isinstance(x, dict):
        if x.get("action") == "keep":
            return {"action": "keep"}
        if x.get("action") == "split":
            return {"action": "split", "parts": x.get("parts")}
        return None
    if isinstance(x, str):
        # a bare "keep" token (index-keyed object / ordered-string list)
        return {"action": "keep"} if x.strip() == "keep" else None
    if isinstance(x, list):
        # a bare parts list = a split
        return {"action": "split", "parts": x}
    return None


def _mix_map_from_list(lst: list, tset: set, targets: list) -> dict:
    if not lst:
        return {}
    m = {}
    for x in lst:
        if not isinstance(x, dict):
            continue
        idx = _as_int(x.get("index"))
        if idx is None or idx not in tset:
            continue
        vd = _verdict_from_item(x)
        if vd:
            m[idx] = vd
    if not m and all(isinstance(x, str) for x in lst):
        # ordered bare strings: "keep" slots in target order
        for i, s in enumerate(lst):
            if i < len(targets) and s.strip() == "keep":
                m[targets[i]] = {"action": "keep"}
    return m


def _mix_map_from_value(v, tset: set, targets: list) -> dict:
    """Extract ``{index: verdict}`` from a parsed JSON value (object or array)."""
    if isinstance(v, dict):
        # {"results" / "verdicts" / ...: [ ... ]}
        for key in ("results", "verdicts", "entries", "items", "list"):
            inner = v.get(key)
            if isinstance(inner, list):
                m = _mix_map_from_list(inner, tset, targets)
                if m:
                    return m
        # single-target fallback: a bare {"action": ...} object (no "index") applies to
        # the lone target
        if len(targets) == 1 and "action" in v:
            vd = _verdict_from_item(v)
            if vd:
                return {targets[0]: vd}
        # index-keyed object: {"0": "keep"}, {"3": {"action": "split", "parts": [...]}}
        m = {}
        for k, val in v.items():
            idx = _as_int(k)
            if idx is None or idx not in tset:
                continue
            vd = _verdict_from_item(val)
            if vd:
                m[idx] = vd
        return m
    if isinstance(v, list):
        return _mix_map_from_list(v, tset, targets)
    return {}


def parse_mix_map(text: str | None, target_indices: list) -> dict:
    """Parse a batch LLM reply into ``{absolute_index: verdict}`` for the target entries.

    Robust to thinking tags / code fences and to the reply being a wrapped
    ``{"results": [...]}`` object, a bare ``[...]`` array, or an index-keyed object (mirrors
    ``parse_speaker_map``'s shape tolerance). Returns ``{}`` for anything unreadable — the
    caller keeps the original entry for those indices (per-item isolation, never guess).
    """
    targets = list(target_indices)
    if not targets or not text:
        return {}
    text = _clean_reply(text.strip())
    tset = set(targets)
    value = _extract_json(text)
    if value is not None:
        m = _mix_map_from_value(value, tset, targets)
        if m:
            return m
    return {}


def validate_split_parts(original: dict, parts, allowed: frozenset) -> tuple[bool, str]:
    """Gate a proposed split; ``(True, "")`` or ``(False, reason)``.

    ANY failure rejects the whole split — the entry is kept unchanged. Gates, in order:

    - (a) ≥2 parts, each a dict with non-empty ``speaker`` and non-blank ``text``;
    - (b) the parts' texts (whitespace-stripped) concatenate EXACTLY to the entry's text
      (whitespace-stripped) — no reordering, nothing dropped, nothing added;
    - (c) every part speaker ∈ ``allowed`` (``NARRATOR`` ∪ the file-wide roster);
    - (d) the parts hold ≥2 DISTINCT speakers (a same-subject "split" is not a mix).
    """
    if not isinstance(parts, list) or len(parts) < 2:
        return False, "拆分少于 2 段"
    for p in parts:
        if not isinstance(p, dict):
            return False, "拆分段不是对象"
        sp, tx = p.get("speaker"), p.get("text")
        if not (isinstance(sp, str) and sp.strip()):
            return False, "拆分段缺少 speaker"
        if not (isinstance(tx, str) and _no_ws(tx)):
            return False, "拆分段 text 为空"
    if "".join(_no_ws(p["text"]) for p in parts) != _no_ws(original.get("text") or ""):
        return False, "拆分段文字无法逐字拼回原文"
    speakers = [p["speaker"].strip() for p in parts]
    for sp in speakers:
        if sp not in allowed:
            return False, f"speaker「{sp}」不在窗口/花名册中"
    if len(set(speakers)) < 2:
        return False, "各段 speaker 未含 ≥2 个不同主体"
    return True, ""


def rebuild_entries(original: list, deletable: set, split_parts: dict) -> list:
    """Rebuild the entry list from the ORIGINAL array.

    Results are keyed by original index (never by position-in-the-moving-result); walking
    the original order and appending makes the new positions unique & contiguous by
    construction. Deletable entries are dropped; a validated split expands IN ORDER to its
    parts, each ``{"speaker", "text", "instruct": ""}`` — a part inherits no voice
    direction, because the original entry's ``instruct`` was written for the whole mixed
    entry and would misdirect TTS; everything else is a shallow copy (speaker/text/instruct
    intact).
    """
    out = []
    for i, e in enumerate(original):
        if i in deletable:
            continue
        parts = split_parts.get(i)
        if parts is not None:
            for p in parts:
                out.append({"speaker": p["speaker"].strip(), "text": p["text"], "instruct": ""})
        else:
            out.append(dict(e))
    return out


def _mix_user_prompt(template: str, context: str, roster: list[str], target_count: int,
                     n: int, retry_notes: list[tuple[int, str]] | None = None) -> str:
    """The per-batch user prompt: the ``{context}`` placeholder filled, then the book-wide
    character roster (a split part's speaker may name a character absent from the local
    window) and the ACTUAL target count — deletable entries are excluded from the targets,
    so the nominal batch size would overstate it (mirrors ``_batch_user_prompt``).

    ``retry_notes`` (the retry pass only): one ``(index, gate_reason)`` per re-asked entry,
    appended as a 【重试提示】 block so the model sees exactly which gate its previous
    split broke and can correct it (the index matches the window's ``index`` field).
    """
    body = template.replace("{context}", context)
    extra = []
    if roster:
        extra.append("【本书角色】" + "、".join(roster))
    extra.append(
        f"【目标条目】上方窗口中 target=true 的条目共 {target_count} 个，请逐一给出判定"
        f"（keep / split）；其余条目（含块前后各 {n} 条上下文）仅供理解，不得改动。"
    )
    if retry_notes:
        extra.append(
            "【重试提示】下列条目上一轮被判 split，但拆分未通过校验（条目保持原样）。请重新判定："
            "确属多主体则严格按规则重新拆分（各段 text 必须是原条 text 的逐字切片，"
            "不得丢字 / 添字 / 重排，speaker 必须来自窗口或本书角色），否则判 keep："
        )
        extra.extend(f"- 窗口中 index={idx} 的条目：{reason}" for idx, reason in retry_notes)
    return body + "\n\n" + "\n".join(extra)


def group_retry_indices(failed: list[int], n: int, batch: int) -> list[list[int]]:
    """Group the gate-rejected indices for the single retry pass (one LLM call per group).

    Indices are walked in ascending order. A group keeps growing while the gap to the next
    failed index is ≤ ``n`` (the two entries' ±n context windows overlap, so one call
    covers both) and it holds at most ``batch`` targets (the shared geometry) — otherwise
    a new group starts, so one call never spans a huge run of irrelevant entries between
    two far-apart failures.
    """
    groups: list[list[int]] = []
    cur: list[int] = []
    for i in sorted(failed):
        if cur and (i - cur[-1] > max(n, 1) or len(cur) >= batch):
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def _stale_manifest_segments(manifest: list, script: list) -> list:
    """Manifest segments no longer consistent with the rebuilt script.

    A segment is stale when its index is out of range OR its (index, text) no longer
    matches — both sides whitespace-stripped (the manifest stores stripped text). A
    count-only comparison would miss a shift that keeps the total (e.g. one deletion plus
    one split-into-two), and a stale manifest would make a resume skip the wrong text.
    """
    stale = []
    for seg in manifest:
        if not isinstance(seg, dict):
            continue
        idx = _as_int(seg.get("index"))
        if idx is None or idx < 0 or idx >= len(script):
            stale.append(seg)
            continue
        if _no_ws(str(seg.get("text") or "")) != _no_ws(str((script[idx] or {}).get("text") or "")):
            stale.append(seg)
    return stale


def mix_check_file(handle, path, llm: LLMConfig, mix: MixCheckConfig,
                   check: SpeakerCheckConfig, generation: GenerationConfig) -> dict:
    """Task worker: split multi-subject entries & drop punctuation-only ones → ``_checked``.

    Contract: first arg is the :class:`TaskHandle`; the second is the absolute path of the
    BASE ``<stem>.json`` in ``03_parsed_json/`` (a ``_checked`` path is reduced to its base
    up front — this stage always re-derives from the pristine base). Geometry (batch size /
    context window) comes from ``check`` — the 角色匹配检查 settings the two stages share by
    design; the prompts come from ``mix``. One shared-gate slot for the whole file; ONE
    write of the ``<stem>_checked.json`` artifact after the whole loop — nothing is written
    on cancel. A batch with no usable verdict is re-asked once in place, immediately;
    between the batch loop and the rebuild, gate-rejected splits get a single retry pass
    (all failed entries re-asked with context and re-gated; see the module docstring) —
    still-failing entries are abandoned and kept unchanged.
    """
    # Fail fast on a misconfigured model *before* taking a concurrency slot.
    if not (llm.model_name or "").strip():
        raise RuntimeError("请先在「文本解析」页配置 LLM 模型名称（模型不能为空）。")

    src = Path(path)
    # The mix check always rebuilds from the pristine BASE file — a prior _checked output
    # is derived state, not source (mirrors the API resolver's suffix strip).
    if src.name.endswith("_checked.json"):
        src = src.with_name(src.name[: -len("_checked.json")] + ".json")

    handle.progress(0.0, "排队中（等待并发槽位）")
    gate().acquire()
    try:
        if not src.is_file():
            raise RuntimeError(f"文件不存在：{src.name}")
        try:
            original = json.loads(src.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{src.name} 无法解析：{e}")
        if not isinstance(original, list) or not original:
            raise RuntimeError(f"{src.name} 为空——请先生成脚本。")
        if not all(isinstance(e, dict) for e in original):
            raise RuntimeError(f"{src.name} 含非对象条目，无法检查。")

        # Geometry from the 角色匹配检查 settings (the two check stages share one config).
        n = max(0, int(check.context_window or 0))
        batch = max(1, int(check.batch_size or 0))
        total = len(original)

        # Deterministic pre-pass: punctuation-only entries are dropped without any LLM
        # verdict and are excluded from the target sets (windows mark them unflagged).
        deletable = {i for i, e in enumerate(original) if is_deletable_text(e.get("text"))}
        roster = build_roster(original)
        allowed = frozenset({"NARRATOR", *roster})

        sys_prompt = mix.system_prompt or mix_check_prompts.DEFAULT_MIX_SYSTEM_PROMPT
        usr_template = mix.user_prompt or mix_check_prompts.DEFAULT_MIX_USER_PROMPT

        handle.log(f"读入 {src.name}（{total} 条）· 每批 {batch} 条 · 上下文窗口 ±{n}")
        handle.log(f"模型：{llm.model_name} · 端点：{llm.base_url}")

        split_parts: dict = {}
        rejected_idx: list[int] = []   # gate-rejected splits, re-asked in the retry pass
        rejected_reasons: dict[int, str] = {}
        proc_start = time.monotonic()
        window_chars = 0

        for start in range(0, total, batch):
            size = min(batch, total - start)
            targets = [i for i in target_indices(start, size, total) if i not in deletable]

            handle.check()  # cooperative cancel / pause before the batch
            handle.progress(start / total, f"混合检查第 {start + 1}-{start + size} 条")

            if not targets:
                # A batch of purely deletable entries needs no LLM call at all.
                handle.progress((start + size) / total, f"已处理 {start + size}/{total} 条（纯标点，直接删除）")
                continue

            handle.llm_rate(0, 0.0)
            window = build_batch_window(original, start, size, n, skip=deletable)
            context = json.dumps(window, ensure_ascii=False, indent=2)
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": _mix_user_prompt(usr_template, context, roster, len(targets), n)},
            ]
            try:
                verdicts = parse_mix_map(_llm_call(llm, generation, messages, handle), targets)
                window_chars += len(context)
            except TaskCancelled:
                raise  # a cancel raised mid-stream must propagate, not be swallowed
            except Exception as e:  # noqa: BLE001 — the batch gets one immediate re-ask below
                handle.log(f"  本批响应无法解析：{e}", "WARNING")
                verdicts = {}

            if not verdicts:
                # NO usable verdict in the whole batch (call failed, or the reply was
                # unreadable) → ONE immediate re-ask of the same window, right here —
                # not deferred to the end-of-file retry pass, which re-asks gate-rejected
                # splits only. Still empty after this → the batch is kept unchanged and
                # flows on (never retried twice).
                handle.check()  # cooperative cancel / pause between the two attempts
                handle.log("  本批无有效判定，立即重试一次…")
                try:
                    verdicts = parse_mix_map(_llm_call(llm, generation, messages, handle), targets)
                    window_chars += len(context)
                except TaskCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    handle.log(f"  重试本批响应仍无法解析：{e}", "WARNING")
                    verdicts = {}
                if not verdicts:
                    handle.log("  重试后本批仍无有效判定，本批全部保留原样", "WARNING")

            for i in targets:
                vd = verdicts.get(i)
                if vd is None or vd.get("action") != "split":
                    continue  # keep (no verdict, or an explicit "keep")
                ok, reason = validate_split_parts(original[i], vd.get("parts"), allowed)
                if ok:
                    split_parts[i] = vd["parts"]
                    speakers = "、".join(dict.fromkeys(p["speaker"].strip() for p in vd["parts"]))
                    handle.log(f"  {i + 1}: 拆分为 {len(vd['parts'])} 段（{speakers}）")
                else:
                    handle.log(f"  {i + 1}: 拆分未通过校验（{reason}），将带上下文重试一次", "WARNING")
                    rejected_idx.append(i)
                    rejected_reasons[i] = reason

            handle.llm_chars(window_chars, time.monotonic() - proc_start)
            handle.progress((start + size) / total, f"已处理 {start + size}/{total} 条")

        # The single retry pass for gate-rejected splits (never more than one): every
        # failed entry — across all batches — is pulled out and re-asked, in groups of at
        # most ``batch`` targets (``group_retry_indices``), each call windowed by the ±n
        # context around the entries' span (only the failed entries are flagged target)
        # and annotated with the gate each entry's previous attempt broke. A re-proposed
        # split is adopted only if it passes ALL four gates again; anything still failing
        # (or re-judged keep) is abandoned and kept unchanged. Two consecutive failures
        # mean the model cannot split this entry reliably — the safe keep stands, and the
        # entry flows on to 角色匹配检查.
        recovered = 0
        if rejected_idx:
            handle.check()  # cooperative cancel / pause before the retry pass
            groups = group_retry_indices(rejected_idx, n, batch)
            handle.log(f"{len(rejected_idx)} 条拆分未通过校验，带上下文窗口（±{n} 条）重试一次…")
            handle.progress(1.0, f"重试 {len(rejected_idx)} 条未通过校验的拆分")
            for g, grp in enumerate(groups, 1):
                handle.check()
                start, size = grp[0], grp[-1] - grp[0] + 1
                span = set(range(start, start + size))
                skip = deletable | (span - set(grp))  # only the failed entries are targets
                handle.llm_rate(0, 0.0)
                window = build_batch_window(original, start, size, n, skip=skip)
                context = json.dumps(window, ensure_ascii=False, indent=2)
                messages = [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": _mix_user_prompt(
                        usr_template, context, roster, len(grp), n,
                        [(i, rejected_reasons[i]) for i in grp])},
                ]
                try:
                    verdicts = parse_mix_map(_llm_call(llm, generation, messages, handle), grp)
                except TaskCancelled:
                    raise  # a cancel raised mid-stream must propagate, not be swallowed
                except Exception as e:  # noqa: BLE001 — unreadable reply → entries stay kept
                    handle.log(f"  重试本批响应无法解析，相关条目保持原样：{e}", "WARNING")
                    verdicts = {}
                for i in grp:
                    vd = verdicts.get(i)
                    if vd is not None and vd.get("action") == "split":
                        ok, reason = validate_split_parts(original[i], vd.get("parts"), allowed)
                        if ok:
                            split_parts[i] = vd["parts"]
                            recovered += 1
                            speakers = "、".join(dict.fromkeys(p["speaker"].strip() for p in vd["parts"]))
                            handle.log(f"  {i + 1}: 重试拆分成功（{len(vd['parts'])} 段：{speakers}）")
                            continue
                    handle.log(f"  {i + 1}: 重试仍未通过，放弃拆分、保持原样", "WARNING")
                window_chars += len(context)
                handle.llm_chars(window_chars, time.monotonic() - proc_start)
                handle.progress(1.0, f"重试未通过校验的拆分（第 {g}/{len(groups)} 组）")

        # Renumber by construction: walk the ORIGINAL order (results are keyed by original
        # index) — positions unique & contiguous, order stable, nothing skipped.
        out = rebuild_entries(original, deletable, split_parts)
        deleted = len(deletable)
        splits_n = len(split_parts)
        parts_out = sum(len(v) for v in split_parts.values())
        kept = total - deleted - splits_n
        rejected_n = len(rejected_idx)
        abandoned = rejected_n - recovered

        # A split changes entry positions: an existing synthesis manifest for this file may
        # no longer line up (a resume would then skip stale-ok segments). Never delete or
        # rewrite it — just warn that 音频合成/合并 should be re-run.
        layout = get_layout()
        audio_chunk = layout.audio_chunk if layout is not None else None
        if audio_chunk is not None:
            pkg = package_for(src)
            manifest_path = audio_chunk / pkg / "manifest.json"
            if manifest_path.is_file():
                try:
                    data = json.loads(manifest_path.read_text("utf-8"))
                except Exception:  # noqa: BLE001
                    data = None
                if isinstance(data, list):
                    stale = _stale_manifest_segments(data, out)
                    if stale:
                        handle.log(
                            f"注意：05_audio_chunk/{pkg}/manifest.json 与新的 {len(out)} 段不再一致"
                            f"（{len(stale)} 段错位）——请重新运行 音频合成（建议「全部重合成」）/ 音频合并。",
                            "WARNING",
                        )

        out_path = src.with_name(src.stem + "_checked.json")
        # A fresh (non-stale) pre-existing _checked may carry 角色匹配检查 results that this
        # rebuild would overwrite — warn so the user re-runs that stage after the mix check.
        if out_path.is_file():
            try:
                if out_path.stat().st_mtime >= src.stat().st_mtime:
                    handle.log(
                        f"已存在较新的 {out_path.name}（可能含角色匹配检查结果）——本次基于原始 {src.name} "
                        f"重建覆盖它；如之前运行过角色匹配检查，请随后重新运行。",
                        "WARNING",
                    )
            except OSError:
                pass
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Single write after the whole loop — the base file stays byte-for-byte intact, and
        # nothing is written on cancel (a re-run re-derives from the pristine base).
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

        speakers = sorted({(e.get("speaker") or "UNKNOWN") for e in out})
        handle.progress(1.0, "完成")
        handle.log(
            f"共 {total} 条：保留 {kept} 条、删除 {deleted} 条纯标点、拆分 {splits_n} 条 → {parts_out} 段"
            + (f"（首轮校验失败 {rejected_n} 条：重试挽回 {recovered} 条、放弃 {abandoned} 条）"
               if rejected_n else "")
            + f" → {out_path.name}"
        )
        return {
            "input_name": src.name,
            "output_name": out_path.name,
            "output_path": str(out_path),
            "total": total,
            "kept": kept,
            "deleted": deleted,
            "splits": splits_n,
            "parts": parts_out,
            "new_total": len(out),
            "speakers": speakers,
            "rejected": rejected_n,
            "recovered": recovered,
            "abandoned": abandoned,
        }
    finally:
        gate().release()
