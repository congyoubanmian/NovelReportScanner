import json
import os
import hashlib
import inspect
import re
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List

from tqdm import tqdm

from analysis_profiles import load_analysis_profile
from fact_validator import classify_scan_error, validate_general_chunk_result
from prompt_templates import prompt_template_metadata, prompt_templates_metadata
from shared_utils import MODEL, chat_completion, get_base_dir, init_token_tracker, is_context_overflow_error, read_file_safely, read_int_env, record_usage
from shared_utils import call_json_chat_completion_with_fallback
from text_anchor import build_chunk_manifest, build_semantic_chunk_manifest, save_chunk_manifest
from outline_prescan import generate_outline, outline_to_compact_text, outline_signature, save_outline, load_outline
from scan_memory import ScanMemory, SCAN_MEMORY_ENABLED as GENERAL_SCAN_MEMORY_ENABLED
from reading_metrics import extract_chunk_scores, aggregate_metrics, render_reading_experience_report, READING_METRICS_ENABLED
from literary_metrics import compute_literary_metrics as _compute_literary_metrics, LITERARY_METRICS_ENABLED


CHUNK_SIZE = read_int_env("GENERAL_SCAN_CHUNK_SIZE", 12000, min_value=1000)
CHUNK_OVERLAP = read_int_env("GENERAL_SCAN_CHUNK_OVERLAP", 1000, min_value=0)
MAX_CHUNKS = read_int_env("GENERAL_SCAN_MAX_CHUNKS", 80, min_value=0)
SMART_DENSITY = os.environ.get("GENERAL_SCAN_SMART_DENSITY", "1").strip() == "1"
CONTENT_AWARE_SAMPLING = os.environ.get("GENERAL_SCAN_CONTENT_AWARE_SAMPLING", "1").strip() == "1"
CONTENT_AWARE_SAMPLING_SCHEMA_VERSION = 1
INCREMENTAL_REUSE = os.environ.get("GENERAL_SCAN_INCREMENTAL_REUSE", "1").strip() == "1"
WRITING_QUALITY_ENABLED = os.environ.get("GENERAL_SCAN_WRITING_QUALITY", "1").strip() == "1"
ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION = 1
NARRATIVE_ARCHITECTURE_ENABLED = os.environ.get("GENERAL_SCAN_NARRATIVE_ARCHITECTURE", "1").strip() == "1"
ROLLING_CONTEXT_ENABLED = os.environ.get("GENERAL_SCAN_ROLLING_CONTEXT", "1").strip() == "1"
CONTEXT_MAX_CHARS = read_int_env("GENERAL_SCAN_CONTEXT_MAX_CHARS", 1600, min_value=0)
ROLLING_CONTEXT_SCHEMA_VERSION = 1
FORESHADOWING_ENGINEERING_ENABLED = os.environ.get("GENERAL_SCAN_FORESHADOWING_ENGINEERING", "1").strip() == "1"
FORESHADOWING_ENGINEERING_SCHEMA_VERSION = 1
SEMANTIC_LAYERS_ENABLED = os.environ.get("GENERAL_SCAN_SEMANTIC_LAYERS", "1").strip() == "1"
SEMANTIC_LAYERS_SCHEMA_VERSION = 1
READER_EXPERIENCE_ENABLED = os.environ.get("GENERAL_SCAN_READER_EXPERIENCE", "1").strip() == "1"
READER_EXPERIENCE_SCHEMA_VERSION = 1
CONTINUITY_AUDIT_ENABLED = os.environ.get("GENERAL_SCAN_CONTINUITY_AUDIT", "1").strip() == "1"
CONTINUITY_AUDIT_SCHEMA_VERSION = 1
KNOWLEDGE_BASE_SCHEMA_VERSION = 2
KNOWLEDGE_BASE_LLM_MERGE_ENABLED = os.environ.get("GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE", "0").strip() == "1"
ENTITY_PRESCAN_ENABLED = os.environ.get("GENERAL_SCAN_ENTITY_PRESCAN", "1").strip() == "1"
ENTITY_PRESCAN_SCHEMA_VERSION = 1
ENTITY_PRESCAN_MAX_CHARS = read_int_env("GENERAL_SCAN_ENTITY_PRESCAN_MAX_CHARS", 500000, min_value=0)
ENTITY_PRESCAN_MAX_ITEMS = read_int_env("GENERAL_SCAN_ENTITY_PRESCAN_MAX_ITEMS", 80, min_value=0)
ENTITY_PRESCAN_PROMPT_ITEMS = read_int_env("GENERAL_SCAN_ENTITY_PRESCAN_PROMPT_ITEMS", 40, min_value=0)

# ---- 两阶段自适应采样配置 ----
TWO_STAGE_SAMPLING_ENABLED = os.environ.get("GENERAL_SCAN_TWO_STAGE_SAMPLING", "1").strip() == "1"
TWO_STAGE_QUICK_MAX_TOKENS = read_int_env("GENERAL_SCAN_TWO_STAGE_QUICK_MAX_TOKENS", 300, min_value=100)
TWO_STAGE_HIGH_VALUE_THRESHOLD = float(os.environ.get("GENERAL_SCAN_TWO_STAGE_HIGH_VALUE_THRESHOLD", "0.5"))

# ---- 大纲注入配置 ----
OUTLINE_INJECT_ENABLED = os.environ.get("OUTLINE_INJECT_ENABLED", "1").strip() == "1"
OUTLINE_INJECT_CONTEXT_CHAPTERS = read_int_env("OUTLINE_INJECT_CONTEXT_CHAPTERS", 5, min_value=0, max_value=20)
OUTLINE_INJECT_MAX_CHARS = read_int_env("OUTLINE_INJECT_MAX_CHARS", 2000, min_value=0)
API_DOWNSHIFT_MAX_DEPTH = read_int_env("GENERAL_SCAN_API_DOWNSHIFT_MAX_DEPTH", 2, min_value=0, max_value=4)
LOW_DENSITY_TERMS = (
    "睡觉", "起床", "吃饭", "喝茶", "闲聊", "聊天", "休息", "赶路", "路上", "返回",
    "日常", "家常", "客栈", "修炼打坐", "打坐", "闭关", "练功", "整理物品",
)
HIGH_DENSITY_TERMS = (
    "战斗", "交手", "决战", "袭击", "追杀", "死亡", "牺牲", "危机", "冲突", "背叛",
    "真相", "揭露", "反转", "线索", "案件", "尸体", "凶手", "审讯", "谈判", "夺权",
    "表白", "告白", "暧昧", "亲吻", "同房", "成亲", "结婚", "突破", "晋升", "觉醒",
)
CONTENT_AWARE_SIGNAL_TERMS = HIGH_DENSITY_TERMS + (
    "伏笔", "悬念", "秘密", "身份", "暴露", "阴谋", "布局", "决裂", "复仇", "逃亡",
    "传承", "遗迹", "宝物", "法宝", "神器", "阵法", "禁制", "任务", "副本", "系统",
    "联姻", "订婚", "婚约", "吃醋", "修罗场", "双修", "亲密", "生离死别", "重逢",
    "战争", "刺杀", "政变", "登基", "造反", "审判", "破案", "证据", "谜团", "谜底",
)
RADAR_SCORE_DIMENSIONS = {
    "plot": "剧情质量",
    "characters": "人物塑造",
    "worldbuilding": "世界观",
    "pacing": "节奏把控",
    "writing": "文笔水准",
    "emotion": "情绪调动",
}
WRITING_QUALITY_DIMENSIONS = {
    "prose_quality": "文笔质量",
    "character_depth": "人物塑造",
    "narrative_technique": "叙事技巧",
    "dialogue_quality": "对话质量",
    "scene_description": "场景描写",
    "emotional_impact": "情感渲染",
    "info_density": "信息密度",
    "worldbuilding_integration": "世界观融入",
}
STAGE1_WORD_POVERTY_MARKERS = {
    "强者描写": ["如斯恐怖", "恐怖如斯", "深不可测", "不可一世", "威压"],
    "吃瓜群众": ["倒吸了一口凉气", "倒吸一口凉气", "全场哗然", "目瞪口呆", "惊掉下巴"],
    "女子描写": ["高贵冷艳", "温婉可人", "风情万种", "小家碧玉", "冰清玉洁", "倾国倾城"],
    "表情描写": ["一脸惊讶", "震惊", "冷笑", "淡淡的", "冷冷的", "不禁", "下意识"],
    "战斗描写": ["身形一闪", "倒退几步", "口中鲜血狂喷", "脸色大变", "瞳孔一缩"],
    "情绪副词": ["不禁", "不由", "下意识", "猛地", "突然", "竟然"],
}


def _effective_max_chunks(text_length: int, base_max_chunks: int = None) -> int:
    base = MAX_CHUNKS if base_max_chunks is None else int(base_max_chunks or 0)
    if base <= 0:
        return base
    text_length = max(0, int(text_length or 0))
    if text_length <= 1_000_000:
        suggested = 80
    elif text_length <= 3_000_000:
        suggested = 120
    elif text_length <= 5_000_000:
        suggested = 160
    elif text_length <= 10_000_000:
        suggested = 300
    else:
        suggested = 400
    return max(base, suggested)


def _term_hits(text: str, terms) -> List[str]:
    normalized = text or ""
    hits = []
    for term in terms:
        if term in normalized and term not in hits:
            hits.append(term)
    return hits


ENTITY_PRESCAN_STOPWORDS = {
    "自己", "什么", "不是", "没有", "这个", "那个", "我们", "他们", "你们", "只是", "已经", "还是", "然后",
    "突然", "现在", "这里", "那里", "出来", "进去", "起来", "下来", "过去", "回来", "时候", "地方", "东西",
    "众人", "所有人", "大家", "所有", "因为", "所以", "但是", "只是", "一道", "一下", "一声", "一种",
    "说道", "问道", "答道", "笑道", "喝道", "喊道", "冷笑道", "低声道", "冷笑", "低声",
}
ENTITY_NAMING_PATTERN = re.compile(r"(?:叫作|叫做|名叫|名字叫|唤作|称作|绰号|外号|表字|字|号)[“\"'‘’]?([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·]{1,7})")
ENTITY_SPEECH_VERBS = "冷笑道|低声道|说道|问道|答道|笑道|喝道|喊道|道"
ENTITY_DIALOGUE_PATTERN = re.compile(rf"[“\"'‘][^”\"'’]{{1,80}}[”\"'’][ \t]*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·]{{1,5}}?)({ENTITY_SPEECH_VERBS})")
ENTITY_SPEAKER_BEFORE_PATTERN = re.compile(rf"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·]{{1,5}}?)({ENTITY_SPEECH_VERBS})[：:]?[“\"'‘]")
ENTITY_CHAPTER_TITLE_PATTERN = re.compile(r"^\s*(?:第[\u4e00-\u9fff零〇一二三四五六七八九十百千万0-9]+[章节回卷部集]|序章|楔子|终章)\s*[：:\-、 ]{0,3}([^\r\n]{2,30})", re.MULTILINE)
ENTITY_TITLE_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·]{1,7}")
ENTITY_SUFFIX_TYPES = {
    "person": ("帝", "王", "皇", "侯", "公", "妃", "后", "太子", "公主", "王爷", "将军", "先生", "姑娘", "仙子", "真人", "道人", "长老", "师兄", "师姐", "师妹"),
    "location": ("城", "镇", "村", "山", "峰", "谷", "府", "宫", "殿", "阁", "楼", "寺", "观", "院", "岛", "海", "洲", "国", "郡", "州", "县", "界"),
    "organization": ("宗", "门", "派", "教", "帮", "会", "阁", "楼", "军", "营", "司", "府", "院", "公司", "集团", "学院", "帝国", "王朝"),
}


def _entity_candidate_type(name: str) -> str:
    text = str(name or "")
    for entity_type, suffixes in ENTITY_SUFFIX_TYPES.items():
        for suffix in suffixes:
            if text.endswith(suffix) and len(text) > len(suffix):
                return entity_type
    return "unknown"


def _entity_prescan_candidates(text: str, max_items: int = None, max_chars: int = None) -> List[Dict[str, Any]]:
    if not ENTITY_PRESCAN_ENABLED:
        return []
    limit = ENTITY_PRESCAN_MAX_ITEMS if max_items is None else max(0, int(max_items or 0))
    if limit <= 0:
        return []
    sample_limit = ENTITY_PRESCAN_MAX_CHARS if max_chars is None else max(0, int(max_chars or 0))
    sample = (text or "")[:sample_limit] if sample_limit > 0 else (text or "")

    freq = Counter()
    sources = {}

    def add(name: str, source: str, weight: int = 1):
        normalized = str(name or "").strip(" \t\r\n，。！？；：、“”‘’\"'")
        if not (2 <= len(normalized) <= 8):
            return
        if normalized in ENTITY_PRESCAN_STOPWORDS:
            return
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", normalized):
            return
        freq[normalized] += max(1, int(weight or 1))
        sources.setdefault(normalized, set()).add(source)

    for pattern, source, weight in (
        (ENTITY_NAMING_PATTERN, "naming", 8),
        (ENTITY_DIALOGUE_PATTERN, "dialogue", 5),
        (ENTITY_SPEAKER_BEFORE_PATTERN, "dialogue", 5),
    ):
        for match in pattern.finditer(sample):
            add(match.group(1), source, weight)

    for match in ENTITY_CHAPTER_TITLE_PATTERN.finditer(sample):
        title = match.group(1)
        for token in ENTITY_TITLE_TOKEN_PATTERN.findall(title):
            if _entity_candidate_type(token) != "unknown":
                add(token, "chapter_title", 4)

    cjk_segments = re.findall(r"[\u4e00-\u9fff]{2,}", sample[: min(len(sample), 300000)])
    ngram_freq = Counter()
    for segment in cjk_segments:
        for n in (2, 3, 4):
            if len(segment) < n:
                continue
            for idx in range(0, len(segment) - n + 1):
                gram = segment[idx:idx + n]
                if gram not in ENTITY_PRESCAN_STOPWORDS:
                    ngram_freq[gram] += 1
    text_len = len(sample)
    min_freq = 8 if text_len > 300000 else 5 if text_len > 80000 else 3
    for name, count in ngram_freq.items():
        entity_type = _entity_candidate_type(name)
        known_type_min_freq = max(2, min_freq - 1)
        if (
            (entity_type != "unknown" and count >= known_type_min_freq)
            or (entity_type == "unknown" and count >= min_freq * 2)
        ):
            add(name, "freq", count)

    results = []
    for name, score in freq.items():
        source_set = sources.get(name, set())
        entity_type = _entity_candidate_type(name)
        if entity_type == "unknown" and ("dialogue" in source_set or "naming" in source_set):
            entity_type = "person"
        confidence = "high" if source_set & {"naming", "dialogue"} else "medium" if score >= min_freq * 2 else "low"
        results.append({
            "name": name,
            "entity_type": entity_type,
            "score": int(score),
            "confidence": confidence,
            "sources": sorted(source_set),
        })
    results = _filter_entity_prescan_substrings(results)
    results.sort(key=lambda item: (
        0 if item.get("confidence") == "high" else 1 if item.get("confidence") == "medium" else 2,
        -int(item.get("score") or 0),
        item.get("name") or "",
    ))
    return results[:limit]


