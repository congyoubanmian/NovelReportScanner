"""
rv_llm_payload.py — 从 novel_reviewer.py 抽出的 LLM 文本裁剪工具函数。

这些函数负责为 LLM prompt 做输入预算控制：
- 按字段长度裁剪 (_clip_llm_text)
- 行级去重 (_unique_llm_lines)
- 头尾保留 + 中间省略 (_head_tail_llm_lines)
- 综合 token 预算压缩 (_compact_llm_lines)
"""

import re
from typing import Any, List, Optional

import shared_utils

REVIEW_LLM_SECTION_MAX_CHARS = shared_utils.read_int_env("REVIEW_LLM_SECTION_MAX_CHARS", 12000, min_value=2000)
REVIEW_LLM_FIELD_MAX_CHARS = shared_utils.read_int_env("REVIEW_LLM_FIELD_MAX_CHARS", 220, min_value=40)
REVIEW_LLM_LIST_MAX_ITEMS = shared_utils.read_int_env("REVIEW_LLM_LIST_MAX_ITEMS", 80, min_value=5)


def _clip_llm_text(value: Any, max_chars: Optional[int] = None) -> str:
    limit = REVIEW_LLM_FIELD_MAX_CHARS if max_chars is None else max(0, int(max_chars or 0))
    text = str(value or "").strip()
    if not limit or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"...(截断{len(text) - limit}字)"


def _unique_llm_lines(lines: List[Any]) -> List[str]:
    unique: List[str] = []
    seen = set()
    for line in lines or []:
        text = str(line or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def _head_tail_llm_lines(lines: List[str], max_items: Optional[int], label: str) -> List[str]:
    if max_items is None:
        max_items = REVIEW_LLM_LIST_MAX_ITEMS
    max_items = int(max_items or 0)
    if max_items <= 0 or len(lines) <= max_items:
        return list(lines)

    tail_count = min(10, max(1, max_items // 5))
    head_count = max(1, max_items - tail_count - 1)
    omitted = len(lines) - head_count - tail_count
    if omitted <= 0:
        return lines[:max_items]
    return (
        lines[:head_count]
        + [f"...(已省略{omitted}条{label}，保留首尾样本)..."]
        + lines[-tail_count:]
    )


def _compact_llm_lines(
    lines: List[Any],
    *,
    max_chars: Optional[int] = None,
    max_items: Optional[int] = None,
    label: str = "记录",
    empty: str = "（无记录）",
) -> str:
    budget = REVIEW_LLM_SECTION_MAX_CHARS if max_chars is None else int(max_chars or 0)
    unique = _unique_llm_lines(lines)
    if not unique:
        return empty

    selected = _head_tail_llm_lines(unique, max_items, label)
    text = "\n".join(selected)
    if budget <= 0 or len(text) <= budget:
        return text

    tail_count = min(6, max(1, len(selected) // 6))
    tail = selected[-tail_count:] if len(selected) > tail_count else []
    tail_keys = set(tail)
    tail_text_len = sum(len(item) + 1 for item in tail)
    note_len = 40
    head: List[str] = []
    used = 0
    for line in selected:
        if line in tail_keys:
            continue
        projected = used + len(line) + 1 + tail_text_len + note_len
        if head and projected > budget:
            break
        if len(line) + 1 > max(1, budget - note_len):
            line = _clip_llm_text(line, max(120, budget - note_len))
        head.append(line)
        used += len(line) + 1
        if used + tail_text_len + note_len >= budget:
            break

    omitted = max(0, len(selected) - len(head) - len(tail))
    compacted = list(head)
    if omitted:
        compacted.append(f"...(已因输入预算省略{omitted}条{label})...")
    for item in tail:
        if item not in compacted:
            compacted.append(item)

    text = "\n".join(compacted)
    if len(text) > budget:
        text = _clip_llm_text(text, budget)
    return text
