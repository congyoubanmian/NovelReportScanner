import json
import os
import hashlib
import inspect
from datetime import datetime
from typing import Any, Dict, List

from tqdm import tqdm

from analysis_profiles import load_analysis_profile
from prompt_templates import prompt_template_metadata, prompt_templates_metadata
from shared_utils import MODEL, chat_completion, get_base_dir, init_token_tracker, is_context_overflow_error, read_file_safely, record_usage
from shared_utils import call_json_chat_completion_with_fallback
from text_anchor import build_chunk_manifest, save_chunk_manifest


CHUNK_SIZE = int(os.environ.get("GENERAL_SCAN_CHUNK_SIZE", "12000"))
CHUNK_OVERLAP = int(os.environ.get("GENERAL_SCAN_CHUNK_OVERLAP", "1000"))
MAX_CHUNKS = int(os.environ.get("GENERAL_SCAN_MAX_CHUNKS", "80"))
SMART_DENSITY = os.environ.get("GENERAL_SCAN_SMART_DENSITY", "1").strip() == "1"
CONTENT_AWARE_SAMPLING = os.environ.get("GENERAL_SCAN_CONTENT_AWARE_SAMPLING", "1").strip() == "1"
CONTENT_AWARE_SAMPLING_SCHEMA_VERSION = 1
INCREMENTAL_REUSE = os.environ.get("GENERAL_SCAN_INCREMENTAL_REUSE", "1").strip() == "1"
WRITING_QUALITY_ENABLED = os.environ.get("GENERAL_SCAN_WRITING_QUALITY", "1").strip() == "1"
NARRATIVE_ARCHITECTURE_ENABLED = os.environ.get("GENERAL_SCAN_NARRATIVE_ARCHITECTURE", "1").strip() == "1"
ROLLING_CONTEXT_ENABLED = os.environ.get("GENERAL_SCAN_ROLLING_CONTEXT", "1").strip() == "1"
CONTEXT_MAX_CHARS = int(os.environ.get("GENERAL_SCAN_CONTEXT_MAX_CHARS", "1600"))
ROLLING_CONTEXT_SCHEMA_VERSION = 1
FORESHADOWING_ENGINEERING_ENABLED = os.environ.get("GENERAL_SCAN_FORESHADOWING_ENGINEERING", "1").strip() == "1"
FORESHADOWING_ENGINEERING_SCHEMA_VERSION = 1
SEMANTIC_LAYERS_ENABLED = os.environ.get("GENERAL_SCAN_SEMANTIC_LAYERS", "1").strip() == "1"
SEMANTIC_LAYERS_SCHEMA_VERSION = 1
READER_EXPERIENCE_ENABLED = os.environ.get("GENERAL_SCAN_READER_EXPERIENCE", "1").strip() == "1"
READER_EXPERIENCE_SCHEMA_VERSION = 1
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
    if data.get("narrative_architecture_enabled") not in {None, NARRATIVE_ARCHITECTURE_ENABLED}:
        return False
    if data.get("foreshadowing_engineering_enabled") not in {None, FORESHADOWING_ENGINEERING_ENABLED}:
        return False
    if data.get("semantic_layers_enabled") not in {None, SEMANTIC_LAYERS_ENABLED}:
        return False
    if data.get("reader_experience_enabled") not in {None, READER_EXPERIENCE_ENABLED}:
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
        if WRITING_QUALITY_ENABLED and not (
            item.get("writing_quality") and item.get("pacing_analysis") and item.get("information_density")
        ):
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


def _copy_reused_chunk_result(result: Dict[str, Any], sample_index: int, original_chunk_index: int, chunk_hash: str, density_profile: Dict[str, Any]) -> Dict[str, Any]:
    copied = json.loads(json.dumps(result, ensure_ascii=False))
    copied["sample_index"] = sample_index
    copied["original_chunk_index"] = original_chunk_index
    copied["chunk_index"] = original_chunk_index - 1
    copied["chunk_hash"] = chunk_hash
    copied["density_profile"] = density_profile
    copied["reused_from_previous"] = True
    return copied


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


