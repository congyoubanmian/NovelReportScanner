import json
from typing import Any, Dict, Iterable, List

from Timerror import extract_status_code
from shared_utils import is_context_overflow_error, is_retryable_transport_error
from name_authority import clean_aliases, core_name, is_generic_person_name, normalize_name


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in list(value)]
    return str(value)


def _json_safe_dict(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _json_safe_value(v) for k, v in value.items()}


def discarded_fact(field: str, value: Any, reason: str, detail: str = "", chunk_index: Any = None) -> Dict[str, Any]:
    return {
        "field": field,
        "value": _json_safe_value(value),
        "reason": reason,
        "detail": detail,
        "chunk_index": _json_safe_value(chunk_index),
    }


def classify_scan_error(exc: Exception) -> str:
    if is_context_overflow_error(exc):
        return "context_overflow"
    text = str(exc or "").lower()
    status_code = extract_status_code(exc)
    if status_code in (429, 500, 502, 503, 504):
        return "api_error"
    if any(token in text for token in ("服务器错误", "server error", "429", "500", "502", "503", "504", "rate limit")):
        return "api_error"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    if is_retryable_transport_error(exc):
        return "api_error"
    if "json" in text or "parse" in text or "解析" in text:
        return "parse_error"
    return "unknown"


def _safe_list(value: Any, limit: int = 50) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, (tuple, set)):
        items = list(value)
    elif isinstance(value, (str, int, float, bool, dict)):
        items = [value]
    else:
        return []
    return items[:limit]


def _clean_discarded_facts(items: Any, limit: int = 200) -> List[Dict[str, Any]]:
    cleaned = []
    for item in _safe_list(items, limit=limit * 2):
        if not isinstance(item, dict):
            continue
        safe_item = _json_safe_dict(item)
        if not safe_item:
            continue
        cleaned.append(safe_item)
        if len(cleaned) >= limit:
            break
    return cleaned


def _clean_text_list(values: Iterable[Any], field: str, chunk_index: Any, limit: int = 50) -> tuple[List[str], List[Dict[str, Any]]]:
    cleaned = []
    discarded = []
    seen = set()
    for value in _safe_list(values, limit=limit * 2):
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > 500:
            discarded.append(discarded_fact(field, text[:120], "oversized_text", "单条事实过长，疑似模型跑偏", chunk_index))
            continue
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned, discarded


def _clean_character_record(record: Dict[str, Any], chunk_index: Any, field: str = "characters") -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    discarded = []
    if not isinstance(record, dict):
        return {}, [discarded_fact(field, record, "invalid_record", "角色记录不是对象", chunk_index)]
    name = normalize_name(record.get("name"))
    if is_generic_person_name(name):
        return {}, [discarded_fact(field, name or record, "generic_person_name", "泛称、称谓或代词不作为独立人物", chunk_index)]
    aliases, alias_discards = clean_aliases(record.get("aliases") or record.get("other_names") or [], name)
    for item in alias_discards:
        item["chunk_index"] = chunk_index
    cleaned = dict(record)
    cleaned["name"] = core_name(name)
    cleaned["aliases"] = aliases
    cleaned["other_names"] = aliases
    discarded.extend(alias_discards)
    return cleaned, discarded


def validate_general_chunk_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    chunk_index = result.get("original_chunk_index", result.get("chunk_index"))
    discarded = _clean_discarded_facts(result.get("discarded_facts"))
    for field in (
        "plot_events", "conflicts", "worldbuilding", "themes", "foreshadowing",
        "quality_notes", "specialty_notes",
    ):
        cleaned, field_discards = _clean_text_list(result.get(field), field, chunk_index)
        result[field] = cleaned
        discarded.extend(field_discards)

    update = result.get("context_state_update")
    if isinstance(update, dict):
        active = []
        for name in _safe_list(update.get("active_characters"), limit=80):
            normalized = normalize_name(name)
            if is_generic_person_name(normalized):
                discarded.append(discarded_fact("context_state_update.active_characters", normalized, "generic_person_name", "上下文人物过滤", chunk_index))
                continue
            if core_name(normalized) not in active:
                active.append(core_name(normalized))
        update["active_characters"] = active[:40]
        result["context_state_update"] = update

    result["chunk_facts"] = build_general_chunk_facts(result)
    result["discarded_facts"] = discarded
    return result