def _filter_entity_prescan_substrings(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items = [item for item in (candidates or []) if isinstance(item, dict) and item.get("name")]
    if len(items) <= 1:
        return items

    def confidence_rank(item):
        confidence = item.get("confidence")
        if confidence == "high":
            return 3
        if confidence == "medium":
            return 2
        return 1

    filtered = []
    for item in items:
        name = str(item.get("name") or "")
        item_type = item.get("entity_type") or "unknown"
        sources = set(item.get("sources") or [])
        score = int(item.get("score") or 0)
        drop = False
        for other in items:
            if other is item:
                continue
            other_name = str(other.get("name") or "")
            if item_type != (other.get("entity_type") or "unknown"):
                if (
                    item_type == "unknown"
                    and name in other_name
                    and (other.get("entity_type") or "unknown") != "unknown"
                    and int(other.get("score") or 0) >= score
                ):
                    drop = True
                    break
                continue
            other_sources = set(other.get("sources") or [])
            other_score = int(other.get("score") or 0)
            if other_name in name:
                if (
                    sources <= {"freq", "chapter_title"}
                    and not (sources & {"dialogue", "naming"})
                    and confidence_rank(item) <= confidence_rank(other)
                    and score < other_score
                ):
                    drop = True
                    break
                continue
            if len(other_name) <= len(name) or name not in other_name:
                continue
            if sources & {"dialogue", "naming"} and not (other_sources & {"dialogue", "naming"}):
                continue
            if confidence_rank(other) < confidence_rank(item):
                continue
            if other_score < score:
                continue
            drop = True
            break
        if not drop:
            filtered.append(item)
    return filtered


def _entity_prescan_prompt_section(entity_prescan: List[Dict[str, Any]], limit: int = None) -> str:
    if not entity_prescan:
        return ""
    max_prompt_items = ENTITY_PRESCAN_PROMPT_ITEMS if limit is None else max(0, int(limit or 0))
    if max_prompt_items <= 0:
        return ""
    lines = [
        "【全书预扫描实体候选】",
        "以下名称来自程序预扫描，只用于提醒不要漏掉高频实体；仍必须以当前片段原文为准，不能把候选当成已确认事实或别名。",
    ]
    type_labels = {"person": "人物", "location": "地点", "organization": "组织", "unknown": "未知"}
    for item in entity_prescan[:max_prompt_items]:
        name = item.get("name")
        if not name:
            continue
        label = type_labels.get(item.get("entity_type") or "unknown", item.get("entity_type") or "未知")
        lines.append(f"- {name}（{label}，{item.get('confidence', 'low')}，score={item.get('score', 0)}）")
    return "\n".join(lines)


def _entity_prescan_type_counts(entity_prescan: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"person": 0, "location": 0, "organization": 0, "unknown": 0}
    for item in entity_prescan or []:
        entity_type = item.get("entity_type") or "unknown"
        if entity_type not in counts:
            counts[entity_type] = 0
        counts[entity_type] += 1
    return counts


def _quick_scan_for_value(chunk_text: str, chunk_index: int, total_chunks: int, profile=None) -> Dict[str, Any]:
    """
    两阶段采样的第一阶段：用极低 max_tokens 快速判断 chunk 是否有高价值内容。

    返回: {"has_high_value": bool, "value_score": float, "tags": [...]}
    """
    if not TWO_STAGE_SAMPLING_ENABLED:
        return {"has_high_value": True, "value_score": 1.0, "tags": []}

    profile = profile or load_analysis_profile("general")
    quick_prompt = f"""快速判断这个小说片段是否有值得深入分析的内容。

片段 {chunk_index + 1}/{total_chunks}：
{chunk_text[:3000]}

只需输出 JSON：
{{"has_high_value": true/false, "value_score": 0.0-1.0, "tags": ["战斗"/"转折"/"情感"/"伏笔"/"设定"/"日常"/...]}}"""

    try:
        data = _call_json(
            [
                {"role": "system", "content": "你是小说分析助手，快速判断片段价值。"},
                {"role": "user", "content": quick_prompt},
            ],
            max_tokens=TWO_STAGE_QUICK_MAX_TOKENS,
        )
        has_high = bool(data.get("has_high_value", True))
        score = float(data.get("value_score", 0.5))
        tags = data.get("tags", [])
        return {"has_high_value": has_high, "value_score": score, "tags": tags}
    except Exception:
        # 快速扫失败时默认保留
        return {"has_high_value": True, "value_score": 0.5, "tags": []}


def _chunk_density_profile(text: str) -> Dict[str, Any]:
    sample = (text or "")[:20000]
    high_hits = _term_hits(sample, HIGH_DENSITY_TERMS)
    low_hits = _term_hits(sample, LOW_DENSITY_TERMS)
    punctuation_events = sum(sample.count(mark) for mark in ("！", "？", "。", "；"))
    length = len(sample)
    high_score = len(high_hits) * 2 + min(4, punctuation_events // 80)
    low_score = len(low_hits)
    if high_score >= 6 or len(high_hits) >= 3:
        level = "high"
    elif high_score <= 1 and low_score >= 2:
        level = "low"
    elif length < 1200 and high_score == 0 and low_score >= 1:
        level = "low"
    else:
        level = "medium"
    return {
        "level": level,
        "high_score": high_score,
        "low_score": low_score,
        "high_terms": high_hits[:12],
        "low_terms": low_hits[:12],
        "strategy": "light" if SMART_DENSITY and level == "low" else "full",
    }


def _chunk_text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _uniform_sample_chunk_entries(chunk_entries: List[Dict[str, Any]], max_chunks: int) -> List[Dict[str, Any]]:
    entries = list(chunk_entries or [])
    if max_chunks <= 0 or len(entries) <= max_chunks:
        return entries
    if max_chunks == 1:
        return [entries[0]]
    last_index = len(entries) - 1
    selected_indices = {
        round(i * last_index / (max_chunks - 1))
        for i in range(max_chunks)
    }
    selected = [entries[idx] for idx in sorted(selected_indices)]
    cursor = 0
    while len(selected) < max_chunks and cursor < len(entries):
        candidate = entries[cursor]
        if candidate not in selected:
            selected.append(candidate)
        cursor += 1
    selected.sort(key=lambda item: int(item.get("chunk_index", 0)))
    return selected[:max_chunks]


def _content_sampling_signal(text: str) -> Dict[str, Any]:
    sample = (text or "")[:20000]
    high_hits = _term_hits(sample, CONTENT_AWARE_SIGNAL_TERMS)
    low_hits = _term_hits(sample, LOW_DENSITY_TERMS)
    punctuation_events = sum(sample.count(mark) for mark in ("！", "？", "。", "；"))
    dialogue_marks = sample.count("“") + sample.count("”") + sample.count('"')
    paragraph_breaks = sample.count("\n")
    score = (
        len(high_hits) * 10
        + min(12, punctuation_events // 35)
        + min(8, dialogue_marks // 12)
        + min(4, paragraph_breaks // 20)
        - min(10, len(low_hits) * 2)
    )
    if len(high_hits) >= 4:
        level = "high"
    elif score >= 18:
        level = "high"
    elif score <= 3 and len(low_hits) >= 2:
        level = "low"
    else:
        level = "medium"
    return {
        "score": max(0, int(score)),
        "level": level,
        "signal_terms": high_hits[:12],
        "low_terms": low_hits[:8],
    }


def _content_aware_sample_chunk_entries(chunk_entries: List[Dict[str, Any]], max_chunks: int) -> List[Dict[str, Any]]:
    entries = list(chunk_entries or [])
    if max_chunks <= 0 or len(entries) <= max_chunks:
        return entries
    if max_chunks == 1:
        return [entries[0]]

    timeline_quota = max(2, min(max_chunks, int(round(max_chunks * 0.65))))
    selected_by_index = {}
    for item in _uniform_sample_chunk_entries(entries, timeline_quota):
        idx = int(item.get("chunk_index", 0) or 0)
        selected_by_index[idx] = item

    scored = []
    for position, item in enumerate(entries):
        idx = int(item.get("chunk_index", position + 1) or position + 1)
        if idx in selected_by_index:
            continue
        signal = _content_sampling_signal(item.get("text", ""))
        scored.append((signal["score"], position, idx, item))
    scored.sort(key=lambda row: (-row[0], row[1]))

    for score, _position, idx, item in scored:
        if len(selected_by_index) >= max_chunks:
            break
        if score <= 0:
            continue
        selected_by_index[idx] = item

    if len(selected_by_index) < max_chunks:
        for item in _uniform_sample_chunk_entries(entries, max_chunks):
            idx = int(item.get("chunk_index", 0) or 0)
            selected_by_index.setdefault(idx, item)
            if len(selected_by_index) >= max_chunks:
                break

    selected = list(selected_by_index.values())
    selected.sort(key=lambda item: int(item.get("chunk_index", 0)))
    return selected[:max_chunks]


def _sample_chunk_entries_for_budget(chunk_entries: List[Dict[str, Any]], max_chunks: int, content_aware: bool = None) -> List[Dict[str, Any]]:
    if content_aware is None:
        content_aware = CONTENT_AWARE_SAMPLING
    if content_aware:
        return _content_aware_sample_chunk_entries(chunk_entries, max_chunks)
    return _uniform_sample_chunk_entries(chunk_entries, max_chunks)


def _record_has_high_signal(record: Dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    signal_keys = {
        "forced_plot_devices",
        "forced_elements",
        "false_foreshadowing",
        "frustration_points",
        "quality_issues",
        "continuity_issues",
        "risks",
        "warnings",
        "weaknesses",
    }
    risk_text_keys = {
        "summary",
        "error",
        "issue",
        "issues",
        "note",
        "notes",
        "description",
        "power_inconsistency",
        "coincidence_dependency",
        "worldbuilding_consistency",
    }
    risk_terms = ("风险", "问题", "崩", "矛盾", "注水", "强行", "割裂", "违和", "失败", "低")

    def visit(value, key_hint=""):
        if key_hint in signal_keys and value:
            return True
        if isinstance(value, dict):
            return any(visit(v, str(k)) for k, v in value.items())
        if isinstance(value, list):
            return any(visit(item, key_hint) for item in value)
        if isinstance(value, str):
            if key_hint in signal_keys and value.strip():
                return True
            return key_hint in risk_text_keys and any(term in value for term in risk_terms)
        return False

    return visit(record)


def _sample_records_for_summary(records: List[Dict[str, Any]], limit: int, sort_key: str = "chunk_index") -> List[Dict[str, Any]]:
    items = [item for item in (records or []) if isinstance(item, dict)]
    if limit <= 0 or len(items) <= limit:
        return items
    if limit == 1:
        return [items[0]]

    selected = {}

    def key_for(position, item):
        if sort_key and item.get(sort_key) is not None:
            return (sort_key, item.get(sort_key), position)
        return ("pos", position)

    high_signal_quota = max(1, limit // 4)
    high_signal = [
        (position, item)
        for position, item in enumerate(items)
        if _record_has_high_signal(item)
    ][:high_signal_quota]
    for position, item in high_signal:
        selected[key_for(position, item)] = (position, item)

    remaining = max(0, limit - len(selected))
    if remaining:
        sampled_positions = _uniform_sample_chunk_entries(
            [{"chunk_index": position, "item": item} for position, item in enumerate(items)],
            remaining,
        )
        for sampled in sampled_positions:
            position = int(sampled.get("chunk_index", 0))
            item = sampled.get("item")
            if isinstance(item, dict):
                selected.setdefault(key_for(position, item), (position, item))

    if len(selected) < limit:
        for position, item in enumerate(items):
            selected.setdefault(key_for(position, item), (position, item))
            if len(selected) >= limit:
                break

    sampled = [item for _position, item in sorted(selected.values(), key=lambda row: row[0])]
    return sampled[:limit]


def _summary_field_label(field: str) -> str:
    try:
        from report import summary_field_label

        return summary_field_label(field)
    except Exception:
        return field.replace("_", " ")


def _summary_field_candidates(field: str) -> List[str]:
    try:
        from report import SUMMARY_FIELD_ALIASES

        canonical = SUMMARY_FIELD_ALIASES.get(field, field)
        candidates = [field]
        if canonical not in candidates:
            candidates.append(canonical)
        aliases = [
            alias
            for alias, target in SUMMARY_FIELD_ALIASES.items()
            if target == canonical
        ]
        for alias in aliases:
            if alias not in candidates:
                candidates.append(alias)
        return candidates
    except Exception:
        return [field]


def _summary_field_value(data: Dict[str, Any], field: str) -> List[str]:
    values = []
    seen = set()
    for candidate in _summary_field_candidates(field):
        for value in _safe_list(data.get(candidate), limit=20):
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
            if len(values) >= 20:
                return values
    return values[:20]


def _summary_field_text(data: Dict[str, Any], field: str) -> str:
    values = _summary_field_value(data, field)
    if not values:
        return ""
    return str(values[0] or "").strip()



def _read_novel(path: str) -> str:
    return read_file_safely(path)


def _latest_summary_path(results_dir: str, clean_name: str, profile_name: str = "general") -> str:
    if profile_name == "general":
        return os.path.join(results_dir, f"{clean_name}_GENERAL_SUMMARY_latest.json")
    return os.path.join(results_dir, f"{clean_name}_{profile_name}_GENERAL_SUMMARY_latest.json")


def _read_json(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _novel_mtime(path: str):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _novel_file_signature(path: str, sample_size: int = 65536):
    try:
        stat = os.stat(path)
        size = int(stat.st_size)
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with open(path, "rb") as f:
            head = f.read(sample_size)
            digest.update(head)
            if size > sample_size:
                f.seek(max(0, size - sample_size))
                digest.update(f.read(sample_size))
        return {
            "size": size,
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            "sample_sha256": digest.hexdigest(),
        }
    except OSError:
        return None


def _is_fresh_summary(data: Dict[str, Any], novel_file: str, profile_name: str = "general") -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("partial_scan") is True:
        return False
    failed_chunk_count = data.get("failed_chunk_count")
    try:
        if int(failed_chunk_count or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False
    scan_coverage_ratio = data.get("scan_coverage_ratio")
    if scan_coverage_ratio is not None:
        try:
            if float(scan_coverage_ratio) < 1.0:
                return False
        except (TypeError, ValueError):
            return False
    if data.get("schema_version") != 1:
        return False
    if data.get("analysis_profile") not in {"general", profile_name}:
        return False
    if data.get("specialty_profile", data.get("analysis_profile", "general")) != profile_name:
        return False
    if os.path.abspath(data.get("novel_path", "")) != os.path.abspath(novel_file):
        return False
    if data.get("chunk_size") != CHUNK_SIZE or data.get("chunk_overlap") != CHUNK_OVERLAP:
        return False
    text_length = int(data.get("text_length") or 0)
    effective_max_chunks = _effective_max_chunks(text_length)
    if data.get("max_chunks") != effective_max_chunks:
        return False
    if data.get("chunk_sampling_strategy") not in {"full", "uniform_timeline", "content_aware_timeline"}:
        return False
    if data.get("smart_density") not in {None, SMART_DENSITY}:
        return False
    if data.get("content_aware_sampling") not in {None, CONTENT_AWARE_SAMPLING}:
        return False
    if CONTENT_AWARE_SAMPLING:
        if data.get("content_aware_sampling_schema_version") != CONTENT_AWARE_SAMPLING_SCHEMA_VERSION:
            return False
    if data.get("incremental_reuse") not in {None, INCREMENTAL_REUSE}:
        return False
    if WRITING_QUALITY_ENABLED and data.get("writing_quality_enabled") is not True:
        return False
    if not WRITING_QUALITY_ENABLED and data.get("writing_quality_enabled") not in {None, False}:
        return False
    if WRITING_QUALITY_ENABLED and data.get("zhihu_writing_insights_schema_version") != ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION:
        return False
    if NARRATIVE_ARCHITECTURE_ENABLED and data.get("narrative_architecture_enabled") is not True:
        return False
    if not NARRATIVE_ARCHITECTURE_ENABLED and data.get("narrative_architecture_enabled") not in {None, False}:
        return False
    if ROLLING_CONTEXT_ENABLED:
        if data.get("rolling_context_enabled") is not True:
            return False
        if data.get("rolling_context_schema_version") != ROLLING_CONTEXT_SCHEMA_VERSION:
            return False
        if data.get("rolling_context_max_chars") != CONTEXT_MAX_CHARS:
            return False
    elif data.get("rolling_context_enabled") not in {None, False}:
        return False
    if FORESHADOWING_ENGINEERING_ENABLED:
        if data.get("foreshadowing_engineering_enabled") is not True:
            return False
        if data.get("foreshadowing_engineering_schema_version") != FORESHADOWING_ENGINEERING_SCHEMA_VERSION:
            return False
    elif data.get("foreshadowing_engineering_enabled") not in {None, False}:
        return False
    if SEMANTIC_LAYERS_ENABLED:
        if data.get("semantic_layers_enabled") is not True:
            return False
        if data.get("semantic_layers_schema_version") != SEMANTIC_LAYERS_SCHEMA_VERSION:
            return False
    elif data.get("semantic_layers_enabled") not in {None, False}:
        return False
    if READER_EXPERIENCE_ENABLED:
        if data.get("reader_experience_enabled") is not True:
            return False
        if data.get("reader_experience_schema_version") != READER_EXPERIENCE_SCHEMA_VERSION:
            return False
    elif data.get("reader_experience_enabled") not in {None, False}:
        return False
    if CONTINUITY_AUDIT_ENABLED:
        if data.get("continuity_audit_enabled") is not True:
            return False
        if data.get("continuity_audit_schema_version") != CONTINUITY_AUDIT_SCHEMA_VERSION:
            return False
    elif data.get("continuity_audit_enabled") not in {None, False}:
        return False
    if data.get("knowledge_base_enabled") is not True:
        return False
    if data.get("knowledge_base_schema_version") != KNOWLEDGE_BASE_SCHEMA_VERSION:
        return False
    if data.get("knowledge_base_llm_merge_enabled") != KNOWLEDGE_BASE_LLM_MERGE_ENABLED:
        return False
    if data.get("entity_prescan_enabled") not in {None, ENTITY_PRESCAN_ENABLED}:
        return False
    if ENTITY_PRESCAN_ENABLED:
        if data.get("entity_prescan_enabled") is not True:
            return False
        if data.get("entity_prescan_schema_version") != ENTITY_PRESCAN_SCHEMA_VERSION:
            return False
    elif data.get("entity_prescan_enabled") not in {None, False}:
        return False
    stored_prompt_templates = data.get("prompt_templates")
    if isinstance(stored_prompt_templates, dict):
        current_prompt_templates = prompt_templates_metadata("general_scan_chunk", "general_summary")
        for name, current_meta in current_prompt_templates.items():
            stored_meta = stored_prompt_templates.get(name)
            if isinstance(stored_meta, dict) and stored_meta.get("version") != current_meta.get("version"):
                return False
    current_mtime = _novel_mtime(novel_file)
    if current_mtime is None or data.get("novel_mtime") != current_mtime:
        return False
    current_signature = _novel_file_signature(novel_file)
    stored_signature = data.get("novel_signature")
    if not isinstance(stored_signature, dict) or current_signature != stored_signature:
        return False
    return bool(_summary_field_text(data.get("summary") or {}, "story_overview") or data.get("chunk_results"))


def _summary_can_reuse_chunk_results(data: Dict[str, Any], profile_name: str = "general") -> bool:
    if ROLLING_CONTEXT_ENABLED:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("schema_version") != 1:
        return False
    if data.get("analysis_profile") not in {"general", profile_name}:
        return False
    if data.get("specialty_profile", data.get("analysis_profile", "general")) != profile_name:
        return False
    if data.get("chunk_size") != CHUNK_SIZE or data.get("chunk_overlap") != CHUNK_OVERLAP:
        return False
    if data.get("smart_density") not in {None, SMART_DENSITY}:
        return False
    if data.get("content_aware_sampling") not in {None, CONTENT_AWARE_SAMPLING}:
        return False
    if CONTENT_AWARE_SAMPLING and data.get("content_aware_sampling_schema_version") not in {None, CONTENT_AWARE_SAMPLING_SCHEMA_VERSION}:
        return False
    if data.get("writing_quality_enabled") not in {None, WRITING_QUALITY_ENABLED}:
        return False
    if WRITING_QUALITY_ENABLED and data.get("zhihu_writing_insights_schema_version") not in {None, ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION}:
        return False
    if data.get("narrative_architecture_enabled") not in {None, NARRATIVE_ARCHITECTURE_ENABLED}:
        return False
    if data.get("foreshadowing_engineering_enabled") not in {None, FORESHADOWING_ENGINEERING_ENABLED}:
        return False
    if data.get("semantic_layers_enabled") not in {None, SEMANTIC_LAYERS_ENABLED}:
        return False
    if data.get("reader_experience_enabled") not in {None, READER_EXPERIENCE_ENABLED}:
        return False
    if data.get("continuity_audit_enabled") not in {None, CONTINUITY_AUDIT_ENABLED}:
        return False
    if CONTINUITY_AUDIT_ENABLED and data.get("continuity_audit_schema_version") not in {None, CONTINUITY_AUDIT_SCHEMA_VERSION}:
        return False
    if data.get("entity_prescan_enabled") not in {None, ENTITY_PRESCAN_ENABLED}:
        return False
    if ENTITY_PRESCAN_ENABLED:
        if data.get("entity_prescan_enabled") is not True:
            return False
        if data.get("entity_prescan_schema_version") != ENTITY_PRESCAN_SCHEMA_VERSION:
            return False
    elif data.get("entity_prescan_enabled") not in {None, False}:
        return False
    stored_prompt_templates = data.get("prompt_templates")
    if isinstance(stored_prompt_templates, dict):
        current_prompt_templates = prompt_templates_metadata("general_scan_chunk", "general_summary")
        for name, current_meta in current_prompt_templates.items():
            stored_meta = stored_prompt_templates.get(name)
            if isinstance(stored_meta, dict) and stored_meta.get("version") != current_meta.get("version"):
                return False
    return bool(data.get("chunk_results"))


def _reusable_chunk_result_map(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    reusable = {}
    if ROLLING_CONTEXT_ENABLED:
        return reusable
    if not isinstance(data, dict):
        return reusable
    for item in data.get("chunk_results") or []:
        if not isinstance(item, dict):
            continue
        if WRITING_QUALITY_ENABLED:
            writing_quality = item.get("writing_quality")
            if not (writing_quality and item.get("pacing_analysis") and item.get("information_density")):
                continue
            if not (_safe_dict(writing_quality).get("zhihu_insights") or {}).get("word_poverty"):
                continue
        if NARRATIVE_ARCHITECTURE_ENABLED and not (
            item.get("narrative_structure") and item.get("outline_architecture")
        ):
            continue
        if FORESHADOWING_ENGINEERING_ENABLED and not item.get("foreshadowing_engineering"):
            continue
        if SEMANTIC_LAYERS_ENABLED and not item.get("semantic_layers"):
            continue
        if READER_EXPERIENCE_ENABLED and not item.get("reader_experience"):
            continue
        chunk_hash = item.get("chunk_hash")
        if isinstance(chunk_hash, str) and chunk_hash:
            reusable.setdefault(chunk_hash, item)
    return reusable


def _summary_can_reuse_overall(data: Dict[str, Any], profile_name: str = "general") -> bool:
    if not _summary_can_reuse_chunk_results(data, profile_name):
        return False
    summary = data.get("summary")
    if not isinstance(summary, dict) or not _summary_field_text(summary, "story_overview"):
        return False
    if WRITING_QUALITY_ENABLED and not summary.get("zhihu_writing_insights_overall"):
        return False
    return True


def _copy_reused_chunk_result(result: Dict[str, Any], sample_index: int, original_chunk_index: int, chunk_hash: str, density_profile: Dict[str, Any]) -> Dict[str, Any]:
    copied = json.loads(json.dumps(result, ensure_ascii=False))
    copied["sample_index"] = sample_index
    copied["original_chunk_index"] = original_chunk_index
    copied["chunk_index"] = original_chunk_index - 1
    copied["chunk_hash"] = chunk_hash
    copied["density_profile"] = density_profile
    copied["reused_from_previous"] = True
    return validate_general_chunk_result(copied)


def _safe_list(value: Any, limit: int = 20) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    out = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_radar_scores(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for key, label in RADAR_SCORE_DIMENSIONS.items():
        raw = value.get(key)
        reason = ""
        if isinstance(raw, dict):
            score_value = raw.get("score")
            reason = str(raw.get("reason") or raw.get("comment") or "").strip()
        else:
            score_value = raw
        try:
            score = float(score_value)
        except (TypeError, ValueError):
            continue
        score = max(0.0, min(10.0, score))
        normalized[key] = {
            "label": label,
            "score": round(score, 1),
            "reason": reason[:120],
        }
    return normalized


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    return round(max(0.0, min(10.0, score)), 1)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _detect_stage1_word_poverty(text: str) -> Dict[str, Any]:
    text = str(text or "")
    text_length = max(1, len(text))
    category_hits = {}
    phrase_counts = {}
    for category, phrases in STAGE1_WORD_POVERTY_MARKERS.items():
        hits = []
        for phrase in phrases:
            count = text.count(phrase)
            if count <= 0:
                continue
            phrase_counts[phrase] = max(phrase_counts.get(phrase, 0), count)
            hits.append({"phrase": phrase, "count": count})
        if hits:
            category_hits[category] = sorted(hits, key=lambda x: x["count"], reverse=True)[:8]
    total = sum(phrase_counts.values())
    density = round(total * 1000 / text_length, 3)
    if density > 5:
        severity = "严重词穷"
    elif density >= 2:
        severity = "轻度词穷"
    elif total:
        severity = "偶见模板词"
    else:
        severity = "未见明显模板词"
    most_frequent = [
        f"{phrase}({count}次)"
        for phrase, count in sorted(phrase_counts.items(), key=lambda x: x[1], reverse=True)[:8]
    ]
    return {
        "template_phrase_count": total,
        "template_phrase_density_per_1k": density,
        "most_frequent_templates": most_frequent,
        "category_hits": category_hits,
        "severity": severity,
    }


def _normalize_reader_inference_space(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "score": _clamp_score(raw.get("score")),
        "l1_tell_count": _safe_int(raw.get("l1_tell_count") or raw.get("tell_count")),
        "l2_show_count": _safe_int(raw.get("l2_show_count") or raw.get("show_count")),
        "l3_subtext_count": _safe_int(raw.get("l3_subtext_count") or raw.get("subtext_count")),
        "implicit_delivery_rate": str(raw.get("implicit_delivery_rate") or "").strip()[:30],
        "tell_examples": _safe_list(raw.get("tell_examples"), limit=3),
        "show_examples": _safe_list(raw.get("show_examples"), limit=3),
        "assessment": str(raw.get("assessment") or "").strip()[:180],
    }


def _normalize_communication_efficiency(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "level": str(raw.get("level") or "").strip()[:20],
        "level_name": str(raw.get("level_name") or "").strip()[:30],
        "information_loss_rate": str(raw.get("information_loss_rate") or "").strip()[:30],
        "reader_comprehension_barrier": str(raw.get("reader_comprehension_barrier") or "").strip()[:30],
        "redundant_expression_rate": str(raw.get("redundant_expression_rate") or "").strip()[:30],
        "precision_score": _clamp_score(raw.get("precision_score")),
        "conciseness_score": _clamp_score(raw.get("conciseness_score")),
        "assessment": str(raw.get("assessment") or "").strip()[:180],
    }


def _normalize_style_identity(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "detected_traits": _safe_list(raw.get("detected_traits"), limit=6),
        "originality_score": _clamp_score(raw.get("originality_score")),
        "consistency_score": _clamp_score(raw.get("consistency_score")),
        "assessment": str(raw.get("assessment") or "").strip()[:180],
    }


def _normalize_emotional_authenticity(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "score": _clamp_score(raw.get("score")),
        "genuine_emotion_vs_forced": str(raw.get("genuine_emotion_vs_forced") or "").strip()[:180],
        "universal_resonance": str(raw.get("universal_resonance") or "").strip()[:180],
        "personal_vs_generic": str(raw.get("personal_vs_generic") or "").strip()[:180],
        "transcendence_potential": str(raw.get("transcendence_potential") or "").strip()[:80],
        "assessment": str(raw.get("assessment") or "").strip()[:180],
    }


def _normalize_zhihu_writing_insights(value: Any, deterministic_word_poverty: Dict[str, Any] = None) -> Dict[str, Any]:
    raw = _safe_dict(value)
    word_poverty = _safe_dict(raw.get("word_poverty") or raw.get("stage1_word_poverty"))
    merged_word_poverty = dict(deterministic_word_poverty or {})
    if word_poverty:
        for key in ("score", "severity", "assessment"):
            if word_poverty.get(key) not in {None, ""}:
                merged_word_poverty[key] = word_poverty.get(key)
        if word_poverty.get("examples"):
            merged_word_poverty["examples"] = _safe_list(word_poverty.get("examples"), limit=5)
    if "score" in merged_word_poverty:
        merged_word_poverty["score"] = _clamp_score(merged_word_poverty.get("score"))
    return {
        "word_poverty": merged_word_poverty,
        "reader_inference_space": _normalize_reader_inference_space(raw.get("reader_inference_space")),
        "communication_efficiency": _normalize_communication_efficiency(raw.get("communication_efficiency")),
        "style_identity": _normalize_style_identity(raw.get("style_identity")),
        "emotional_authenticity": _normalize_emotional_authenticity(raw.get("emotional_authenticity")),
    }


def _normalize_writing_quality(value: Any, source_text: str = "") -> Dict[str, Any]:
    raw = _safe_dict(value)
    normalized = {}
    for key, label in WRITING_QUALITY_DIMENSIONS.items():
        item = _safe_dict(raw.get(key))
        normalized[key] = {
            "label": label,
            "score": _clamp_score(item.get("score")),
            "strength": str(item.get("strength") or item.get("advantage") or "").strip()[:160],
            "weakness": str(item.get("weakness") or item.get("issue") or "").strip()[:160],
        }
        if key == "info_density":
            normalized[key]["water_chapter_score"] = _clamp_score(item.get("water_chapter_score"))
    evidence = []
    for item in raw.get("evidence") or raw.get("notable_passages") or []:
        if not isinstance(item, dict):
            continue
        evidence.append({
            "type": str(item.get("type") or "").strip()[:20],
            "dimension": str(item.get("dimension") or "").strip()[:30],
            "quote": str(item.get("quote") or "").strip()[:60],
            "note": str(item.get("note") or "").strip()[:80],
        })
        if len(evidence) >= 3:
            break
    normalized["chunk_assessment"] = str(raw.get("chunk_assessment") or "").strip()[:120]
    normalized["evidence"] = [x for x in evidence if x.get("quote") or x.get("note")]
    deterministic_word_poverty = _detect_stage1_word_poverty(source_text) if source_text else {}
    normalized["zhihu_insights"] = _normalize_zhihu_writing_insights(
        raw.get("zhihu_insights"),
        deterministic_word_poverty=deterministic_word_poverty,
    )
    return normalized


def _normalize_pacing_analysis(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "pacing_type": str(raw.get("pacing_type") or raw.get("pacing_type_tag") or "").strip()[:40],
        "tension_level": _clamp_score(raw.get("tension_level")),
        "emotion_tone": str(raw.get("emotion_tone") or raw.get("emotion_tone_tag") or "").strip()[:20],
        "emotion_intensity": _clamp_score(raw.get("emotion_intensity")),
        "payoff_moment": str(raw.get("payoff_moment") or "").strip()[:160],
        "suffering_moment": str(raw.get("suffering_moment") or "").strip()[:160],
        "cliffhanger_quality": str(
            (_safe_dict(raw.get("cliffhanger")).get("cliffhanger_quality") if isinstance(raw.get("cliffhanger"), dict) else raw.get("cliffhanger_quality"))
            or ""
        ).strip()[:20],
        "reader_engagement_prediction": str(raw.get("reader_engagement_prediction") or "").strip()[:20],
    }


def _normalize_information_density(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "density_score": str(raw.get("density_score") or raw.get("information_density") or "").strip()[:20],
        "skipability": str(raw.get("skipability") or raw.get("skipability_score") or "").strip()[:30],
        "key_information": _safe_list(raw.get("key_information") or raw.get("key_information_conveyed"), limit=5),
        "redundancy_flags": _safe_list(raw.get("redundancy_flags") or raw.get("water_chapter_indicators"), limit=5),
        "narrative_efficiency": str(raw.get("narrative_efficiency") or "").strip()[:160],
    }


def _normalize_foreshadowing_item(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    if not raw and isinstance(value, str):
        raw = {"description": value}
    description = str(raw.get("description") or raw.get("desc") or raw.get("item") or "").strip()[:180]
    if not description:
        return {}
    return {
        "type": str(raw.get("type") or raw.get("kind") or "").strip()[:30],
        "description": description,
        "estimated_importance": str(raw.get("estimated_importance") or raw.get("importance") or "").strip()[:20],
        "evidence": str(raw.get("evidence") or raw.get("quote") or "").strip()[:120],
    }


def _normalize_resolution_item(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    if not raw and isinstance(value, str):
        raw = {"resolved_item": value}
    resolved_item = str(raw.get("resolved_item") or raw.get("item") or raw.get("description") or "").strip()[:160]
    resolution_description = str(raw.get("resolution_description") or raw.get("resolution") or raw.get("payoff") or "").strip()[:180]
    if not resolved_item and not resolution_description:
        return {}
    return {
        "resolved_item": resolved_item,
        "resolution_description": resolution_description,
        "satisfaction": str(raw.get("satisfaction") or raw.get("payoff_quality") or "").strip()[:30],
        "evidence": str(raw.get("evidence") or raw.get("quote") or "").strip()[:120],
    }


def _normalize_foreshadowing_engineering(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    new_items = []
    for item in raw.get("new_foreshadowing") or raw.get("new_threads") or []:
        normalized = _normalize_foreshadowing_item(item)
        if normalized:
            new_items.append(normalized)
        if len(new_items) >= 6:
            break
    resolutions = []
    for item in raw.get("foreshadowing_resolutions") or raw.get("resolutions") or raw.get("resolved_foreshadowing") or []:
        normalized = _normalize_resolution_item(item)
        if normalized:
            resolutions.append(normalized)
        if len(resolutions) >= 6:
            break
    false_items = []
    for item in raw.get("false_foreshadowing") or raw.get("red_herrings") or []:
        if isinstance(item, dict):
            text = str(item.get("description") or item.get("item") or "").strip()
        else:
            text = str(item or "").strip()
        if text and text not in false_items:
            false_items.append(text[:160])
        if len(false_items) >= 5:
            break
    return {
        "new_foreshadowing": new_items,
        "foreshadowing_resolutions": resolutions,
        "false_foreshadowing": false_items,
        "engineering_notes": _safe_list(raw.get("engineering_notes") or raw.get("notes"), limit=4),
        "recycling_rate": str(raw.get("recycling_rate") or "").strip()[:40],
    }


def _normalize_semantic_layers(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    if not raw and isinstance(value, str):
        raw = {"deep_semantic": value}
    confidence = str(raw.get("confidence") or raw.get("confidence_level") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = ""
    return {
        "literal_meaning": str(raw.get("literal_meaning") or raw.get("facts") or "").strip()[:180],
        "author_intent": str(raw.get("author_intent") or raw.get("intent") or raw.get("why") or "").strip()[:180],
        "surface_emotion": str(raw.get("surface_emotion") or raw.get("emotion") or "").strip()[:120],
        "reader_effect": str(raw.get("reader_effect") or raw.get("effect") or "").strip()[:180],
        "deep_semantic": str(raw.get("deep_semantic") or raw.get("subtext") or "").strip()[:220],
        "technique": str(raw.get("technique") or raw.get("craft") or raw.get("how") or "").strip()[:180],
        "subtext_or_irony": _safe_list(
            raw.get("subtext_or_irony") or raw.get("irony") or raw.get("subtexts"),
            limit=5,
        ),
        "confidence": confidence,
    }


def _normalize_reader_experience_point(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    if not raw and isinstance(value, str):
        raw = {"description": value}
    description = str(raw.get("description") or raw.get("desc") or raw.get("point") or "").strip()[:180]
    if not description:
        return {}
    return {
        "type": str(raw.get("type") or raw.get("kind") or "").strip()[:30],
        "description": description,
        "intensity": _clamp_score(raw.get("intensity"), default=0.0),
        "evidence": str(raw.get("evidence") or raw.get("quote") or "").strip()[:100],
    }


def _normalize_reader_experience(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    emotion = _safe_dict(raw.get("immediate_emotion"))
    anticipation = _safe_dict(raw.get("anticipation"))
    satisfaction_points = []
    for item in raw.get("satisfaction_points") or raw.get("payoff_points") or []:
        normalized = _normalize_reader_experience_point(item)
        if normalized:
            satisfaction_points.append(normalized)
        if len(satisfaction_points) >= 5:
            break
    frustration_points = []
    for item in raw.get("frustration_points") or raw.get("risk_points") or raw.get("poison_points") or []:
        normalized = _normalize_reader_experience_point(item)
        if normalized:
            frustration_points.append(normalized)
        if len(frustration_points) >= 5:
            break
    engagement = str(raw.get("engagement_level") or raw.get("reader_engagement") or "").strip().lower()
    if engagement not in {"high", "medium", "low"}:
        engagement = ""
    return {
        "immediate_emotion": {
            "emotion": str(emotion.get("emotion") or emotion.get("type") or "").strip()[:30],
            "intensity": _clamp_score(emotion.get("intensity"), default=0.0),
            "trigger": str(emotion.get("trigger") or emotion.get("trigger_quote") or "").strip()[:100],
        },
        "immersion_anchor": str(raw.get("immersion_anchor") or raw.get("substitution_anchor") or "").strip()[:160],
        "anticipation": {
            "expected": str(anticipation.get("expected") or anticipation.get("expectation") or "").strip()[:160],
            "intensity": _clamp_score(anticipation.get("intensity"), default=0.0),
            "hook_type": str(anticipation.get("hook_type") or anticipation.get("type") or "").strip()[:40],
        },
        "satisfaction_points": satisfaction_points,
        "frustration_points": frustration_points,
        "engagement_level": engagement,
        "experience_notes": _safe_list(raw.get("experience_notes") or raw.get("notes"), limit=5),
    }


def _normalize_context_state_update(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "progress_summary": str(raw.get("progress_summary") or "").strip()[:220],
        "active_characters": _safe_list(raw.get("active_characters"), limit=8),
        "relationship_updates": _safe_list(raw.get("relationship_updates"), limit=6),
        "open_threads": _safe_list(raw.get("open_threads"), limit=8),
        "resolved_threads": _safe_list(raw.get("resolved_threads"), limit=6),
        "worldbuilding_updates": _safe_list(raw.get("worldbuilding_updates"), limit=6),
        "current_stage": str(raw.get("current_stage") or "").strip()[:120],
    }


def _dedupe_extend(existing: List[str], new_items: List[str], limit: int = 30) -> List[str]:
    out = []
    seen = set()
    for item in list(existing or []) + list(new_items or []):
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out[-limit:]


def _append_unique_record(records: List[Dict[str, Any]], record: Dict[str, Any], key_fields, limit: int = 80):
    if not any(record.get(key) for key in key_fields):
        return
    key = tuple(str(record.get(field) or "").strip() for field in key_fields)
    for existing in records:
        existing_key = tuple(str(existing.get(field) or "").strip() for field in key_fields)
        if existing_key == key:
            if record.get("chunk_index") and not existing.get("chunk_index"):
                existing["chunk_index"] = record.get("chunk_index")
            if record.get("evidence") and not existing.get("evidence"):
                existing["evidence"] = record.get("evidence")
            return
    records.append(record)
    if len(records) > limit:
        del records[0:len(records) - limit]


def _knowledge_entity_record(name: str, chunk_index: Any, summary: str = "") -> Dict[str, Any]:
    text = str(name or "").strip()
    if not text:
        return {}
    role = ""
    display_name = text
    if "(" in text and ")" in text and text.index("(") < text.rindex(")"):
        display_name = text[:text.index("(")].strip() or text
        role = text[text.index("(") + 1:text.rindex(")")].strip()[:80]
    return {
        "name": display_name[:80],
        "role_or_note": role,
        "first_seen_chunk": chunk_index,
        "evidence": str(summary or "").strip()[:160],
    }


def _build_knowledge_base(chunk_results: List[Dict[str, Any]], limit: int = 120) -> Dict[str, Any]:
    base = {
        "schema_version": KNOWLEDGE_BASE_SCHEMA_VERSION,
        "facts": [],
        "entities": [],
        "relationships": [],
        "worldbuilding_facts": [],
        "foreshadowing_threads": [],
        "plot_timeline": [],
        "risk_facts": [],
        "open_threads": [],
        "resolved_threads": [],
    }
    seen_entities = set()
    for item in chunk_results or []:
        if not isinstance(item, dict):
            continue
        chunk_index = item.get("original_chunk_index", item.get("chunk_index"))
        summary = str(item.get("one_sentence_summary") or "").strip()
        update = _normalize_context_state_update(item.get("context_state_update"))
        foreshadowing = _normalize_foreshadowing_engineering(item.get("foreshadowing_engineering"))
        chunk_facts = item.get("chunk_facts") if isinstance(item.get("chunk_facts"), dict) else {}

        for fact_key, entries in (chunk_facts or {}).items():
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                description = (
                    entry.get("description")
                    or entry.get("event")
                    or entry.get("fact")
                    or entry.get("name")
                    or ""
                )
                if not description:
                    continue
                _append_unique_record(
                    base["facts"],
                    {
                        "chunk_index": entry.get("chunk_index", chunk_index),
                        "fact_type": fact_key,
                        "description": str(description or "").strip()[:220],
                        "evidence": str(entry.get("evidence") or entry.get("summary") or summary).strip()[:180],
                        "confidence": entry.get("confidence") or "medium",
                    },
                    ("fact_type", "description"),
                    limit=limit * 2,
                )

        for risk in chunk_facts.get("risk_facts") or []:
            if not isinstance(risk, dict):
                continue
            _append_unique_record(
                base["risk_facts"],
                {
                    "chunk_index": risk.get("chunk_index", chunk_index),
                    "type": str(risk.get("type") or "").strip()[:60],
                    "description": str(risk.get("description") or "").strip()[:220],
                    "evidence": str(risk.get("evidence") or summary).strip()[:180],
                    "confidence": risk.get("confidence") or "medium",
                },
                ("type", "description"),
                limit=limit,
            )

        for name in update.get("active_characters") or []:
            entity = _knowledge_entity_record(name, chunk_index, summary)
            entity_name = entity.get("name")
            if entity_name and entity_name not in seen_entities:
                seen_entities.add(entity_name)
                base["entities"].append(entity)
                if len(base["entities"]) >= limit:
                    break

        for relation in update.get("relationship_updates") or []:
            _append_unique_record(
                base["relationships"],
                {
                    "chunk_index": chunk_index,
                    "description": str(relation or "").strip()[:180],
                    "evidence": summary[:160],
                },
                ("description",),
                limit=limit,
            )

        for fact in _safe_list(item.get("worldbuilding"), limit=20) + _safe_list(update.get("worldbuilding_updates"), limit=20):
            _append_unique_record(
                base["worldbuilding_facts"],
                {
                    "chunk_index": chunk_index,
                    "fact": str(fact or "").strip()[:180],
                    "evidence": summary[:160],
                },
                ("fact",),
                limit=limit,
            )

        for thread in update.get("open_threads") or []:
            _append_unique_record(
                base["open_threads"],
                {"chunk_index": chunk_index, "thread": str(thread or "").strip()[:180], "evidence": summary[:160]},
                ("thread",),
                limit=limit,
            )
        for thread in update.get("resolved_threads") or []:
            _append_unique_record(
                base["resolved_threads"],
                {"chunk_index": chunk_index, "thread": str(thread or "").strip()[:180], "evidence": summary[:160]},
                ("thread",),
                limit=limit,
            )

        for thread in foreshadowing.get("new_foreshadowing") or []:
            _append_unique_record(
                base["foreshadowing_threads"],
                {
                    "chunk_index": chunk_index,
                    "type": thread.get("type") or "",
                    "description": thread.get("description") or "",
                    "status": "active",
                    "importance": thread.get("estimated_importance") or "",
                    "evidence": thread.get("evidence") or summary[:160],
                },
                ("description", "status"),
                limit=limit,
            )
        for thread in foreshadowing.get("foreshadowing_resolutions") or []:
            _append_unique_record(
                base["foreshadowing_threads"],
                {
                    "chunk_index": chunk_index,
                    "type": "",
                    "description": thread.get("resolved_item") or thread.get("resolution_description") or "",
                    "status": "resolved",
                    "importance": thread.get("satisfaction") or "",
                    "evidence": thread.get("evidence") or summary[:160],
                },
                ("description", "status"),
                limit=limit,
            )

        for event in item.get("plot_events") or []:
            _append_unique_record(
                base["plot_timeline"],
                {
                    "chunk_index": chunk_index,
                    "event": str(event or "").strip()[:180],
                    "summary": summary[:160],
                },
                ("event",),
                limit=limit,
            )

    resolved_texts = {x.get("thread") for x in base["resolved_threads"] if x.get("thread")}
    if resolved_texts:
        base["open_threads"] = [
            item for item in base["open_threads"]
            if item.get("thread") not in resolved_texts
        ]
    return base


def _knowledge_base_counts(knowledge_base: Dict[str, Any]) -> Dict[str, int]:
    return {
        "facts": len((knowledge_base or {}).get("facts") or []),
        "entities": len((knowledge_base or {}).get("entities") or []),
        "relationships": len((knowledge_base or {}).get("relationships") or []),
        "worldbuilding_facts": len((knowledge_base or {}).get("worldbuilding_facts") or []),
        "foreshadowing_threads": len((knowledge_base or {}).get("foreshadowing_threads") or []),
        "plot_timeline": len((knowledge_base or {}).get("plot_timeline") or []),
        "risk_facts": len((knowledge_base or {}).get("risk_facts") or []),
        "open_threads": len((knowledge_base or {}).get("open_threads") or []),
        "resolved_threads": len((knowledge_base or {}).get("resolved_threads") or []),
    }


def _normalize_llm_knowledge_base(value: Any, fallback: Dict[str, Any], limit: int = 120) -> Dict[str, Any]:
    raw = _safe_dict(value)
    if not raw:
        return fallback
    base = {
        "schema_version": KNOWLEDGE_BASE_SCHEMA_VERSION,
        "facts": [],
        "entities": [],
        "relationships": [],
        "worldbuilding_facts": [],
        "foreshadowing_threads": [],
        "plot_timeline": [],
        "risk_facts": [],
        "open_threads": [],
        "resolved_threads": [],
    }
    for item in raw.get("facts") or []:
        record = _safe_dict(item)
        text = record.get("description") if record else item
        _append_unique_record(
            base["facts"],
            {
                "chunk_index": record.get("chunk_index") if record else None,
                "fact_type": str(record.get("fact_type") or record.get("type") or "").strip()[:60] if record else "",
                "description": str(text or "").strip()[:220],
                "evidence": str(record.get("evidence") or "").strip()[:180] if record else "",
                "confidence": str(record.get("confidence") or "medium").strip()[:30] if record else "medium",
            },
            ("fact_type", "description"),
            limit=limit * 2,
        )
    for item in raw.get("entities") or []:
        record = _safe_dict(item)
        if not record and isinstance(item, str):
            record = {"name": item}
        _append_unique_record(
            base["entities"],
            {
                "name": str(record.get("name") or "").strip()[:80],
                "role_or_note": str(record.get("role_or_note") or record.get("role") or record.get("note") or "").strip()[:120],
                "first_seen_chunk": record.get("first_seen_chunk") or record.get("chunk_index"),
                "evidence": str(record.get("evidence") or "").strip()[:180],
            },
            ("name",),
            limit=limit,
        )
    for item in raw.get("relationships") or []:
        record = _safe_dict(item)
        text = record.get("description") if record else item
        _append_unique_record(
            base["relationships"],
            {"chunk_index": record.get("chunk_index") if record else None, "description": str(text or "").strip()[:180], "evidence": str(record.get("evidence") or "").strip()[:180] if record else ""},
            ("description",),
            limit=limit,
        )
    for item in raw.get("worldbuilding_facts") or []:
        record = _safe_dict(item)
        text = record.get("fact") if record else item
        _append_unique_record(
            base["worldbuilding_facts"],
            {"chunk_index": record.get("chunk_index") if record else None, "fact": str(text or "").strip()[:180], "evidence": str(record.get("evidence") or "").strip()[:180] if record else ""},
            ("fact",),
            limit=limit,
        )
    for item in raw.get("foreshadowing_threads") or []:
        record = _safe_dict(item)
        text = record.get("description") if record else item
        status = str(record.get("status") or "active").strip()[:30] if record else "active"
        _append_unique_record(
            base["foreshadowing_threads"],
            {
                "chunk_index": record.get("chunk_index") if record else None,
                "type": str(record.get("type") or "").strip()[:60] if record else "",
                "description": str(text or "").strip()[:180],
                "status": status,
                "importance": str(record.get("importance") or "").strip()[:60] if record else "",
                "evidence": str(record.get("evidence") or "").strip()[:180] if record else "",
            },
            ("description", "status"),
            limit=limit,
        )
    for item in raw.get("plot_timeline") or []:
        record = _safe_dict(item)
        text = record.get("event") if record else item
        _append_unique_record(
            base["plot_timeline"],
            {"chunk_index": record.get("chunk_index") if record else None, "event": str(text or "").strip()[:180], "summary": str(record.get("summary") or "").strip()[:180] if record else ""},
            ("event",),
            limit=limit,
        )
    for item in raw.get("risk_facts") or []:
        record = _safe_dict(item)
        text = record.get("description") if record else item
        _append_unique_record(
            base["risk_facts"],
            {
                "chunk_index": record.get("chunk_index") if record else None,
                "type": str(record.get("type") or "").strip()[:60] if record else "",
                "description": str(text or "").strip()[:220],
                "evidence": str(record.get("evidence") or "").strip()[:180] if record else "",
                "confidence": str(record.get("confidence") or "medium").strip()[:30] if record else "medium",
            },
            ("type", "description"),
            limit=limit,
        )
    for key in ("open_threads", "resolved_threads"):
        for item in raw.get(key) or []:
            record = _safe_dict(item)
            text = record.get("thread") if record else item
            _append_unique_record(
                base[key],
                {"chunk_index": record.get("chunk_index") if record else None, "thread": str(text or "").strip()[:180], "evidence": str(record.get("evidence") or "").strip()[:180] if record else ""},
                ("thread",),
                limit=limit,
            )
    if not any(base.get(key) for key in ("facts", "entities", "relationships", "worldbuilding_facts", "foreshadowing_threads", "plot_timeline", "risk_facts", "open_threads", "resolved_threads")):
        return fallback
    resolved_texts = {x.get("thread") for x in base["resolved_threads"] if x.get("thread")}
    if resolved_texts:
        base["open_threads"] = [item for item in base["open_threads"] if item.get("thread") not in resolved_texts]
    return base


def _merge_knowledge_base_with_llm(book_name: str, knowledge_base: Dict[str, Any], profile=None) -> Dict[str, Any]:
    if not KNOWLEDGE_BASE_LLM_MERGE_ENABLED or not isinstance(knowledge_base, dict):
        return knowledge_base
    profile = profile or load_analysis_profile("general")
    material = _compact_knowledge_base_for_summary(knowledge_base, limit=80)
    system_prompt = f"""你是{profile.display_name}知识库合并器。请只基于输入 knowledge_base 做语义合并，不要添加输入中不存在的新事实。

目标：
1. 合并同一人物、同一设定、同一关系、同一伏笔/线索的重复表达。
2. 保留足够支撑整书总评的关键实体、关系变化、设定事实、伏笔线程、事件时间线、未解线索和已回收线索。
3. 如果 open_threads 与 resolved_threads 语义上指向同一线索，应从 open_threads 删除已回收项。

输出必须是 JSON 对象，字段固定为：
facts, entities, relationships, worldbuilding_facts, foreshadowing_threads, plot_timeline, risk_facts, open_threads, resolved_threads。
各字段保持数组；数组元素优先沿用输入对象字段。"""
    user_prompt = f"""书名：{book_name}

knowledge_base:
{json.dumps(material, ensure_ascii=False, indent=2)}

请输出合并后的 knowledge_base JSON。"""
    data = _call_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4200,
    )
    return _normalize_llm_knowledge_base(data, knowledge_base)


def _compact_knowledge_base_for_summary(knowledge_base: Dict[str, Any], limit: int = 40) -> Dict[str, Any]:
    if not isinstance(knowledge_base, dict):
        return {}
    return {
        "facts": _sample_records_for_summary(knowledge_base.get("facts") or [], limit),
        "entities": _sample_records_for_summary(knowledge_base.get("entities") or [], limit),
        "relationships": _sample_records_for_summary(knowledge_base.get("relationships") or [], limit),
        "worldbuilding_facts": _sample_records_for_summary(knowledge_base.get("worldbuilding_facts") or [], limit),
        "foreshadowing_threads": _sample_records_for_summary(knowledge_base.get("foreshadowing_threads") or [], limit),
        "plot_timeline": _sample_records_for_summary(knowledge_base.get("plot_timeline") or [], limit),
        "risk_facts": _sample_records_for_summary(knowledge_base.get("risk_facts") or [], limit),
        "open_threads": _sample_records_for_summary(knowledge_base.get("open_threads") or [], limit),
        "resolved_threads": _sample_records_for_summary(knowledge_base.get("resolved_threads") or [], limit),
    }


def _empty_rolling_context_state() -> Dict[str, Any]:
    return {
        "progress_summaries": [],
        "active_characters": [],
        "relationship_updates": [],
        "open_threads": [],
        "resolved_threads": [],
        "active_foreshadowing": [],
        "resolved_foreshadowing": [],
        "false_foreshadowing": [],
        "worldbuilding_updates": [],
        "current_stage": "",
        "last_chunk_index": None,
    }


def _rolling_context_snapshot(state: Dict[str, Any], max_chars: int = None) -> Dict[str, Any]:
    if not ROLLING_CONTEXT_ENABLED:
        return {}
    max_chars = CONTEXT_MAX_CHARS if max_chars is None else int(max_chars or 0)
    if max_chars <= 0 or not isinstance(state, dict):
        return {}
    snapshot = {
        "previous_progress": "；".join(_safe_list(state.get("progress_summaries"), limit=6)[-6:]),
        "current_stage": str(state.get("current_stage") or "").strip(),
        "active_characters": _safe_list(state.get("active_characters"), limit=16),
        "relationship_updates": _safe_list(state.get("relationship_updates"), limit=10),
        "open_threads": _safe_list(state.get("open_threads"), limit=12),
        "resolved_threads": _safe_list(state.get("resolved_threads"), limit=8),
        "active_foreshadowing": _safe_list(state.get("active_foreshadowing"), limit=10),
        "resolved_foreshadowing": _safe_list(state.get("resolved_foreshadowing"), limit=6),
        "worldbuilding_updates": _safe_list(state.get("worldbuilding_updates"), limit=10),
    }
    while len(json.dumps(snapshot, ensure_ascii=False)) > max_chars:
        reduced = False
        for key in ("relationship_updates", "worldbuilding_updates", "resolved_threads", "open_threads", "active_characters"):
            if len(snapshot.get(key) or []) > 3:
                snapshot[key] = snapshot[key][1:]
                reduced = True
                break
        if not reduced:
            previous = snapshot.get("previous_progress") or ""
            if len(previous) > 120:
                snapshot["previous_progress"] = previous[-120:]
                reduced = True
        if not reduced:
            break
    return _trim_context_snapshot(snapshot, max_chars)


def _trim_context_snapshot(snapshot: Dict[str, Any], max_chars: int = None) -> Dict[str, Any]:
    max_chars = CONTEXT_MAX_CHARS if max_chars is None else int(max_chars or 0)
    if max_chars <= 0 or not isinstance(snapshot, dict):
        return {}
    trimmed = json.loads(json.dumps(snapshot, ensure_ascii=False))
    drop_order = [
        "relationship_updates",
        "worldbuilding_updates",
        "resolved_foreshadowing",
        "resolved_threads",
        "active_foreshadowing",
        "open_threads",
        "active_characters",
        "previous_progress",
        "current_stage",
        "sampling_note",
    ]
    while len(json.dumps(trimmed, ensure_ascii=False)) > max_chars:
        reduced = False
        for key in ("relationship_updates", "worldbuilding_updates", "resolved_threads", "open_threads", "active_characters"):
            if len(trimmed.get(key) or []) > 2:
                trimmed[key] = trimmed[key][1:]
                reduced = True
                break
        if not reduced:
            previous = str(trimmed.get("previous_progress") or "")
            if len(previous) > 100:
                trimmed["previous_progress"] = previous[-100:]
                reduced = True
        if not reduced:
            for key in list(trimmed.keys()):
                value = trimmed.get(key)
                if isinstance(value, str) and len(value) > 80:
                    trimmed[key] = value[:max(20, min(80, max_chars // 2))]
                    reduced = True
                    break
        if not reduced:
            for key in drop_order:
                if key in trimmed and trimmed.get(key) not in (None, "", [], {}):
                    trimmed[key] = [] if isinstance(trimmed.get(key), list) else ""
                    reduced = True
                    break
        if not reduced:
            break
    if len(json.dumps(trimmed, ensure_ascii=False)) > max_chars:
        minimal = {}
        for key in ("sampled_context", "source_chunk_count", "current_original_chunk_index"):
            if key in trimmed:
                minimal[key] = trimmed[key]
        if len(json.dumps(minimal, ensure_ascii=False)) <= max_chars:
            return minimal
        return {}
    return trimmed


def _rolling_context_instruction(context_snapshot: Dict[str, Any]) -> str:
    if not ROLLING_CONTEXT_ENABLED:
        return ""
    if not context_snapshot:
        return """

【跨块上下文】
这是本次扫描的开端或前序上下文为空。请额外输出 context_state_update，用于后续片段理解人物、关系、未解问题和当前阶段。"""
    return f"""

【跨块上下文】
以下是前序片段的压缩状态，只用于辅助指代、别名、关系阶段、未解问题和设定连续性判断；不要把它当成当前片段事实证据。
{json.dumps(context_snapshot, ensure_ascii=False, indent=2)}

请额外输出 context_state_update，用于更新后续片段上下文。"""


def _context_state_json_hint() -> str:
    if not ROLLING_CONTEXT_ENABLED:
        return ""
    return """,
  "context_state_update": {
    "progress_summary": "本片段后全书进展的一句话增量摘要",
    "active_characters": ["本片段明确活跃或重要的人物/别名/身份"],
    "relationship_updates": ["人物关系阶段变化或重要互动"],
    "open_threads": ["新增或仍未解决的问题/目标/悬念"],
    "resolved_threads": ["本片段解决或回收的问题"],
    "worldbuilding_updates": ["新增或修正的设定/规则/势力/地点"],
    "current_stage": "当前主线阶段或篇章状态"
  }"""


def _update_rolling_context_state(state: Dict[str, Any], chunk_result: Dict[str, Any]) -> Dict[str, Any]:
    state = json.loads(json.dumps(state or _empty_rolling_context_state(), ensure_ascii=False))
    update = _normalize_context_state_update((chunk_result or {}).get("context_state_update"))
    foreshadowing = _normalize_foreshadowing_engineering((chunk_result or {}).get("foreshadowing_engineering"))
    progress = update.get("progress_summary") or str((chunk_result or {}).get("one_sentence_summary") or "").strip()[:220]
    if progress:
        state["progress_summaries"] = _dedupe_extend(state.get("progress_summaries") or [], [progress], limit=10)
    state["active_characters"] = _dedupe_extend(state.get("active_characters") or [], update.get("active_characters") or [], limit=40)
    state["relationship_updates"] = _dedupe_extend(state.get("relationship_updates") or [], update.get("relationship_updates") or [], limit=30)
    state["open_threads"] = _dedupe_extend(state.get("open_threads") or [], update.get("open_threads") or [], limit=40)
    state["resolved_threads"] = _dedupe_extend(state.get("resolved_threads") or [], update.get("resolved_threads") or [], limit=30)
    state["worldbuilding_updates"] = _dedupe_extend(state.get("worldbuilding_updates") or [], update.get("worldbuilding_updates") or [], limit=30)
    resolved = set(update.get("resolved_threads") or [])
    if resolved:
        state["open_threads"] = [item for item in state.get("open_threads") or [] if item not in resolved]
    new_foreshadowing = [item.get("description") for item in foreshadowing.get("new_foreshadowing") or [] if item.get("description")]
    resolved_foreshadowing = [
        item.get("resolved_item") or item.get("resolution_description")
        for item in foreshadowing.get("foreshadowing_resolutions") or []
        if item.get("resolved_item") or item.get("resolution_description")
    ]
    false_foreshadowing = foreshadowing.get("false_foreshadowing") or []
    state["active_foreshadowing"] = _dedupe_extend(state.get("active_foreshadowing") or [], new_foreshadowing, limit=50)
    state["resolved_foreshadowing"] = _dedupe_extend(state.get("resolved_foreshadowing") or [], resolved_foreshadowing, limit=40)
    state["false_foreshadowing"] = _dedupe_extend(state.get("false_foreshadowing") or [], false_foreshadowing, limit=30)
    if resolved_foreshadowing:
        resolved_text = set(resolved_foreshadowing)
        state["active_foreshadowing"] = [
            item for item in state.get("active_foreshadowing") or []
            if item not in resolved_text
        ]
    if update.get("current_stage"):
        state["current_stage"] = update["current_stage"]
    state["last_chunk_index"] = (chunk_result or {}).get("original_chunk_index", (chunk_result or {}).get("chunk_index"))
    return state


def _compact_rolling_context_timeline(chunk_results: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    timeline = []
    for item in chunk_results or []:
        if not isinstance(item, dict):
            continue
        update = _normalize_context_state_update(item.get("context_state_update"))
        foreshadowing = _normalize_foreshadowing_engineering(item.get("foreshadowing_engineering"))
        if not any(update.values()) and not any([
            foreshadowing.get("new_foreshadowing"),
            foreshadowing.get("foreshadowing_resolutions"),
            foreshadowing.get("false_foreshadowing"),
            foreshadowing.get("engineering_notes"),
            foreshadowing.get("recycling_rate"),
        ]):
            continue
        timeline.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "progress_summary": update.get("progress_summary"),
            "active_characters": update.get("active_characters"),
            "relationship_updates": update.get("relationship_updates"),
            "open_threads": update.get("open_threads"),
            "resolved_threads": update.get("resolved_threads"),
            "foreshadowing": foreshadowing,
            "worldbuilding_updates": update.get("worldbuilding_updates"),
            "current_stage": update.get("current_stage"),
        })
        if len(timeline) >= limit:
            break
    return timeline


def _merged_context_state_update(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = _empty_rolling_context_state()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        state = _update_rolling_context_state(state, result)
    return {
        "progress_summary": "；".join(_safe_list(state.get("progress_summaries"), limit=4)[-4:])[:220],
        "active_characters": _safe_list(state.get("active_characters"), limit=8),
        "relationship_updates": _safe_list(state.get("relationship_updates"), limit=6),
        "open_threads": _safe_list(state.get("open_threads"), limit=8),
        "resolved_threads": _safe_list(state.get("resolved_threads"), limit=6),
        "worldbuilding_updates": _safe_list(state.get("worldbuilding_updates"), limit=6),
        "current_stage": str(state.get("current_stage") or "").strip()[:120],
    }


def _normalize_narrative_structure(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    return {
        "structural_function": str(raw.get("structural_function") or "").strip()[:180],
        "structural_function_tag": str(raw.get("structural_function_tag") or "").strip()[:30],
        "structure_pattern": str(raw.get("structure_pattern") or "").strip()[:120],
        "beat_phase": str(raw.get("beat_phase") or "").strip()[:10],
        "turning_point": str(raw.get("turning_point") or "").strip()[:160],
        "arc_position": str(raw.get("arc_position") or "").strip()[:20],
        "estimated_cycle_position": str(raw.get("estimated_cycle_position") or "").strip()[:120],
    }


def _normalize_outline_architecture(value: Any) -> Dict[str, Any]:
    raw = _safe_dict(value)
    causal = _safe_dict(raw.get("causal_chain"))
    growth = _safe_dict(raw.get("protagonist_growth"))
    expansion = _safe_dict(raw.get("worldbuilding_expansion"))
    integrity = _safe_dict(raw.get("architecture_integrity"))
    return {
        "causal_chain": {
            "causal_strength": str(causal.get("causal_strength") or causal.get("strength") or "").strip()[:40],
            "causal_description": str(causal.get("causal_description") or causal.get("observation") or "").strip()[:180],
            "forced_elements": _safe_list(causal.get("forced_elements"), limit=4),
            "coincidence_dependency": str(causal.get("coincidence_dependency") or "").strip()[:30],
        },
        "protagonist_growth": {
            "growth_type": str(growth.get("growth_type") or "").strip()[:40],
            "growth_significance": str(growth.get("growth_significance") or "").strip()[:40],
            "growth_description": str(growth.get("growth_description") or "").strip()[:180],
            "growth_smoothness": str(growth.get("growth_smoothness") or "").strip()[:40],
        },
        "worldbuilding_expansion": {
            "new_elements": _safe_list(expansion.get("new_elements"), limit=5),
            "expansion_pacing": str(expansion.get("expansion_pacing") or "").strip()[:40],
            "consistency_check": str(expansion.get("consistency_check") or "").strip()[:50],
        },
        "architecture_integrity": {
            "integrity_score": _clamp_score(integrity.get("integrity_score"), default=0.0),
            "forced_plot_devices": _safe_list(integrity.get("forced_plot_devices"), limit=4),
            "power_inconsistency": str(integrity.get("power_inconsistency") or "").strip()[:180],
            "threat_level": str(integrity.get("threat_level") or "").strip()[:160],
        },
    }


def _compact_writing_quality_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 80) -> List[Dict[str, Any]]:
    compact = []
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        writing_quality = _safe_dict(item.get("writing_quality"))
        pacing = _safe_dict(item.get("pacing_analysis"))
        density = _safe_dict(item.get("information_density"))
        if not writing_quality and not pacing and not density:
            continue
        compact.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "writing_quality": writing_quality,
            "pacing_analysis": pacing,
            "information_density": density,
        })
    return _sample_records_for_summary(compact, limit)


def _compact_zhihu_writing_insights_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 80) -> Dict[str, Any]:
    records = []
    total_template_count = 0
    weighted_density_sum = 0.0
    density_weight = 0
    phrase_counts = {}
    category_counts = {}
    severity_counts = {}
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        writing_quality = _safe_dict(item.get("writing_quality"))
        insights = _safe_dict(writing_quality.get("zhihu_insights"))
        if not insights:
            continue
        word_poverty = _safe_dict(insights.get("word_poverty"))
        reader_space = _safe_dict(insights.get("reader_inference_space"))
        communication = _safe_dict(insights.get("communication_efficiency"))
        style = _safe_dict(insights.get("style_identity"))
        authenticity = _safe_dict(insights.get("emotional_authenticity"))
        if not any([word_poverty, reader_space, communication, style, authenticity]):
            continue

        count = _safe_int(word_poverty.get("template_phrase_count"))
        density = word_poverty.get("template_phrase_density_per_1k")
        try:
            density_value = float(density)
        except (TypeError, ValueError):
            density_value = 0.0
        severity = str(word_poverty.get("severity") or "").strip()
        if severity:
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
        total_template_count += count
        if density_value:
            weighted_density_sum += density_value
            density_weight += 1
        for item_text in word_poverty.get("most_frequent_templates") or []:
            text = str(item_text or "").strip()
            if not text:
                continue
            phrase, _, rest = text.partition("(")
            try:
                phrase_count = int(rest.split("次", 1)[0])
            except (TypeError, ValueError):
                phrase_count = 1
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + phrase_count
        for category, hits in (_safe_dict(word_poverty.get("category_hits"))).items():
            category_total = 0
            for hit in hits or []:
                if isinstance(hit, dict):
                    category_total += _safe_int(hit.get("count"))
            if category_total:
                category_counts[category] = category_counts.get(category, 0) + category_total

        records.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "word_poverty": {
                "template_phrase_count": count,
                "template_phrase_density_per_1k": density_value,
                "most_frequent_templates": _safe_list(word_poverty.get("most_frequent_templates"), limit=5),
                "severity": severity,
            },
            "reader_inference_space": {
                "score": reader_space.get("score"),
                "l1_tell_count": reader_space.get("l1_tell_count"),
                "l2_show_count": reader_space.get("l2_show_count"),
                "l3_subtext_count": reader_space.get("l3_subtext_count"),
                "assessment": reader_space.get("assessment"),
            },
            "communication_efficiency": {
                "level": communication.get("level"),
                "level_name": communication.get("level_name"),
                "redundant_expression_rate": communication.get("redundant_expression_rate"),
                "assessment": communication.get("assessment"),
            },
            "style_identity": {
                "detected_traits": _safe_list(style.get("detected_traits"), limit=4),
                "originality_score": style.get("originality_score"),
                "consistency_score": style.get("consistency_score"),
            },
            "emotional_authenticity": {
                "score": authenticity.get("score"),
                "transcendence_potential": authenticity.get("transcendence_potential"),
                "assessment": authenticity.get("assessment"),
            },
        })

    return {
        "template_phrase_count": total_template_count,
        "average_template_density_per_1k": round(weighted_density_sum / density_weight, 3) if density_weight else 0.0,
        "most_frequent_templates": [
            f"{phrase}({count}次)"
            for phrase, count in sorted(phrase_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ],
        "category_counts": dict(sorted(category_counts.items(), key=lambda x: x[1], reverse=True)[:8]),
        "severity_counts": severity_counts,
        "sample_chunks": _sample_records_for_summary(records, limit),
    }


def _compact_narrative_architecture_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    compact = []
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        structure = _safe_dict(item.get("narrative_structure"))
        architecture = _safe_dict(item.get("outline_architecture"))
        if not structure and not architecture:
            continue
        causal = _safe_dict(architecture.get("causal_chain"))
        growth = _safe_dict(architecture.get("protagonist_growth"))
        expansion = _safe_dict(architecture.get("worldbuilding_expansion"))
        integrity = _safe_dict(architecture.get("architecture_integrity"))
        compact.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "structural_function_tag": structure.get("structural_function_tag"),
            "structure_pattern": structure.get("structure_pattern"),
            "beat_phase": structure.get("beat_phase"),
            "turning_point": structure.get("turning_point"),
            "arc_position": structure.get("arc_position"),
            "estimated_cycle_position": structure.get("estimated_cycle_position"),
            "causal_strength": causal.get("causal_strength"),
            "growth_smoothness": growth.get("growth_smoothness"),
            "growth_significance": growth.get("growth_significance"),
            "expansion_pacing": expansion.get("expansion_pacing"),
            "consistency_check": expansion.get("consistency_check"),
            "integrity_score": integrity.get("integrity_score"),
            "forced_plot_devices": integrity.get("forced_plot_devices") or [],
        })
    return _sample_records_for_summary(compact, limit)


def _compact_foreshadowing_engineering_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    compact = []
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        engineering = _normalize_foreshadowing_engineering(item.get("foreshadowing_engineering"))
        if not any([
            engineering.get("new_foreshadowing"),
            engineering.get("foreshadowing_resolutions"),
            engineering.get("false_foreshadowing"),
            engineering.get("engineering_notes"),
            engineering.get("recycling_rate"),
        ]):
            continue
        compact.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "new_foreshadowing": engineering.get("new_foreshadowing") or [],
            "foreshadowing_resolutions": engineering.get("foreshadowing_resolutions") or [],
            "false_foreshadowing": engineering.get("false_foreshadowing") or [],
            "engineering_notes": engineering.get("engineering_notes") or [],
            "recycling_rate": engineering.get("recycling_rate") or "",
        })
    return _sample_records_for_summary(compact, limit)


def _compact_semantic_layers_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    compact = []
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        semantic = _normalize_semantic_layers(item.get("semantic_layers"))
        if not any([
            semantic.get("literal_meaning"),
            semantic.get("author_intent"),
            semantic.get("surface_emotion"),
            semantic.get("reader_effect"),
            semantic.get("deep_semantic"),
            semantic.get("technique"),
            semantic.get("subtext_or_irony"),
        ]):
            continue
        compact.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "literal_meaning": semantic.get("literal_meaning"),
            "author_intent": semantic.get("author_intent"),
            "surface_emotion": semantic.get("surface_emotion"),
            "reader_effect": semantic.get("reader_effect"),
            "deep_semantic": semantic.get("deep_semantic"),
            "technique": semantic.get("technique"),
            "subtext_or_irony": semantic.get("subtext_or_irony") or [],
            "confidence": semantic.get("confidence"),
        })
    return _sample_records_for_summary(compact, limit)


def _compact_reader_experience_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    compact = []
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        experience = _normalize_reader_experience(item.get("reader_experience"))
        emotion = _safe_dict(experience.get("immediate_emotion"))
        anticipation = _safe_dict(experience.get("anticipation"))
        if not any([
            emotion.get("emotion"),
            emotion.get("trigger"),
            experience.get("immersion_anchor"),
            anticipation.get("expected"),
            experience.get("satisfaction_points"),
            experience.get("frustration_points"),
            experience.get("engagement_level"),
            experience.get("experience_notes"),
        ]):
            continue
        compact.append({
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "immediate_emotion": emotion,
            "immersion_anchor": experience.get("immersion_anchor"),
            "anticipation": anticipation,
            "satisfaction_points": experience.get("satisfaction_points") or [],
            "frustration_points": experience.get("frustration_points") or [],
            "engagement_level": experience.get("engagement_level"),
            "experience_notes": experience.get("experience_notes") or [],
        })
    return _sample_records_for_summary(compact, limit)


def _compact_continuity_for_summary(chunk_results: List[Dict[str, Any]], limit: int = 120) -> List[Dict[str, Any]]:
    compact = []
    for item in chunk_results:
        if not isinstance(item, dict):
            continue
        update = _normalize_context_state_update(item.get("context_state_update"))
        architecture = _safe_dict(item.get("outline_architecture"))
        expansion = _safe_dict(architecture.get("worldbuilding_expansion"))
        integrity = _safe_dict(architecture.get("architecture_integrity"))
        causal = _safe_dict(architecture.get("causal_chain"))
        foreshadowing = _normalize_foreshadowing_engineering(item.get("foreshadowing_engineering"))
        material = {
            "chunk_index": item.get("original_chunk_index", item.get("chunk_index")),
            "summary": item.get("one_sentence_summary"),
            "conflicts": _safe_list(item.get("conflicts"), limit=4),
            "worldbuilding": _safe_list(item.get("worldbuilding"), limit=4),
            "quality_notes": _safe_list(item.get("quality_notes"), limit=4),
            "relationship_updates": update.get("relationship_updates"),
            "open_threads": update.get("open_threads"),
            "resolved_threads": update.get("resolved_threads"),
            "worldbuilding_updates": update.get("worldbuilding_updates"),
            "causal_strength": causal.get("causal_strength"),
            "forced_elements": causal.get("forced_elements") or [],
            "coincidence_dependency": causal.get("coincidence_dependency"),
            "worldbuilding_consistency": expansion.get("consistency_check"),
            "power_inconsistency": integrity.get("power_inconsistency"),
            "forced_plot_devices": integrity.get("forced_plot_devices") or [],
            "new_foreshadowing": foreshadowing.get("new_foreshadowing") or [],
            "foreshadowing_resolutions": foreshadowing.get("foreshadowing_resolutions") or [],
        }
        if not any(value for key, value in material.items() if key not in {"chunk_index", "summary"}):
            continue
        compact.append(material)
    return _sample_records_for_summary(compact, limit)


def _merge_foreshadowing_engineering_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    merged = {
        "new_foreshadowing": [],
        "foreshadowing_resolutions": [],
        "false_foreshadowing": [],
        "engineering_notes": [],
        "recycling_rate": "",
    }
    seen_new = set()
    seen_resolved = set()
    seen_false = set()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        engineering = _normalize_foreshadowing_engineering(result.get("foreshadowing_engineering"))
        for item in engineering.get("new_foreshadowing") or []:
            key = item.get("description")
            if not key or key in seen_new:
                continue
            seen_new.add(key)
            merged["new_foreshadowing"].append(item)
        for item in engineering.get("foreshadowing_resolutions") or []:
            key = item.get("resolved_item") or item.get("resolution_description")
            if not key or key in seen_resolved:
                continue
            seen_resolved.add(key)
            merged["foreshadowing_resolutions"].append(item)
        for item in engineering.get("false_foreshadowing") or []:
            if item in seen_false:
                continue
            seen_false.add(item)
            merged["false_foreshadowing"].append(item)
        merged["engineering_notes"] = _dedupe_extend(
            merged.get("engineering_notes") or [],
            engineering.get("engineering_notes") or [],
            limit=8,
        )
        if engineering.get("recycling_rate"):
            merged["recycling_rate"] = engineering["recycling_rate"]
    merged["new_foreshadowing"] = merged["new_foreshadowing"][:6]
    merged["foreshadowing_resolutions"] = merged["foreshadowing_resolutions"][:6]
    merged["false_foreshadowing"] = merged["false_foreshadowing"][:5]
    return merged


def _normalize_object_summary(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for key, item in value.items():
        if isinstance(item, list):
            normalized[key] = _safe_list(item, limit=10)
        elif isinstance(item, dict):
            normalized[key] = _normalize_object_summary(item)
        else:
            text = str(item or "").strip()
            if text:
                normalized[key] = text[:600]
    return normalized


def _rules_lines_from_file(rules_file: str, import_categories=None, import_points=None) -> List[str]:
    data = _read_json(rules_file)
    if not isinstance(data, dict):
        return []
    category_filter = {str(x) for x in (import_categories or []) if str(x).strip()}
    point_filter = {str(x) for x in (import_points or []) if str(x).strip()}
    lines = []
    for category in data.get("categories", []) or []:
        if not isinstance(category, dict):
            continue
        name = str(category.get("name") or "").strip()
        description = str(category.get("description") or "").strip()
        include_category = not category_filter or name in category_filter
        point_lines = []
        for point in category.get("points", []) or []:
            if not isinstance(point, dict):
                continue
            point_name = str(point.get("name") or "").strip()
            point_desc = str(point.get("description") or "").strip()
            if point_filter and point_name not in point_filter:
                continue
            if point_name:
                point_lines.append(f"- {point_name}: {point_desc}")
        if not include_category and not point_lines:
            continue
        if name:
            lines.append(f"【{name}】{description}")
        lines.extend(point_lines)
    return lines


def _cross_profile_rules_text(profile) -> str:
    cross_rules = getattr(profile, "cross_profile_rules", {}) or {}
    if not isinstance(cross_rules, dict):
        return ""
    sections = []
    for source_name, config in cross_rules.items():
        if not isinstance(config, dict):
            continue
        source_profile = load_analysis_profile(str(source_name))
        lines = _rules_lines_from_file(
            getattr(source_profile, "rules_file", "") or "",
            import_categories=config.get("import_categories") if isinstance(config.get("import_categories"), list) else None,
            import_points=config.get("import_points") if isinstance(config.get("import_points"), list) else None,
        )
        if lines:
            sections.append(f"【跨类型导入：{source_profile.display_name}】\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _profile_rules_text(profile) -> str:
    lines = _rules_lines_from_file(getattr(profile, "rules_file", "") or "")
    cross_text = _cross_profile_rules_text(profile)
    if cross_text:
        lines.append(cross_text)
    return "\n".join(lines) if lines else "（无专项规则）"


def _call_json(messages, max_tokens=3000) -> Dict[str, Any]:
    return call_json_chat_completion_with_fallback(
        chat_completion_func=chat_completion,
        messages=messages,
        model=MODEL,
        temperature=0.1,
        max_tokens=max_tokens,
        record_usage_func=record_usage,
    )


def _focus_text(profile) -> str:
    focus = profile.scan_focus or [
        "剧情主线与关键事件",
        "核心冲突与人物目标",
        "世界观、时代背景或制度设定",
        "主题表达与情绪基调",
        "伏笔、悬念与回收",
        "节奏、逻辑、人物动机、优点和阅读门槛",
    ]
    return "\n".join(f"- {item}" for item in focus)


def _density_instruction(density_profile: Dict[str, Any]) -> str:
    if not SMART_DENSITY or not isinstance(density_profile, dict):
        return "密度策略：full。按完整字段抽取。"
    level = density_profile.get("level") or "medium"
    if density_profile.get("strategy") == "light":
        return (
            "密度策略：light。当前片段疑似低密度过渡/日常/重复内容；"
            "只保留真实推动剧情的信息。plot_events 和 one_sentence_summary 必须简洁；"
            "conflicts/worldbuilding/themes/foreshadowing/quality_notes/specialty_notes 没有明确新增信息时输出空数组。"
        )
    return f"密度策略：full。当前片段密度={level}，按完整字段抽取。"


def _writing_quality_system_instruction() -> str:
    if not WRITING_QUALITY_ENABLED:
        return ""
    return """

【写作质量、节奏和信息密度评估】
请额外输出 writing_quality、pacing_analysis、information_density 三个结构化字段。判断必须基于当前片段可见证据，不要把缺失内容脑补成整书结论。

writing_quality 采用 8 个维度，每个维度输出 score(0-10)、strength、weakness：
- prose_quality: 文笔质量，关注词汇、句式、修辞、语体自然度、模板化表达。
- character_depth: 人物塑造，关注立体度、行为一致性、角色区分度、工具人风险。
- narrative_technique: 叙事技巧，关注视角、时空处理、详略、信息控制和衔接。
- dialogue_quality: 对话质量，关注自然度、性格化、信息效率、潜台词、说教感。
- scene_description: 场景描写，关注画面、空间、五感、动作/战斗清晰度和氛围。
- emotional_impact: 情感渲染，关注情绪递进、共情、燃点/泪点/爽点是否成立。
- info_density: 信息密度，额外输出 water_chapter_score(0=完全不水，10=明显水章)。
- worldbuilding_integration: 世界观融入，关注设定是否自然服务剧情，是否信息倾倒。

知乎文笔洞察专项检测作为 writing_quality.zhihu_insights 输出，不替代 8 维主评分：
- word_poverty: 词穷症状，结合系统给出的模板词统计，判断是否依赖“恐怖如斯、倒吸一口凉气、冷冷的、不禁、身形一闪”等模板化表达。
- reader_inference_space: 读者推导空间，将情绪/状态表达分为 L1直白告知、L2行为暗示、L3意境留白；好文笔应让读者自行感受，而不是全靠作者说明。
- communication_efficiency: 信息传播效率，按门外汉/入门/发展/进阶/大师五级判断表达是否冗余、是否需要读者翻译、能否用更少字传达同样信息。
- style_identity: 风格辨识度，只输出风格标签和原创/一致性判断，不强行对标具体作家。
- emotional_authenticity: 情感真实度，判断情绪是否有个人化细节和真实痛感，而非模板化煽情。

pacing_analysis 输出 pacing_type、tension_level、emotion_tone、emotion_intensity、payoff_moment、suffering_moment、cliffhanger_quality、reader_engagement_prediction。
information_density 输出 density_score(high/medium/low/water)、skipability(essential/helpful/skippable/deletable)、key_information、redundancy_flags、narrative_efficiency。
evidence 最多 3 条，quote 原文摘录不超过 30 字。"""


def _writing_quality_json_hint() -> str:
    if not WRITING_QUALITY_ENABLED:
        return ""
    return """,
  "writing_quality": {
    "prose_quality": {"score": 0-10, "strength": "...", "weakness": "..."},
    "character_depth": {"score": 0-10, "strength": "...", "weakness": "..."},
    "narrative_technique": {"score": 0-10, "strength": "...", "weakness": "..."},
    "dialogue_quality": {"score": 0-10, "strength": "...", "weakness": "..."},
    "scene_description": {"score": 0-10, "strength": "...", "weakness": "..."},
    "emotional_impact": {"score": 0-10, "strength": "...", "weakness": "..."},
    "info_density": {"score": 0-10, "water_chapter_score": 0-10, "strength": "...", "weakness": "..."},
    "worldbuilding_integration": {"score": 0-10, "strength": "...", "weakness": "..."},
    "zhihu_insights": {
      "word_poverty": {"score": 0-10, "severity": "未见明显模板词|偶见模板词|轻度词穷|严重词穷", "assessment": "..."},
      "reader_inference_space": {"score": 0-10, "l1_tell_count": 0, "l2_show_count": 0, "l3_subtext_count": 0, "implicit_delivery_rate": "...", "tell_examples": ["..."], "show_examples": ["..."], "assessment": "..."},
      "communication_efficiency": {"level": "1-5", "level_name": "门外汉|入门|发展|进阶|大师", "information_loss_rate": "...", "reader_comprehension_barrier": "高|中|低|极低|无", "redundant_expression_rate": "...", "precision_score": 0-10, "conciseness_score": 0-10, "assessment": "..."},
      "style_identity": {"detected_traits": ["冷峻简洁/口语化/古风韵味等"], "originality_score": 0-10, "consistency_score": 0-10, "assessment": "..."},
      "emotional_authenticity": {"score": 0-10, "genuine_emotion_vs_forced": "...", "universal_resonance": "...", "personal_vs_generic": "...", "transcendence_potential": "高|中|低", "assessment": "..."}
    },
    "chunk_assessment": "本片段写作质量一句话评价",
    "evidence": [{"type": "亮点/问题", "dimension": "维度名", "quote": "原文短摘录", "note": "说明"}]
  },
  "pacing_analysis": {
    "pacing_type": "fast|slow|climax|transition|dense|action|emotional|filler",
    "tension_level": 0-10,
    "emotion_tone": "爽|虐|燃|悲|悬|甜|恐|平|怒|喜",
    "emotion_intensity": 0-10,
    "payoff_moment": "爽点/燃点/甜点，没有则空",
    "suffering_moment": "虐点/痛点/泪点，没有则空",
    "cliffhanger_quality": "strong|medium|weak|none",
    "reader_engagement_prediction": "high|medium|low"
  },
  "information_density": {
    "density_score": "high|medium|low|water",
    "skipability": "essential|helpful|skippable|deletable",
    "key_information": ["本片段实际推进的新信息"],
    "redundancy_flags": ["重复解释/无效日常/重复心理等，没有则空"],
    "narrative_efficiency": "每千字有效推进量的简短评价"
  }"""


def _writing_quality_summary_json_hint() -> str:
    if not WRITING_QUALITY_ENABLED:
        return ""
    return """
  "writing_quality_overall": {
    "overall_score": 0-10,
    "grade": "S/A/B/C/D/E/F",
    "dimension_scores": {
      "prose_quality": 0-10,
      "character_depth": 0-10,
      "narrative_technique": 0-10,
      "dialogue_quality": 0-10,
      "scene_description": 0-10,
      "emotional_impact": 0-10,
      "info_density": 0-10,
      "worldbuilding_integration": 0-10
    },
    "strengths": ["写作层面的主要优势"],
    "weaknesses": ["写作层面的主要短板"],
    "evidence": ["基于分块材料的短证据"],
    "assessment": "整书写作质量评价"
  },
  "pacing_analysis_overall": {
    "rhythm_curve": "节奏曲线描述",
    "high_points": ["主要高潮/爽点/燃点"],
    "slow_or_water_segments": ["拖慢阅读的位置或类型"],
    "emotion_pattern": "情绪调动模式",
    "risks": ["节奏风险"]
  },
  "information_density_audit": {
    "density_verdict": "整体信息密度判断",
    "water_ratio_estimate": "水章比例估计",
    "high_density_material": ["高信息量内容类型"],
    "redundancy_patterns": ["重复解释/无效日常/重复心理等"],
    "skip_advice": "哪些内容可跳读或不建议跳读"
  },
  "water_chapter_analysis": ["水文/冗余/低效叙事的具体表现"],"""


def _zhihu_writing_insights_summary_json_hint() -> str:
    if not WRITING_QUALITY_ENABLED:
        return ""
    return """
  "zhihu_writing_insights_overall": {
    "word_poverty": {
      "severity": "未见明显模板词|偶见模板词|轻度词穷|严重词穷",
      "template_phrase_count": 0,
      "template_phrase_density_per_1k": 0,
      "most_frequent_templates": ["模板词(次数)"],
      "category_patterns": ["高频模板类别"],
      "assessment": "词汇精准度和模板化表达评价"
    },
    "reader_inference_space": {
      "score": 0-10,
      "l1_l2_l3_pattern": "直白告知/行为暗示/意境留白的整体比例判断",
      "assessment": "是否给读者留出推导空间"
    },
    "communication_efficiency": {
      "level": "1-5",
      "level_name": "门外汉|入门|发展|进阶|大师",
      "redundancy_verdict": "冗余和理解负担判断",
      "assessment": "信息传播效率评价"
    },
    "style_identity": {
      "detected_traits": ["风格标签"],
      "originality_score": 0-10,
      "consistency_score": 0-10,
      "assessment": "文风辨识度评价"
    },
    "emotional_authenticity": {
      "score": 0-10,
      "transcendence_potential": "高|中|低",
      "assessment": "情感真实度评价"
    },
    "priority_improvements": ["最值得优先修改的写作问题"]
  },"""


def _narrative_architecture_system_instruction() -> str:
    if not NARRATIVE_ARCHITECTURE_ENABLED:
        return ""
    return """

【叙事结构与大纲架构评估】
请额外输出 narrative_structure、outline_architecture 两个结构化字段。判断只基于当前片段可见证据；不能把单个片段脑补成整本书结论。

narrative_structure 用来标注本片段在叙事工程中的功能：
- structural_function: 当前片段主要承担的结构功能，例如铺垫、升温、高潮、回落、转场、信息桥接。
- structural_function_tag: setup/rising/climax/falling/bridge/transition 中选一个主标签。
- structure_pattern: 如果能识别循环或单元结构，标注如“升级流-突破段”“打脸循环-压制段”“单元案件-收束段”。
- beat_phase: 起/承/转/合/无。
- turning_point: 地图切换、时间跳跃、境界突破、势力变更、身份揭示、关键人物退场等结构性转折点；没有则空。
- arc_position: 开端/发展/高潮/收尾/过渡。
- estimated_cycle_position: 如果处于循环模式，说明所在段位；无法判断则空。

outline_architecture 用来从大纲角度评估当前片段对整书架构的影响：
- causal_chain: 事件因果是否自然，有无关键巧合或强行推进。
- protagonist_growth: 主角能力、地位、关系、认知或道德成长是否自然。
- worldbuilding_expansion: 新设定引入是否及时、过载或前后矛盾。
- architecture_integrity: 结构完整度、强行剧情装置、体系/战力一致性和威胁层级是否稳定。"""


def _narrative_architecture_json_hint() -> str:
    if not NARRATIVE_ARCHITECTURE_ENABLED:
        return ""
    return """,
  "narrative_structure": {
    "structural_function": "本片段的主要结构功能",
    "structural_function_tag": "setup|rising|climax|falling|bridge|transition",
    "structure_pattern": "升级流/打脸循环/单元案件/事业里程碑等模式及段位",
    "beat_phase": "起|承|转|合|无",
    "turning_point": "结构性转折点，没有则空",
    "arc_position": "开端|发展|高潮|收尾|过渡",
    "estimated_cycle_position": "循环模式中的位置，没有则空"
  },
  "outline_architecture": {
    "causal_chain": {
      "causal_strength": "必然因果|自然发展|逻辑通顺|有些牵强|明显强行|毫无关联",
      "causal_description": "因果链评价",
      "forced_elements": ["牵强元素，没有则空"],
      "coincidence_dependency": "none|minor|major|deus_ex"
    },
    "protagonist_growth": {
      "growth_type": "power|status|relationship|knowledge|morality|none",
      "growth_significance": "major|moderate|minor|none",
      "growth_description": "成长内容",
      "growth_smoothness": "smooth|reasonable|abrupt|ass_pull"
    },
    "worldbuilding_expansion": {
      "new_elements": ["新设定元素"],
      "expansion_pacing": "natural|timely|abrupt|overloaded|sparse",
      "consistency_check": "consistent|minor_issue|major_contradiction|retcon"
    },
    "architecture_integrity": {
      "integrity_score": 0-10,
      "forced_plot_devices": ["强行剧情装置，没有则空"],
      "power_inconsistency": "战力/体系一致性评价",
      "threat_level": "当前威胁水平合理性"
    }
  }"""


def _narrative_architecture_summary_json_hint() -> str:
    if not NARRATIVE_ARCHITECTURE_ENABLED:
        return ""
    return """
  "narrative_structure_analysis": {
    "primary_structure_pattern": "主要结构模式",
    "structure_pattern_description": "该结构模式的具体描述",
    "rhythm_curve_description": "全书节奏曲线描述",
    "major_turning_points": ["主要结构性转折点"],
    "arc_structure": "叙事弧结构描述",
    "sub_arc_analysis": ["子篇章/阶段的起承转合评价"],
    "structure_execution_quality": "优秀|良好|一般|较差，并说明理由",
    "structure_risks": ["结构风险"]
  },
  "outline_architecture_overall": {
    "structural_completeness": "结构完整性和烂尾风险评价",
    "causal_chain_strength": "strong|medium|weak|fragmented",
    "growth_curve": {
      "smoothness": "smooth|natural|abrupt|rollercoaster|stagnant",
      "curve_description": "成长曲线描述",
      "major_jumps": ["主要跳跃点"],
      "stagnation_periods": ["停滞期"]
    },
    "worldbuilding_pacing": {
      "expansion_quality": "excellent|good|uneven|poor",
      "expansion_description": "世界观展开节奏描述",
      "overload_points": ["设定过载点"],
      "famine_points": ["设定供给不足点"]
    },
    "system_stability": "体系/战力/规则稳定性评价",
    "architecture_damage": ["大纲层面的损伤"],
    "overall_architecture_rating": "excellent|good|average|poor",
    "architecture_score": 0-10,
    "improvement_suggestions": ["结构层面的改进建议"]
  },"""


def _foreshadowing_engineering_system_instruction() -> str:
    if not FORESHADOWING_ENGINEERING_ENABLED:
        return ""
    return """

【伏笔工程追踪】
请额外输出 foreshadowing_engineering。它用于追踪“设置-维持-误导-回收”的工程质量，不等同于普通悬念列表。

判断规则：
- new_foreshadowing：只记录当前片段新出现、后续可能需要回收的具体物件、台词、异常事件、人物身份疑点、环境/设定线索；普通未完成剧情目标不要泛化成伏笔。
- foreshadowing_resolutions：只记录当前片段明确解释、兑现或反转了前文线索的内容；要写清回收方式和满足度。
- false_foreshadowing：记录当前片段证明是烟雾弹、误导或假线索的内容。
- estimated_importance 必须基于片段证据估计 high/medium/low；不确定时用 low 或留空。
- recycling_rate 只在片段内可估算时填写，例如“本片段回收1条/新增2条”；无法估算则空。
每类最多保留关键项，不要堆砌普通信息。"""


def _foreshadowing_engineering_json_hint() -> str:
    if not FORESHADOWING_ENGINEERING_ENABLED:
        return ""
    return """,
  "foreshadowing_engineering": {
    "new_foreshadowing": [
      {"type": "item|dialogue|event|character|environment", "description": "新设置的具体伏笔", "estimated_importance": "high|medium|low", "evidence": "原文短证据"}
    ],
    "foreshadowing_resolutions": [
      {"resolved_item": "被回收的伏笔", "resolution_description": "如何回收/兑现/反转", "satisfaction": "satisfying|okay|disappointing|unresolved", "evidence": "原文短证据"}
    ],
    "false_foreshadowing": ["被证明为烟雾弹/假线索的内容"],
    "engineering_notes": ["伏笔设置或回收的工程性评价"],
    "recycling_rate": "片段内可估算回收率，无法估算则空"
  }"""


def _foreshadowing_engineering_summary_json_hint() -> str:
    if not FORESHADOWING_ENGINEERING_ENABLED:
        return ""
    return """
  "foreshadowing_engineering_analysis": {
    "setup_quality": "excellent|good|average|weak",
    "active_threads": ["仍未回收的重要伏笔/线索"],
    "resolved_threads": ["已回收伏笔及回收质量"],
    "false_or_red_herring": ["烟雾弹/假线索/误导线"],
    "payoff_satisfaction": "satisfying|okay|uneven|weak",
    "recycling_rate_estimate": "估计回收率或无法估算原因",
    "risks": ["伏笔工程风险"]
  },"""


def _semantic_layers_system_instruction() -> str:
    if not SEMANTIC_LAYERS_ENABLED:
        return ""
    return """

【中文深层语义与四层分析】
请额外输出 semantic_layers。它用于分析当前片段的中文语义、潜台词、反讽、读者效果和写作手法，不替代事实抽取。

四层判断：
- literal_meaning：事实层，当前片段字面发生了什么或角色明确说了什么。
- author_intent：意图层，作者为什么这样安排，如铺垫、制造期待、压抑后反弹、解释设定、强化人设。
- reader_effect：效果层，普通读者可能产生的阅读感受，如爽、压抑、期待、困惑、厌烦、紧张、共情。
- technique：技法层，使用了什么写作手法，如对比反衬、先抑后扬、信息延迟、视角限制、重复强调、留白、误导。

deep_semantic 和 subtext_or_irony 只记录有明确文本依据的潜台词、言外之意或反讽；没有则留空/空数组。confidence 用 high/medium/low。"""


def _semantic_layers_json_hint() -> str:
    if not SEMANTIC_LAYERS_ENABLED:
        return ""
    return """,
  "semantic_layers": {
    "literal_meaning": "事实层：片段字面信息",
    "author_intent": "意图层：作者安排此段的叙事目的",
    "surface_emotion": "表层情绪基调",
    "reader_effect": "效果层：读者可能感受",
    "deep_semantic": "深层语义/潜台词/言外之意，没有则空",
    "technique": "技法层：主要写作手法",
    "subtext_or_irony": ["明确可见的潜台词或反讽"],
    "confidence": "high|medium|low"
  }"""


def _semantic_layers_summary_json_hint() -> str:
    if not SEMANTIC_LAYERS_ENABLED:
        return ""
    return """
  "semantic_layers_analysis": {
    "dominant_author_intent": "全书主要叙事意图模式",
    "reader_effect_pattern": "读者效果与情绪反馈模式",
    "deep_semantic_pattern": "潜台词/言外之意/反讽的整体特征",
    "technique_pattern": ["常用语义与叙事技法"],
    "subtext_or_irony": ["有代表性的潜台词或反讽"],
    "semantic_strengths": ["语义表达层面的优势"],
    "semantic_risks": ["语义表达层面的风险或误读点"]
  },"""


def _reader_experience_system_instruction() -> str:
    if not READER_EXPERIENCE_ENABLED:
        return ""
    return """

【读者体验、爽虐点与期待管理】
请额外输出 reader_experience。它用于评估当前片段对目标网文读者的即时阅读体验，不等同于写作质量评分，也不要使用后宫专项排雷标准。

判断要点：
- immediate_emotion：读者读到本片段最可能产生的即时情绪及强度，例如爽、燃、紧张、压抑、困惑、厌烦、共情、期待。
- immersion_anchor：读者代入或关注的锚点，例如主角收益、角色处境、悬念问题、势力对抗、感情进展。
- anticipation：读完本片段后被引导期待什么，以及期待强度和钩子类型。
- satisfaction_points：记录明确带来满足感的爽点、燃点、甜点、解谜满足、成长兑现等。
- frustration_points：记录可能削弱体验的憋屈、拖延、重复解释、期待落空、逻辑卡顿、情绪疲劳等；不确定时不要扩大化。
- engagement_level：按当前片段估计读者投入度 high/medium/low。
每类最多保留关键项，必须基于片段证据。"""


def _reader_experience_json_hint() -> str:
    if not READER_EXPERIENCE_ENABLED:
        return ""
    return """,
  "reader_experience": {
    "immediate_emotion": {"emotion": "爽|燃|紧张|压抑|困惑|厌烦|共情|期待|平", "intensity": 0-10, "trigger": "触发情绪的短证据"},
    "immersion_anchor": "读者代入/关注锚点",
    "anticipation": {"expected": "读者读完后期待什么", "intensity": 0-10, "hook_type": "悬念|反击|成长|感情|解谜|危机|设定|其他"},
    "satisfaction_points": [{"type": "爽点|燃点|甜点|解谜|成长兑现|其他", "description": "满足感来源", "intensity": 0-10, "evidence": "短证据"}],
    "frustration_points": [{"type": "憋屈|拖延|重复|期待落空|逻辑卡顿|情绪疲劳|其他", "description": "可能削弱体验的点", "intensity": 0-10, "evidence": "短证据"}],
    "engagement_level": "high|medium|low",
    "experience_notes": ["读者体验补充判断"]
  }"""


def _reader_experience_summary_json_hint() -> str:
    if not READER_EXPERIENCE_ENABLED:
        return ""
    return """
  "reader_experience_analysis": {
    "engagement_curve": "整书读者投入度曲线",
    "dominant_emotions": ["主导阅读情绪"],
    "satisfaction_design": ["主要爽点/燃点/甜点/解谜满足如何设计"],
    "anticipation_management": "期待钩子的设置、延迟和兑现情况",
    "immersion_anchors": ["读者主要代入或关注的锚点"],
    "frustration_risks": ["可能导致读者疲劳、憋屈或弃书的体验风险"],
    "reader_experience_rating": "excellent|good|average|weak",
    "improvement_suggestions": ["读者体验层面的改进建议"]
  },"""


def _continuity_audit_summary_json_hint() -> str:
    if not CONTINUITY_AUDIT_ENABLED:
        return ""
    return """
  "continuity_audit_analysis": {
    "overall_continuity_rating": "excellent|good|average|risky",
    "risk_level": "low|medium|high",
    "character_continuity": ["人物身份、称呼、关系阶段或行为动机的连续性判断"],
    "relationship_consistency": ["重要人物关系是否自然演化，有无跳变或回退"],
    "worldbuilding_consistency": ["设定、规则、势力、地点、时间线是否自洽"],
    "foreshadowing_continuity": ["伏笔、悬念、回收和遗留线的连续性问题"],
    "causal_chain_issues": ["因果链、巧合依赖、强行推进或战力/规则跳变"],
    "unresolved_threads": ["需要后续复核的重要未解问题"],
    "evidence": ["来自分块材料的短证据"],
    "fix_suggestions": ["连续性和一致性层面的改进建议"]
  },"""


def _outline_context_for_chunk(outline_data, chunk_char_offset, context_chapters=None, max_chars=None):
    """从大纲中提取当前 chunk 附近的章节上下文。"""
    if not outline_data or not outline_data.get("chapters"):
        return ""
    context_chapters = context_chapters or OUTLINE_INJECT_CONTEXT_CHAPTERS
    max_chars = max_chars or OUTLINE_INJECT_MAX_CHARS
    if max_chars <= 0:
        return ""
    chapters = outline_data["chapters"]
    current_idx = 0
    for i, ch in enumerate(chapters):
        if ch.get("start", 0) <= chunk_char_offset:
            current_idx = i
        else:
            break
    start_idx = max(0, current_idx - context_chapters)
    end_idx = min(len(chapters), current_idx + context_chapters + 1)
    context_chs = chapters[start_idx:end_idx]
    lines = [f"【大纲上下文（第{chapters[start_idx].get('index', '?')}-{chapters[end_idx-1].get('index', '?')}章）】"]
    used = len(lines[0])
    for ch in context_chs:
        title = ch.get("title", f"第{ch.get('index', '?')}章")
        tags = ch.get("tags", [])
        tag_str = f" [{','.join(tags)}]" if tags else ""
        head = ch.get("head_text", "")
        head_summary = f"：{head[:40]}…" if head else ""
        marker = " ◀当前" if ch.get("start", 0) <= chunk_char_offset < ch.get("end", 0) else ""
        line = f"  {ch.get('index', '?')}.{title}{tag_str}{head_summary}{marker}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    lines.append("（大纲仅供参考，不能替代原文证据）")
    return "\n".join(lines)


def _scan_chunk(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, density_profile=None, context_snapshot=None, entity_prescan=None, outline_block="", memory_block="") -> Dict[str, Any]:
    profile = profile or load_analysis_profile("general")
    density_profile = density_profile or _chunk_density_profile(text_chunk)
    rules_text = _profile_rules_text(profile)
    template_meta = prompt_template_metadata("general_scan_chunk")
    system_prompt = f"""你是{profile.display_name}助手。请从片段中抽取对整本小说分析有用的信息。

Prompt模板：{template_meta["name"]}@{template_meta["version"]}

关注范围：
- plot_events: 推动主线或支线的关键事件
- conflicts: 人物、阵营、目标、价值观或外部危机冲突
- worldbuilding: 世界观、时代背景、制度、科技/魔法/功法/历史设定
- themes: 反复出现的主题、价值观、情绪母题
- foreshadowing: 伏笔、悬念、未解决问题
- quality_notes: 节奏、逻辑、人物动机、爽点、虐点、亮点或明显问题

本 profile 的专项关注：
{_focus_text(profile)}

本 profile 的专项规则：
{rules_text}

当前片段密度：
{_density_instruction(density_profile)}
{_writing_quality_system_instruction()}
{_narrative_architecture_system_instruction()}
{_foreshadowing_engineering_system_instruction()}
{_semantic_layers_system_instruction()}
{_reader_experience_system_instruction()}
{_rolling_context_instruction(context_snapshot or {})}
{_entity_prescan_prompt_section(entity_prescan or [])}

要求：
1. 只根据片段内容输出，不要凭空补全。
2. 每条尽量短，保留可复核的具体信息。
3. specialty_notes 必须围绕专项规则记录命中点、疑点或亮点；若片段没有专项内容，输出空数组。
4. 输出 JSON 对象，不要 Markdown。"""
    outline_section = ""
    if outline_block:
        outline_section = f"\n{outline_block}\n"
    memory_section = ""
    if memory_block:
        memory_section = f"\n{memory_block}\n"
    user_prompt = f"""片段 {chunk_index + 1}/{total_chunks}：
{outline_section}{memory_section}
--- 开始 ---
{text_chunk}
--- 结束 ---

请输出：
{{
  "plot_events": ["..."],
  "conflicts": ["..."],
  "worldbuilding": ["..."],
  "themes": ["..."],
  "foreshadowing": ["..."],
  "quality_notes": ["..."],
  "specialty_notes": ["专项规则相关要点"]{_writing_quality_json_hint()}{_narrative_architecture_json_hint()}{_foreshadowing_engineering_json_hint()}{_semantic_layers_json_hint()}{_reader_experience_json_hint()}{_context_state_json_hint()},
  "one_sentence_summary": "本片段一句话概要"
}}"""
    data = _call_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2400 if density_profile.get("strategy") == "light" else 3800,
    )
    return {
        "chunk_index": chunk_index,
        "prompt_template": template_meta,
        "density_profile": density_profile,
        "chunk_hash": _chunk_text_hash(text_chunk),
        "plot_events": _safe_list(data.get("plot_events")),
        "conflicts": _safe_list(data.get("conflicts")),
        "worldbuilding": _safe_list(data.get("worldbuilding")),
        "themes": _safe_list(data.get("themes")),
        "foreshadowing": _safe_list(data.get("foreshadowing")),
        "quality_notes": _safe_list(data.get("quality_notes")),
        "specialty_notes": _safe_list(data.get("specialty_notes")),
        "writing_quality": _normalize_writing_quality(data.get("writing_quality"), source_text=text_chunk) if WRITING_QUALITY_ENABLED else {},
        "pacing_analysis": _normalize_pacing_analysis(data.get("pacing_analysis")) if WRITING_QUALITY_ENABLED else {},
        "information_density": _normalize_information_density(data.get("information_density")) if WRITING_QUALITY_ENABLED else {},
        "narrative_structure": _normalize_narrative_structure(data.get("narrative_structure")) if NARRATIVE_ARCHITECTURE_ENABLED else {},
        "outline_architecture": _normalize_outline_architecture(data.get("outline_architecture")) if NARRATIVE_ARCHITECTURE_ENABLED else {},
        "foreshadowing_engineering": _normalize_foreshadowing_engineering(data.get("foreshadowing_engineering")) if FORESHADOWING_ENGINEERING_ENABLED else {},
        "semantic_layers": _normalize_semantic_layers(data.get("semantic_layers")) if SEMANTIC_LAYERS_ENABLED else {},
        "reader_experience": _normalize_reader_experience(data.get("reader_experience")) if READER_EXPERIENCE_ENABLED else {},
        "context_snapshot_used": context_snapshot or {},
        "context_state_update": _normalize_context_state_update(data.get("context_state_update")) if ROLLING_CONTEXT_ENABLED else {},
        "one_sentence_summary": str(data.get("one_sentence_summary", "") or "").strip(),
    }


def _call_scan_chunk(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, density_profile=None, context_snapshot=None, entity_prescan=None) -> Dict[str, Any]:
    parameters = inspect.signature(_scan_chunk).parameters
    kwargs = {"profile": profile}
    if "density_profile" in parameters:
        kwargs["density_profile"] = density_profile
    if "context_snapshot" in parameters:
        kwargs["context_snapshot"] = context_snapshot
    if "entity_prescan" in parameters:
        kwargs["entity_prescan"] = entity_prescan
    return validate_general_chunk_result(_scan_chunk(text_chunk, chunk_index, total_chunks, **kwargs))


def _merge_partial_scan_results(results: List[Dict[str, Any]], chunk_index: int, reason: str) -> Dict[str, Any]:
    merged = {
        "chunk_index": chunk_index,
        "plot_events": [],
        "conflicts": [],
        "worldbuilding": [],
        "themes": [],
        "foreshadowing": [],
        "quality_notes": [],
        "specialty_notes": [],
        "writing_quality": {},
        "pacing_analysis": {},
        "information_density": {},
        "narrative_structure": {},
        "outline_architecture": {},
        "foreshadowing_engineering": {},
        "semantic_layers": {},
        "reader_experience": {},
        "one_sentence_summary": "",
        "partial_result": True,
        "partial_reason": reason,
        "partial_count": len(results),
        "context_snapshot_used": {},
        "context_state_update": {},
        "chunk_facts": {},
        "discarded_facts": [],
    }
    seen_by_field = {field: set() for field in (
        "plot_events",
        "conflicts",
        "worldbuilding",
        "themes",
        "foreshadowing",
        "quality_notes",
        "specialty_notes",
    )}
    summaries = []
    for result in results:
        if not isinstance(result, dict):
            continue
        for field, seen in seen_by_field.items():
            for item in _safe_list(result.get(field), limit=20):
                if item in seen:
                    continue
                seen.add(item)
                merged[field].append(item)
        summary = str(result.get("one_sentence_summary") or "").strip()
        if summary:
            summaries.append(summary)
        if not merged["context_snapshot_used"] and isinstance(result.get("context_snapshot_used"), dict):
            merged["context_snapshot_used"] = result.get("context_snapshot_used") or {}
        merged["discarded_facts"].extend(result.get("discarded_facts") or [])
        for object_field in (
            "writing_quality",
            "pacing_analysis",
            "information_density",
            "narrative_structure",
            "outline_architecture",
            "semantic_layers",
            "reader_experience",
        ):
            if not merged.get(object_field) and isinstance(result.get(object_field), dict):
                merged[object_field] = result.get(object_field) or {}
    if FORESHADOWING_ENGINEERING_ENABLED:
        merged["foreshadowing_engineering"] = _merge_foreshadowing_engineering_results(results)
    merged["one_sentence_summary"] = "；".join(summaries[:3])
    if ROLLING_CONTEXT_ENABLED:
        merged["context_state_update"] = _merged_context_state_update(results)
    return validate_general_chunk_result(merged)


def _split_text_for_downshift(text: str) -> List[str]:
    text = text or ""
    if len(text) < 2:
        return [text]
    midpoint = len(text) // 2
    candidates = [
        text.rfind("\n", 0, midpoint),
        text.find("\n", midpoint),
        text.rfind("。", 0, midpoint),
        text.find("。", midpoint),
    ]
    split_at = min(
        [pos for pos in candidates if 0 < pos < len(text) - 1],
        key=lambda pos: abs(pos - midpoint),
        default=midpoint,
    )
    return [text[:split_at].strip(), text[split_at:].strip()]


def _downshift_entity_prescan(entity_prescan, depth: int):
    items = list(entity_prescan or [])
    if depth <= 0:
        return items
    keep = max(5, ENTITY_PRESCAN_PROMPT_ITEMS // (2 ** depth))
    return items[:keep]


def _should_downshift_scan_chunk_error(error_type: str, error: Exception) -> bool:
    if error_type in {"context_overflow", "api_error", "timeout"}:
        return True
    if error_type != "parse_error":
        return False
    text = str(error or "")
    return any(marker in text for marker in (
        "truncated_json_response",
        "response_flags=",
        "json_unbalanced",
        "likely_truncated",
        "near_max_tokens_truncated",
        "code_fence_unclosed",
        "JSON解析失败",
        "unable to parse json",
        "Expecting ',' delimiter",
    ))


def _scan_chunk_downshifted(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, context_snapshot=None, entity_prescan=None, depth: int = 0) -> Dict[str, Any]:
    density_profile = _chunk_density_profile(text_chunk)
    if ROLLING_CONTEXT_ENABLED:
        context_limit = max(300, CONTEXT_MAX_CHARS // (2 ** max(0, depth)))
        context_snapshot = _trim_context_snapshot(context_snapshot or {}, max_chars=context_limit)
    else:
        context_snapshot = {}
    try:
        result = _call_scan_chunk(
            text_chunk,
            chunk_index,
            total_chunks,
            profile=profile,
            density_profile=density_profile,
            context_snapshot=context_snapshot,
            entity_prescan=_downshift_entity_prescan(entity_prescan, depth),
        )
        result.setdefault("density_profile", density_profile)
        if depth:
            result["downshift_depth"] = depth
        return result
    except Exception as exc:
        error_type = classify_scan_error(exc)
        if (
            not _should_downshift_scan_chunk_error(error_type, exc)
            or depth >= API_DOWNSHIFT_MAX_DEPTH
            or len(text_chunk or "") < 2
        ):
            raise
        parts = _split_text_for_downshift(text_chunk)
        partial_results = []
        fallback_context = _trim_context_snapshot(context_snapshot, max_chars=max(240, CONTEXT_MAX_CHARS // (2 ** (depth + 1)))) if ROLLING_CONTEXT_ENABLED else {}
        for part_index, part in enumerate(parts, 1):
            if not part.strip():
                continue
            result = _scan_chunk_downshifted(
                part,
                chunk_index,
                total_chunks,
                profile=profile,
                context_snapshot=fallback_context,
                entity_prescan=entity_prescan,
                depth=depth + 1,
            )
            result["partial_index"] = part_index
            partial_results.append(result)
        if not partial_results:
            raise
        reason = "context_overflow_split" if error_type == "context_overflow" else f"{error_type}_downshift_split"
        return _merge_partial_scan_results(partial_results, chunk_index, reason)


def _scan_chunk_with_context_overflow_fallback(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, context_snapshot=None, entity_prescan=None) -> Dict[str, Any]:
    return _scan_chunk_downshifted(
        text_chunk,
        chunk_index,
        total_chunks,
        profile=profile,
        context_snapshot=context_snapshot,
        entity_prescan=entity_prescan,
        depth=0,
    )


def _merge_items(chunk_results: List[Dict[str, Any]], key: str, limit: int = 80) -> List[str]:
    seen = set()
    out = []
    for item in chunk_results:
        for text in item.get(key, []) or []:
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                return out
    return out


def _summarize_book(book_name: str, chunk_results: List[Dict[str, Any]], profile=None, knowledge_base: Dict[str, Any] = None, raw_text: str = None) -> Dict[str, Any]:
    profile = profile or load_analysis_profile("general")
    knowledge_base = knowledge_base if isinstance(knowledge_base, dict) else _build_knowledge_base(chunk_results)
    def build_material(limit_scale: float = 1.0):
        summary_limit = max(30, int(120 * limit_scale))
        merge_limit = max(20, int(80 * limit_scale))
        compact_limit = max(20, int(40 * limit_scale))
        material_data = {
        "chunk_summaries": [
            {"chunk_index": x.get("chunk_index"), "summary": x.get("one_sentence_summary")}
            for x in chunk_results
            if x.get("one_sentence_summary")
        ][:summary_limit],
            "plot_events": _merge_items(chunk_results, "plot_events", limit=merge_limit),
            "conflicts": _merge_items(chunk_results, "conflicts", limit=merge_limit),
            "worldbuilding": _merge_items(chunk_results, "worldbuilding", limit=merge_limit),
            "themes": _merge_items(chunk_results, "themes", limit=merge_limit),
            "foreshadowing": _merge_items(chunk_results, "foreshadowing", limit=merge_limit),
            "quality_notes": _merge_items(chunk_results, "quality_notes", limit=merge_limit),
            "specialty_notes": _merge_items(chunk_results, "specialty_notes", limit=merge_limit),
            "knowledge_base": _compact_knowledge_base_for_summary(knowledge_base, limit=compact_limit),
        }
        if WRITING_QUALITY_ENABLED:
            material_data["writing_quality_chunks"] = _compact_writing_quality_for_summary(chunk_results, limit=max(30, int(80 * limit_scale)))
            material_data["zhihu_writing_insights_material"] = _compact_zhihu_writing_insights_for_summary(chunk_results, limit=max(30, int(80 * limit_scale)))
        if NARRATIVE_ARCHITECTURE_ENABLED:
            material_data["narrative_architecture_chunks"] = _compact_narrative_architecture_for_summary(chunk_results, limit=max(40, int(120 * limit_scale)))
        if FORESHADOWING_ENGINEERING_ENABLED:
            material_data["foreshadowing_engineering_chunks"] = _compact_foreshadowing_engineering_for_summary(chunk_results, limit=max(40, int(120 * limit_scale)))
        if SEMANTIC_LAYERS_ENABLED:
            material_data["semantic_layers_chunks"] = _compact_semantic_layers_for_summary(chunk_results, limit=max(40, int(120 * limit_scale)))
        if READER_EXPERIENCE_ENABLED:
            material_data["reader_experience_chunks"] = _compact_reader_experience_for_summary(chunk_results, limit=max(40, int(120 * limit_scale)))
        if ROLLING_CONTEXT_ENABLED:
            material_data["rolling_context_timeline"] = _compact_rolling_context_timeline(chunk_results, limit=max(40, int(120 * limit_scale)))
        if CONTINUITY_AUDIT_ENABLED:
            material_data["continuity_audit_material"] = _compact_continuity_for_summary(chunk_results, limit=max(40, int(120 * limit_scale)))
        return material_data

    material = build_material()
    base_summary_fields = {
        "main_plot",
        "core_conflicts",
        "worldbuilding",
        "themes",
        "foreshadowing_and_payoff",
        "writing_quality_overall",
        "pacing_analysis_overall",
        "information_density_audit",
        "water_chapter_analysis",
        "zhihu_writing_insights_overall",
        "narrative_structure_analysis",
        "outline_architecture_overall",
        "foreshadowing_engineering_analysis",
        "semantic_layers_analysis",
        "reader_experience_analysis",
        "continuity_audit_analysis",
        "strengths",
        "risks_or_issues",
    }
    specialty_fields = [
        x for x in profile.summary_fields
        if not (set(_summary_field_candidates(x)) & base_summary_fields)
    ]
    specialty_json_hint = ""
    if specialty_fields:
        specialty_json_hint = "\n".join(
            f'  "{field}": ["{_summary_field_label(field)}专项分析要点"],'
            for field in specialty_fields
        )
    rules_text = _profile_rules_text(profile)
    template_meta = prompt_template_metadata("general_summary")

    system_prompt = f"""你是{profile.display_name}总评分析师。请基于分块抽取结果，形成整本书的分析结论。

Prompt模板：{template_meta["name"]}@{template_meta["version"]}

本 profile 的专项规则：
{rules_text}

输出必须是 JSON 对象。不要使用后宫、初处、漏女、排雷等专用标准。
请优先参考 knowledge_base 中的实体、关系、设定、伏笔和事件时间线来形成全局判断；分块摘要只作为补充证据，不要忽略知识库中持续出现的开放线索。
开启叙事架构分析时，请基于 narrative_architecture_chunks 判断整书结构模式、阶段转折、因果链、成长曲线和大纲风险；不要把单个片段孤证当成整书结论。
开启伏笔工程追踪时，请结合 foreshadowing_engineering_chunks 和 rolling_context_timeline 判断伏笔设置、活跃线索、回收质量、烟雾弹和风险；不要把普通未完成剧情目标都算作伏笔。
开启深层语义分析时，请基于 semantic_layers_chunks 归纳事实层、意图层、效果层和技法层的稳定模式；潜台词/反讽必须来自分块证据，不要强行拔高主题。
开启读者体验分析时，请基于 reader_experience_chunks 判断投入度曲线、爽点/燃点/甜点/解谜满足、期待管理和体验风险；不要把单个片段的挫败点扩大成整书结论。
开启连续性审计时，请基于 continuity_audit_material 和 rolling_context_timeline 检查人物关系、世界观设定、伏笔回收、因果链、战力/规则是否前后自洽；只能标记有分块证据支持的风险，不要把“尚未完结”本身判为错误。
开启滚动上下文时，请基于 rolling_context_timeline 理解全书阶段推进、人物关系延续、未解问题和回收情况；不要要求或引用 context_snapshot_used 这类逐块内部快照。"""
    if WRITING_QUALITY_ENABLED:
        system_prompt += "\n开启知乎文笔洞察时，请结合 zhihu_writing_insights_material 中的确定性模板词统计和分块AI判断，归纳词穷症状、读者推导空间、信息传播效率、风格辨识度与情感真实度；模板词次数必须优先服从材料里的程序统计。"
    user_prompt = f"""书名：{book_name}

分块材料：
{json.dumps(material, ensure_ascii=False, indent=2)}

请输出：
{{
  "story_overview": "整本书概览，100-200字",
  "main_plot": ["主线剧情要点"],
  "core_conflicts": ["核心冲突"],
  "worldbuilding": ["世界观/设定要点"],
  "themes": ["主题表达"],
  "foreshadowing_and_payoff": ["伏笔、悬念、回收情况"],
{specialty_json_hint}{_narrative_architecture_summary_json_hint()}{_foreshadowing_engineering_summary_json_hint()}{_semantic_layers_summary_json_hint()}{_reader_experience_summary_json_hint()}{_continuity_audit_summary_json_hint()}{_writing_quality_summary_json_hint()}{_zhihu_writing_insights_summary_json_hint()}
  "strengths": ["作品优点"],
  "risks_or_issues": ["可能的问题或阅读门槛"],
  "reader_fit": "适合什么读者",
  "overall_assessment": "总体评价",
  "radar_scores": {{
    "plot": {{"score": 0-10, "reason": "剧情质量评分依据"}},
    "characters": {{"score": 0-10, "reason": "人物塑造评分依据"}},
    "worldbuilding": {{"score": 0-10, "reason": "世界观评分依据"}},
    "pacing": {{"score": 0-10, "reason": "节奏把控评分依据"}},
    "writing": {{"score": 0-10, "reason": "文笔水准评分依据"}},
    "emotion": {{"score": 0-10, "reason": "情绪调动评分依据"}}
  }}
}}"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        data = _call_json(messages, max_tokens=5200)
    except Exception as exc:
        error_type = classify_scan_error(exc)
        if error_type not in {"context_overflow", "api_error", "timeout"}:
            raise
        compact_material = build_material(limit_scale=0.45)
        compact_prompt = f"""书名：{book_name}

分块材料（已降载压缩，用于规避接口 504/上下文过载）：
{json.dumps(compact_material, ensure_ascii=False, indent=2)}

请按原要求输出同结构 JSON。"""
        data = _call_json(
            [
                {"role": "system", "content": system_prompt + "\n当前为降载重试，请优先保留高置信事实和主要风险。"},
                {"role": "user", "content": compact_prompt},
            ],
            max_tokens=3800,
        )
    summary = {
        "prompt_template": template_meta,
        "story_overview": _summary_field_text(data, "story_overview"),
        "main_plot": _summary_field_value(data, "main_plot"),
        "core_conflicts": _summary_field_value(data, "core_conflicts"),
        "worldbuilding": _summary_field_value(data, "worldbuilding"),
        "themes": _summary_field_value(data, "themes"),
        "foreshadowing_and_payoff": _summary_field_value(data, "foreshadowing_and_payoff"),
        "writing_quality_overall": _normalize_object_summary(data.get("writing_quality_overall")),
        "pacing_analysis_overall": _normalize_object_summary(data.get("pacing_analysis_overall")),
        "information_density_audit": _normalize_object_summary(data.get("information_density_audit")),
        "water_chapter_analysis": _summary_field_value(data, "water_chapter_analysis"),
        "zhihu_writing_insights_overall": _normalize_object_summary(data.get("zhihu_writing_insights_overall")),
        "narrative_structure_analysis": _normalize_object_summary(data.get("narrative_structure_analysis")),
        "outline_architecture_overall": _normalize_object_summary(data.get("outline_architecture_overall")),
        "foreshadowing_engineering_analysis": _normalize_object_summary(data.get("foreshadowing_engineering_analysis")),
        "semantic_layers_analysis": _normalize_object_summary(data.get("semantic_layers_analysis")),
        "reader_experience_analysis": _normalize_object_summary(data.get("reader_experience_analysis")),
        "continuity_audit_analysis": _normalize_object_summary(data.get("continuity_audit_analysis")),
        "strengths": _summary_field_value(data, "strengths"),
        "risks_or_issues": _summary_field_value(data, "risks_or_issues"),
        "reader_fit": "；".join(_summary_field_value(data, "reader_fit")),
        "overall_assessment": "；".join(_summary_field_value(data, "overall_assessment")),
        "radar_scores": _normalize_radar_scores(data.get("radar_scores")),
    }

    # 聚合阅读体验量化指标
    if READING_METRICS_ENABLED:
        chunk_score_data = extract_chunk_scores(chunk_results)
        reading_metrics = aggregate_metrics(chunk_score_data)
        summary["reading_metrics"] = reading_metrics
        summary["reading_metrics_report"] = render_reading_experience_report(reading_metrics, chunk_score_data)
    # 文学质感量化（纯规则统计）
    if LITERARY_METRICS_ENABLED and raw_text:
        literary_m = _compute_literary_metrics(raw_text)
        summary["literary_metrics"] = literary_m
    for field in specialty_fields:
        summary[field] = _summary_field_value(data, field)
    return summary


def main(novel_path=None, book_name=None, run_id=None, detail_path=None, profile_override=None):
    base = get_base_dir()
    if novel_path:
        os.environ["NOVEL_PATH"] = novel_path
    novel_file = novel_path or os.environ.get("NOVEL_PATH", os.path.join(base, "novels", "default.txt"))
    clean_name = (book_name or os.path.splitext(os.path.basename(novel_file))[0]).strip()
    profile = profile_override or load_analysis_profile(os.environ.get("ANALYSIS_PROFILE", "general"))
    init_token_tracker(clean_name, run_id=run_id, out_path=os.path.join(base, "results", "token_usage.json"))

    results_dir = os.path.join(base, "results")
    latest_file = _latest_summary_path(results_dir, clean_name, profile.name)
    latest_data = _read_json(latest_file)
    if _is_fresh_summary(latest_data, novel_file, profile.name):
        print(f"★ 通用扫描已是最新，复用: {latest_file}")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_dir = os.path.join(results_dir, f"{clean_name}_{profile.name}_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    text = _read_novel(novel_file)
    entity_prescan = _entity_prescan_candidates(text) if ENTITY_PRESCAN_ENABLED else []
    if ENTITY_PRESCAN_ENABLED:
        print(f"★ 实体预扫描候选 {len(entity_prescan)} 个（仅作为片段抽取提示）")
    manifest = build_semantic_chunk_manifest(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP) if os.environ.get("SEMANTIC_CHUNK_ENABLED", "1").strip() == "1" else build_chunk_manifest(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    save_chunk_manifest(manifest, os.path.join(output_dir, "chunk_manifest.json"))

    # 大纲预扫描
    outline_data = None
    chunk_outline_blocks = {}
    if OUTLINE_INJECT_ENABLED:
        outline_path = os.path.join(output_dir, "outline.json")
        text_sig = outline_signature(text)
        outline_data = load_outline(outline_path)
        if outline_data and outline_data.get("text_signature") == text_sig:
            print(f"  📖 大纲已存在，复用（{outline_data.get('chapter_count', 0)} 章）")
        else:
            outline_data = generate_outline(text)
            outline_data["text_signature"] = text_sig
            save_outline(outline_data, outline_path)
            print(f"  📖 大纲预扫描完成（{outline_data.get('chapter_count', 0)} 章）")
        if outline_data and outline_data.get("chapters"):
            chunk_entries = manifest.get("chunks", [])
            for entry in chunk_entries:
                ci = entry.get("chunk_index", 0)
                core_start = entry.get("core_start", 0)
                chunk_outline_blocks[ci] = _outline_context_for_chunk(outline_data, core_start)
    source_chunk_entries = list(manifest.get("chunks", []) or [])
    source_chunk_count = len(source_chunk_entries)
    effective_max_chunks = _effective_max_chunks(manifest.get("text_length") or len(text))
    selected_chunk_entries = _sample_chunk_entries_for_budget(source_chunk_entries, effective_max_chunks)

    # 两阶段自适应采样：先快速扫判断价值，再筛选
    quick_scan_results = {}
    if TWO_STAGE_SAMPLING_ENABLED and len(selected_chunk_entries) > 30:
        print(f"  🔍 两阶段采样：对 {len(selected_chunk_entries)} 个片段做快速价值评估...")
        for entry in tqdm(selected_chunk_entries, desc="快速评估"):
            ci = int(entry.get("chunk_index", 0))
            chunk_text = entry.get("text", "")
            quick_result = _quick_scan_for_value(chunk_text, ci, len(selected_chunk_entries), profile)
            quick_scan_results[ci] = quick_result

        # 筛选：保留高价值 + 保留时间线均匀采样（至少每 20 个 chunk 保留 1 个）
        high_value_indices = set()
        for ci, qr in quick_scan_results.items():
            if qr.get("has_high_value") or qr.get("value_score", 0) >= TWO_STAGE_HIGH_VALUE_THRESHOLD:
                high_value_indices.add(ci)

        # 均匀采样保底
        total_selected = len(selected_chunk_entries)
        min_uniform = max(10, total_selected // 20)
        uniform_indices = set()
        step = max(1, total_selected // min_uniform)
        for i in range(0, total_selected, step):
            ci = int(selected_chunk_entries[i].get("chunk_index", 0))
            uniform_indices.add(ci)

        keep_indices = high_value_indices | uniform_indices
        selected_chunk_entries = [e for e in selected_chunk_entries if int(e.get("chunk_index", 0)) in keep_indices]
        print(f"  🔍 两阶段采样：保留 {len(selected_chunk_entries)} 个高价值/保底片段")
    chunks = [x.get("text", "") for x in selected_chunk_entries]
    selected_original_indices = [
        int(x.get("chunk_index", idx + 1))
        for idx, x in enumerate(selected_chunk_entries)
    ]
    if effective_max_chunks > 0:
        if source_chunk_count <= effective_max_chunks:
            sampling_strategy = "full"
        elif CONTENT_AWARE_SAMPLING:
            sampling_strategy = "content_aware_timeline"
        else:
            sampling_strategy = "uniform_timeline"
    else:
        sampling_strategy = "full"

    print(f"★ {profile.display_name}：{clean_name}，共 {len(chunks)} 个片段（原始 {source_chunk_count} 个，策略={sampling_strategy}）")
    reusable_results = {}
    if INCREMENTAL_REUSE and _summary_can_reuse_chunk_results(latest_data, profile.name):
        reusable_results = _reusable_chunk_result_map(latest_data)
    chunk_results = []
    failed = []
    reused_chunk_count = 0
    scanned_chunk_count = 0
    rolling_context_state = _empty_rolling_context_state()
    scan_memory = ScanMemory()
    for idx, chunk in enumerate(tqdm(chunks, desc="通用扫描")):
        original_chunk_index = selected_original_indices[idx] if idx < len(selected_original_indices) else idx + 1
        chunk_hash = _chunk_text_hash(chunk)
        density_profile = _chunk_density_profile(chunk)
        context_snapshot = {}
        if ROLLING_CONTEXT_ENABLED:
            context_snapshot = _rolling_context_snapshot(rolling_context_state)
            if sampling_strategy != "full":
                context_snapshot = dict(context_snapshot)
                context_snapshot.update({
                    "sampled_context": True,
                    "source_chunk_count": source_chunk_count,
                    "current_original_chunk_index": original_chunk_index,
                    "sampling_note": (
                        "当前扫描为全书内容感知抽样，前序上下文来自已扫描样本，不代表原文连续章节。"
                        if sampling_strategy == "content_aware_timeline"
                        else "当前扫描为全书均匀抽样，前序上下文来自已扫描样本，不代表原文连续章节。"
                    ),
                })
            context_snapshot = _trim_context_snapshot(context_snapshot)
        reusable_result = reusable_results.get(chunk_hash)
        if reusable_result:
            result = _copy_reused_chunk_result(
                reusable_result,
                idx,
                original_chunk_index,
                chunk_hash,
                density_profile,
            )
            if ROLLING_CONTEXT_ENABLED:
                result["context_snapshot_used"] = context_snapshot
                rolling_context_state = _update_rolling_context_state(rolling_context_state, result)
            chunk_results.append(result)
            reused_chunk_count += 1
            continue
        try:
            result = _scan_chunk_with_context_overflow_fallback(
                chunk,
                original_chunk_index - 1,
                source_chunk_count or len(chunks),
                profile=profile,
                context_snapshot=context_snapshot,
                entity_prescan=entity_prescan,
            )
            result["sample_index"] = idx
            result["original_chunk_index"] = original_chunk_index
            result["chunk_hash"] = chunk_hash
            result.setdefault("density_profile", density_profile)
            if ROLLING_CONTEXT_ENABLED:
                result.setdefault("context_snapshot_used", context_snapshot)
                rolling_context_state = _update_rolling_context_state(rolling_context_state, result)
            chunk_results.append(result)
            scanned_chunk_count += 1
        except Exception as exc:
            failed.append({
                "chunk_index": original_chunk_index - 1,
                "error": str(exc),
                "error_type": classify_scan_error(exc),
            })

    raw_knowledge_base = _build_knowledge_base(chunk_results) if chunk_results else {}
    knowledge_base = raw_knowledge_base
    knowledge_base_llm_merge_error = ""
    knowledge_base_llm_merge_applied = False
    if chunk_results and KNOWLEDGE_BASE_LLM_MERGE_ENABLED:
        try:
            merged_knowledge_base = _merge_knowledge_base_with_llm(clean_name, raw_knowledge_base, profile=profile)
            if isinstance(merged_knowledge_base, dict):
                knowledge_base = merged_knowledge_base
                knowledge_base_llm_merge_applied = True
        except Exception as exc:
            knowledge_base_llm_merge_error = str(exc) or exc.__class__.__name__
    summary_reused = False
    if (
        chunk_results
        and reused_chunk_count == len(chunk_results)
        and scanned_chunk_count == 0
        and not failed
        and _summary_can_reuse_overall(latest_data, profile.name)
    ):
        summary = json.loads(json.dumps(latest_data.get("summary") or {}, ensure_ascii=False))
        summary_reused = True
    else:
        summary = _summarize_book(clean_name, chunk_results, profile=profile, knowledge_base=knowledge_base, raw_text=text) if chunk_results else {}
    density_counts = {"low": 0, "medium": 0, "high": 0}
    for item in chunk_results:
        level = ((item.get("density_profile") or {}).get("level") or "medium")
        if level not in density_counts:
            level = "medium"
        density_counts[level] += 1
    failed_chunk_count = len(failed)
    attempted_chunk_count = len(chunks)
    successful_chunk_count = len(chunk_results)
    failed_chunk_ratio = (failed_chunk_count / attempted_chunk_count) if attempted_chunk_count else 0.0
    scan_coverage_ratio = (successful_chunk_count / attempted_chunk_count) if attempted_chunk_count else 0.0
    partial_scan = failed_chunk_count > 0
    out = {
        "schema_version": 1,
        "analysis_profile": "general",
        "specialty_profile": profile.name,
        "profile_display_name": profile.display_name,
        "scan_focus": profile.scan_focus,
        "summary_fields": profile.summary_fields,
        "book_name": clean_name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "novel_path": novel_file,
        "novel_mtime": _novel_mtime(novel_file),
        "novel_signature": _novel_file_signature(novel_file),
        "detail_path": detail_path,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "max_chunks": effective_max_chunks,
        "base_max_chunks": MAX_CHUNKS,
        "text_length": manifest.get("text_length") or len(text),
        "source_chunk_count": source_chunk_count,
        "chunk_count": len(chunks),
        "chunk_sampling_strategy": sampling_strategy,
        "sampled_chunk_indices": selected_original_indices,
        "smart_density": SMART_DENSITY,
        "content_aware_sampling": CONTENT_AWARE_SAMPLING,
        "content_aware_sampling_schema_version": CONTENT_AWARE_SAMPLING_SCHEMA_VERSION if CONTENT_AWARE_SAMPLING else None,
        "writing_quality_enabled": WRITING_QUALITY_ENABLED,
        "zhihu_writing_insights_schema_version": ZHIHU_WRITING_INSIGHTS_SCHEMA_VERSION if WRITING_QUALITY_ENABLED else None,
        "narrative_architecture_enabled": NARRATIVE_ARCHITECTURE_ENABLED,
        "rolling_context_enabled": ROLLING_CONTEXT_ENABLED,
        "rolling_context_schema_version": ROLLING_CONTEXT_SCHEMA_VERSION if ROLLING_CONTEXT_ENABLED else None,
        "rolling_context_max_chars": CONTEXT_MAX_CHARS if ROLLING_CONTEXT_ENABLED else 0,
        "rolling_context_state": rolling_context_state if ROLLING_CONTEXT_ENABLED else {},
        "rolling_context_timeline_count": len(_compact_rolling_context_timeline(chunk_results)) if ROLLING_CONTEXT_ENABLED else 0,
        "foreshadowing_engineering_enabled": FORESHADOWING_ENGINEERING_ENABLED,
        "foreshadowing_engineering_schema_version": FORESHADOWING_ENGINEERING_SCHEMA_VERSION if FORESHADOWING_ENGINEERING_ENABLED else None,
        "foreshadowing_engineering_timeline_count": len(_compact_foreshadowing_engineering_for_summary(chunk_results)) if FORESHADOWING_ENGINEERING_ENABLED else 0,
        "semantic_layers_enabled": SEMANTIC_LAYERS_ENABLED,
        "semantic_layers_schema_version": SEMANTIC_LAYERS_SCHEMA_VERSION if SEMANTIC_LAYERS_ENABLED else None,
        "semantic_layers_timeline_count": len(_compact_semantic_layers_for_summary(chunk_results)) if SEMANTIC_LAYERS_ENABLED else 0,
        "reader_experience_enabled": READER_EXPERIENCE_ENABLED,
        "reader_experience_schema_version": READER_EXPERIENCE_SCHEMA_VERSION if READER_EXPERIENCE_ENABLED else None,
        "reader_experience_timeline_count": len(_compact_reader_experience_for_summary(chunk_results)) if READER_EXPERIENCE_ENABLED else 0,
        "continuity_audit_enabled": CONTINUITY_AUDIT_ENABLED,
        "continuity_audit_schema_version": CONTINUITY_AUDIT_SCHEMA_VERSION if CONTINUITY_AUDIT_ENABLED else None,
        "continuity_audit_timeline_count": len(_compact_continuity_for_summary(chunk_results)) if CONTINUITY_AUDIT_ENABLED else 0,
        "entity_prescan_enabled": ENTITY_PRESCAN_ENABLED,
        "entity_prescan_schema_version": ENTITY_PRESCAN_SCHEMA_VERSION if ENTITY_PRESCAN_ENABLED else None,
        "entity_prescan_max_chars": ENTITY_PRESCAN_MAX_CHARS if ENTITY_PRESCAN_ENABLED else 0,
        "entity_prescan_count": len(entity_prescan),
        "entity_prescan_type_counts": _entity_prescan_type_counts(entity_prescan),
        "entity_prescan": entity_prescan,
        "knowledge_base_enabled": True,
        "knowledge_base_schema_version": KNOWLEDGE_BASE_SCHEMA_VERSION,
        "knowledge_base_llm_merge_enabled": KNOWLEDGE_BASE_LLM_MERGE_ENABLED,
        "knowledge_base_llm_merge_applied": knowledge_base_llm_merge_applied,
        "knowledge_base_llm_merge_error": knowledge_base_llm_merge_error,
        "raw_knowledge_base_counts": _knowledge_base_counts(raw_knowledge_base),
        "density_counts": density_counts,
        "incremental_reuse": INCREMENTAL_REUSE,
        "summary_reused": summary_reused,
        "reused_chunk_count": reused_chunk_count,
        "scanned_chunk_count": scanned_chunk_count,
        "successful_chunk_count": successful_chunk_count,
        "attempted_chunk_count": attempted_chunk_count,
        "partial_scan": partial_scan,
        "failed_chunk_count": failed_chunk_count,
        "failed_chunk_ratio": round(failed_chunk_ratio, 6),
        "scan_coverage_ratio": round(scan_coverage_ratio, 6),
        "prompt_templates": prompt_templates_metadata("general_scan_chunk", "general_summary"),
        "failed_chunks": failed,
        "knowledge_base": knowledge_base,
        "knowledge_base_counts": _knowledge_base_counts(knowledge_base),
        "chunk_results": chunk_results,
        "summary": summary,
        "literary_metrics": summary.get("literary_metrics"),
    }
    out_file = os.path.join(output_dir, "GENERAL_SUMMARY.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"★ 通用扫描结果: {out_file}")
    return 0


if __name__ == "__main__":
    main()
