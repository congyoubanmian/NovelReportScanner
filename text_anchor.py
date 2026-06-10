"""
文本锚定工具模块：统一的 evidence 定位与上下文截取方法。

从 diag_rerun_child_owner.py 迁移并增强，供 novel_reviewer.py 和其他模块复用。

功能：
1. 换行符规范化
2. 统一 chunk manifest 生成与读取
3. 多策略锚点定位：EXACT -> ELLIPSIS -> FUZZY_ALIGN -> MULTI_FRAGMENT
4. 句子切分与窗口截取
5. evidence 在上下文中的存在性检查
6. evidence span 迁移（从 LLM 输出的 span 切片原文）
"""

import hashlib
import json
import re
import os
from bisect import bisect_left
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ===================== 配置常量 =====================
# Chunk 切分配置
DEFAULT_CHUNK_SIZE = 6000
DEFAULT_CHUNK_OVERLAP = 500
DEFAULT_EXPAND_RANGE = 1
CHUNK_MANIFEST_VERSION = 1
CHUNK_MANIFEST_MODE = "paragraph_window_v1"

# 上下文截取配置
CTX_MAX_CHARS = 2200
BEFORE_SENTS = 3
AFTER_SENTS = 3

# ELLIPSIS 策略配置
ELLIPSIS_DELIMITERS = ['……', '...', '…', '..']
ELLIPSIS_PREFIX_LEN = 16
ELLIPSIS_SUFFIX_LEN = 16
ELLIPSIS_MAX_GAP = 1200

# FUZZY_ALIGN 策略配置
FUZZY_NGRAM_SIZE = 3
FUZZY_WINDOW_SIZE = 200
FUZZY_STEP_SIZE = 30
FUZZY_THRESHOLD = 0.65

# MULTI_FRAGMENT 策略配置
MULTI_FRAG_MIN_LEN = 8
MULTI_FRAG_MAX_GAP = 800
MULTI_FRAG_MIN_SCORE = 15

# 泛短语列表（低信息量，需降权）
GENERIC_PHRASES = [
    "是我们的女儿", "是女孩哦", "是个女孩", "是男孩", "是个男孩",
    "是我们的孩子", "我们的孩子", "生了", "怀孕了", "有孩子了",
    "是女儿", "是儿子", "叫什么", "取名", "名字"
]


# ===================== 换行符规范化 =====================



# ===================== 语义边界感知切分配置 =====================
SEMANTIC_CHUNK_ENABLED = os.environ.get("SEMANTIC_CHUNK_ENABLED", "1").strip() == "1"

# 章节标题正则（覆盖中文网文常见格式）
# 策略：以"第X章"为主力检测，序章/终章/番外等作为补充
_CHAPTER_HEADING_RE = re.compile(
    r'(?:^|\n)[\s\u3000]*'
    r'(?:'
    r'第[零一二三四五六七八九十百千万〇０-９\d]+[章节卷部篇章回集][^\n]*'
    r'|[Cc]hapter\s*\d+[^\n]*'
    r'|卷[零一二三四五六七八九十百千万〇\d]+[^\n]*'
    r'|[Pp]art\s*\d+[^\n]*'
    r'|序[章言][^\n]*'
    r'|终章[^\n]*'
    r'|尾声[^\n]*'
    r'|楔子[^\n]*'
    r'|番外[^\n]*'
    r'|引子[^\n]*'
    r'|后记[^\n]*'
    r'|附录[^\n]*'
    r')',
    re.MULTILINE,
)

# 场景切换信号词（仅在大章节内部作为备选分割点使用）
_SCENE_SHIFT_TIME_RE = re.compile(
    r'(?:次日|翌日|三天后|数日后|半月后|一月后|半年后|数月后|一年后|数年后'
    r'|翌晨|翌晚|入夜|深夜|天明|清晨|傍晚|午后'
    r'|午时|子时|丑时|寅时|卯时|辰时|巳时|未时|申时|酉时|戌时|亥时)[，。！？\s]'
)
_SCENE_SHIFT_PLACE_RE = re.compile(
    r'^[\s\u3000]*(?:另一处|另一边|与此同时|却说|且说|话分两头|远处|千里之外|百里之外)[，。！？\s]',
    re.MULTILINE,
)

# 场景断点：连续空行数阈值
SCENE_BREAK_MIN_BLANK_LINES = 2

# 大章节阈值：超过此大小的章节才做内部分割
LARGE_CHAPTER_THRESHOLD_FACTOR = 1.5  # chunk_size 的倍数


def _find_chapter_heading_matches(text: str) -> List[Tuple[int, str]]:
    """
    查找所有章节标题的 (标题行起始位置, 完整标题文本)。

    返回按位置排序的列表。同一行只保留一个标题。
    标题文本包含章节号和标题名，如 "第001章 溪上何人品玉箫"。
    """
    results = []
    seen_positions = set()
    for match in _CHAPTER_HEADING_RE.finditer(text):
        # match.group() 包含可能的 \n 前缀，strip 后得到标题行
        title = match.group().strip()
        if not title:
            continue
        # 定位标题行的真正起始（跳过 match 中的 \n 前缀）
        raw = match.group()
        newline_in_match = raw.find("\n")
        if newline_in_match >= 0:
            line_start = match.start() + newline_in_match + 1
        else:
            line_start = match.start()
        # 同一位置去重
        if line_start in seen_positions:
            continue
        seen_positions.add(line_start)
        results.append((line_start, title))
    return results


def _find_scene_breaks_in_range(text: str, start: int, end: int) -> List[int]:
    """
    在指定范围内查找场景切换断点。

    仅返回行首位置，用于大章节内部分割。
    """
    segment = text[start:end]
    if not segment:
        return []

    breaks = []

    # 策略1：连续空行
    lines = segment.split("\n")
    blank_run = 0
    offset = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
        else:
            if blank_run >= SCENE_BREAK_MIN_BLANK_LINES:
                breaks.append(start + offset)
            blank_run = 0
            # 场景跳变信号词
            stripped = line.strip()
            if _SCENE_SHIFT_TIME_RE.search(stripped) or _SCENE_SHIFT_PLACE_RE.search(stripped):
                breaks.append(start + offset)
        offset += len(line) + 1

    return breaks


def _build_chunks_from_paragraphs(
    paragraph_spans: List[Tuple[int, int]],
    para_indices: List[int],
    chunk_size: int,
    overlap: int,
    normalized_text: str,
    chunk_no_start: int,
    seg_start: int,
    seg_end: int,
    is_chapter_boundary: bool = True,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    在给定段落索引范围内，按 chunk_size 积累生成 chunks。

    返回 (chunks列表, 下一个chunk编号)。
    """
    chunks = []
    chunk_no = chunk_no_start
    if not para_indices:
        return chunks, chunk_no

    start_idx = 0
    while start_idx < len(para_indices):
        end_idx = start_idx
        core_start = paragraph_spans[para_indices[start_idx]][0]
        core_end = paragraph_spans[para_indices[start_idx]][1]

        while end_idx + 1 < len(para_indices):
            next_pi = para_indices[end_idx + 1]
            next_end = paragraph_spans[next_pi][1]
            if (next_end - core_start) > chunk_size and end_idx >= start_idx:
                break
            end_idx += 1
            core_end = next_end
            if (core_end - core_start) >= chunk_size:
                break

        # window 扩展（但不跨语义段边界）
        para_start_idx = para_indices[start_idx]
        para_end_idx = para_indices[end_idx]

        window_start_idx = para_start_idx
        while window_start_idx > 0 and (core_start - paragraph_spans[window_start_idx - 1][0]) < overlap:
            if paragraph_spans[window_start_idx - 1][0] < seg_start:
                break
            window_start_idx -= 1

        window_end_idx = para_end_idx
        while window_end_idx + 1 < len(paragraph_spans) and (paragraph_spans[window_end_idx + 1][1] - core_end) < overlap:
            if paragraph_spans[window_end_idx + 1][1] > seg_end:
                break
            window_end_idx += 1

        window_start = paragraph_spans[window_start_idx][0]
        window_end = paragraph_spans[window_end_idx][1]

        chunks.append({
            "chunk_index": chunk_no,
            "core_start": core_start,
            "core_end": core_end,
            "window_start": window_start,
            "window_end": window_end,
            "text": normalized_text[window_start:window_end],
            "semantic_boundary_at_start": is_chapter_boundary and start_idx == 0,
        })
        chunk_no += 1
        start_idx = end_idx + 1

    return chunks, chunk_no


def build_semantic_chunk_manifest(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    *,
    semantic_aware: bool = None,
) -> Dict[str, Any]:
    """
    语义边界感知的 chunk manifest 生成。

    核心策略（章节优先）：
    1. 检测章节标题，以章节为第一级分割单位
    2. 每章尽量作为一个 chunk；超过 chunk_size 的章节内部按段落分割
    3. 超大章节（> chunk_size * 1.5）内部检测场景切换断点作为辅助分割
    4. chunk 的 window 扩展不跨越章节边界

    若 semantic_aware=False 或检测不到章节边界，回退到原始 build_chunk_manifest。
    """
    if semantic_aware is None:
        semantic_aware = SEMANTIC_CHUNK_ENABLED

    if not semantic_aware:
        return build_chunk_manifest(text, chunk_size=chunk_size, overlap=overlap)

    normalized_text = normalize_newlines(text or "")
    if not normalized_text.strip():
        return build_chunk_manifest(text, chunk_size=chunk_size, overlap=overlap)

    chunk_size = _coerce_chunk_size(chunk_size)
    overlap = _coerce_overlap(overlap, chunk_size)
    paragraph_spans = _iter_nonempty_line_spans(normalized_text)

    # 检测章节边界
    chapter_matches = _find_chapter_heading_matches(normalized_text)
    chapter_starts = [pos for pos, _title in chapter_matches]

    # 若章节数太少（<3），回退到原始逻辑
    if len(chapter_starts) < 3:
        manifest = build_chunk_manifest(text, chunk_size=chunk_size, overlap=overlap)
        manifest["semantic_chunk_fallback"] = True
        manifest["chapter_count"] = len(chapter_starts)
        return manifest

    # 构建段落起始位置索引（用于二分查找）
    para_starts = [ps for ps, pe in paragraph_spans]

    # 构建章节段列表: [(seg_start, seg_end, is_chapter_start)]
    chapter_set = set(chapter_starts)
    segments: List[Tuple[int, int, bool]] = []

    # 前言部分（第一个章节标题之前的内容）
    first_chapter = chapter_starts[0]
    if first_chapter > 0:
        segments.append((0, first_chapter, False))

    # 每个章节
    for i, ch_start in enumerate(chapter_starts):
        ch_end = chapter_starts[i + 1] if i + 1 < len(chapter_starts) else len(normalized_text)
        segments.append((ch_start, ch_end, True))

    # 在每个章节段内生成 chunks
    large_chapter_threshold = int(chunk_size * LARGE_CHAPTER_THRESHOLD_FACTOR)
    chunks: List[Dict[str, Any]] = []
    chunk_no = 1
    total_scene_breaks_used = 0

    for seg_start, seg_end, is_chapter in segments:
        seg_len = seg_end - seg_start
        is_chapter_start = is_chapter

        # 用二分查找定位此段内的段落索引（O(log n) 替代 O(n) 线性扫描）
        left = bisect_left(para_starts, seg_start)
        right = bisect_left(para_starts, seg_end)
        seg_para_indices = list(range(left, right))

        if not seg_para_indices:
            continue

        # 小章节：整章作为一个 chunk
        if seg_len <= chunk_size and len(seg_para_indices) > 0:
            core_start = paragraph_spans[seg_para_indices[0]][0]
            core_end = paragraph_spans[seg_para_indices[-1]][1]

            # window 扩展
            window_start_idx = seg_para_indices[0]
            while window_start_idx > 0 and (core_start - paragraph_spans[window_start_idx - 1][0]) < overlap:
                if paragraph_spans[window_start_idx - 1][0] < seg_start:
                    break
                window_start_idx -= 1

            window_end_idx = seg_para_indices[-1]
            while window_end_idx + 1 < len(paragraph_spans) and (paragraph_spans[window_end_idx + 1][1] - core_end) < overlap:
                if paragraph_spans[window_end_idx + 1][1] > seg_end:
                    break
                window_end_idx += 1

            chunks.append({
                "chunk_index": chunk_no,
                "core_start": core_start,
                "core_end": core_end,
                "window_start": paragraph_spans[window_start_idx][0],
                "window_end": paragraph_spans[window_end_idx][1],
                "text": normalized_text[paragraph_spans[window_start_idx][0]:paragraph_spans[window_end_idx][1]],
                "semantic_boundary_at_start": is_chapter_start,
            })
            chunk_no += 1
            continue

        # 大章节：内部按 chunk_size 分割
        # 若超过 large_chapter_threshold，先用场景断点做二级分割
        if seg_len > large_chapter_threshold:
            scene_breaks = _find_scene_breaks_in_range(normalized_text, seg_start, seg_end)
            total_scene_breaks_used += len(scene_breaks)
            if scene_breaks:
                # 在大章节内按场景断点分子段，每个子段内再按 chunk_size 积累
                sub_segments = []
                prev = seg_start
                for sb in scene_breaks:
                    if sb > prev:
                        sub_segments.append((prev, sb))
                    prev = sb
                if prev < seg_end:
                    sub_segments.append((prev, seg_end))

                for sub_start, sub_end in sub_segments:
                    # 二分查找子段内的段落索引
                    sub_left = bisect_left(para_starts, sub_start, lo=left, hi=right)
                    sub_right = bisect_left(para_starts, sub_end, lo=sub_left, hi=right)
                    sub_para_indices = list(range(sub_left, sub_right))
                    new_chunks, chunk_no = _build_chunks_from_paragraphs(
                        paragraph_spans, sub_para_indices, chunk_size, overlap,
                        normalized_text, chunk_no, sub_start, sub_end,
                        is_chapter_boundary=False,
                    )
                    chunks.extend(new_chunks)
                continue

        # 普通大章节：直接按段落积累
        new_chunks, chunk_no = _build_chunks_from_paragraphs(
            paragraph_spans, seg_para_indices, chunk_size, overlap,
            normalized_text, chunk_no, seg_start, seg_end,
            is_chapter_boundary=is_chapter_start,
        )
        chunks.extend(new_chunks)

    # 构建 manifest
    chunking_mode = "semantic_chapter_primary"
    manifest = {
        "full_text": normalized_text,
        "chunk_size": chunk_size,
        "chunk_overlap": overlap,
        "chunk_count": len(chunks),
        "text_length": len(normalized_text),
        "chunking_mode": chunking_mode,
        "version": CHUNK_MANIFEST_VERSION,
        "chapter_count": len(chapter_starts),
        "scene_breaks_used": total_scene_breaks_used,
        "chunks": chunks,
    }
    manifest["signature"] = _compute_chunk_manifest_signature(
        manifest["full_text"],
        manifest["chunk_size"],
        manifest["chunk_overlap"],
        manifest["chunks"],
    )
    return manifest

def normalize_newlines(text: str) -> str:
    """
    统一换行符为 \n（全链路必须调用一次，且只调用一次）。
    
    规则：
    - \r\n -> \n
    - \r -> \n
    """
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ===================== Chunk Manifest =====================
def _coerce_chunk_size(chunk_size: Any) -> int:
    try:
        value = int(chunk_size)
    except Exception:
        value = DEFAULT_CHUNK_SIZE
    return max(1, value)


def _coerce_overlap(overlap: Any, chunk_size: int) -> int:
    try:
        value = int(overlap)
    except Exception:
        value = DEFAULT_CHUNK_OVERLAP
    value = max(0, value)
    if value >= chunk_size:
        return max(0, chunk_size - 1)
    return value


def _iter_nonempty_line_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line_body = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        if line_body.strip():
            spans.append((offset, offset + len(line_body)))
        offset += len(raw_line)
    if text and (not text.endswith("\n")):
        last_nl = text.rfind("\n")
        last_start = last_nl + 1 if last_nl >= 0 else 0
        if last_start < len(text):
            tail = text[last_start:]
            if tail.strip():
                tail_span = (last_start, len(text))
                if not spans or spans[-1] != tail_span:
                    spans.append(tail_span)
    return spans


def _make_chunk_signature_payload(
    full_text: str,
    chunk_size: int,
    overlap: int,
    chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "version": CHUNK_MANIFEST_VERSION,
        "chunking_mode": CHUNK_MANIFEST_MODE,
        "chunk_size": int(chunk_size),
        "chunk_overlap": int(overlap),
        "text_length": len(full_text or ""),
        "text_sha1": hashlib.sha1((full_text or "").encode("utf-8")).hexdigest(),
        "chunks": [
            {
                "chunk_index": int(entry.get("chunk_index", 0)),
                "core_start": int(entry.get("core_start", 0)),
                "core_end": int(entry.get("core_end", 0)),
                "window_start": int(entry.get("window_start", 0)),
                "window_end": int(entry.get("window_end", 0)),
            }
            for entry in chunks or []
        ],
    }


def _compute_chunk_manifest_signature(
    full_text: str,
    chunk_size: int,
    overlap: int,
    chunks: List[Dict[str, Any]],
) -> str:
    payload = _make_chunk_signature_payload(full_text, chunk_size, overlap, chunks)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_chunk_manifest(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> Dict[str, Any]:
    """
    以“非空行即段落”为单位生成统一 chunk manifest。

    规则：
    - core 区域按顺序累积到接近 chunk_size，但绝不拆段
    - window 区域在 core 基础上向前/向后至少吸附 overlap 个字符，并同样按整段扩张
    - 单段超过 chunk_size 时整段保留
    """
    normalized_text = normalize_newlines(text or "")
    chunk_size = _coerce_chunk_size(chunk_size)
    overlap = _coerce_overlap(overlap, chunk_size)
    paragraph_spans = _iter_nonempty_line_spans(normalized_text)

    chunks: List[Dict[str, Any]] = []
    if not paragraph_spans:
        manifest = {
            "full_text": normalized_text,
            "chunk_size": chunk_size,
            "chunk_overlap": overlap,
            "chunk_count": 0,
            "text_length": len(normalized_text),
            "chunking_mode": CHUNK_MANIFEST_MODE,
            "version": CHUNK_MANIFEST_VERSION,
            "chunks": [],
        }
        manifest["signature"] = _compute_chunk_manifest_signature(
            manifest["full_text"],
            manifest["chunk_size"],
            manifest["chunk_overlap"],
            manifest["chunks"],
        )
        return manifest

    core_ranges: List[Tuple[int, int]] = []
    start_idx = 0
    while start_idx < len(paragraph_spans):
        end_idx = start_idx
        core_start = paragraph_spans[start_idx][0]
        core_end = paragraph_spans[start_idx][1]
        while end_idx + 1 < len(paragraph_spans):
            next_end = paragraph_spans[end_idx + 1][1]
            if (next_end - core_start) > chunk_size and end_idx >= start_idx:
                break
            end_idx += 1
            core_end = next_end
            if (core_end - core_start) >= chunk_size:
                break
        core_ranges.append((start_idx, end_idx))
        start_idx = end_idx + 1

    for chunk_no, (para_start_idx, para_end_idx) in enumerate(core_ranges, start=1):
        core_start = paragraph_spans[para_start_idx][0]
        core_end = paragraph_spans[para_end_idx][1]

        window_start_idx = para_start_idx
        while window_start_idx > 0 and (core_start - paragraph_spans[window_start_idx][0]) < overlap:
            window_start_idx -= 1

        window_end_idx = para_end_idx
        while window_end_idx + 1 < len(paragraph_spans) and (paragraph_spans[window_end_idx][1] - core_end) < overlap:
            window_end_idx += 1

        window_start = paragraph_spans[window_start_idx][0]
        window_end = paragraph_spans[window_end_idx][1]
        chunks.append(
            {
                "chunk_index": chunk_no,
                "core_start": core_start,
                "core_end": core_end,
                "window_start": window_start,
                "window_end": window_end,
                "text": normalized_text[window_start:window_end],
            }
        )

    manifest = {
        "full_text": normalized_text,
        "chunk_size": chunk_size,
        "chunk_overlap": overlap,
        "chunk_count": len(chunks),
        "text_length": len(normalized_text),
        "chunking_mode": CHUNK_MANIFEST_MODE,
        "version": CHUNK_MANIFEST_VERSION,
        "chunks": chunks,
    }
    manifest["signature"] = _compute_chunk_manifest_signature(
        manifest["full_text"],
        manifest["chunk_size"],
        manifest["chunk_overlap"],
        manifest["chunks"],
    )
    return manifest


def save_chunk_manifest(manifest: Dict[str, Any], path: str) -> None:
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_chunk_manifest(path: str) -> Dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"chunk manifest not found: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"invalid chunk manifest payload: {path}")
    chunks = manifest.get("chunks", [])
    if not isinstance(chunks, list):
        raise ValueError(f"invalid chunk manifest chunks: {path}")
    return manifest


# ===================== Chunk 切分 =====================
def split_to_chunks(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> Tuple[List[str], List[int]]:
    """
    兼容包装：返回 manifest 中每个 window chunk 的文本和起始位置。
    """
    manifest = build_chunk_manifest(text, chunk_size=chunk_size, overlap=overlap)
    chunks = [entry.get("text", "") for entry in manifest.get("chunks", [])]
    starts = [int(entry.get("window_start", 0)) for entry in manifest.get("chunks", [])]
    return chunks, starts


def get_context_around_chunk(
    chunks: List[str],
    chunk_index: int,
    expand_range: int = DEFAULT_EXPAND_RANGE,
    joiner: str = "\n\n---\n\n",
) -> Tuple[str, str]:
    """
    获取 big_context 与 center_chunk。chunk_index 为 1-based。
    
    返回:
    - big_context: 拼接后的扩展上下文
    - center_chunk: 中心 chunk 文本
    """
    if not chunks or chunk_index < 1:
        return "", ""
    
    idx = chunk_index - 1
    if idx >= len(chunks):
        return "", ""
    
    center = chunks[idx]
    start = max(0, idx - expand_range)
    end = min(len(chunks), idx + expand_range + 1)
    big_parts = chunks[start:end]
    big_ctx = joiner.join(big_parts)
    
    return big_ctx, center


def _get_context_bounds_from_source(
    full_text: str,
    chunk_index: int,
    expand_range: int = DEFAULT_EXPAND_RANGE,
    chunk_entries: Optional[List[Dict[str, Any]]] = None,
    chunk_starts: Optional[List[int]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Tuple[int, int, int, int]:
    if chunk_entries:
        n = len(chunk_entries)
        if chunk_index < 1 or chunk_index > n:
            return 0, 0, 0, 0
        lo_ci = max(1, chunk_index - expand_range)
        hi_ci = min(n, chunk_index + expand_range)
        lo_entry = chunk_entries[lo_ci - 1]
        hi_entry = chunk_entries[hi_ci - 1]
        center_entry = chunk_entries[chunk_index - 1]
        return (
            int(lo_entry.get("window_start", 0)),
            min(len(full_text), int(hi_entry.get("window_end", 0))),
            int(center_entry.get("window_start", 0)),
            min(len(full_text), int(center_entry.get("window_end", 0))),
        )

    if not chunk_starts:
        return 0, 0, 0, 0

    n = len(chunk_starts)
    if chunk_index < 1 or chunk_index > n:
        return 0, 0, 0, 0
    lo_ci = max(1, chunk_index - expand_range)
    hi_ci = min(n, chunk_index + expand_range)
    win_start = int(chunk_starts[lo_ci - 1])
    win_end = min(int(chunk_starts[hi_ci - 1]) + int(chunk_size), len(full_text))
    center_start = int(chunk_starts[chunk_index - 1])
    center_end = min(center_start + int(chunk_size), len(full_text))
    return win_start, win_end, center_start, center_end


def get_context_around_chunk_from_fulltext(
    full_text: str,
    chunk_starts: Optional[List[int]],
    chunk_index: int,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    expand_range: int = DEFAULT_EXPAND_RANGE,
    chunk_entries: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, Dict[str, int]]:
    """
    从 full_text 连续切窗构造 big_context（不插入分隔符），并返回 center_chunk。
    chunk_index 为 1-based。
    
    返回:
    - big_context: full_text[win_start:win_end]
    - center_chunk: full_text[center_global_start:center_global_end]
    - spans: 包含 win_start, win_end, center_start, center_end
    """
    if not full_text:
        return "", "", {"win_start": 0, "win_end": 0, "center_start": 0, "center_end": 0}

    win_start, win_end, center_global_start, center_global_end = _get_context_bounds_from_source(
        full_text=full_text,
        chunk_index=chunk_index,
        expand_range=expand_range,
        chunk_entries=chunk_entries,
        chunk_starts=chunk_starts,
        chunk_size=chunk_size,
    )
    if win_end <= win_start or center_global_end <= center_global_start:
        return "", "", {"win_start": 0, "win_end": 0, "center_start": 0, "center_end": 0}

    big_context = full_text[win_start:win_end]
    center_chunk = full_text[center_global_start:center_global_end]

    center_start_local = max(0, center_global_start - win_start)
    center_end_local = max(center_start_local, center_global_end - win_start)

    spans = {
        "win_start": win_start,
        "win_end": win_end,
        "center_start": center_start_local,
        "center_end": center_end_local,
        "center_global_start": center_global_start,
        "center_global_end": center_global_end,
    }
    return big_context, center_chunk, spans


# ===================== 基础工具函数 =====================
def normalize_text(text: str) -> str:
    """标准化文本：去引号、压缩空白。"""
    if not text:
        return ""
    
    text = text.strip()
    for quote in ['"', '"', '"', "'", "'", "'", '「', '」', '『', '』']:
        if text.startswith(quote):
            text = text[1:]
        if text.endswith(quote):
            text = text[:-1]
    
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def has_ellipsis(text: str) -> bool:
    """检查文本是否包含省略号。"""
    return any(delim in text for delim in ELLIPSIS_DELIMITERS)


def split_by_ellipsis(text: str) -> Tuple[str, str]:
    """按省略号分割文本，返回 (left_part, right_part)。"""
    for delim in ELLIPSIS_DELIMITERS:
        if delim in text:
            parts = text.split(delim)
            left = parts[0].strip() if parts else ""
            right = parts[-1].strip() if len(parts) > 1 else ""
            return left, right
    return text, ""


def find_all_positions(text: str, pattern: str) -> List[int]:
    """查找所有匹配位置。"""
    positions = []
    start = 0
    while True:
        pos = text.find(pattern, start)
        if pos < 0:
            break
        positions.append(pos)
        start = pos + 1
    return positions


def generate_match_candidates(text: str, prefix_len: int) -> List[str]:
    """生成匹配候选片段。"""
    if not text:
        return []
    
    normalized = normalize_text(text)
    candidates = []
    
    if text:
        candidates.append(text)
    
    if normalized and normalized != text:
        candidates.append(normalized)
    
    for length in [prefix_len, prefix_len - 2, prefix_len - 4, max(8, prefix_len - 6)]:
        if len(normalized) >= length:
            candidates.append(normalized[:length])
    
    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    
    return result


def normalize_for_match(text: str) -> str:
    """生成去引号版本用于匹配。"""
    if not text:
        return ""
    quotes = ['"', '"', '"', "'", "'", "'", '「', '」', '『', '』', '《', '》', '〈', '〉', '"', '"']
    result = text
    for q in quotes:
        result = result.replace(q, '')
    return result


# ===================== 句子切分 =====================
def split_cn_sentences(text: str) -> List[str]:
    """按中文标点/换行粗切句。"""
    if not text:
        return []
    
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if not lines:
        lines = [str(text)]
    
    sents: List[str] = []
    for ln in lines:
        parts = re.split(r"(?<=[。！？!?；;])", ln)
        for p in parts:
            p = p.strip()
            if p:
                sents.append(p)
    
    return sents


def split_cn_sentences_with_spans(text: str) -> List[Tuple[int, int]]:
    """
    在"原始 text"上切句并保留 span（不 strip、不重排字符）。
    
    返回: [(start, end)]，均为 0-based，end 为开区间。
    """
    if text is None:
        return []
    t = str(text)
    if not t:
        return []
    
    end_punct = set("。！？!?；;")
    spans: List[Tuple[int, int]] = []
    start = 0
    i = 0
    n = len(t)
    
    while i < n:
        ch = t[i]
        
        # 连续空行边界
        if ch == "\n":
            j = i
            while j < n and t[j] == "\n":
                j += 1
            if (j - i) >= 2:
                if j > start:
                    spans.append((start, j))
                start = j
                i = j
                continue
        
        # 句末标点边界
        if ch in end_punct:
            end = i + 1
            if end > start:
                spans.append((start, end))
            start = end
            i = end
            continue
        
        i += 1
    
    if start < n:
        spans.append((start, n))
    
    spans = [(s, e) for (s, e) in spans if e > s]
    return spans


def sentence_hit_index_by_spans(spans: List[Tuple[int, int]], anchor_start: int) -> Optional[int]:
    """用 span 判断 anchor_start 命中哪个句段。"""
    if not spans or anchor_start is None:
        return None
    a = int(anchor_start)
    for i, (s, e) in enumerate(spans):
        if s <= a < e:
            return i
    return None


# ===================== MULTI_FRAGMENT 策略 =====================
def _is_generic_phrase(text: str) -> bool:
    """检查是否为泛短语（低信息量）。"""
    text_lower = text.lower()
    for phrase in GENERIC_PHRASES:
        if phrase in text_lower or text_lower in phrase:
            return True
    return False


def _score_fragment(
    fragment: str,
    child_name: str,
    current_heroine: str,
    father: str,
) -> int:
    """计算片段得分。"""
    if not fragment:
        return 0
    
    score = len(fragment)
    
    if _is_generic_phrase(fragment):
        score = max(1, score // 3)
    
    if child_name and child_name in fragment:
        score += 20
    if current_heroine and current_heroine in fragment:
        score += 10
    if father and father in fragment:
        score += 6
    
    return score


def _should_keep_fragment(
    fragment: str,
    child_name: str,
    current_heroine: str,
    father: str,
) -> bool:
    """判断是否保留片段。"""
    if not fragment:
        return False
    
    if child_name and child_name in fragment:
        return True
    if current_heroine and current_heroine in fragment:
        return True
    if father and father in fragment:
        return True
    
    if len(fragment) >= MULTI_FRAG_MIN_LEN:
        return True
    
    return False


def split_evidence_fragments(evidence: str) -> List[str]:
    """将聚合 evidence 拆分为独立片段。"""
    if not evidence:
        return []
    
    fragments = []
    
    # 优先提取引号内片段
    quote_patterns = [
        r'"([^"]+)"',
        r'"([^"]+)"',
        r'「([^」]+)」',
        r'『([^』]+)』',
        r"'([^']+)'",
    ]
    
    found_quoted = False
    for pattern in quote_patterns:
        matches = re.findall(pattern, evidence)
        if matches:
            found_quoted = True
            for m in matches:
                frag = _normalize_fragment(m)
                if frag and frag not in fragments:
                    fragments.append(frag)
    
    if not found_quoted:
        temp = evidence
        for ellipsis in ['……', '...', '…', '..']:
            temp = temp.replace(ellipsis, '|SPLIT|')
        
        parts = re.split(r'\|SPLIT\||[、,，;；]+', temp)
        for p in parts:
            frag = _normalize_fragment(p)
            if frag and frag not in fragments:
                fragments.append(frag)
    
    return fragments


def _normalize_fragment(text: str) -> str:
    """标准化单个片段。"""
    if not text:
        return ""
    
    result = text.strip()
    
    strip_chars = ['"', '"', '"', "'", "'", "'", '「', '」', '『', '』', '"', '"',
                   ',', '，', '.', '。', '、', '!', '！', '?', '？', ' ']
    changed = True
    while changed:
        changed = False
        for c in strip_chars:
            if result.startswith(c):
                result = result[1:]
                changed = True
            if result.endswith(c):
                result = result[:-1]
                changed = True
    
    result = re.sub(r'\s+', ' ', result)
    
    for ellipsis in ['……', '...', '…']:
        result = result.replace(ellipsis, '')
    
    return result.strip()


def find_multi_fragment_anchor(
    big_context: str,
    full_text: str,
    fragments: List[str],
    child_name: str = "",
    current_heroine: str = "",
    father: str = "",
    max_gap: int = MULTI_FRAG_MAX_GAP,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """MULTI_FRAGMENT 策略：多片段聚合匹配。"""
    meta: Dict[str, Any] = {
        "matched_fragments": [],
        "missed_fragments": [],
        "cluster_count": 0,
        "cluster_score": 0,
        "anchor_from": None,
        "risk_flags": [],
        "all_hits": [],
    }
    
    if not fragments:
        meta["risk_flags"].append("NO_FRAGMENTS")
        return None, meta
    
    valid_fragments = []
    for frag in fragments:
        if _should_keep_fragment(frag, child_name, current_heroine, father):
            valid_fragments.append(frag)
        else:
            meta["missed_fragments"].append({"fragment": frag, "reason": "too_short_no_key_name"})
    
    if not valid_fragments:
        meta["risk_flags"].append("ALL_FRAGMENTS_FILTERED")
        return None, meta
    
    search_targets = [
        ("big_context", big_context),
        ("full_text", full_text),
    ]
    
    all_hits = []
    
    for frag in valid_fragments:
        frag_score = _score_fragment(frag, child_name, current_heroine, father)
        frag_normalized = normalize_for_match(frag)
        
        found = False
        for source_name, source_text in search_targets:
            if not source_text:
                continue
            
            positions = find_all_positions(source_text, frag)
            for pos in positions:
                all_hits.append((pos, frag, frag_score, source_name, len(frag)))
                found = True
            
            if frag_normalized and frag_normalized != frag:
                positions = find_all_positions(source_text, frag_normalized)
                for pos in positions:
                    all_hits.append((pos, frag, frag_score, source_name, len(frag_normalized)))
                    found = True
        
        if found:
            meta["matched_fragments"].append(frag)
        else:
            meta["missed_fragments"].append({"fragment": frag, "reason": "not_found"})
    
    meta["all_hits"] = [(h[0], h[1][:20], h[2], h[3]) for h in all_hits[:20]]
    
    if not all_hits:
        meta["risk_flags"].append("NO_HITS")
        return None, meta
    
    if len(meta["matched_fragments"]) == 1:
        meta["risk_flags"].append("ONLY_1_FRAG_FOUND")
    
    all_generic = all(_is_generic_phrase(frag) for frag in meta["matched_fragments"])
    if all_generic and meta["matched_fragments"]:
        meta["risk_flags"].append("GENERIC_FRAG_ONLY")
    
    hits_by_source: Dict[str, List[Tuple]] = {}
    for hit in all_hits:
        pos, frag, score, source, frag_len = hit
        if source not in hits_by_source:
            hits_by_source[source] = []
        hits_by_source[source].append((pos, frag, score, frag_len))
    
    best_anchor = None
    best_cluster_score = 0
    best_source = None
    
    for source_name in ["big_context", "full_text"]:
        if source_name not in hits_by_source:
            continue
        
        hits = hits_by_source[source_name]
        if not hits:
            continue
        
        hits_sorted = sorted(hits, key=lambda x: x[0])
        
        clusters = []
        current_cluster = [hits_sorted[0]]
        
        for i in range(1, len(hits_sorted)):
            prev_pos, _, _, prev_len = current_cluster[-1]
            curr_pos, _, _, _ = hits_sorted[i]
            
            if curr_pos - (prev_pos + prev_len) <= max_gap:
                current_cluster.append(hits_sorted[i])
            else:
                clusters.append(current_cluster)
                current_cluster = [hits_sorted[i]]
        
        clusters.append(current_cluster)
        
        for cluster in clusters:
            cluster_score = sum(h[2] for h in cluster)
            
            if cluster_score > best_cluster_score:
                best_cluster_score = cluster_score
                min_pos = min(h[0] for h in cluster)
                max_pos = max(h[0] + h[3] for h in cluster)
                best_anchor = {
                    "start": min_pos,
                    "end": max_pos,
                    "strategy": "MULTI_FRAGMENT",
                    "meta": {},
                }
                best_source = source_name
                meta["cluster_count"] = len(clusters)
    
    meta["cluster_score"] = best_cluster_score
    meta["anchor_from"] = best_source
    
    if best_cluster_score < MULTI_FRAG_MIN_SCORE:
        meta["risk_flags"].append("LOW_CLUSTER_SCORE")
        return None, meta
    
    if best_anchor:
        best_anchor["meta"] = {
            "matched_fragments": meta["matched_fragments"],
            "cluster_score": best_cluster_score,
            "anchor_from": best_source,
            "risk_flags": meta["risk_flags"],
        }
    
    return best_anchor, meta


# ===================== ELLIPSIS 策略 =====================
def find_anchor_ellipsis(
    text: str,
    evidence: str,
    center_pos: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """ELLIPSIS 策略：处理含省略号的 evidence。"""
    if not has_ellipsis(evidence):
        return None
    
    left_part, right_part = split_by_ellipsis(evidence)
    
    left_candidates = generate_match_candidates(left_part, ELLIPSIS_PREFIX_LEN)
    right_candidates = generate_match_candidates(right_part, ELLIPSIS_SUFFIX_LEN)
    
    left_hits: Dict[str, List[int]] = {}
    right_hits: Dict[str, List[int]] = {}
    
    for cand in left_candidates:
        positions = find_all_positions(text, cand)
        if positions:
            left_hits[cand] = positions
    
    for cand in right_candidates:
        positions = find_all_positions(text, cand)
        if positions:
            right_hits[cand] = positions
    
    # 两边都存在：尝试配对
    if left_hits and right_hits:
        best_pair = None
        best_gap = float('inf')
        
        for left_cand, left_positions in left_hits.items():
            for right_cand, right_positions in right_hits.items():
                for lpos in left_positions:
                    for rpos in right_positions:
                        if rpos > lpos:
                            gap = rpos - (lpos + len(left_cand))
                            if 0 <= gap <= ELLIPSIS_MAX_GAP and gap < best_gap:
                                best_gap = gap
                                best_pair = (lpos, rpos, left_cand, right_cand, gap)
        
        if best_pair:
            lpos, rpos, left_cand, right_cand, gap = best_pair
            return {
                "start": lpos,
                "end": rpos + len(right_cand),
                "strategy": "ELLIPSIS_BOTH",
                "matched_side": "both",
                "left_candidate": left_cand,
                "right_candidate": right_cand,
                "gap": gap,
                "ambiguous": False,
            }
        
        total_hits = sum(len(p) for p in left_hits.values()) + sum(len(p) for p in right_hits.values())
        if total_hits > 2:
            return {
                "start": -1,
                "end": -1,
                "strategy": "ELLIPSIS_AMBIGUOUS",
                "matched_side": "both",
                "left_candidate": "",
                "right_candidate": "",
                "gap": -1,
                "ambiguous": True,
            }
    
    # 只有左侧存在
    if left_hits and not right_hits:
        all_left_positions = []
        for cand, positions in left_hits.items():
            for pos in positions:
                all_left_positions.append((pos, cand))
        
        if len(all_left_positions) == 1:
            pos, cand = all_left_positions[0]
            return {
                "start": pos,
                "end": pos + len(cand),
                "strategy": "ELLIPSIS_LEFT_ONLY",
                "matched_side": "left",
                "left_candidate": cand,
                "right_candidate": "",
                "gap": 0,
                "ambiguous": False,
            }
        
        if center_pos is not None and all_left_positions:
            best_pos = min(all_left_positions, key=lambda x: abs(x[0] - center_pos))
            pos, cand = best_pos
            return {
                "start": pos,
                "end": pos + len(cand),
                "strategy": "ELLIPSIS_LEFT_ONLY",
                "matched_side": "left",
                "left_candidate": cand,
                "right_candidate": "",
                "gap": 0,
                "ambiguous": True,
            }
    
    # 只有右侧存在
    if right_hits and not left_hits:
        all_right_positions = []
        for cand, positions in right_hits.items():
            for pos in positions:
                all_right_positions.append((pos, cand))
        
        if len(all_right_positions) == 1:
            pos, cand = all_right_positions[0]
            return {
                "start": pos,
                "end": pos + len(cand),
                "strategy": "ELLIPSIS_RIGHT_ONLY",
                "matched_side": "right",
                "left_candidate": "",
                "right_candidate": cand,
                "gap": 0,
                "ambiguous": False,
            }
        
        if center_pos is not None and all_right_positions:
            best_pos = min(all_right_positions, key=lambda x: abs(x[0] - center_pos))
            pos, cand = best_pos
            return {
                "start": pos,
                "end": pos + len(cand),
                "strategy": "ELLIPSIS_RIGHT_ONLY",
                "matched_side": "right",
                "left_candidate": "",
                "right_candidate": cand,
                "gap": 0,
                "ambiguous": True,
            }
    
    return None


# ===================== FUZZY_ALIGN 策略 =====================
def compute_ngrams(text: str, n: int = FUZZY_NGRAM_SIZE) -> set:
    """计算 n-gram 集合。"""
    if len(text) < n:
        return {text}
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def find_anchor_fuzzy(text: str, evidence: str) -> Optional[Dict[str, Any]]:
    """FUZZY_ALIGN 策略：模糊对齐恢复 anchor。"""
    if not text or not evidence:
        return None
    
    evidence_ngrams = compute_ngrams(evidence, FUZZY_NGRAM_SIZE)
    if not evidence_ngrams:
        return None
    
    best_score = 0.0
    best_window = None
    best_start = -1
    
    for start in range(0, len(text), FUZZY_STEP_SIZE):
        end = min(start + FUZZY_WINDOW_SIZE, len(text))
        window = text[start:end]
        
        window_ngrams = compute_ngrams(window, FUZZY_NGRAM_SIZE)
        if not window_ngrams:
            continue
        
        intersection = evidence_ngrams & window_ngrams
        score = len(intersection) / len(evidence_ngrams)
        
        if score > best_score:
            best_score = score
            best_window = window
            best_start = start
    
    if best_score >= FUZZY_THRESHOLD and best_window:
        return {
            "start": best_start,
            "end": best_start + len(best_window),
            "strategy": "FUZZY_ALIGN",
            "score": best_score,
            "recovered_quote": best_window[:100],
        }
    
    return None


# ===================== 综合锚点定位 =====================
def pick_best_hit(
    hits: List[Dict[str, Any]],
    center_pos: Optional[int],
    prefer_span: Optional[Tuple[int, int]],
) -> Optional[Dict[str, Any]]:
    """从多个命中里选择最优。"""
    if not hits:
        return None
    
    def _score(h: Dict[str, Any]) -> Tuple[int, int, int]:
        s = int(h.get("start", -1))
        e = int(h.get("end", -1))
        L = max(0, e - s)
        
        bonus = 0
        if prefer_span is not None:
            ps, pe = prefer_span
            if ps <= s and e <= pe:
                bonus = 10000
        
        dist_score = 0
        if center_pos is not None and s >= 0 and e >= 0:
            mid = (s + e) // 2
            dist_score = -abs(mid - center_pos)
        
        return (bonus, dist_score, L)
    
    return max(hits, key=_score)


def find_evidence_anchor(
    text: str,
    evidence: str,
    center_pos: Optional[int] = None,
    prefer_span: Optional[Tuple[int, int]] = None,
    full_text: Optional[str] = None,
    child_name: Optional[str] = None,
    current_heroine: Optional[str] = None,
    father: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    综合 anchor 定位：按优先级尝试 EXACT -> ELLIPSIS -> FUZZY_ALIGN -> MULTI_FRAGMENT。
    
    返回:
    {
        "start": int,
        "end": int,
        "strategy": "EXACT" | "ELLIPSIS_*" | "FUZZY_ALIGN" | "MULTI_FRAGMENT",
        "meta": {...},
    }
    """
    if not text or not evidence:
        return None
    
    t = str(text)
    e = str(evidence).strip()
    
    # A1) EXACT：精确匹配
    hits: List[Dict[str, Any]] = []
    for pos in find_all_positions(t, e):
        hits.append({
            "start": pos,
            "end": pos + len(e),
            "pattern": e,
            "mode": "EXACT_FULL",
        })
    
    # 去空白后的精确匹配
    e_clean = e.replace(" ", "").replace("\t", "").replace("\n", "")
    for L in (20, 16, 12, 10, 8):
        if len(e_clean) >= L:
            candidate = e_clean[:L]
            for p in find_all_positions(t, candidate):
                hits.append({
                    "start": p,
                    "end": p + len(candidate),
                    "pattern": candidate,
                    "mode": "EXACT_PREFIX_CLEAN",
                    "prefix_len": L,
                })
    
    best = pick_best_hit(hits, center_pos=center_pos, prefer_span=prefer_span)
    if best:
        meta = {
            "pattern": best.get("pattern", ""),
            "mode": best.get("mode", ""),
        }
        if "prefix_len" in best:
            meta["prefix_len"] = best["prefix_len"]
        meta["hit_count"] = len(hits)
        return {
            "start": best["start"],
            "end": best["end"],
            "strategy": "EXACT",
            "meta": meta,
        }
    
    # A2) ELLIPSIS：省略号匹配
    ellipsis_result = find_anchor_ellipsis(t, e, center_pos)
    if ellipsis_result:
        return {
            "start": ellipsis_result["start"],
            "end": ellipsis_result["end"],
            "strategy": ellipsis_result["strategy"],
            "meta": ellipsis_result,
        }
    
    # A3) FUZZY_ALIGN：模糊对齐
    fuzzy_result = find_anchor_fuzzy(t, e)
    if fuzzy_result:
        return {
            "start": fuzzy_result["start"],
            "end": fuzzy_result["end"],
            "strategy": fuzzy_result["strategy"],
            "meta": fuzzy_result,
        }
    
    # A4) MULTI_FRAGMENT：多片段聚合匹配
    fragments = split_evidence_fragments(e)
    if fragments:
        multi_anchor, multi_meta = find_multi_fragment_anchor(
            big_context=t,
            full_text=full_text or "",
            fragments=fragments,
            child_name=child_name or "",
            current_heroine=current_heroine or "",
            father=father or "",
            max_gap=MULTI_FRAG_MAX_GAP,
        )
        if multi_anchor:
            multi_anchor["meta"]["multi_fragment_meta"] = multi_meta
            return multi_anchor
    
    return None


# ===================== 上下文截取 =====================
def _center_window(text: str, mid: int, max_chars: int) -> Tuple[str, Dict[str, Any]]:
    """围绕 mid 居中截断到 max_chars。"""
    if not text:
        return "", {"truncated": False, "left": 0, "right": 0, "mid": mid}
    if max_chars <= 0:
        return "", {"truncated": False, "left": 0, "right": 0, "mid": mid}
    if len(text) <= max_chars:
        return text, {"truncated": False, "left": 0, "right": len(text), "mid": mid}
    
    half = max_chars // 2
    left = max(0, mid - half)
    right = min(len(text), left + max_chars)
    if right - left < max_chars and left > 0:
        left = max(0, right - max_chars)
    ctx = text[left:right]
    return ctx, {"truncated": True, "left": left, "right": right, "mid": mid}


def evidence_in_ctx(ctx: str, evidence: str) -> Tuple[bool, Dict[str, Any]]:
    """检查 evidence 是否在 ctx 中（精确、前缀片段或省略号匹配）。"""
    meta = {
        "matched_side": "none",
        "candidates_used": [],
    }
    
    if not ctx or not evidence:
        return False, meta
    
    # 精确匹配
    if evidence in ctx:
        meta["matched_side"] = "exact"
        return True, meta
    
    # 前缀片段匹配
    e_clean = evidence.replace(" ", "").replace("\t", "").replace("\n", "")
    for L in (20, 16, 12, 10, 8):
        if len(e_clean) >= L:
            candidate = e_clean[:L]
            if candidate in ctx:
                meta["matched_side"] = "prefix"
                meta["candidates_used"].append(candidate)
                return True, meta
    
    # 省略号匹配
    if has_ellipsis(evidence):
        left_part, right_part = split_by_ellipsis(evidence)
        left_candidates = generate_match_candidates(left_part, ELLIPSIS_PREFIX_LEN)
        right_candidates = generate_match_candidates(right_part, ELLIPSIS_SUFFIX_LEN)
        
        left_found = any(cand in ctx for cand in left_candidates)
        right_found = any(cand in ctx for cand in right_candidates)
        
        if left_found and right_found:
            meta["matched_side"] = "both"
            meta["candidates_used"] = left_candidates + right_candidates
            return True, meta
        elif left_found:
            meta["matched_side"] = "left"
            meta["candidates_used"] = left_candidates
            return True, meta
        elif right_found:
            meta["matched_side"] = "right"
            meta["candidates_used"] = right_candidates
            return True, meta
    
    return False, meta


def anchor_evidence(
    normalized_full_text: Optional[str],
    chunks: List[str],
    chunk_starts: List[int],
    chunk_index: int,
    evidence_text: str,
    expand_range: int = DEFAULT_EXPAND_RANGE,
    before_sents: int = BEFORE_SENTS,
    after_sents: int = AFTER_SENTS,
    max_chars: int = CTX_MAX_CHARS,
    child_name: Optional[str] = None,
    current_heroine: Optional[str] = None,
    father: Optional[str] = None,
    chunk_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    统一的 evidence 锚定接口。
    
    返回:
    {
        "ctx_text": str,
        "anchor_found": bool,
        "anchor_strategy": "EXACT"|"ELLIPSIS_*"|"FUZZY_ALIGN"|"MULTI_FRAGMENT"|"NONE",
        "hit_sentence": str,
        "hit_sentence_span": [start, end],  # 相对 big_context
        "evidence_in_ctx": bool,
        "meta": {...}
    }
    """
    result: Dict[str, Any] = {
        "ctx_text": "",
        "anchor_found": False,
        "anchor_strategy": "NONE",
        "hit_sentence": "",
        "hit_sentence_span": None,
        "evidence_in_ctx": False,
        "meta": {},
    }
    
    if not evidence_text:
        result["meta"]["reason"] = "evidence_empty"
        return result
    
    # 获取 big_context
    if normalized_full_text and (chunk_entries or chunk_starts) and chunk_index >= 1:
        big_context, center_chunk, spans = get_context_around_chunk_from_fulltext(
            normalized_full_text,
            chunk_starts,
            chunk_index,
            chunk_size=DEFAULT_CHUNK_SIZE,
            expand_range=expand_range,
            chunk_entries=chunk_entries,
        )
        center_pos = (spans.get("center_start", 0) + spans.get("center_end", 0)) // 2
        prefer_span = (spans.get("center_start", 0), spans.get("center_end", 0))
        result["meta"]["big_context_start"] = spans.get("win_start", 0)
        result["meta"]["big_context_end"] = spans.get("win_end", 0)
    elif chunks and chunk_index >= 1 and chunk_index <= len(chunks):
        big_context, center_chunk = get_context_around_chunk(chunks, chunk_index, expand_range)
        center_pos = len(big_context) // 2
        prefer_span = None
        result["meta"]["big_context_start"] = 0
        result["meta"]["big_context_end"] = len(big_context)
    else:
        result["meta"]["reason"] = "invalid_chunk_index"
        return result
    
    if not big_context:
        result["meta"]["reason"] = "empty_big_context"
        return result
    
    # 尝试锚定
    anchor = find_evidence_anchor(
        text=big_context,
        evidence=evidence_text,
        center_pos=center_pos,
        prefer_span=prefer_span,
        full_text=normalized_full_text,
        child_name=child_name,
        current_heroine=current_heroine,
        father=father,
    )
    
    if anchor and anchor.get("start", -1) >= 0:
        result["anchor_found"] = True
        result["anchor_strategy"] = anchor.get("strategy", "UNKNOWN")
        result["meta"]["anchor"] = anchor
        
        start, end = anchor["start"], anchor["end"]
        
        # 用句子切分截取上下文
        spans_list = split_cn_sentences_with_spans(big_context)
        if spans_list:
            hit_i = sentence_hit_index_by_spans(spans_list, start)
            if hit_i is not None:
                lo = max(0, hit_i - before_sents)
                hi = min(len(spans_list), hit_i + after_sents + 1)
                ctx_start = spans_list[lo][0]
                ctx_end = spans_list[hi - 1][1]
                ctx = big_context[ctx_start:ctx_end]
                
                # 记录命中句
                result["hit_sentence"] = big_context[spans_list[hit_i][0]:spans_list[hit_i][1]][:200]
                result["hit_sentence_span"] = [int(spans_list[hit_i][0]), int(spans_list[hit_i][1])]
                
                # 超长则居中截断
                if len(ctx) > max_chars:
                    anchor_in_ctx = max(0, min(len(ctx), int(start) - int(ctx_start)))
                    ctx, _ = _center_window(ctx, anchor_in_ctx, max_chars)
                
                # 后验检查
                present, _ = evidence_in_ctx(ctx, evidence_text)
                if not present:
                    # 回退到中心窗口
                    center_mid = (int(start) + int(end)) // 2
                    ctx, _ = _center_window(big_context, center_mid, max_chars)
                    result["meta"]["sentence_window_fallback"] = True
                
                result["ctx_text"] = ctx
                result["evidence_in_ctx"], _ = evidence_in_ctx(ctx, evidence_text)
                return result
        
        # 句子切分失败，用字符窗口
        center_mid = (int(start) + int(end)) // 2
        ctx, _ = _center_window(big_context, center_mid, max_chars)
        result["ctx_text"] = ctx
        result["evidence_in_ctx"], _ = evidence_in_ctx(ctx, evidence_text)
        result["meta"]["char_window_fallback"] = True
        return result
    
    # 锚定失败，回退到 center_chunk 中间窗口
    if center_chunk:
        if len(center_chunk) <= max_chars:
            result["ctx_text"] = center_chunk
        else:
            center = len(center_chunk) // 2
            ctx, _ = _center_window(center_chunk, center, max_chars)
            result["ctx_text"] = ctx
        result["evidence_in_ctx"], _ = evidence_in_ctx(result["ctx_text"], evidence_text)
        result["meta"]["fallback"] = "center_chunk_middle"
    
    return result


# ===================== Evidence Span 迁移 =====================
def migrate_evidence_from_span(
    normalized_full_text: str,
    chunks: List[str],
    evidence_span: Optional[Dict[str, Any]],
    min_evidence_len: int = 6,
) -> Dict[str, Any]:
    """
    从 evidence_span（LLM 输出的索引）切片生成 evidence 文本。
    
    evidence_span 格式：
    - chunk 相对索引：{"chunk_index": int, "start": int, "end": int}
    - 或全局索引：{"global_start": int, "global_end": int}
    
    返回:
    {
        "evidence": str,  # 切片得到的证据文本
        "evidence_span_valid": bool,
        "reason": str,  # 失败原因（如有）
    }
    """
    result: Dict[str, Any] = {
        "evidence": "",
        "evidence_span_valid": False,
        "reason": "",
    }
    
    if not evidence_span:
        result["reason"] = "evidence_span_is_null"
        return result
    
    # 尝试全局索引
    if "global_start" in evidence_span and "global_end" in evidence_span:
        try:
            gs = int(evidence_span["global_start"])
            ge = int(evidence_span["global_end"])
            
            if gs < 0 or ge < 0:
                result["reason"] = "negative_index"
                return result
            
            if ge <= gs:
                result["reason"] = "invalid_range_end_le_start"
                return result
            
            if gs >= len(normalized_full_text):
                result["reason"] = "start_out_of_bounds"
                return result
            
            ge = min(ge, len(normalized_full_text))
            evidence = normalized_full_text[gs:ge]
            
            if len(evidence) < min_evidence_len:
                result["reason"] = f"evidence_too_short_{len(evidence)}"
                return result
            
            result["evidence"] = evidence
            result["evidence_span_valid"] = True
            return result
        except (ValueError, TypeError) as e:
            result["reason"] = f"index_conversion_error_{e}"
            return result
    
    # 尝试 chunk 相对索引
    if "chunk_index" in evidence_span and "start" in evidence_span and "end" in evidence_span:
        try:
            ci = int(evidence_span["chunk_index"])
            start = int(evidence_span["start"])
            end = int(evidence_span["end"])
            
            # chunk_index 是 1-based
            if ci < 1 or ci > len(chunks):
                result["reason"] = f"chunk_index_out_of_range_{ci}"
                return result
            
            chunk_text = chunks[ci - 1]
            
            if start < 0 or end < 0:
                result["reason"] = "negative_index"
                return result
            
            if end <= start:
                result["reason"] = "invalid_range_end_le_start"
                return result
            
            if start >= len(chunk_text):
                result["reason"] = "start_out_of_bounds"
                return result
            
            end = min(end, len(chunk_text))
            evidence = chunk_text[start:end]
            
            if len(evidence) < min_evidence_len:
                result["reason"] = f"evidence_too_short_{len(evidence)}"
                return result
            
            result["evidence"] = evidence
            result["evidence_span_valid"] = True
            return result
        except (ValueError, TypeError) as e:
            result["reason"] = f"index_conversion_error_{e}"
            return result
    
    result["reason"] = "unrecognized_span_format"
    return result


def reverse_locate_evidence_span(
    normalized_full_text: str,
    chunks: List[str],
    chunk_starts: Optional[List[int]] = None,
    chunk_index: int = 0,
    evidence_text: str = "",
    expand_range: int = DEFAULT_EXPAND_RANGE,
    chunk_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    反向定位：从 evidence 文本在原文中定位 span。
    
    用于兼容旧数据（只有 evidence 文本，没有 span）。
    
    返回:
    {
        "evidence_span": {"global_start": int, "global_end": int} or None,
        "anchor_strategy": str,
        "success": bool,
    }
    """
    result: Dict[str, Any] = {
        "evidence_span": None,
        "anchor_strategy": "NONE",
        "success": False,
    }
    
    if not evidence_text or not normalized_full_text:
        return result
    
    # 使用 anchor_evidence 定位
    anchor_result = anchor_evidence(
        normalized_full_text=normalized_full_text,
        chunks=chunks,
        chunk_starts=chunk_starts,
        chunk_index=chunk_index,
        evidence_text=evidence_text,
        expand_range=expand_range,
        chunk_entries=chunk_entries,
    )
    
    if anchor_result.get("anchor_found"):
        anchor = anchor_result.get("meta", {}).get("anchor", {})
        if anchor and "start" in anchor and "end" in anchor:
            win_start = anchor_result.get("meta", {}).get("big_context_start")
            if win_start is None:
                if chunk_entries or (chunk_starts and chunk_index >= 1 and chunk_index <= len(chunk_starts)):
                    win_start, _win_end, _center_start, _center_end = _get_context_bounds_from_source(
                        full_text=normalized_full_text,
                        chunk_index=chunk_index,
                        expand_range=expand_range,
                        chunk_entries=chunk_entries,
                        chunk_starts=chunk_starts,
                        chunk_size=DEFAULT_CHUNK_SIZE,
                    )
                else:
                    win_start = 0

            global_start = int(win_start) + int(anchor["start"])
            global_end = int(win_start) + int(anchor["end"])
            result["evidence_span"] = {
                "global_start": global_start,
                "global_end": global_end,
            }
            result["anchor_strategy"] = anchor_result.get("anchor_strategy", "UNKNOWN")
            result["success"] = True
    
    return result


# ===================== 统计日志 =====================
class EvidenceSpanStats:
    """Evidence span 迁移统计。"""
    
    def __init__(self):
        self.total = 0
        self.valid = 0
        self.invalid = 0
        self.reasons: Dict[str, int] = {}
    
    def record(self, result: Dict[str, Any]) -> None:
        self.total += 1
        if result.get("evidence_span_valid"):
            self.valid += 1
        else:
            self.invalid += 1
            reason = result.get("reason", "unknown")
            self.reasons[reason] = self.reasons.get(reason, 0) + 1
    
    def summary(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "valid_rate": self.valid / max(1, self.total),
            "top_reasons": sorted(self.reasons.items(), key=lambda x: -x[1])[:10],
        }
    
    def log_summary(self, logger) -> None:
        s = self.summary()
        logger.info(f"[Evidence Span 迁移统计] 总数={s['total']}, 有效={s['valid']}, "
                    f"无效={s['invalid']}, 有效率={s['valid_rate']:.2%}")
        if s["top_reasons"]:
            logger.info(f"  无效原因 Top: {s['top_reasons'][:5]}")