def _normalize_writing_quality(value: Any) -> Dict[str, Any]:
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
        if len(compact) >= limit:
            break
    return compact


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
        if len(compact) >= limit:
            break
    return compact


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
        if len(compact) >= limit:
            break
    return compact


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
        if len(compact) >= limit:
            break
    return compact


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
        if len(compact) >= limit:
            break
    return compact


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


def _scan_chunk(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, density_profile=None, context_snapshot=None) -> Dict[str, Any]:
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

要求：
1. 只根据片段内容输出，不要凭空补全。
2. 每条尽量短，保留可复核的具体信息。
3. specialty_notes 必须围绕专项规则记录命中点、疑点或亮点；若片段没有专项内容，输出空数组。
4. 输出 JSON 对象，不要 Markdown。"""
    user_prompt = f"""片段 {chunk_index + 1}/{total_chunks}：

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
        "writing_quality": _normalize_writing_quality(data.get("writing_quality")) if WRITING_QUALITY_ENABLED else {},
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


def _call_scan_chunk(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, density_profile=None, context_snapshot=None) -> Dict[str, Any]:
    parameters = inspect.signature(_scan_chunk).parameters
    kwargs = {"profile": profile}
    if "density_profile" in parameters:
        kwargs["density_profile"] = density_profile
    if "context_snapshot" in parameters:
        kwargs["context_snapshot"] = context_snapshot
    return _scan_chunk(text_chunk, chunk_index, total_chunks, **kwargs)


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
    return merged


def _scan_chunk_with_context_overflow_fallback(text_chunk: str, chunk_index: int, total_chunks: int, profile=None, context_snapshot=None) -> Dict[str, Any]:
    density_profile = _chunk_density_profile(text_chunk)
    context_snapshot = _trim_context_snapshot(context_snapshot or {}) if ROLLING_CONTEXT_ENABLED else {}
    try:
        result = _call_scan_chunk(
            text_chunk,
            chunk_index,
            total_chunks,
            profile=profile,
            density_profile=density_profile,
            context_snapshot=context_snapshot,
        )
        result.setdefault("density_profile", density_profile)
        return result
    except Exception as exc:
        if not is_context_overflow_error(exc) or len(text_chunk or "") < 2:
            raise
        midpoint = max(1, len(text_chunk) // 2)
        parts = [text_chunk[:midpoint], text_chunk[midpoint:]]
        partial_results = []
        fallback_context = _trim_context_snapshot(context_snapshot, max(400, CONTEXT_MAX_CHARS // 2)) if ROLLING_CONTEXT_ENABLED else {}
        for part_index, part in enumerate(parts, 1):
            if not part.strip():
                continue
            result = _call_scan_chunk(
                part,
                chunk_index,
                total_chunks,
                profile=profile,
                density_profile=_chunk_density_profile(part),
                context_snapshot=fallback_context,
            )
            result["partial_index"] = part_index
            partial_results.append(result)
        if not partial_results:
            raise
        return _merge_partial_scan_results(partial_results, chunk_index, "context_overflow_split")


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


def _summarize_book(book_name: str, chunk_results: List[Dict[str, Any]], profile=None) -> Dict[str, Any]:
    profile = profile or load_analysis_profile("general")
    material = {
        "chunk_summaries": [
            {"chunk_index": x.get("chunk_index"), "summary": x.get("one_sentence_summary")}
            for x in chunk_results
            if x.get("one_sentence_summary")
        ][:120],
        "plot_events": _merge_items(chunk_results, "plot_events"),
        "conflicts": _merge_items(chunk_results, "conflicts"),
        "worldbuilding": _merge_items(chunk_results, "worldbuilding"),
        "themes": _merge_items(chunk_results, "themes"),
        "foreshadowing": _merge_items(chunk_results, "foreshadowing"),
        "quality_notes": _merge_items(chunk_results, "quality_notes"),
        "specialty_notes": _merge_items(chunk_results, "specialty_notes"),
    }
    if WRITING_QUALITY_ENABLED:
        material["writing_quality_chunks"] = _compact_writing_quality_for_summary(chunk_results)
    if NARRATIVE_ARCHITECTURE_ENABLED:
        material["narrative_architecture_chunks"] = _compact_narrative_architecture_for_summary(chunk_results)
    if FORESHADOWING_ENGINEERING_ENABLED:
        material["foreshadowing_engineering_chunks"] = _compact_foreshadowing_engineering_for_summary(chunk_results)
    if SEMANTIC_LAYERS_ENABLED:
        material["semantic_layers_chunks"] = _compact_semantic_layers_for_summary(chunk_results)
    if READER_EXPERIENCE_ENABLED:
        material["reader_experience_chunks"] = _compact_reader_experience_for_summary(chunk_results)
    if ROLLING_CONTEXT_ENABLED:
        material["rolling_context_timeline"] = _compact_rolling_context_timeline(chunk_results)
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
        "narrative_structure_analysis",
        "outline_architecture_overall",
        "foreshadowing_engineering_analysis",
        "semantic_layers_analysis",
        "reader_experience_analysis",
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
开启叙事架构分析时，请基于 narrative_architecture_chunks 判断整书结构模式、阶段转折、因果链、成长曲线和大纲风险；不要把单个片段孤证当成整书结论。
开启伏笔工程追踪时，请结合 foreshadowing_engineering_chunks 和 rolling_context_timeline 判断伏笔设置、活跃线索、回收质量、烟雾弹和风险；不要把普通未完成剧情目标都算作伏笔。
开启深层语义分析时，请基于 semantic_layers_chunks 归纳事实层、意图层、效果层和技法层的稳定模式；潜台词/反讽必须来自分块证据，不要强行拔高主题。
开启读者体验分析时，请基于 reader_experience_chunks 判断投入度曲线、爽点/燃点/甜点/解谜满足、期待管理和体验风险；不要把单个片段的挫败点扩大成整书结论。
开启滚动上下文时，请基于 rolling_context_timeline 理解全书阶段推进、人物关系延续、未解问题和回收情况；不要要求或引用 context_snapshot_used 这类逐块内部快照。"""
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
{specialty_json_hint}{_narrative_architecture_summary_json_hint()}{_foreshadowing_engineering_summary_json_hint()}{_semantic_layers_summary_json_hint()}{_reader_experience_summary_json_hint()}{_writing_quality_summary_json_hint()}
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
    data = _call_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=5200,
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
        "narrative_structure_analysis": _normalize_object_summary(data.get("narrative_structure_analysis")),
        "outline_architecture_overall": _normalize_object_summary(data.get("outline_architecture_overall")),
        "foreshadowing_engineering_analysis": _normalize_object_summary(data.get("foreshadowing_engineering_analysis")),
        "semantic_layers_analysis": _normalize_object_summary(data.get("semantic_layers_analysis")),
        "reader_experience_analysis": _normalize_object_summary(data.get("reader_experience_analysis")),
        "strengths": _summary_field_value(data, "strengths"),
        "risks_or_issues": _summary_field_value(data, "risks_or_issues"),
        "reader_fit": "；".join(_summary_field_value(data, "reader_fit")),
        "overall_assessment": "；".join(_summary_field_value(data, "overall_assessment")),
        "radar_scores": _normalize_radar_scores(data.get("radar_scores")),
    }
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
    manifest = build_chunk_manifest(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    save_chunk_manifest(manifest, os.path.join(output_dir, "chunk_manifest.json"))
    source_chunk_entries = list(manifest.get("chunks", []) or [])
    source_chunk_count = len(source_chunk_entries)
    effective_max_chunks = _effective_max_chunks(manifest.get("text_length") or len(text))
    selected_chunk_entries = _sample_chunk_entries_for_budget(source_chunk_entries, effective_max_chunks)
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
                "error_type": "context_overflow" if is_context_overflow_error(exc) else "api_error",
            })

    summary = _summarize_book(clean_name, chunk_results, profile=profile) if chunk_results else {}
    density_counts = {"low": 0, "medium": 0, "high": 0}
    for item in chunk_results:
        level = ((item.get("density_profile") or {}).get("level") or "medium")
        if level not in density_counts:
            level = "medium"
        density_counts[level] += 1
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
        "density_counts": density_counts,
        "incremental_reuse": INCREMENTAL_REUSE,
        "reused_chunk_count": reused_chunk_count,
        "scanned_chunk_count": scanned_chunk_count,
        "prompt_templates": prompt_templates_metadata("general_scan_chunk", "general_summary"),
        "failed_chunks": failed,
        "chunk_results": chunk_results,
        "summary": summary,
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
