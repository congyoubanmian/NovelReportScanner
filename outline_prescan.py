"""
outline_prescan.py - 大纲预扫描模块

从小说文本中提取章节结构和关键信息，生成结构化大纲。
大纲作为后续 chunk 扫描的全局上下文注入，让 LLM 在扫某个 chunk 时
知道"前文整体发生了什么、后续走向是什么"，大幅减少信息孤岛导致的误判。

核心策略：
1. 规则提取章节标题 + 首段/尾段（零 LLM 成本）
2. 可选：用一次 LLM 调用生成章节摘要流（低成本）
3. 输出结构化大纲 JSON

设计原则：
- 大纲是"提示"而非"事实"，不能替代原文证据
- 优先使用规则提取，LLM 仅用于可选的摘要增强
- 大纲体积必须压缩，避免占用过多 chunk prompt 空间
"""

import json
import os
import re
import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from text_anchor import (
    _find_chapter_heading_matches,
    normalize_newlines,
)
from shared_utils import get_base_dir, read_int_env

logger = logging.getLogger(__name__)

# ===================== 配置 =====================
OUTLINE_PRESCAN_ENABLED = os.environ.get("OUTLINE_PRESCAN_ENABLED", "1").strip() == "1"
OUTLINE_PRESCAN_SAMPLE_CHARS = read_int_env("OUTLINE_PRESCAN_SAMPLE_CHARS", 300, min_value=50, max_value=2000)
OUTLINE_PRESCAN_MAX_CHAPTERS = read_int_env("OUTLINE_PRESCAN_MAX_CHAPTERS", 500, min_value=10)
OUTLINE_PRESCAN_LLM_ENABLED = os.environ.get("OUTLINE_PRESCAN_LLM_ENABLED", "0").strip() == "1"
OUTLINE_PRESCAN_LLM_MAX_CHAPTERS = read_int_env("OUTLINE_PRESCAN_LLM_MAX_CHAPTERS", 100, min_value=10)
OUTLINE_PRESCAN_COMPACT_MAX_CHARS = read_int_env("OUTLINE_PRESCAN_COMPACT_MAX_CHARS", 4000, min_value=500)
OUTLINE_SCHEMA_VERSION = 1


# ===================== 章节结构提取 =====================
def _extract_chapter_structures(text: str, sample_chars: int = 300) -> List[Dict[str, Any]]:
    """
    规则提取：从每章中提取标题 + 首段 + 尾段。

    不调用 LLM，零 token 成本。

    返回章节结构列表:
    [
        {
            "index": 1,
            "title": "第001章 溪上何人品玉箫",
            "start": 349,
            "end": 5204,
            "head_text": "正值盛夏...",
            "tail_text": "...他默默走了出去。",
            "length": 4855,
        },
        ...
    ]
    """
    if not text:
        return []

    normalized = normalize_newlines(text)
    chapter_matches = _find_chapter_heading_matches(normalized)

    if not chapter_matches:
        return []

    chapters = []
    for i, (ch_start, ch_title) in enumerate(chapter_matches):
        ch_end = chapter_matches[i + 1][0] if i + 1 < len(chapter_matches) else len(normalized)
        ch_text = normalized[ch_start:ch_end]

        # 提取首段（跳过标题行及紧随的空行）
        lines = ch_text.split("\n")
        body_lines = []
        past_title = False
        for line in lines:
            if not past_title:
                # 跳过标题行及其前导空行
                if line.strip():
                    past_title = True
                    # 标题行本身跳过，继续找正文
                    continue
                continue
            if not line.strip() and not body_lines:
                # 标题和正文之间的空行
                continue
            if line.strip():
                body_lines.append(line.strip())

        head_text = ""
        if body_lines:
            # 收集首段直到达到 sample_chars
            head_parts = []
            char_count = 0
            for bl in body_lines:
                if char_count + len(bl) > sample_chars and head_parts:
                    break
                head_parts.append(bl)
                char_count += len(bl)
            head_text = "".join(head_parts)[:sample_chars]

        # 提取尾段
        tail_text = ""
        if body_lines:
            tail_parts = []
            char_count = 0
            for bl in reversed(body_lines):
                if char_count + len(bl) > sample_chars and tail_parts:
                    break
                tail_parts.insert(0, bl)
                char_count += len(bl)
            tail_text = "".join(tail_parts)[:sample_chars]

        chapters.append({
            "index": i + 1,
            "title": ch_title,
            "start": ch_start,
            "end": ch_end,
            "head_text": head_text,
            "tail_text": tail_text,
            "length": ch_end - ch_start,
        })

    return chapters


# ===================== 关键词标签提取 =====================
# 从章节首尾段中提取结构化标签，帮助 LLM 快速判断章节性质

_HIGH_SIGNAL_TAGS = {
    # 战斗/冲突
    "战斗": ["战斗", "交手", "对决", "厮杀", "搏杀", "激战", "大战", "动手", "出招", "杀招"],
    # 突破/成长
    "突破": ["突破", "晋升", "进阶", "觉醒", "蜕变", "悟道", "顿悟", "开窍"],
    # 情感/关系
    "情感": ["表白", "告白", "亲吻", "拥抱", "暧昧", "吃醋", "修罗场", "成亲", "订婚"],
    # 阴谋/悬疑
    "悬疑": ["阴谋", "布局", "暗算", "陷阱", "诡计", "内鬼", "密谋", "真相", "线索"],
    # 重要事件
    "转折": ["背叛", "反水", "倒戈", "揭秘", "身世", "阴谋败露", "大逆转"],
    # 日常/过渡
    "日常": ["修炼", "闭关", "赶路", "吃饭", "休息", "闲聊", "逛街", "整理"],
}

_TAG_TERM_MAP: Dict[str, str] = {}
for tag, terms in _HIGH_SIGNAL_TAGS.items():
    for term in terms:
        _TAG_TERM_MAP[term] = tag


def _tag_chapter(head_text: str, tail_text: str) -> List[str]:
    """
    从章节首尾段提取信号标签。

    返回去重标签列表，如 ["战斗", "突破", "情感"]。
    """
    combined = (head_text + tail_text)[:800]
    tags = set()
    for term, tag in _TAG_TERM_MAP.items():
        if term in combined:
            tags.add(tag)
    return sorted(tags)


# ===================== 大纲生成 =====================
def generate_outline(
    text: str,
    *,
    sample_chars: int = None,
    max_chapters: int = None,
) -> Dict[str, Any]:
    """
    从小说文本生成结构化大纲。

    返回:
    {
        "schema_version": 1,
        "chapter_count": 1039,
        "total_chars": 3927265,
        "chapters": [
            {
                "index": 1,
                "title": "第001章 ...",
                "tags": ["日常", "转折"],
                "head_text": "...",
                "tail_text": "...",
                "length": 4855,
            },
            ...
        ],
    }
    """
    if not OUTLINE_PRESCAN_ENABLED:
        return {"schema_version": OUTLINE_SCHEMA_VERSION, "chapters": []}

    sample_chars = sample_chars or OUTLINE_PRESCAN_SAMPLE_CHARS
    max_chapters = max_chapters or OUTLINE_PRESCAN_MAX_CHAPTERS

    chapters = _extract_chapter_structures(text, sample_chars=sample_chars)

    if not chapters:
        return {
            "schema_version": OUTLINE_SCHEMA_VERSION,
            "chapter_count": 0,
            "total_chars": len(text or ""),
            "chapters": [],
        }

    # 为每章添加标签
    for ch in chapters:
        ch["tags"] = _tag_chapter(ch.get("head_text", ""), ch.get("tail_text", ""))

    # 限制章节数量（超长小说只保留前 N 章的详细大纲）
    if len(chapters) > max_chapters:
        # 保留首尾各 max_chapters//2 章
        half = max_chapters // 2
        kept = chapters[:half] + chapters[-(max_chapters - half):]
        # 标记跳过的章节
        skipped = len(chapters) - max_chapters
        chapters = kept

    result = {
        "schema_version": OUTLINE_SCHEMA_VERSION,
        "chapter_count": len(chapters),
        "total_chapters_in_novel": len(_extract_chapter_structures(text, sample_chars=50)),
        "total_chars": len(text or ""),
        "chapters": chapters,
    }

    return result


# ===================== 大纲压缩（用于注入 prompt） =====================
def outline_to_compact_text(outline: Dict[str, Any], max_chars: int = None) -> str:
    """
    将结构化大纲压缩为可注入 chunk prompt 的紧凑文本。

    格式示例：
    【全书大纲（共1039章）】
    1.第001章 溪上何人品玉箫 [日常]
    2.第002章 鞘藏寒气绣春刀 [悬疑]
    ...
    1039.后记

    首尾章节附带首段摘要，中间章节仅标题+标签。
    """
    if not outline or not outline.get("chapters"):
        return ""

    max_chars = max_chars or OUTLINE_PRESCAN_COMPACT_MAX_CHARS
    chapters = outline["chapters"]
    total_count = outline.get("total_chapters_in_novel") or outline.get("chapter_count", len(chapters))

    lines = [f"【全书大纲（共{total_count}章）】"]

    # 首尾各展示几章带摘要
    head_detail = min(5, len(chapters))
    tail_detail = min(3, max(0, len(chapters) - head_detail))

    budget = max_chars - len(lines[0]) - 100  # 预留格式开销
    used = 0

    for i, ch in enumerate(chapters):
        is_head_detail = i < head_detail
        is_tail_detail = i >= len(chapters) - tail_detail

        title = ch.get("title", f"第{ch.get('index', i+1)}章")
        tags = ch.get("tags", [])
        tag_str = f" [{','.join(tags)}]" if tags else ""

        if is_head_detail or is_tail_detail:
            head_text = ch.get("head_text", "")
            head_summary = f"：{head_text[:50]}…" if head_text else ""
            line = f"{ch.get('index', i+1)}.{title}{tag_str}{head_summary}"
        else:
            line = f"{ch.get('index', i+1)}.{title}{tag_str}"

        line_len = len(line) + 1  # +1 for \n
        if used + line_len > budget:
            # 空间不足，只列标题
            remaining_budget = budget - used
            if remaining_budget > 20:
                lines.append(f"...（中间{total_count - len(lines)}章省略）...")
            break

        lines.append(line)
        used += line_len

    return "\n".join(lines)


# ===================== LLM 增强大纲（可选） =====================
def enhance_outline_with_llm(
    outline: Dict[str, Any],
    *,
    chat_completion_func=None,
    model: str = "",
    record_usage_func=None,
) -> Dict[str, Any]:
    """
    可选：用一次 LLM 调用为大纲增强章节摘要。

    输入：规则提取的章节标题+首尾段
    输出：每章增加 summary 字段（一句话摘要）

    注意：此功能默认关闭（OUTLINE_PRESCAN_LLM_ENABLED=0），
    因为对大多数场景规则提取已经足够。
    """
    if not OUTLINE_PRESCAN_LLM_ENABLED or not chat_completion_func:
        return outline

    chapters = outline.get("chapters", [])
    max_ch = OUTLINE_PRESCAN_LLM_MAX_CHAPTERS

    # 选取需要 LLM 摘要的章节（首尾 + 关键标签章节）
    selected_indices = set()
    # 首尾各 20 章
    for i in range(min(20, len(chapters))):
        selected_indices.add(i)
    for i in range(max(0, len(chapters) - 20), len(chapters)):
        selected_indices.add(i)
    # 含关键标签的章节
    key_tags = {"战斗", "突破", "转折", "情感", "悬疑"}
    for i, ch in enumerate(chapters):
        if any(t in key_tags for t in ch.get("tags", [])):
            selected_indices.add(i)

    selected_indices = sorted(selected_indices)[:max_ch]

    # 构建 LLM 请求
    chapter_inputs = []
    for i in selected_indices:
        ch = chapters[i]
        chapter_inputs.append({
            "index": ch.get("index", i + 1),
            "title": ch.get("title", ""),
            "head_text": ch.get("head_text", "")[:150],
            "tail_text": ch.get("tail_text", "")[:150],
        })

    prompt = f"""请为以下小说章节各写一句话摘要（不超过30字），以JSON数组返回。
每项格式：{{"index": 章节序号, "summary": "一句话摘要"}}

章节列表：
{json.dumps(chapter_inputs, ensure_ascii=False, indent=1)}

只返回JSON数组，不要其他内容。"""

    try:
        from shared_utils import call_json_chat_completion_with_fallback, create_chat_completion
        from token_tracker import create_default_tracker

        messages = [
            {"role": "system", "content": "你是小说摘要专家，为每章写一句话概要。"},
            {"role": "user", "content": prompt},
        ]

        data = call_json_chat_completion_with_fallback(
            chat_completion_func=chat_completion_func,
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=2000,
        )

        if isinstance(data, list):
            summary_map = {item.get("index"): item.get("summary", "") for item in data if isinstance(item, dict)}
            for ch in chapters:
                idx = ch.get("index")
                if idx in summary_map:
                    ch["summary"] = summary_map[idx]
            outline["llm_enhanced"] = True

    except Exception as e:
        logger.warning(f"大纲 LLM 增强失败: {e}")

    return outline


# ===================== 持久化 =====================
def save_outline(outline: Dict[str, Any], path: str) -> None:
    """保存大纲到 JSON 文件。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)


def load_outline(path: str) -> Optional[Dict[str, Any]]:
    """从 JSON 文件加载大纲。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def outline_signature(text: str) -> str:
    """计算文本的大纲签名（用于判断是否需要重新生成大纲）。"""
    return hashlib.sha1((text or "")[:100000].encode("utf-8")).hexdigest()[:16]