def build_general_chunk_facts(result: Dict[str, Any]) -> Dict[str, Any]:
    chunk_index = result.get("original_chunk_index", result.get("chunk_index"))
    summary = str(result.get("one_sentence_summary") or "").strip()
    update = result.get("context_state_update") if isinstance(result.get("context_state_update"), dict) else {}
    facts = {
        "characters": [],
        "relationships": [],
        "events": [],
        "worldbuilding": [],
        "foreshadowing": [],
        "risk_facts": [],
    }
    for name in update.get("active_characters") or []:
        facts["characters"].append({
            "name": name,
            "chunk_index": chunk_index,
            "evidence": summary[:160],
            "confidence": "medium",
        })
    for item in update.get("relationship_updates") or []:
        text = str(item or "").strip()
        if text:
            facts["relationships"].append({"description": text[:220], "chunk_index": chunk_index, "evidence": summary[:160], "confidence": "medium"})
    for item in result.get("plot_events") or []:
        facts["events"].append({"event": str(item)[:220], "chunk_index": chunk_index, "summary": summary[:160], "confidence": "medium"})
    for item in (result.get("worldbuilding") or []) + (update.get("worldbuilding_updates") or []):
        text = str(item or "").strip()
        if text:
            facts["worldbuilding"].append({"fact": text[:220], "chunk_index": chunk_index, "evidence": summary[:160], "confidence": "medium"})
    for item in result.get("foreshadowing") or []:
        text = str(item or "").strip()
        if text:
            facts["foreshadowing"].append({"description": text[:220], "status": "active", "chunk_index": chunk_index, "evidence": summary[:160], "confidence": "medium"})
    for field in ("quality_notes", "conflicts", "specialty_notes"):
        for item in result.get(field) or []:
            text = str(item or "").strip()
            if any(term in text for term in ("风险", "问题", "矛盾", "崩", "绿帽", "送女", "雷", "毒", "注水", "强行")):
                facts["risk_facts"].append({"type": field, "description": text[:220], "chunk_index": chunk_index, "evidence": summary[:160], "confidence": "medium"})
    return facts


def validate_harem_character_result(result: Dict[str, Any], chunk_index: Any = None) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    if chunk_index is None:
        chunk_index = result.get("chunk_index")
    discarded = _clean_discarded_facts(result.get("discarded_facts"))
    male = result.get("male_protagonist")
    if isinstance(male, dict):
        cleaned, discards = _clean_character_record(male, chunk_index, field="male_protagonist")
        result["male_protagonist"] = cleaned or None
        discarded.extend(discards)
    cleaned_chars = []
    for char in result.get("female_characters") or []:
        cleaned, discards = _clean_character_record(char, chunk_index, field="female_characters")
        discarded.extend(discards)
        if cleaned:
            cleaned_chars.append(cleaned)
    result["female_characters"] = cleaned_chars
    if "general_characters" in result:
        cleaned_general = []
        for char in result.get("general_characters") or []:
            cleaned, discards = _clean_character_record(char, chunk_index, field="general_characters")
            discarded.extend(discards)
            if cleaned:
                cleaned_general.append(cleaned)
        result["general_characters"] = cleaned_general
    result["discarded_facts"] = discarded
    return result


def merge_discarded_facts(*containers: Any, limit: int = 200) -> List[Dict[str, Any]]:
    merged = []
    seen = set()
    for container in containers:
        if isinstance(container, dict):
            items = container.get("discarded_facts") or []
        else:
            items = container or []
        for item in items:
            if not isinstance(item, dict):
                continue
            item = _json_safe_dict(item)
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged
