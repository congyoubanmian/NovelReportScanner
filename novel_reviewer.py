import os
import json
import logging
import glob
import concurrent.futures
from datetime import datetime
from typing import Tuple, Dict, Any, List, Optional
import time
import threading
import hashlib
import re

# 导入文本锚定工具模块
# 注意：scan 阶段不再使用 span/索引切片机制，evidence 作为字符串参与后续锚定
try:
    from text_anchor import (
        normalize_newlines,
        split_to_chunks,
        load_chunk_manifest,
        get_context_around_chunk,
        get_context_around_chunk_from_fulltext,
        find_evidence_anchor,
        anchor_evidence,
        evidence_in_ctx,
        split_cn_sentences_with_spans,
        sentence_hit_index_by_spans,
        CTX_MAX_CHARS as TEXT_ANCHOR_CTX_MAX_CHARS,
        BEFORE_SENTS as TEXT_ANCHOR_BEFORE_SENTS,
        AFTER_SENTS as TEXT_ANCHOR_AFTER_SENTS,
    )
    TEXT_ANCHOR_AVAILABLE = True
except ImportError:
    TEXT_ANCHOR_AVAILABLE = False
try:
    from openai import APIStatusError
except Exception:
    APIStatusError = Exception
from tqdm import tqdm
from shared_utils import (
    API_KEY_POOL,
    BASE_DIR,
    MAX_WORKERS,
    MODEL,
    SCAN_RESULTS_DIR,
    _safe_json_loads_maybe,
    call_json_chat_completion_with_fallback,
    chat_completion,
    configure_rotating_file_logger,
    get_token_tracker,
    init_token_tracker,
    logger,
    read_int_env,
    record_usage,
)
from toxic_reviewer import batch_review_toxic_points, load_rules_dict

# ================= 指代消解 & 编码相关常量 =================
# 指代消解置信度门槛：低于此值不写回 partner
CONFIDENCE_THRESHOLD = 0.75
# 编码评分阈值：最高分低于此值视为编码不可靠
SCORE_THRESHOLD = 0.2
# 二次截取最大字符数
REFINED_CTX_MAX_CHARS = 2500

# ================= 孩子母亲归属校验相关常量 =================
# 母亲重归属置信度门槛：高于此值才允许移动到目标母亲名下
CHILD_OWNER_CONF_THRESHOLD = 0.75
# 母亲归属上下文最大字符数
CHILD_OWNER_CTX_MAX_CHARS = 2200
# 母亲归属上下文前后句子数
CHILD_OWNER_BEFORE_SENTS = 3
CHILD_OWNER_AFTER_SENTS = 3
# 触发 LLM 重归属的关键词（evidence/ctx_text 含任一即触发）
CHILD_OWNER_TRIGGER_WORDS = [
    "出生", "生下", "分娩", "怀孕", "肚子里", "孩子", 
    "产下", "临盆", "流产", "打胎", "堕胎"
]

_CORE_FACT_DIMENSIONS = [
    "sexual_relations",
    "children_info",
    "physical_contacts",
    "romantic_feelings",
    "partner_relations",
]
_EXTENDED_FACT_DIMENSIONS = [
    "economic_attachments",
    "power_relations",
    "political_marriages",
    "victim_records",
]
_FACT_DIMENSIONS = _CORE_FACT_DIMENSIONS + _EXTENDED_FACT_DIMENSIONS

# 后宫二审会汇总 scan/detail 的大量证据。这里限制进入单次 LLM 的文本体积，
# 避免 5万到 17万字符的大请求触发网关 504 或返回残缺 JSON。
# 常量定义在 rv_llm_payload.py 中（单一来源），此处 re-import 保持命名空间兼容。
from rv_llm_payload import (
    REVIEW_LLM_SECTION_MAX_CHARS,
    REVIEW_LLM_FIELD_MAX_CHARS,
    REVIEW_LLM_LIST_MAX_ITEMS,
    _clip_llm_text,
    _unique_llm_lines,
    _head_tail_llm_lines,
    _compact_llm_lines,
)


def _call_json_chat_completion(messages, *, model: str = None, temperature: float = 0.1, max_tokens: int = None) -> Dict[str, Any]:
    return call_json_chat_completion_with_fallback(
        chat_completion_func=chat_completion,
        model=model or MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        record_usage_func=record_usage,
    )


def _empty_purity_fact_bucket() -> Dict[str, List[Any]]:
    return {key: [] for key in _FACT_DIMENSIONS}


def _normalize_purity_fact_bucket(facts: Dict[str, Any]) -> Dict[str, List[Any]]:
    normalized = _empty_purity_fact_bucket()
    if not isinstance(facts, dict):
        return normalized
    for key in _FACT_DIMENSIONS:
        value = facts.get(key, [])
        normalized[key] = list(value or []) if isinstance(value, list) else []
    return normalized




def _dedupe_fact_bucket_by_evidence(facts: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
    for key in _FACT_DIMENSIONS:
        seen = set()
        unique = []
        for item in facts.get(key, []) or []:
            if not isinstance(item, dict):
                signature = str(item)
            else:
                signature = "|".join(
                    [
                        str(item.get("evidence", "") or ""),
                        str(item.get("detail", "") or ""),
                        str(item.get("partner") or item.get("target") or item.get("father") or item.get("child_name") or ""),
                        str(item.get("benefactor") or item.get("superior") or item.get("perpetrator") or ""),
                    ]
                )
            if signature in seen:
                continue
            seen.add(signature)
            unique.append(item)
        facts[key] = unique
    return facts


def _append_extended_relationship_facts_text(facts_text: List[str], facts: Dict[str, Any]) -> None:
    for ea in facts.get("economic_attachments", []) or []:
        if not isinstance(ea, dict):
            continue
        benefactor = ea.get("benefactor", "未知")
        relationship = ea.get("relationship", "")
        status = ea.get("status", "")
        forced_flag = ea.get("forced", None)
        forced = "被迫" if forced_flag is True else ("自愿" if forced_flag is False else "未知")
        evidence = str(ea.get("evidence", "") or "")[:100]
        facts_text.append(f"[经济依附] 对象:{benefactor}, 关系:{relationship}, 状态:{status}, {forced} | 证据: {evidence}")

    for power in facts.get("power_relations", []) or []:
        if not isinstance(power, dict):
            continue
        superior = power.get("superior", "未知")
        relationship = power.get("relationship", "")
        abuse = "存在滥用" if power.get("has_abuse") is True else ("未见滥用" if power.get("has_abuse") is False else "滥用未知")
        evidence = str(power.get("evidence", "") or "")[:100]
        facts_text.append(f"[权力关系] 上位者:{superior}, 关系:{relationship}, {abuse} | 证据: {evidence}")

    for marriage in facts.get("political_marriages", []) or []:
        if not isinstance(marriage, dict):
            continue
        partner = marriage.get("partner", "未知")
        marriage_type = marriage.get("type", "")
        status = marriage.get("status", "")
        forced_flag = marriage.get("forced", None)
        forced = "被迫" if forced_flag is True else ("自愿" if forced_flag is False else "未知")
        consummation = "已圆房" if marriage.get("has_consummation") is True else ("未圆房" if marriage.get("has_consummation") is False else "圆房未知")
        evidence = str(marriage.get("evidence", "") or "")[:100]
        facts_text.append(f"[政治联姻] 对象:{partner}, 类型:{marriage_type}, 状态:{status}, {forced}, {consummation} | 证据: {evidence}")

    for victim in facts.get("victim_records", []) or []:
        if not isinstance(victim, dict):
            continue
        perpetrator = victim.get("perpetrator", "未知")
        record_type = victim.get("type", "")
        outcome = victim.get("outcome", "")
        rescued_by = victim.get("rescued_by", "")
        evidence = str(victim.get("evidence", "") or "")[:100]
        facts_text.append(f"[受害/胁迫] 侵害者:{perpetrator}, 类型:{record_type}, 结果:{outcome}, 救援:{rescued_by} | 证据: {evidence}")

def find_latest_scan_data(scan_dir: Optional[str] = None):
    """在指定目录（默认 SCAN_RESULTS_DIR）递归查找最新 raw_data.json"""
    base = scan_dir or SCAN_RESULTS_DIR
    files = []
    for root, dirs, filenames in os.walk(base):
        for filename in filenames:
            if filename == "raw_data.json":
                files.append(os.path.join(root, filename))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def _read_detail_path_from_raw_data(scan_file_path):
    if not scan_file_path or not os.path.exists(scan_file_path):
        return None
    try:
        with open(scan_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    detail_path = (data or {}).get("detail_path")
    if not detail_path:
        return None
    return os.path.abspath(detail_path)


def find_character_data(scan_file_path, detail_path=None):
    """
    根据 raw_data 所在目录推断同一部小说的 *_detailed_*.json
    优先匹配同名小说，避免拿到其他小说的最新文件。
    """
    if detail_path:
        return os.path.abspath(detail_path)
    raw_data_detail_path = _read_detail_path_from_raw_data(scan_file_path)
    if raw_data_detail_path:
        return raw_data_detail_path
    base_dirs = []
    novel_key = None
    if scan_file_path:
        scan_dir = os.path.dirname(os.path.abspath(scan_file_path))
        base_dirs.append(scan_dir)
        base_dirs.append(os.path.dirname(scan_dir))
        dir_name = os.path.basename(scan_dir)
        if "_scan_" in dir_name:
            novel_key = dir_name.split("_scan_", 1)[0]
    base_dirs.extend([SCAN_RESULTS_DIR, BASE_DIR])

    candidates = []
    seen = set()
    # 先按 novel_key 过滤
    for root in base_dirs:
        pattern = os.path.join(root, "**", "*_detailed_*.json")
        for path in glob.glob(pattern, recursive=True):
            ap = os.path.abspath(path)
            if ap in seen:
                continue
            if novel_key and novel_key not in os.path.basename(ap):
                continue
            seen.add(ap)
            candidates.append(ap)
    # 如果按 novel_key 没找到，再不带过滤搜一遍
    if not candidates:
        for root in base_dirs:
            pattern = os.path.join(root, "**", "*_detailed_*.json")
            for path in glob.glob(pattern, recursive=True):
                ap = os.path.abspath(path)
                if ap in seen:
                    continue
                seen.add(ap)
                candidates.append(ap)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


# ========= 新增：漏女判定相关 =========

def _infer_novel_path_from_scan(raw_data_path: str) -> Optional[str]:
    """根据 scan 目录名推断原始小说文件，尝试在 ./novels 或上级目录搜索"""
    try:
        base_dir = os.path.dirname(raw_data_path)
        dir_name = os.path.basename(base_dir)
        novel_key = dir_name.split("_scan_", 1)[0]
        search_dirs = ["./novels", "../novels", "."]
        patterns = [f"{novel_key}.txt", f"{novel_key}*.txt"]
        for root in search_dirs:
            for pat in patterns:
                matches = glob.glob(os.path.join(root, "**", pat), recursive=True)
                if matches:
                    return max(matches, key=os.path.getmtime)
    except Exception:
        return None
    return None


def _novel_file_signature(path: str, sample_size: int = 65536):
    try:
        stat = os.stat(path)
        size = int(stat.st_size)
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with open(path, "rb") as f:
            digest.update(f.read(sample_size))
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


def _validate_raw_data_matches_novel(raw_data: dict, raw_data_path: str, novel_path: Optional[str], book_name: Optional[str]):
    if not isinstance(raw_data, dict):
        raise ValueError(f"raw_data.json 格式无效: {raw_data_path}")
    stored_book = str(raw_data.get("book_name") or "").strip()
    expected_book = str(book_name or "").strip()
    if expected_book and stored_book and stored_book != expected_book:
        raise ValueError(f"raw_data 书名不匹配: expected={expected_book}, actual={stored_book}")
    expected_path = os.path.abspath(novel_path) if novel_path else ""
    stored_path = str(raw_data.get("novel_path") or "").strip()
    if expected_path and stored_path and os.path.abspath(stored_path) != expected_path:
        raise ValueError(f"raw_data 小说路径不匹配: expected={expected_path}, actual={stored_path}")
    stored_signature = raw_data.get("novel_signature")
    if expected_path and isinstance(stored_signature, dict):
        current_signature = _novel_file_signature(expected_path)
        if current_signature != stored_signature:
            raise ValueError(f"raw_data 小说签名不匹配: {raw_data_path}")


def _read_tail(file_path: str, max_chars: int = 10000) -> Optional[str]:
    """
    读取小说尾部文本（复用统一编码识别逻辑，避免 errors='ignore' 吞字）。
    """
    text = _read_novel_file(file_path)
    if not text:
        return None
    return text[-max_chars:]


def judge_novel_finished(text_tail: str) -> Tuple[Optional[bool], str]:
    """调用 LLM 判断小说是否完结；包含番外/后记也视为完结"""
    if not API_KEY_POOL:
        return None, "未设置 API_KEY 或 API_KEY_POOL"
    system_prompt = (
        "你是小说完结判定助手。给出尾部文本，判断本书是否已完结或进入番外/后记。"
        "出现“完结”“全文完”“大结局”“后记”“番外”“完本”等即可视为完结。"
        "若明显仍在进行主线且无收尾信号，则判定未完结。"
    )
    user_prompt = f"以下为小说尾部节选，请输出 JSON {{\"finished\":true/false, \"reason\":\"简述\"}}：\n{text_tail}"
    try:
        data = _call_json_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        return data.get("finished"), data.get("reason", "")
    except Exception as e:
        return None, f"判定失败: {e}"


def _build_heroine_map(char_file_path: str) -> Dict[str, Dict[str, Any]]:
    """返回 name -> heroine_info 的映射，优先使用 heroine_result"""
    if not char_file_path or not os.path.exists(char_file_path):
        return {}
    try:
        with open(char_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    mapping = {}
    hr = data.get("heroine_result", {}).get("heroines", [])
    for h in hr:
        name = h.get("name")
        if name:
            mapping[name] = h
    # 补充 all_female_characters 以便兜底
    afc = data.get("all_female_characters", {})
    for name, info in afc.items():
        if name not in mapping:
            mapping[name] = {"name": name, **info}
        else:
            # 若 heroine_result 里缺少证据字段，则从 all_female_characters 合并补齐
            for field in ["interactions", "summaries", "summary", "interaction_with_male_lead", "emotion_signals"]:
                if not mapping[name].get(field) and info.get(field):
                    mapping[name][field] = info.get(field)
    return mapping


def judge_pushed_by_male_lead(heroine: Dict[str, Any], male_name: str = "男主") -> Tuple[Optional[bool], str]:
    """
    调用大模型判断该女主是否已与男主发生实质亲密关系。
    返回 (bool|None, reason)
    """
    if not API_KEY_POOL:
        return None, "未设置 API_KEY 或 API_KEY_POOL"
    texts: List[str] = []
    for field in ["interactions", "summaries", "summary", "interaction_with_male_lead", "emotion_signals"]:
        val = heroine.get(field)
        if isinstance(val, list):
            texts.extend([str(x) for x in val])
        elif isinstance(val, str):
            texts.append(val)
    evidence = "\n".join(texts) or "（无证据）"
    system_prompt = (
        "你是严谨的小说情节判定助手。核心任务：判定该女性角色是否已与男主发生过【实质亲密关系】。"
        "只在有明确行为证据时判定为已发生。"
        "优先识别的实质行为包含：推倒、发生关系、上床、同房、圆房、啪啪啪、怀孕、亲密性行为等。"
        "仅有暧昧/恋爱/动心/拥抱/接吻/暧昧暧昧暗示，不视为实质亲密关系。"
        "如证据不足或无明确性行为描述，判定为未证实。"
        "【输出格式要求】只输出一个 JSON 对象，不要 Markdown，不要代码块，不要解释文字。"
    )
    user_prompt = (
        f"男主：{male_name}\n"
        f"请根据下列证据判断该女性角色是否已被男主推倒（发生实质亲密行为）：\n"
        f"证据：\n{evidence}\n"
        f"只输出 JSON：{{\"pushed\": true/false, \"reason\": \"简要理由\"}}"
    )
    last_err = None
    for attempt in range(3):
        try:
            data = _call_json_chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
            )
            return bool(data.get("pushed", False)), str(data.get("reason", "无理由"))
        except Exception as e:
            last_err = str(e)
            time.sleep(1 + attempt)
            continue
    return None, f"判定失败: {last_err}"


def _ending_accounted_in_tail(tail: str, name: str, aliases: List[str]) -> Tuple[bool, str]:
    if not tail:
        return False, "尾声检索：无尾声文本"

    candidates = [c for c in [name] + aliases if c]
    if not any(c in tail for c in candidates):
        return False, "尾声检索：未命中姓名或别名"

    ending_markers = (
        "结局", "最终", "最后", "后来", "此后", "从此", "余生", "一生", "多年后", "番外",
        "归宿", "去处", "留下", "留在", "陪在", "跟随", "同行", "同去", "回到", "去了",
        "嫁", "娶", "婚", "成亲", "大婚", "完婚", "圆房", "同房", "怀孕", "生下", "孩子",
        "道侣", "伴侣", "妻", "妾", "夫人", "皇后", "王妃", "后宫", "收入", "收了",
        "在一起", "相伴", "白头", "团聚", "重逢", "守着", "等着", "阵营", "府中", "身边",
        "死", "牺牲", "葬", "坟", "墓", "陨落",
    )
    weak_mention_markers = ("想起", "提到", "听说", "传闻", "名字", "名单", "回忆", "梦见", "路过", "问起")
    negated_ending_markers = (
        "未交代归宿", "没有交代归宿", "未明确交代", "没有明确交代", "归宿不明", "去向不明",
        "去向未知", "不知去了哪里", "不知道去了哪里", "不知道去处", "不知去处", "去处不明",
        "没有去向说明", "未说明去向", "未交代去向", "下落不明", "行踪不明", "再无音讯",
        "音讯全无", "没有留在", "未留在", "没有同行", "未同行", "没有跟随", "未跟随",
        "结局未交代", "尾声未交代",
    )
    nonfactual_ending_markers = (
        "梦见", "梦到", "梦境", "幻境", "幻觉", "假死", "假消息", "证实是假", "后来证实",
        "并非真的", "不是真的", "传闻", "传言", "听说", "据说", "墓葬制度", "坟墓结构",
        "讲解墓", "讨论墓", "研究墓",
    )
    strong_ending_markers = (
        "归宿", "去处", "留下", "留在", "陪在", "跟随", "同行", "同去", "回到", "去了",
        "嫁", "娶", "婚", "成亲", "大婚", "完婚", "圆房", "同房", "怀孕", "生下", "孩子",
        "道侣", "伴侣", "妻", "妾", "夫人", "皇后", "王妃", "后宫", "收入", "收了",
        "在一起", "相伴", "余生", "一生", "白头", "团聚", "重逢", "守着", "等着", "阵营", "府中", "身边",
        "死", "牺牲", "葬", "坟", "墓", "陨落",
    )
    non_ending_action_followers = (
        "线索", "证据", "伏笔", "案件", "调查", "评审", "报告", "评价", "评论", "记录",
        "名单", "名字", "遗物", "遗迹", "传说", "故事", "痕迹", "传闻", "消息", "信息",
        "资料", "档案", "谜题", "悬念", "铺垫", "后", "时", "期间", "过程",
    )

    def _has_substantive_ending_marker(text: str) -> bool:
        for marker in strong_ending_markers:
            start = 0
            while True:
                marker_idx = text.find(marker, start)
                if marker_idx < 0:
                    break
                next_text = text[marker_idx + len(marker):marker_idx + len(marker) + 6]
                non_ending_action = marker in ("留下", "留在", "跟随", "同行", "同去", "回到") and any(
                    next_text.startswith(hint) for hint in non_ending_action_followers
                )
                if not non_ending_action:
                    return True
                start = marker_idx + len(marker)
        return False

    for candidate in candidates:
        start = 0
        while True:
            idx = tail.find(candidate, start)
            if idx < 0:
                break
            window = tail[max(0, idx - 80): idx + len(candidate) + 120]
            negated_named_ending = re.search(r"(?:不知|不知道|未说明|没有说明).{0,12}(?:去处|归宿|下落|行踪)", window)
            if (
                any(marker in window for marker in negated_ending_markers)
                or any(marker in window for marker in nonfactual_ending_markers)
                or negated_named_ending
            ):
                start = idx + len(candidate)
                continue
            weak_mention_only = (
                any(marker in window for marker in weak_mention_markers)
                and not any(marker in window for marker in strong_ending_markers)
            )
            if any(marker in window for marker in ending_markers) and _has_substantive_ending_marker(window) and not weak_mention_only:
                return True, f"尾声检索：{candidate} 周边出现明确结局交代"
            start = idx + len(candidate)

    weak_hint = "；可能只是提及" if any(marker in tail for marker in weak_mention_markers) else ""
    return False, f"尾声检索：命中姓名或别名，但缺少归宿/关系/去向等结局语义{weak_hint}"


def _heroine_has_emotional_depth_for_leak(heroine_info: Dict[str, Any]) -> Tuple[bool, str]:
    texts: List[str] = []
    for field in [
        "interactions",
        "summaries",
        "summary",
        "interaction_with_male_lead",
        "emotion_signals",
        "relationship_with_protagonist",
        "key_events",
    ]:
        val = (heroine_info or {}).get(field)
        if isinstance(val, list):
            texts.extend(str(x) for x in val if str(x).strip())
        elif isinstance(val, str) and val.strip():
            texts.append(val)
    keywords = ("暧昧", "喜欢", "爱", "动心", "倾心", "表白", "告白", "吃醋", "道侣", "恋人", "未婚妻")
    effective_text = _leak_emotional_depth_effective_text(texts)
    matched = [kw for kw in keywords if kw in effective_text]
    if matched:
        return True, f"命中情感/亲密关键词：{','.join(matched[:5])}"
    if texts:
        return False, "已有女主材料，但未见稳定情感深度关键词"
    return False, "缺少女主关系/互动材料"


def _leak_emotional_depth_effective_text(texts: List[str]) -> str:
    negative_markers = (
        "没有暧昧", "无暧昧", "没有喜欢", "不喜欢", "没有爱", "不爱", "没有动心", "未动心",
        "没有感情", "无感情", "没感情", "没有感情线", "无感情线", "没有感情戏", "无感情戏",
        "没有恋爱", "无恋爱", "没有恋爱线", "无恋爱线", "没有亲密", "无亲密",
        "无后宫关系确认",
    )
    nonfactual_markers = (
        "调侃", "玩笑", "误会", "误传", "传闻", "传言", "据说", "听说", "疑似", "像", "读者觉得",
    )
    meta_emotion_markers = (
        "读者喜欢", "读者爱看", "读者偏爱", "读者觉得", "作者喜欢", "作者偏爱", "作者宠",
        "粉丝喜欢", "粉丝爱看", "粉丝偏爱", "人气", "受欢迎", "观众喜欢", "观众爱看",
    )
    hobby_or_function_markers = (
        "喜欢破案", "喜欢推理", "喜欢研究", "喜欢探索", "喜欢冒险", "喜欢吐槽",
        "爱吃", "爱喝", "爱玩", "爱看", "爱研究", "爱推理", "热爱推理", "热爱研究",
        "倾心研究", "倾心学术", "负责解释", "负责说明", "提供线索", "活跃气氛",
    )
    roleplay_markers = (
        "假装表白", "假装告白", "假装喜欢", "假装恋人", "假装情侣", "假装夫妻",
        "扮演恋人", "扮演情侣", "扮演夫妻", "告白台词", "表白台词", "排练舞台剧",
        "排练剧本", "演戏", "舞台剧", "剧本", "套取情报", "任务潜入", "潜入宴会",
    )
    familial_or_comrade_markers = (
        "亲情", "战友情", "友情", "家人式", "像兄妹", "像姐弟", "兄妹一样", "姐弟一样",
        "当弟弟", "当哥哥", "当妹妹", "当姐姐", "爱护后辈", "照顾后辈", "照顾晚辈",
        "姐姐照顾", "妹妹照顾", "师徒情", "同伴情", "伙伴情",
    )
    effective: List[str] = []
    for text in texts or []:
        for chunk in re.split(r"[\n，,。；;！？!?]+", str(text or "")):
            chunk = chunk.strip()
            if not chunk:
                continue
            if any(marker in chunk for marker in negative_markers):
                continue
            if any(marker in chunk for marker in nonfactual_markers):
                continue
            if any(marker in chunk for marker in meta_emotion_markers):
                continue
            if any(marker in chunk for marker in hobby_or_function_markers):
                continue
            if any(marker in chunk for marker in roleplay_markers):
                continue
            if any(marker in chunk for marker in familial_or_comrade_markers):
                continue
            effective.append(chunk)
    return " ".join(effective)


def _issue_identity_key(issue: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    return (
        issue.get("category"),
        issue.get("type"),
        issue.get("content"),
        issue.get("chunk_index"),
    )


def _merge_unique_review_issues(
    verified_issues: List[Dict[str, Any]],
    new_issues: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    merged = list(verified_issues or [])
    seen = {_issue_identity_key(item) for item in merged}
    added = 0
    for issue in new_issues or []:
        key = _issue_identity_key(issue)
        if key in seen:
            continue
        merged.append(issue)
        seen.add(key)
        added += 1
    return merged, added


def _rebuild_leak_state_from_pushed_map(
    female_leads: List[str],
    char_file_path: str,
    novel_tail: Optional[str],
    finished: Optional[bool],
    pushed_map: Optional[Dict[str, Tuple[Optional[bool], str]]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    heroine_map = _build_heroine_map(char_file_path)
    pushed_map = pushed_map or {}
    issues: List[Dict[str, Any]] = []
    leak_status_map: Dict[str, Dict[str, Any]] = {}

    if finished is False:
        for name in female_leads:
            hinfo = heroine_map.get(name, {"name": name})
            has_emotional_depth, emotional_depth_reason = _heroine_has_emotional_depth_for_leak(hinfo)
            leak_status_map[name] = {
                "is_leak_heroine": None,
                "leak_reason": "小说未完结，暂不判定漏女",
                "leak_emotional_depth": has_emotional_depth,
                "leak_emotional_depth_reason": emotional_depth_reason,
                "leak_relationship_confirmed": None,
                "leak_relationship_reason": "小说未完结，暂不判定关系收束",
                "leak_ending_accounted": None,
                "leak_ending_reason": "小说未完结，暂不判定结局交代",
            }
        return issues, leak_status_map

    if finished is None:
        for name in female_leads:
            hinfo = heroine_map.get(name, {"name": name})
            has_emotional_depth, emotional_depth_reason = _heroine_has_emotional_depth_for_leak(hinfo)
            leak_status_map[name] = {
                "is_leak_heroine": None,
                "leak_reason": "完结状态未知，暂不判定漏女",
                "leak_emotional_depth": has_emotional_depth,
                "leak_emotional_depth_reason": emotional_depth_reason,
                "leak_relationship_confirmed": None,
                "leak_relationship_reason": "完结状态未知，暂不判定关系收束",
                "leak_ending_accounted": None,
                "leak_ending_reason": "完结状态未知，暂不判定结局交代",
            }
        return issues, leak_status_map

    for name in female_leads:
        pushed, pushed_reason = pushed_map.get(name, (None, "未判定"))
        hinfo = heroine_map.get(name, {"name": name})
        if pushed is True and _pushed_confirmation_is_nominal_or_negated(pushed_reason, hinfo):
            pushed = None
            pushed_reason = f"{pushed_reason}；命中名义/称呼/未圆房等非实质确认语境，关系确认改为未知"
        if pushed is True:
            relationship_confirmed: Optional[bool] = True
        elif pushed is False:
            relationship_confirmed = False
        else:
            relationship_confirmed = None
        pushed_ok = relationship_confirmed is True
        has_emotional_depth, emotional_depth_reason = _heroine_has_emotional_depth_for_leak(hinfo)
        aliases = hinfo.get("aliases", []) if isinstance(hinfo.get("aliases", []), list) else []
        ending_accounted, ending_reason = _ending_accounted_in_tail(novel_tail or "", name, aliases)
        is_leak = has_emotional_depth and (relationship_confirmed is False) and (not ending_accounted)

        if is_leak:
            leak_reason = (
                f"情感深度：{emotional_depth_reason}；"
                f"推倒判定：{pushed_reason}；{ending_reason}"
            )
            issues.append({
                "category": "郁闷点",
                "type": "漏女",
                "content": f"{name} 未被男主明确推倒，且尾声未明确交代结局",
                "reason": leak_reason,
                "review_comment": f"{name} 未被男主明确推倒，且尾声未交代结局，判为漏女。",
                "chunk_index": -1,
            })
        elif not has_emotional_depth:
            leak_reason = f"情感深度：{emotional_depth_reason}；未达到漏女判定门槛"
        elif pushed_ok:
            leak_reason = f"推倒判定：{pushed_reason}；已被男主明确推倒，不算漏女"
        elif relationship_confirmed is None:
            leak_reason = f"推倒判定：{pushed_reason}；关系确认未知，暂不判漏女"
        else:
            leak_reason = f"推倒判定：{pushed_reason}；{ending_reason}"

        leak_status_map[name] = {
            "is_leak_heroine": is_leak,
            "leak_reason": leak_reason,
            "leak_emotional_depth": has_emotional_depth,
            "leak_emotional_depth_reason": emotional_depth_reason,
            "leak_relationship_confirmed": relationship_confirmed,
            "leak_relationship_reason": f"推倒判定：{pushed_reason}",
            "leak_ending_accounted": ending_accounted,
            "leak_ending_reason": ending_reason,
        }
        logger.info(f"[漏女判定] {name}: pushed_ok={pushed_ok}, ending_accounted={ending_accounted}, pushed={pushed}")

    return issues, leak_status_map


def _pushed_confirmation_is_nominal_or_negated(pushed_reason: str, hinfo: Dict[str, Any]) -> bool:
    text_parts = [str(pushed_reason or "")]
    if isinstance(hinfo, dict):
        for key in ("summary", "summaries", "relationship_with_protagonist", "features", "key_events"):
            value = hinfo.get(key)
            if isinstance(value, (list, tuple, set)):
                text_parts.extend(str(item) for item in value if item is not None)
            elif value is not None:
                text_parts.append(str(value))
    text = " ".join(part for part in text_parts if part)
    if not text:
        return False

    nominal_or_negated_markers = (
        "只是称呼", "只是个称呼", "只是外号", "只是绰号", "玩笑称呼", "调侃称呼", "口头称呼",
        "只是玩笑", "开玩笑", "读者调侃", "读者脑补", "粉丝称呼", "书友称呼",
        "有名无实", "名义夫妻", "名义上的夫妻", "名义婚约", "名义关系",
        "假结婚", "假扮夫妻", "伪装夫妻", "契约夫妻", "政治婚约", "政治联姻",
        "未圆房", "没有圆房", "未同房", "没有同房", "未发生关系", "没有发生关系",
        "无实质关系", "没有实质关系", "无身体关系", "没有身体关系",
    )
    factual_override_markers = (
        "已同房", "已经同房", "明确同房", "发生关系", "发生性关系", "圆房了", "已经圆房",
        "确认推倒", "明确推倒", "收入后宫", "收进后宫", "成为道侣", "确认关系",
    )
    if _contains_positive_phrase_for_leak_confirmation(text, factual_override_markers):
        return False
    return any(marker in text for marker in nominal_or_negated_markers)


def _contains_positive_phrase_for_leak_confirmation(text: str, markers: Tuple[str, ...]) -> bool:
    negative_prefixes = (
        "没有", "没", "无", "未", "并未", "尚未", "不曾", "未曾", "不是", "并非",
        "不算", "不能算", "称不上", "谈不上",
    )
    for marker in markers:
        start = 0
        while marker:
            idx = text.find(marker, start)
            if idx < 0:
                break
            window = text[max(0, idx - 8):idx]
            if not any(prefix in window for prefix in negative_prefixes):
                return True
            start = idx + len(marker)
    return False


def detect_leak_heroines(
    female_leads: List[str],
    male_lead: str,
    char_file_path: str,
    novel_tail: Optional[str],
    finished: Optional[bool]
) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[Optional[bool], str]]]:
    """
    先逐女主判定是否被男主推倒，再结合情感深度与尾声登场情况生成“漏女”郁闷点。
    规则：仅当“有情感深度”“明确未被推倒”“尾声未出现”三层同时满足时，才认定漏女。
    返回 (issues, pushed_results_map)
    """
    heroine_map = _build_heroine_map(char_file_path)
    pushed_results: Dict[str, Tuple[Optional[bool], str]] = {}
    if female_leads:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_map = {
                executor.submit(judge_pushed_by_male_lead, heroine_map.get(name, {"name": name}), male_lead or "男主"): name
                for name in female_leads
            }
            for future in concurrent.futures.as_completed(future_map):
                name = future_map[future]
                try:
                    pushed_results[name] = future.result()
                except Exception as e:
                    pushed_results[name] = (None, f"判定异常: {e}")

    issues, _ = _rebuild_leak_state_from_pushed_map(
        female_leads=female_leads,
        char_file_path=char_file_path,
        novel_tail=novel_tail,
        finished=finished,
        pushed_map=pushed_results,
    )
    return issues, pushed_results


def extract_roles(char_file_path):
    """兼容 novel4.py 的详细输出与旧版 character_details"""
    if not char_file_path or not os.path.exists(char_file_path):
        return "未识别", []
    try:
        with open(char_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        male_lead = "未识别"
        female_leads = []

        # 1) 优先读取 novel4.py 的字段
        mp = data.get("male_protagonist") or data.get("male_protagonist_stats")
        if isinstance(mp, str):
            male_lead = mp
        elif isinstance(mp, dict):
            male_lead = mp.get("name") or next(iter(mp.keys()), "未识别")

        # 优先且仅使用 heroine_result.heroines 的 name；为空时再降级
        hr = data.get("heroine_result", {}).get("heroines", [])
        if hr:
            female_leads = [h.get("name") for h in hr if h.get("name")]

        if not female_leads:
            afc = data.get("all_female_characters", {})
            if afc:
                sorted_chars = sorted(
                    afc.items(),
                    key=lambda x: (x[1].get("avg_score", 0), x[1].get("count", 0)),
                    reverse=True,
                )
                female_leads.extend([name for name, _ in sorted_chars[:20]])

        # 2) 兼容旧版 character_details
        if not female_leads or male_lead == "未识别":
            details = data.get("character_details", {})
            potential_mls = []
            for name, info in details.items():
                types = info.get("types", [])
                score = info.get("avg_score", 0)
                if "男主" in types or ("男配" not in types and "女" not in "".join(types) and score >= 9):
                    potential_mls.append((name, score))
                if ("女主" in types) or ("女配" in types and score >= 6) or ("女" in "".join(types) and score >= 6):
                    female_leads.append(name)
            if male_lead == "未识别" and potential_mls:
                potential_mls.sort(key=lambda x: x[1], reverse=True)
                male_lead = potential_mls[0][0]

        # 去重
        female_leads = list(dict.fromkeys(female_leads))
        return male_lead, female_leads
    except Exception:
        return "未识别", []


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "y"}:
            return True
        if v in {"false", "0", "no", "n"}:
            return False
    return default

def _bool_mark(value: Optional[bool]) -> str:
    if value is True:
        return "✅"
    if value is False:
        return "❌"
    return "❓"


def _format_heroine_purity_supplement(info: Dict[str, Any]) -> List[str]:
    """Format supplemental purity fields for the final text report."""
    contact_level = str(info.get("contact_level") or "L0")
    contact_label = str(info.get("contact_level_label") or "无非男主接触事实")
    contact_reason = str(info.get("contact_level_reason") or "未见明确非男主接触证据")
    past_status = str(info.get("past_life_status") or "未见前世/原故事线洁度线索")
    past_severity = str(info.get("past_life_severity") or "none")
    past_label = str(info.get("past_life_severity_label") or "无前世/原故事线线索")
    past_reason = str(info.get("past_life_reason") or "未见前世/原故事线婚恋或接触证据")

    return [
        f"  - 接触等级: {contact_level}（{contact_label}）",
        f"  - 接触等级说明: {contact_reason[:160]}",
        f"  - 前世洁度: {past_status}",
        f"  - 前世风险等级: {past_severity}（{past_label}）",
        f"  - 前世洁度说明: {past_reason[:160]}",
    ]


def _derive_past_life_cleanliness(facts: Dict[str, Any], summary: str = "") -> Dict[str, Any]:
    """Derive a conservative 前世/原故事线 cleanliness note from existing facts."""
    blob_parts = [summary or ""]
    for values in (facts or {}).values():
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    blob_parts.append(json.dumps(item, ensure_ascii=False))
                else:
                    blob_parts.append(str(item))
    blob = "\n".join(blob_parts)
    past_markers = ("前世", "原故事线", "原著", "前传", "上一世", "轮回", "重生前", "穿越前")
    if not any(marker in blob for marker in past_markers):
        return {
            "past_life_clean": None,
            "past_life_severity": "none",
            "past_life_severity_label": "无前世/原故事线线索",
            "past_life_status": "未见前世/原故事线洁度线索",
            "past_life_reason": "现有事实未出现前世、原故事线、轮回或穿越前婚恋/接触证据。",
        }
    if _past_life_blob_is_negated_or_nonfactual(blob):
        return {
            "past_life_clean": True,
            "past_life_severity": "clean",
            "past_life_severity_label": "前世/原故事线未见不洁事实",
            "past_life_status": "前世/原故事线未见不洁事实",
            "past_life_reason": "前世/原故事线材料属于否定、传闻或误会语境，未见可作为风险的明确事实。",
        }

    risk_blob = _past_life_effective_risk_text(blob) or blob
    severe_markers = ("万人骑", "轮奸", "群交", "多人", "强奸", "强暴", "侵犯", "洗脑", "雌堕", "背叛")
    sexual_markers = ("同房", "圆房", "失身", "破身", "怀孕", "生下", "孩子", "性关系")
    partner_markers = ("前夫", "丈夫", "男友", "恋人", "未婚夫", "嫁给", "成婚", "结婚", "婚约")
    romantic_markers = ("爱过", "喜欢过", "动心", "恋慕", "暗恋")
    forced_markers = ("被迫", "强迫", "逼婚", "包办", "受害")
    non_ml_markers = ("非男主", "其他男人", "别的男人", "他人")

    if any(marker in risk_blob for marker in severe_markers):
        severity = "severe"
        severity_label = "严重前世雷点/受害风险"
    elif any(marker in risk_blob for marker in sexual_markers):
        severity = "sexual"
        severity_label = "前世/原故事线性关系或子女风险"
    elif any(marker in risk_blob for marker in partner_markers):
        severity = "partner"
        severity_label = "前世/原故事线伴侣或婚约风险"
    elif any(marker in risk_blob for marker in romantic_markers):
        severity = "romantic"
        severity_label = "前世/原故事线情感经历风险"
    elif any(marker in risk_blob for marker in forced_markers) and any(marker in risk_blob for marker in non_ml_markers):
        severity = "forced"
        severity_label = "前世/原故事线被迫关系风险"
    else:
        severity = "clean"
        severity_label = "前世/原故事线未见不洁事实"

    if severity != "clean":
        return {
            "past_life_clean": False,
            "past_life_severity": severity,
            "past_life_severity_label": severity_label,
            "past_life_status": "前世/原故事线存在风险线索",
            "past_life_reason": f"现有事实出现前世/原故事线相关线索：{severity_label}，需单独提示。",
        }
    return {
        "past_life_clean": True,
        "past_life_severity": "clean",
        "past_life_severity_label": "前世/原故事线未见不洁事实",
        "past_life_status": "前世/原故事线未见不洁事实",
        "past_life_reason": "虽出现前世/原故事线线索，但未见明确非男主婚恋、身体接触、情感或受害事实。",
    }


def _past_life_blob_is_negated_or_nonfactual(blob: str) -> bool:
    text = str(blob or "")
    if not text:
        return False
    if _past_life_blob_has_explicit_factual_risk(text):
        return False
    nonfactual_words = (
        "传言", "传闻", "据说", "听说", "流言", "谣言", "误会", "误传", "谣传", "猜测", "怀疑", "疑似",
        "误认为", "被误认", "误认成", "误称", "被误称",
    )
    resolved_words = (
        "证实是误会", "证实不成立", "后来证实", "澄清", "不属实", "并非事实", "不是事实", "并无事实",
        "假的", "假消息",
    )
    negated_patterns = (
        "没有婚恋", "没有喜欢过", "没有爱过", "没有动心", "没有嫁",
        "没有丈夫", "没有前夫", "没有男友", "没有恋人", "没有未婚夫",
        "无丈夫", "无前夫", "无男友", "无恋人", "无未婚夫", "无感情",
        "未嫁", "未曾嫁", "从未嫁", "未婚", "没有成婚", "没有结婚", "未结婚",
        "没有同房", "未同房", "没有圆房", "未圆房", "没有发生关系", "无性关系",
        "未发生性关系", "没有孩子", "未生子", "未怀孕",
    )
    nominal_patterns = (
        "只是政治婚约", "仅是政治婚约", "只是名义婚约", "仅是名义婚约", "只是婚约安排",
        "只是称呼", "只是玩笑", "玩笑称呼", "有名无实", "名义夫妻", "名义上的夫妻",
    )
    if any(word in text for word in resolved_words):
        return True
    if any(word in text for word in negated_patterns):
        return True
    if any(pattern in text for pattern in nominal_patterns):
        return True
    return any(word in text for word in nonfactual_words) and not any(word in text for word in ("确认", "实锤", "明确", "证据"))


def _past_life_effective_risk_text(text: str) -> str:
    """Remove rumor/resolved/negated clauses before ranking past-life risk severity."""
    chunks = re.split(r"[\n，,。；;！？!?]+", str(text or ""))
    effective: List[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if _past_life_clause_is_nonfactual_or_negated(chunk):
            continue
        effective.append(chunk)
    return "\n".join(effective)


def _past_life_clause_is_nonfactual_or_negated(text: str) -> bool:
    nonfactual_words = (
        "传言", "传闻", "据说", "听说", "流言", "谣言", "误会", "误传", "谣传", "猜测", "怀疑", "疑似",
        "误认为", "被误认", "误认成", "误称", "被误称",
    )
    resolved_words = (
        "证实是误会", "证实不成立", "后来证实", "澄清", "不属实", "并非事实", "不是事实", "并无事实",
        "假的", "假消息",
    )
    negated_patterns = (
        "没有婚恋", "没有喜欢过", "没有爱过", "没有动心", "没有嫁",
        "没有丈夫", "没有前夫", "没有男友", "没有恋人", "没有未婚夫",
        "无丈夫", "无前夫", "无男友", "无恋人", "无未婚夫", "无感情",
        "未嫁", "未曾嫁", "从未嫁", "未婚", "没有成婚", "没有结婚", "未结婚",
        "没有同房", "未同房", "没有圆房", "未圆房", "没有发生关系", "无性关系",
        "未发生性关系", "没有孩子", "未生子", "未怀孕",
    )
    nominal_patterns = (
        "只是政治婚约", "仅是政治婚约", "只是名义婚约", "仅是名义婚约", "只是婚约安排",
        "只是称呼", "只是玩笑", "玩笑称呼", "有名无实", "名义夫妻", "名义上的夫妻",
    )
    if any(word in text for word in resolved_words):
        return True
    if any(pattern in text for pattern in negated_patterns):
        return True
    if any(pattern in text for pattern in nominal_patterns):
        return True
    if any(word in text for word in nonfactual_words):
        return not _past_life_blob_has_explicit_factual_risk(text)
    return False


def _past_life_blob_has_explicit_factual_risk(text: str) -> bool:
    """Keep mixed past-life text from being fully washed out by rumor/negation words."""
    strong_factual_anchors = (
        "确实", "明确", "实锤", "证据显示", "事实是", "实际发生", "真的发生", "已确认", "确认了",
    )
    narrative_anchors = (
        "原故事线她", "原著里她", "上一世她", "前世她",
    )
    nonfactual_words = ("传言", "传闻", "据说", "听说", "流言", "谣言", "误会", "误传", "谣传", "猜测", "怀疑", "疑似")
    risk_markers = (
        "爱过", "喜欢过", "动心", "恋慕", "暗恋",
        "前夫", "丈夫", "男友", "恋人", "未婚夫", "嫁给", "成婚", "结婚", "婚约",
        "同房", "圆房", "失身", "破身", "怀孕", "生下", "孩子", "性关系",
        "强奸", "强暴", "侵犯", "洗脑", "雌堕", "背叛",
    )
    negated_risk_phrases = (
        "没有喜欢过", "没有爱过", "没有动心", "未嫁", "未曾嫁", "从未嫁", "没有嫁",
        "没有丈夫", "没有前夫", "没有男友", "没有恋人", "没有未婚夫",
        "无丈夫", "无前夫", "无男友", "无恋人", "无未婚夫",
        "没有同房", "未同房", "没有圆房", "未圆房", "没有发生关系", "未发生性关系",
        "没有孩子", "未生子", "未怀孕",
    )
    nominal_risk_phrases = (
        "只是政治婚约", "仅是政治婚约", "只是名义婚约", "仅是名义婚约", "只是婚约安排",
        "只是称呼", "只是玩笑", "玩笑称呼", "有名无实", "名义夫妻", "名义上的夫妻",
    )
    if any(phrase in text for phrase in negated_risk_phrases):
        protected_text = text
        for phrase in negated_risk_phrases:
            protected_text = protected_text.replace(phrase, "")
    else:
        protected_text = text
    for phrase in nominal_risk_phrases:
        protected_text = protected_text.replace(phrase, "")
    has_risk = any(marker in protected_text for marker in risk_markers)
    if not has_risk:
        return False
    if any(word in protected_text for word in nonfactual_words):
        return any(anchor in protected_text for anchor in strong_factual_anchors)
    return any(anchor in protected_text for anchor in strong_factual_anchors + narrative_anchors)


def _derive_contact_level(
    facts: Dict[str, Any],
    male_lead: str = "男主",
    non_male_male_interactions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Derive report-only L0-L5 contact level from structured facts."""
    facts = facts or {}
    sexual_relations = facts.get("sexual_relations", []) or []
    children_info = facts.get("children_info", []) or []
    physical_contacts = facts.get("physical_contacts", []) or []
    partner_relations = facts.get("partner_relations", []) or []
    romantic_feelings = facts.get("romantic_feelings", []) or []

    def non_ml(item, key="partner"):
        if not isinstance(item, dict):
            return False
        if item.get("is_male_lead") is True:
            return False
        value = str(item.get(key, "") or item.get("father", "") or "")
        return not _is_placeholder(value)

    # L5: explicit non-ML sex, biological child with non-ML/unknown father, group/rape/personality rewrite signals.
    for item in sexual_relations:
        if non_ml(item):
            text = json.dumps(item, ensure_ascii=False)
            level = "L5"
            label = "明确非男主性关系"
            if _contains_any(text, ["群", "多人", "轮奸", "洗脑", "雌堕", "人格", "调教"]):
                label = "群体/人格改造/严重性关系风险"
            return {
                "contact_level": level,
                "contact_level_label": label,
                "contact_level_reason": str(item.get("evidence") or item.get("detail") or "")[:120],
            }
    for child in children_info:
        if not isinstance(child, dict):
            continue
        father = str(child.get("father", "") or "")
        if father in {"男主", "主角", male_lead}:
            continue
        if child.get("is_biological") is False:
            continue
        text = json.dumps(child, ensure_ascii=False)
        if _contains_any(text, ["亲生", "怀孕", "生下", "正常生育", "conception_method\": \"sex"]):
            return {
                "contact_level": "L5",
                "contact_level_label": "非男主亲生子女/怀孕强后果",
                "contact_level_reason": str(child.get("evidence") or child.get("detail") or "")[:120],
            }

    # L4/L3/L2: physical contact severity.
    for item in physical_contacts:
        if not non_ml(item):
            continue
        text = json.dumps(item, ensure_ascii=False)
        reason = str(item.get("evidence") or item.get("detail") or item.get("contact_type") or "")[:120]
        if _contains_any(text, ["猥亵", "侵犯", "强暴", "强奸", "摸胸", "下体", "脱光", "扒光", "重度", "隐私曝光", "录像", "直播"]):
            return {"contact_level": "L4", "contact_level_label": "严重猥亵/侵犯未遂/隐私曝光", "contact_level_reason": reason}
        if _contains_any(text, ["下药", "绑架", "按倒", "强吻", "亲吻", "搂抱", "抱住", "摸", "抚摸", "撕衣"]):
            return {"contact_level": "L3", "contact_level_label": "强迫亲密/敏感接触/侵犯未遂", "contact_level_reason": reason}
        return {"contact_level": "L2", "contact_level_label": "轻度身体接触/被迫暴露", "contact_level_reason": reason}

    # L2/L3 can also be inferred from forced partner relation without consummation.
    for item in partner_relations:
        if not non_ml(item):
            continue
        text = json.dumps(item, ensure_ascii=False)
        reason = str(item.get("evidence") or item.get("detail") or item.get("relationship") or "")[:120]
        if item.get("forced") is True or _contains_any(text, ["被迫", "强迫", "逼婚", "包办", "卖嫁"]):
            return {"contact_level": "L2", "contact_level_label": "被迫婚约/伴侣关系线索", "contact_level_reason": reason}

    # L1: report-only early warning from non-contact harassment, gaze, rumors, or one-sided pursuit.
    # These do not imply broken first-touch or impurity; they only preserve low-severity reader-risk context.
    l1_sources: List[str] = []
    for item in romantic_feelings:
        if not non_ml(item, key="target"):
            continue
        text = json.dumps(item, ensure_ascii=False)
        if _contains_any(text, ["调戏", "口花花", "意淫", "觊觎", "垂涎", "追求", "纠缠", "尾随", "骚扰", "围观", "注视", "盯着", "流言", "传闻"]):
            l1_sources.append(str(item.get("evidence") or item.get("detail") or text)[:120])
            break
    if not l1_sources:
        for interaction in (non_male_male_interactions or []):
            text = str(interaction or "").strip()
            if not text:
                continue
            if _contains_any(text, ["调戏", "口花花", "意淫", "觊觎", "垂涎", "追求", "纠缠", "尾随", "骚扰", "围观", "注视", "盯着", "流言", "传闻"]):
                l1_sources.append(text[:120])
                break
    if l1_sources:
        return {
            "contact_level": "L1",
            "contact_level_label": "言语调戏/被意淫/追求骚扰",
            "contact_level_reason": l1_sources[0],
        }

    return {
        "contact_level": "L0",
        "contact_level_label": "无非男主接触事实",
        "contact_level_reason": "现有结构化事实未见非男主接触、伴侣、性关系或亲生子女线索。",
    }



def _normalize_purity_result_consistency(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    统一修复 purity 结果内的字段一致性，避免出现：
    - is_virgin=False 但 virgin_status=处女（仅男主）
    - body_status 与单字段不一致
    """
    if not isinstance(result, dict):
        return result

    status = str(result.get("virgin_status", "") or "").strip()
    rule_status = str(result.get("rule_virgin_status", "") or "").strip()
    is_virgin = _to_bool(result.get("is_virgin", True), True)
    rule_is_virgin = result.get("rule_is_virgin")
    rule_is_virgin = _to_bool(rule_is_virgin, False) if rule_is_virgin is not None else None

    only_male_lead_markers = (
        "仅男主", "仅与男主", "只与男主", "仅和男主", "只和男主", "仅限男主"
    )
    male_lead_loss_markers = (
        "男主", "主角", "仅男主", "仅与男主", "只与男主", "仅和男主", "只和男主",
        "仅限男主", "被男主", "给男主", "与男主", "和男主"
    )
    explicit_non_male_markers = (
        "非男主", "其他男人", "别的男人", "其他男性", "别的男性", "他人", "第三者"
    )
    only_male_lead = any(m in status or m in rule_status for m in only_male_lead_markers)
    has_non_virgin_word = "非处" in status
    has_virgin_word = ("处女" in status) and (not has_non_virgin_word)
    non_ml_partners = []
    non_ml = result.get("non_ml_sex_partners", [])
    if isinstance(non_ml, list):
        for p in non_ml:
            if isinstance(p, dict):
                pn = str(p.get("name", "") or "").strip()
                if pn:
                    non_ml_partners.append(pn)
    explicit_non_male = (
        bool(non_ml_partners)
        or any(m in status or m in rule_status for m in explicit_non_male_markers)
        or rule_is_virgin is False
    )
    male_lead_only_loss = (
        has_non_virgin_word
        and any(m in status or m in rule_status for m in male_lead_loss_markers)
        and not explicit_non_male
    )

    # 语义优先级：
    # 1) 规则判定为处女且无非男主证据 => 处女（排他性）
    # 2) 明确“仅男主/被男主破处” => 处女（仅男主）
    # 3) 明确“非处”且非男主证据存在 => 非处
    if rule_is_virgin is False and explicit_non_male:
        is_virgin = False
    elif rule_is_virgin is True and not explicit_non_male:
        is_virgin = True
    elif only_male_lead or male_lead_only_loss:
        is_virgin = True
    elif has_non_virgin_word:
        is_virgin = False

    result["is_virgin"] = is_virgin

    if is_virgin:
        if "处女" in rule_status and "非处" not in rule_status:
            result["virgin_status"] = rule_status
        elif has_non_virgin_word:
            result["virgin_status"] = "✅ 处女（仅男主）" if (only_male_lead or male_lead_only_loss) else "✅ 处女"
        elif only_male_lead:
            result["virgin_status"] = "✅ 处女（仅男主）"
    else:
        if "非处" in rule_status:
            result["virgin_status"] = rule_status
        elif has_non_virgin_word:
            result["virgin_status"] = status
        elif has_virgin_word or only_male_lead:
            result["virgin_status"] = f"❌ 非处（与非男主{','.join(non_ml_partners)}有性关系）" if non_ml_partners else "❌ 非处"

    # 同步 body_status，避免展示层出现旧值
    if any(k in result for k in ("virgin_status", "contact_status", "partner_status", "body_status")):
        result["body_status"] = (
            f"处女:{result.get('virgin_status', '?')} | "
            f"接触:{result.get('contact_status', '?')} | "
            f"男伴:{result.get('partner_status', '?')}"
        )

    # 统一 is_clean（兼容顶层与历史 verification 中的豁免字段）
    if all(k in result for k in ("has_other_contact", "no_partner", "is_spirit_clean", "is_virgin")):
        has_other_contact = _to_bool(result.get("has_other_contact", False), False)
        no_partner = _to_bool(result.get("no_partner", True), True)
        is_spirit_clean = _to_bool(result.get("is_spirit_clean", True), True)
        partner_exempted = _to_bool(result.get("partner_exempted_for_clean", False), False)
        verification = result.get("verification", {})
        if isinstance(verification, dict):
            partner_exempted = partner_exempted or _to_bool(verification.get("partner_exempted_for_clean", False), False)
        result["is_clean"] = bool(result.get("is_virgin", True)) and (not has_other_contact) and (no_partner or partner_exempted) and is_spirit_clean

    return result


def _normalize_heroine_report_consistency(heroine_report: Any) -> Any:
    if not isinstance(heroine_report, dict):
        return heroine_report
    for k, v in list(heroine_report.items()):
        if isinstance(v, dict):
            heroine_report[k] = _normalize_purity_result_consistency(v)
    return heroine_report


def save_checkpoint(
    raw_data_path,
    verified_issues,
    rejected_count,
    rejected_issues,
    processed_issue_indices,
    checkpoint_file,
    heroine_report=None,
    pushed_map=None,
    finished=None,
    finished_reason="",
    purity_done=False,
    finish_done=False,
):
    """保存审查进度，支持断点续传；附带女主身心解读与完结判定状态"""
    heroine_report = _normalize_heroine_report_consistency(heroine_report)
    data = {
        "raw_data_path": os.path.abspath(raw_data_path),
        "verified_issues": verified_issues,
        "rejected_count": rejected_count,
        "rejected_issues": rejected_issues,
        "processed_issue_indices": sorted(processed_issue_indices),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "heroine_report": heroine_report,
        "pushed_map": pushed_map,
        "finished": finished,
        "finished_reason": finished_reason,
        "purity_done": purity_done,
        "finish_done": finish_done,
    }
    try:
        _atomic_write_json_checkpoint(checkpoint_file, data)
        logger.info(f"✅ 断点已保存: {checkpoint_file}")
    except Exception as e:
        logger.error(f"保存断点失败: {e}")


def _checkpoint_backup_file(checkpoint_file):
    return f"{checkpoint_file}.bak" if checkpoint_file else None


def _json_checkpoint_is_readable(checkpoint_file):
    try:
        with open(checkpoint_file, "r", encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False


def _fsync_parent_dir(path):
    parent_dir = os.path.dirname(path) or "."
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        dir_fd = os.open(parent_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _copy_json_checkpoint(src_path, dst_path):
    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        dst.write(src.read())
        dst.flush()
        os.fsync(dst.fileno())
    _fsync_parent_dir(dst_path)


def _atomic_write_json_checkpoint(checkpoint_file, data):
    os.makedirs(os.path.dirname(checkpoint_file) or ".", exist_ok=True)
    backup_file = _checkpoint_backup_file(checkpoint_file)
    if os.path.exists(checkpoint_file) and backup_file:
        if _json_checkpoint_is_readable(checkpoint_file):
            try:
                _copy_json_checkpoint(checkpoint_file, backup_file)
            except Exception as exc:
                logger.warning(f"断点备份写入失败: {exc}")
        else:
            logger.warning(f"主断点不可解析，保留现有备份不覆盖: {checkpoint_file}")
    tmp_path = f"{checkpoint_file}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, checkpoint_file)
    _fsync_parent_dir(checkpoint_file)
    if backup_file:
        try:
            _copy_json_checkpoint(checkpoint_file, backup_file)
        except Exception as exc:
            logger.warning(f"断点最新备份同步失败: {exc}")


def _load_json_checkpoint_with_backup(checkpoint_file):
    candidates = [checkpoint_file, _checkpoint_backup_file(checkpoint_file)]
    last_error = None
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if candidate != checkpoint_file:
                logger.warning(f"主断点损坏或不可用，已从备份恢复: {candidate}")
            return data
        except Exception as exc:
            last_error = exc
            logger.warning(f"读取断点失败 {candidate}: {exc}")
    if last_error:
        raise last_error
    return None


def load_checkpoint(raw_data_path, checkpoint_file):
    """加载审查进度；raw_data_path 不匹配时忽略旧断点"""
    backup_file = _checkpoint_backup_file(checkpoint_file)
    if not checkpoint_file or (not os.path.exists(checkpoint_file) and not (backup_file and os.path.exists(backup_file))):
        return [], 0, [], set(), None, None, None, "", False, False
    try:
        data = _load_json_checkpoint_with_backup(checkpoint_file)
        if not isinstance(data, dict):
            return [], 0, [], set(), None, None, None, "", False, False
        if os.path.abspath(raw_data_path) != data.get("raw_data_path"):
            logger.warning("⚠️ 断点文件与当前 raw_data 不匹配，忽略旧进度")
            return [], 0, [], set(), None, None, None, "", False, False
        verified_issues = data.get("verified_issues", [])
        rejected_count = data.get("rejected_count", 0)
        rejected_issues = data.get("rejected_issues", [])
        processed_issue_indices = set(data.get("processed_issue_indices", []))
        heroine_report = _normalize_heroine_report_consistency(data.get("heroine_report"))
        logger.info(f"🧩 已加载断点，已完成 {len(processed_issue_indices)} 条指控")
        return (
            verified_issues,
            rejected_count,
            rejected_issues,
            processed_issue_indices,
            heroine_report,
            data.get("pushed_map"),
            data.get("finished"),
            data.get("finished_reason", ""),
            data.get("purity_done", False),
            data.get("finish_done", False),
        )
    except Exception as e:
        logger.error(f"加载断点失败: {e}，将从头开始")
        return [], 0, [], set(), None, None, None, "", False, False

# --- 2. 新增：基于结构化事实的规则判定函数 ---

# 占位符黑名单：这些不是真正的角色名，应该被忽略
PLACEHOLDER_NAMES = {
    "无", "没有", "不存在", "无人", "无男性", "无伴侣", "无对象",
    "null", "none", "n/a", "na", "未知", "空", "暂无", "无记录",
}

# “父亲/对象=未知”时的强提示：倾向认为是【亲生/正常生育】而不是收养/特殊诞生
# 注意：不要用“怀孕/有孩子”这类过宽词，避免把“想让XX怀孕/求借种”等意图句误判成已生育。
BIOLOGICAL_BIRTH_HINT_KEYWORDS = [
    "正常生育", "正常分娩", "亲生", "生育", "分娩", "产下", "生下", "生了", "诞下", "育有", "已有孩子", "育有子女",
]

# 仅表达“意愿/请求/计划”的关键词（不代表已经发生）
INTENT_ONLY_KEYWORDS = [
    "想要", "希望", "打算", "准备", "求求", "请求", "恳求", "借种", "将来", "以后",
]

# 当 partner/father 为占位符时，仍可凭证据判断发生过【性关系】的强提示
SEX_ACT_HINT_KEYWORDS = [
    "发生关系", "同房", "上床", "圆房", "做爱", "性交", "插入", "抽插", "啪啪", "啪啪啪",
    "强奸", "被强奸", "迷奸", "轮奸", "睡了", "破处", "第一次",
]


def _contains_any(text: str, keywords: List[str]) -> bool:
    if not text:
        return False
    return any(kw in text for kw in keywords)

def _looks_like_parentage_as_child(heroine_name: str, text: str) -> bool:
    """
    识别“角色是某人的女儿/儿子/孩子/之女”等身世描述，避免误判为“该角色生育了孩子”。
    这是对结构化事实抽取噪声的防呆。
    """
    if not text:
        return False
    t = str(text)
    # 明确出现“生下了/产下了/诞下了 + 角色名” => 角色是孩子
    if heroine_name:
        for verb in ["生下了", "产下了", "诞下了", "生下", "产下", "诞下"]:
            if verb + heroine_name in t:
                return True
        # “{name}是X的女儿/之女/儿子/孩子”
        if (heroine_name + "是") in t and ("的女儿" in t or "之女" in t or "的儿子" in t or "的孩子" in t or "的妹妹" in t or "的姐姐" in t):
            return True
        # “X的女儿{name}”
        if ("的女儿" + heroine_name) in t or ("之女" + heroine_name) in t:
            return True
    # 代词版："她是X的女儿/之女"（且不是"她的女儿"）
    if "她是" in t and ("女儿" in t or "之女" in t or "儿子" in t or "孩子" in t):
        if "她的女儿" not in t and "她的儿子" not in t and "她的孩子" not in t:
            return True
    # 母亲视角：女主名 + 生育动词 → 她是母亲，不是孩子
    if heroine_name:
        mother_verbs = ["生下了", "产下了", "诞下了", "生下", "产下", "诞下",
                        "生了", "生育了", "怀了", "分娩了"]
        for verb in mother_verbs:
            if heroine_name + verb in t:
                return False
        # "XX的女儿/儿子/孩子" where XX = heroine → heroine is mother
        if (heroine_name + "的女儿") in t or (heroine_name + "的儿子") in t or (heroine_name + "的孩子") in t:
            return False
        # "XX生下的女儿" pattern
        if (heroine_name + "生下的") in t:
            return False
    # 文本描述了生育事件 → 不是身世句
    has_birth_verb = any(v in t for v in ["生下", "产下", "诞下", "分娩", "生了", "生育"])
    if has_birth_verb:
        return False
    # 异父同母/同父异母/同母异父 + 亲属称谓 → heroine 是孩子/兄弟姐妹
    _half_sibling_markers = ["同母异父", "同父异母", "异父同母", "异母同父"]
    if any(marker in t for marker in _half_sibling_markers):
        _sibling_child_terms = ["的女儿", "之女", "的儿子", "的孩子",
                                "的妹妹", "的姐姐", "的弟弟", "的哥哥"]
        if any(term in t for term in _sibling_child_terms):
            # 排除 "{heroine}的同母异父的妹妹" → heroine 是参照点，不是孩子
            if not heroine_name or not any(
                (heroine_name + "的" + m) in t for m in _half_sibling_markers
            ):
                return True
    # "养女/养子" → heroine 是被收养的孩子
    if any(term in t for term in ["养女", "养子"]):
        _mother_adopted = ["她的养女", "她的养子"]
        if heroine_name:
            _mother_adopted.extend([heroine_name + "的养女", heroine_name + "的养子"])
        if not any(neg in t for neg in _mother_adopted):
            return True
    # 一般形式："某某的女儿/之女"（排除"她的女儿"这种母亲视角）
    if ("的女儿" in t or "之女" in t or "的儿子" in t or "的孩子" in t):
        if "她的女儿" not in t and "她的儿子" not in t and "她的孩子" not in t:
            # 这里宁可保守：只要像身世句就当作"她是孩子"，避免误伤洁度判定
            return True
    return False


# children_info 常见误抽：把同辈/旁系亲属当成“孩子”
NON_CHILD_KINSHIP_TERMS = [
    "妹妹", "姐姐", "弟弟", "哥哥",
    "侄女", "侄子", "外甥女", "外甥",
    "表妹", "表姐", "表弟", "表哥",
    "堂妹", "堂姐", "堂弟", "堂哥",
]


def _looks_like_non_child_kinship_fact(heroine_name: str, text: str, child_name: str = "") -> bool:
    """
    识别 children_info 中把“妹妹/姐姐/弟弟...”等亲属关系误当作孩子的噪声。
    """
    t = str(text or "")
    cn = str(child_name or "").strip()
    if not t and not cn:
        return False

    has_kinship_child_name = bool(cn) and any(term in cn for term in NON_CHILD_KINSHIP_TERMS)

    has_kinship_sentence = False
    kinship_alt = "|".join(re.escape(term) for term in NON_CHILD_KINSHIP_TERMS)
    if kinship_alt:
        try:
            # 如："{name}有一个在读高中的妹妹" / "她有一个弟弟"
            if heroine_name and re.search(
                rf"{re.escape(heroine_name)}有一[个名位][^。；，,\n]{{0,24}}({kinship_alt})",
                t,
            ):
                has_kinship_sentence = True
            if re.search(rf"她有一[个名位][^。；，,\n]{{0,24}}({kinship_alt})", t):
                has_kinship_sentence = True
        except Exception:
            has_kinship_sentence = False

    if not has_kinship_child_name and not has_kinship_sentence:
        return False

    # 若文本明确是“她生下/她收养/她的女儿”，不按噪声处理（防误杀）。
    mother_hints = [
        "她生下", "她产下", "她诞下", "她分娩", "她收养", "她领养",
        "她的女儿", "她的儿子", "她的孩子",
    ]
    if heroine_name:
        mother_hints.extend([
            f"{heroine_name}生下", f"{heroine_name}产下", f"{heroine_name}诞下",
            f"{heroine_name}分娩", f"{heroine_name}收养", f"{heroine_name}领养",
            f"{heroine_name}的女儿", f"{heroine_name}的儿子", f"{heroine_name}的孩子",
        ])
    if any(h in t for h in mother_hints):
        return False

    # child_name 直接是“妹妹/姐姐...”时，优先视为噪声。
    if has_kinship_child_name:
        return True

    # 句式命中且没有任何生育/收养行为词，也视为噪声。
    if not any(w in t for w in ("生下", "产下", "诞下", "分娩", "收养", "领养")):
        return True
    return False


_NONFACT_RELATION_SPEECH_ACT = {"hypothesis", "advice_persuasion", "comparison", "rumor", "question"}
_RELATION_CONDITIONAL_HINTS = ("如果", "假如", "万一", "要是", "倘若", "若是", "就算", "即便", "哪怕")
_RELATION_SCENARIO_HINTS = (
    "想想", "场景", "你想", "会不会", "能忍受", "难受吗", "恶心",
    "以后", "将来", "后来", "可能会", "最后会", "投入了别人的怀抱",
)
_RELATION_FACT_ANCHORS = ("曾经", "已经", "当时", "此前", "已", "确实", "目前")
_RELATION_EVENT_HINTS = ("谈恋爱", "恋爱", "在一起", "结婚", "爱上", "男友", "男朋友", "丈夫", "未婚夫", "恋人")
_RELATION_FUTURE_HINTS = ("以后", "将来", "未来", "后来")
_RELATION_SPECULATIVE_HINTS = (
    "可能会", "会被", "会不会", "你想", "你觉得", "想想", "场景",
    "要和", "要跟", "只能", "无奈地", "想看到这样的场景",
)

# 精神维度豁免相关关键词（不改 Schema，仅用于后置清洗/兜底）
_SPIRIT_FORCED_OR_TRAUMA_HINTS = (
    "强奸", "强暴", "性侵", "猥亵", "胁迫", "逼迫", "强迫", "被迫", "威胁", "要挟",
    "未遂", "没得逞", "企图强奸", "企图侵犯", "收买父母", "收买了", "逼迫受孕", "被迫受孕",
    "家暴", "被伤害", "创伤", "厌恶", "讨厌", "仇恨", "畜生", "不屑于欺骗",
)
_SPIRIT_NO_SEX_CONCEPTION_HINTS = (
    "没有性行为", "无性行为", "未发生性行为", "未同房", "未圆房",
    "试管", "试管婴儿", "人工授精", "人工受精", "人工受孕", "辅助生殖",
    "供精", "精子库", "胚胎移植", "体外受精", "借种",
)
_SPIRIT_NEGATIVE_FEELING_HINTS = (
    "不喜欢", "不爱", "并不爱", "不是爱", "没感情", "无感情", "没有感情",
    "厌恶", "讨厌", "仇恨", "恨", "反感", "抗拒",
)
_SPIRIT_POSITIVE_FEELING_PATTERNS = (
    r"爱上",
    r"爱过",
    r"真心.{0,4}(爱|喜欢)",
    r"动心",
    r"心动",
    r"深爱",
    r"相爱",
    r"喜欢上",
)
_SPIRIT_SEX_CONFIRM_HINTS = (
    "发生关系", "同房", "上床", "圆房", "做爱", "性交", "插入", "抽插",
    "破处", "第一次", "睡了", "被强奸", "强奸了", "强暴了",
)
_SPIRIT_SEX_NEGATION_HINTS = (
    "未遂", "没得逞", "企图", "没有性行为", "无性行为", "未发生性行为", "未同房", "未圆房",
)


def _spirit_join_blob(*parts: Any) -> str:
    return "\n".join(str(p or "") for p in parts if str(p or "").strip())


def _has_forced_or_trauma_signal(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    return any(k in t for k in _SPIRIT_FORCED_OR_TRAUMA_HINTS)


def _has_no_sex_conception_signal(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    return any(k in t for k in _SPIRIT_NO_SEX_CONCEPTION_HINTS)


def _has_negative_feeling_signal(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    return any(k in t for k in _SPIRIT_NEGATIVE_FEELING_HINTS)


def _has_positive_feeling_signal(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    for pat in _SPIRIT_POSITIVE_FEELING_PATTERNS:
        try:
            if re.search(pat, t):
                return True
        except Exception:
            continue
    return False


def _has_confirmed_non_ml_sex_signal(text: str) -> bool:
    t = str(text or "")
    if not t:
        return False
    if _has_no_sex_conception_signal(t):
        return False
    if any(k in t for k in _SPIRIT_SEX_NEGATION_HINTS):
        # 出现未遂/无性行为信号时，不直接按已发生性关系处理
        # 但若存在非常明确的“已发生”动作词，再交给后续关键词判断
        neg_only = not any(k in t for k in _SPIRIT_SEX_CONFIRM_HINTS)
        if neg_only:
            return False
    return any(k in t for k in _SPIRIT_SEX_CONFIRM_HINTS)


def _collect_non_ml_sex_evidence_for_spirit(
    sexual_relations: Optional[List[Dict[str, Any]]],
    male_lead: str,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    ml = str(male_lead or "").strip()
    for sr in (sexual_relations or []):
        if not isinstance(sr, dict):
            continue
        if sr.get("is_male_lead", False):
            continue
        partner = str(sr.get("partner", "") or "").strip()
        detail = str(sr.get("detail", "") or "")
        evidence = str(sr.get("evidence", "") or "")
        blob = _spirit_join_blob(partner, detail, evidence)
        if ml and _text_contains_male_lead(blob, ml):
            continue
        if not _has_confirmed_non_ml_sex_signal(blob):
            continue
        out.append(
            {
                "partner": partner if partner and (not _is_placeholder(partner)) else "未知男性",
                "evidence": (evidence or detail or "")[:120],
            }
        )
    return out


def _build_partner_analysis_map(analyzed_partners: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for p in (analyzed_partners or []):
        if not isinstance(p, dict):
            continue
        partner = str(p.get("partner", "") or p.get("name", "") or "").strip()
        if not partner:
            continue
        key = _normalize_partner_key(partner)
        out[key] = p
    return out


def _is_positive_non_ml_feeling_record(record: Dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        if _is_nonfact_relation_or_feeling_record(record):
            return False
    except Exception:
        pass
    blob = _spirit_join_blob(
        record.get("target", ""),
        record.get("detail", ""),
        record.get("evidence", ""),
    )
    # 仅把“明确正向动心”作为兜底覆盖依据，减少把“是否是爱”的犹疑句误当事实
    return _has_positive_feeling_signal(blob)


def _is_spirit_exempt_partner_relation(
    partner_record: Dict[str, Any],
    analyzed_partner_map: Optional[Dict[str, Dict[str, Any]]] = None,
    has_non_ml_sex_evidence: bool = False,
) -> Tuple[bool, str]:
    """
    判断某条非男主伴侣记录是否属于“精神维度豁免关系”：
    1) 强迫/受害信号（或无性行为受孕/试管信号）
    2) 且 has_feelings=false 或无正向动心证据
    3) 且无已发生的非男主性关系证据
    """
    if not isinstance(partner_record, dict):
        return False, "记录无效"

    partner = str(partner_record.get("partner", "") or "").strip()
    rel = str(
        partner_record.get("relationship")
        or partner_record.get("relation")
        or partner_record.get("relation_type")
        or ""
    ).strip()
    status = str(partner_record.get("status", "") or "").strip()
    detail = str(partner_record.get("detail", "") or "")
    evidence = str(partner_record.get("evidence", "") or "")
    analysis_reason = str(partner_record.get("analysis_reason", "") or "")
    blob = _spirit_join_blob(partner, rel, status, detail, evidence, analysis_reason)

    analyzed = None
    if analyzed_partner_map:
        try:
            key = _normalize_partner_key(partner)
            analyzed = analyzed_partner_map.get(key)
        except Exception:
            analyzed = None

    forced_flag = partner_record.get("forced", None)
    has_feelings_flag = partner_record.get("has_feelings", None)
    if isinstance(analyzed, dict):
        if forced_flag is None:
            forced_flag = analyzed.get("forced", None)
        if has_feelings_flag is None:
            has_feelings_flag = analyzed.get("has_feelings", None)
        blob = _spirit_join_blob(blob, analyzed.get("analysis_reason", ""), analyzed.get("evidence", ""))

    forced_or_trauma = (forced_flag is True) or _has_forced_or_trauma_signal(blob)
    no_sex_conception = _has_no_sex_conception_signal(blob)
    has_positive_feelings = _has_positive_feeling_signal(blob)
    no_positive_feelings = (has_feelings_flag is False) or (not has_positive_feelings)
    has_sex_signal = has_non_ml_sex_evidence or _has_confirmed_non_ml_sex_signal(blob)

    exempt = (forced_or_trauma or no_sex_conception) and no_positive_feelings and (not has_sex_signal)

    reason_parts: List[str] = []
    if forced_flag is True:
        reason_parts.append("forced=true")
    if forced_or_trauma and forced_flag is not True:
        reason_parts.append("命中强迫/受害关键词")
    if no_sex_conception:
        reason_parts.append("命中无性行为受孕/试管信号")
    if has_feelings_flag is False:
        reason_parts.append("has_feelings=false")
    elif not has_positive_feelings:
        reason_parts.append("无正向动心证据")
    if has_sex_signal:
        reason_parts.append("存在非男主性关系证据")
    return exempt, "，".join(reason_parts) if reason_parts else "未命中豁免条件"


def _is_nonfact_relation_or_feeling_record(record: Dict[str, Any]) -> bool:
    """
    识别 partner_relations / romantic_feelings 中“假设/劝说/类比/传闻”条目，避免误判。
    """
    if not isinstance(record, dict):
        return True

    speech_act = _normalize_speech_act(record.get("speech_act", ""))
    if speech_act and speech_act != "asserted_fact":
        if speech_act in _NONFACT_RELATION_SPEECH_ACT:
            return True
        # 非空且非 asserted_fact，保守视为非事实
        return True

    # 【新增】检查回溯阶段 LLM 检测到的非事实语气
    resolution_nonfact = str(record.get("_resolution_nonfact_speech_act", "") or "").strip().lower()
    if resolution_nonfact and resolution_nonfact != "asserted_fact":
        # 豁免：原始 speech_act 为 asserted_fact 且含明确关系词 → 不过滤
        original_speech_act = _normalize_speech_act(record.get("speech_act", ""))
        blob_for_rel_check = "\n".join([
            str(record.get("relationship", "") or ""),
            str(record.get("relation", "") or ""),
            str(record.get("relation_type", "") or ""),
            str(record.get("evidence", "") or ""),
            str(record.get("detail", "") or ""),
        ])
        has_explicit_rel = any(k in blob_for_rel_check for k in _EXPLICIT_PARTNER_ROLE_HINTS)
        if original_speech_act == "asserted_fact" and has_explicit_rel:
            pass  # 豁免：优先信任原始提取的事实性判定
        else:
            return True

    evidence_strength = str(record.get("evidence_strength", "") or "").strip().lower()
    if evidence_strength in {"weak", "low"}:
        return True

    blob = "\n".join([
        str(record.get("relationship", "") or ""),
        str(record.get("relation", "") or ""),
        str(record.get("relation_type", "") or ""),
        str(record.get("status", "") or ""),
        str(record.get("detail", "") or ""),
        str(record.get("evidence", "") or ""),
    ]).strip()
    if not blob:
        return True

    has_conditional = any(k in blob for k in _RELATION_CONDITIONAL_HINTS)
    has_scenario = any(k in blob for k in _RELATION_SCENARIO_HINTS)
    if has_conditional and has_scenario:
        return True

    # 未来场景 + 推测语气 + 关系事件（即使没有“如果/就算”也按非事实处理）
    has_relation_event = any(k in blob for k in _RELATION_EVENT_HINTS)
    has_future_hint = any(k in blob for k in _RELATION_FUTURE_HINTS)
    has_speculative = any(k in blob for k in _RELATION_SPECULATIVE_HINTS)
    if has_relation_event and has_future_hint and has_speculative:
        if not any(k in blob for k in _RELATION_FACT_ANCHORS):
            return True

    if has_scenario and ("?" in blob or "？" in blob):
        if not any(k in blob for k in _RELATION_FACT_ANCHORS):
            return True

    return False


def _sanitize_purity_facts_for_heroine(heroine_name: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    清洗结构化事实，移除“身世/出身”及“妹妹/姐姐等亲属关系”
    被误抽到 children_info 的噪声项。
    典型误抽：`evidence` 类似“老夫人与X生下了{heroine}”“{heroine}是一条夫人的女儿”。

    目的：
    - 避免 LLM/规则把“她是某人的女儿”误当成“她生过孩子”，从而把处女误判成非处。
    """
    if not facts or not isinstance(facts, dict):
        return _empty_purity_fact_bucket()

    cleaned: Dict[str, Any] = _normalize_purity_fact_bucket(facts)
    cleaned["children_info"] = []
    cleaned["romantic_feelings"] = []
    cleaned["partner_relations"] = []

    for child in (facts.get("children_info", []) or []):
        try:
            # 聚合可能出现“身世句”的字段
            evidence = str(child.get("evidence", "") or "")
            detail = str(child.get("detail", "") or "")
            origin = str(child.get("origin", "") or "")
            child_name = str(child.get("child_name", "") or "")
            # 直接字段检查：child_name 就是女主名 → 女主是孩子，不是母亲
            if heroine_name and child_name and child_name == heroine_name:
                continue
            blob = "\n".join([evidence, detail, origin, child_name]).strip()
            if _looks_like_parentage_as_child(heroine_name, blob):
                continue
            if _looks_like_non_child_kinship_fact(heroine_name, blob, child_name):
                continue
        except Exception:
            # 保守策略：清洗失败时不丢数据
            pass
        cleaned["children_info"].append(child)

    # romantic_feelings：过滤“假设/劝说/类比”噪声
    for rf in (facts.get("romantic_feelings", []) or []):
        if not isinstance(rf, dict):
            continue
        target = str(rf.get("target", "") or "").strip()
        if _is_placeholder(target):
            continue
        try:
            if _is_nonfact_relation_or_feeling_record(rf):
                continue
        except Exception:
            pass
        cleaned["romantic_feelings"].append(rf)

    # partner_relations：过滤“假设/劝说/类比”噪声
    for pr in (facts.get("partner_relations", []) or []):
        if not isinstance(pr, dict):
            continue
        partner = str(pr.get("partner", "") or "").strip()
        if _is_placeholder(partner):
            continue
        try:
            if _is_nonfact_relation_or_feeling_record(pr):
                continue
        except Exception:
            pass
        cleaned["partner_relations"].append(pr)

    return cleaned


def _text_contains_male_lead(text: str, male_lead: str) -> bool:
    """弱规则：文本中是否提到男主（用于纠正 is_male_lead 抽取错误）。"""
    if not text or not male_lead:
        return False
    ml = str(male_lead).strip()
    if not ml:
        return False
    t = str(text)
    # 男主名太短时避免误匹配
    if len(ml) <= 1:
        return t.strip() == ml
    # 兼容英文大小写
    return ml.lower() in t.lower()


def _normalize_is_male_lead_flags_in_facts(facts: Dict[str, Any], male_lead: str) -> Dict[str, Any]:
    """
    纠正结构化事实中 is_male_lead 的抽取错误：
    - 若 partner/target 或 evidence/detail/origin 明确包含男主名，则强制 is_male_lead=True
    - 目的：避免把“与男主发生关系”误当成“与非男主发生关系”，从而误判处女/精神洁/男伴等。
    """
    if not facts or not isinstance(facts, dict) or not male_lead:
        return facts

    ml = str(male_lead).strip()
    if not ml:
        return facts

    def _fix(items: Any, name_keys: List[str]) -> None:
        if not items:
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            # 已明确为男主则不动
            if it.get("is_male_lead") is True:
                continue
            # 显式“男主/主角”视为男主
            for k in name_keys:
                v = str(it.get(k, "") or "").strip()
                if v in ("男主", "主角"):
                    it["is_male_lead"] = True
                    break
            if it.get("is_male_lead") is True:
                continue

            blob = "\n".join(
                [
                    *(str(it.get(k, "") or "") for k in name_keys),
                    str(it.get("evidence", "") or ""),
                    str(it.get("detail", "") or ""),
                    str(it.get("origin", "") or ""),
                ]
            )
            if _text_contains_male_lead(blob, ml):
                it["is_male_lead"] = True

    _fix(facts.get("sexual_relations", []), ["partner"])
    _fix(facts.get("partner_relations", []), ["partner"])
    _fix(facts.get("physical_contacts", []), ["partner"])
    _fix(facts.get("romantic_feelings", []), ["target"])
    return facts


def _infer_non_male_lead_sex(
    sexual_relations: List[Dict[str, Any]],
    male_lead: str,
) -> Tuple[bool, List[Dict[str, str]], bool]:
    """
    基于结构化 sexual_relations 推断是否存在【非男主】性关系（用于“排他性处女”兜底）。

    返回：
    - has_non_ml_sex: 是否存在非男主性关系
    - non_ml_partners: 非男主性关系对象列表（用于报告）
    - has_male_lead_sex: 是否存在与男主性关系（用于输出“仅男主”）
    """
    non_ml_partners: List[Dict[str, str]] = []
    has_male_lead_sex = False
    ml = str(male_lead or "").strip()

    for sr in sexual_relations or []:
        if not isinstance(sr, dict):
            continue
        partner = str(sr.get("partner", "") or "").strip()
        evidence = str(sr.get("evidence", "") or "")
        detail = str(sr.get("detail", "") or "")
        blob = "\n".join([partner, detail, evidence])

        # 判断是否男主（优先信 is_male_lead；其次看文本是否提到男主）
        is_ml = bool(sr.get("is_male_lead", False))
        if not is_ml and ml and _text_contains_male_lead(blob, ml):
            is_ml = True
        if partner in ("男主", "主角"):
            is_ml = True
        if is_ml:
            has_male_lead_sex = True
            continue

        # 非男主：partner 若非占位符，直接记为非男主性关系
        if partner and (not _is_placeholder(partner)):
            non_ml_partners.append({"name": partner, "evidence": (evidence or detail or "")})
            continue

        # partner 为占位符：仅在有明确性行为提示时才算非男主性关系
        if _contains_any(detail + "\n" + evidence, SEX_ACT_HINT_KEYWORDS):
            # 若同时能看出是男主，则归为男主（避免“未知”误伤）
            if ml and _text_contains_male_lead(blob, ml):
                has_male_lead_sex = True
            else:
                non_ml_partners.append({"name": "未知男性", "evidence": (evidence or detail or "")})

    return (len(non_ml_partners) > 0, non_ml_partners, has_male_lead_sex)


# ========= 伴侣对象名"泛指/指代不明"清洗（避免误判） =========
_VAGUE_MALE_NAME_PATTERNS = [
    # 典型：某个男人/一个男人/那个男人/这男人/某位男性/一名男子 等
    r"^(某(个|位|名)?|一(个|位|名)?|那(个)?|这(个)?|另(外)?一(个|位|名)?|别(的)?|其(他)?)(男人|男性|男子|男士|男的|男生)$",
    # 某人/那人/这个人
    r"^(某人|那人|这个人|那个人|另(外)?一(个|位|名)?人|别人|其他人)$",
    # 仅代词
    r"^(他|他人)$",
]


def _is_vague_male_name(name: str) -> bool:
    """判断对象名是否为"泛指男性/指代不明"（如"某个男人""他"）。"""
    s = str(name or "").strip()
    if not s:
        return True
    for pat in _VAGUE_MALE_NAME_PATTERNS:
        try:
            import re
            if re.match(pat, s):
                return True
        except Exception:
            continue
    return False


_FEMALE_ROLE_HINTS = (
    "女性", "女人", "女生", "女孩", "女孩子", "女方",
    "小姐", "小姐姐", "姐姐", "妹妹",
    "母亲", "妈妈", "太太", "妻子", "女友", "前女友",
    "闺蜜", "百合", "蕾丝", "女同",
)

_MALE_ROLE_HINTS = (
    "男性", "男人", "男生", "男孩", "男方", "男士", "先生",
    "男友", "前男友", "丈夫", "老公",
    "父亲", "爸爸", "哥哥", "弟弟",
    "某个男人", "一名男子", "男性角色",
)

_STRONG_MALE_RELATION_HINTS = (
    "父亲", "爸爸", "丈夫", "老公",
    "前夫", "未婚夫", "男友", "前男友",
    "男朋友", "夫君", "夫婿", "相公",
)


def _normalize_name_for_match(name: str) -> str:
    s = str(name or "").strip()
    if not s:
        return ""
    return re.sub(r"[\s·•\-_/,，。:：;；!！?？'\"“”‘’`~（）()\[\]【】<>《》|]+", "", s)


def _build_name_norm_set(names: Optional[List[str]]) -> set[str]:
    out: set[str] = set()
    for n in (names or []):
        nn = _normalize_name_for_match(n)
        if nn:
            out.add(nn)
    return out


def _name_matches_norm_set(name: str, norm_set: set[str]) -> bool:
    name_norm = _normalize_name_for_match(name)
    if not name_norm or not norm_set:
        return False
    for item in norm_set:
        if name_norm == item:
            return True
        if len(name_norm) >= 2 and len(item) >= 2 and (name_norm in item or item in name_norm):
            return True
    return False


def _extract_partner_from_contact_interaction(interaction: str) -> str:
    s = str(interaction or "")
    if not s:
        return ""
    patterns = [
        r"[与和]([^:：，,。；;\s\(\)（）\[\]【】]+)互动",
        r"(?:被|由)([^:：，,。；;\s\(\)（）\[\]【】]+)(?:触碰|接触|按摩|拥抱|亲吻|猥亵|抚摸|摸)",
    ]
    for pat in patterns:
        try:
            m = re.search(pat, s)
            if m:
                return str(m.group(1) or "").strip()
        except Exception:
            continue
    return ""


def _is_likely_male_counterpart(
    partner: str,
    context_text: str = "",
    female_name_norm_set: Optional[set[str]] = None,
) -> Optional[bool]:
    """
    返回：
    - True: 明确男性
    - False: 明确女性
    - None: 性别未知
    """
    p = str(partner or "").strip()
    ctx = str(context_text or "")
    blob = f"{p} {ctx}"
    female_name_norm_set = female_name_norm_set or set()

    # 最小修复：出现明确男性关系词时，不要被女性名子串误判后过滤掉
    if any(k in blob for k in _STRONG_MALE_RELATION_HINTS):
        return True

    if p and _name_matches_norm_set(p, female_name_norm_set):
        return False

    if any(k in blob for k in _FEMALE_ROLE_HINTS):
        return False

    if any(k in blob for k in _MALE_ROLE_HINTS):
        return True

    if p and _is_vague_male_name(p):
        return True

    if _contains_any(blob, ["男性", "男人", "男生", "男孩", "男士", "男的"]):
        return True
    if _contains_any(blob, ["女性", "女人", "女生", "女孩", "女的", "女士"]):
        return False

    return None


# ========= children_info 预清洗常量（集中定义，易维护） =========

# speech_act 中英文标准化映射（LLM 有时输出中文而非英文枚举值）
_SPEECH_ACT_NORMALIZE_MAP = {
    "陈述": "asserted_fact",
    "叙述": "asserted_fact",
    "事实": "asserted_fact",
    "事实陈述": "asserted_fact",
    "描述": "asserted_fact",
    "提及": "asserted_fact",
    "提到": "asserted_fact",
    "假设": "hypothesis",
    "推测": "hypothesis",
    "猜测": "hypothesis",
    "劝说": "advice_persuasion",
    "建议": "advice_persuasion",
    "劝告": "advice_persuasion",
    "类比": "comparison",
    "比喻": "comparison",
    "传闻": "rumor",
    "谣言": "rumor",
    "听说": "rumor",
    "疑问": "question",
}

_SPEECH_ACT_KNOWN_VALUES = frozenset({
    "asserted_fact", "hypothesis", "advice_persuasion", "comparison", "rumor", "question",
})

def _normalize_speech_act(raw) -> str:
    """将中文/非标准 speech_act 标准化为英文枚举值。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if s in _SPEECH_ACT_KNOWN_VALUES:
        return s
    return _SPEECH_ACT_NORMALIZE_MAP.get(s, s)

# 仅怀孕提及的关键词（不代表孩子已存在）
PREGNANCY_ONLY_WORDS = [
    "怀孕", "有孕", "身孕", "孕肚", "怀上", "怀了", "有了身孕", "有喜",
    "肚子里的孩子", "腹中胎儿", "胎儿", "胎动", "怀胎", "害喜", "孕吐",
    "我们都怀孕了", "怀上了孩子", "怀的孩子", "怀了孩子",
]

# 已出生/已存在的强证据词。与泛词“孩子/宝宝”分开，避免“怀上小宝宝”被误判为已生育。
STRONG_CHILD_EXISTENCE_WORDS = [
    "生下", "产下", "分娩", "出生", "诞生", "降生", "生产", "生了",
    "收养", "领养", "养女", "养子", "继子", "继女", "认作女儿", "认作儿子",
    "当作女儿", "当作儿子", "过继", "寄养", "托孤", "遗孤",
    "满月", "周岁", "长大", "成年", "已经X岁",
]

# 孩子确实存在的强证据关键词
CHILD_EXISTS_WORDS = [
    *STRONG_CHILD_EXISTENCE_WORDS,
    "儿子", "女儿", "孩子", "宝宝", "婴儿", "小孩",
]
GENERIC_CHILD_REFERENCE_WORDS = ["儿子", "女儿", "孩子", "宝宝", "婴儿", "小孩"]

# 收养/非亲生关键词
ADOPTION_WORDS = [
    "收养", "领养", "养女", "养子", "继子", "继女", "继母", "继父",
    "认作女儿", "认作儿子", "当作女儿", "当作儿子", "认成女儿",
    "过继", "寄养", "托孤", "遗孤", "捡来的", "救下后收养",
    "不是亲生", "非亲生", "没有血缘", "毫无血缘",
]

NEGATED_CHILD_EXISTENCE_PREFIXES = [
    "未写明", "未出现", "未实际", "未见", "未能", "尚未", "并未",
    "没有", "并没有", "没写", "没出现", "没能", "不曾",
    "尚无", "无法确认", "不能确认", "仅能确认",
]


def _has_strong_child_existence_evidence(text: str) -> bool:
    """识别出生/收养等强证据，同时排除“未写明出生”这类否定语境。"""
    text = str(text or "")
    for word in STRONG_CHILD_EXISTENCE_WORDS + ADOPTION_WORDS:
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            prefix = text[max(0, idx - 12):idx]
            if not any(marker in prefix for marker in NEGATED_CHILD_EXISTENCE_PREFIXES):
                return True
            start = idx + len(word)
    return False


def _has_generic_child_reference(text: str) -> bool:
    """识别泛称子女词，同时排除“没有孩子”这类否定语境。"""
    text = str(text or "")
    for word in GENERIC_CHILD_REFERENCE_WORDS:
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            prefix = text[max(0, idx - 8):idx]
            if not any(marker in prefix for marker in NEGATED_CHILD_EXISTENCE_PREFIXES):
                return True
            start = idx + len(word)
    return False

# 试管/人工受孕关键词
IVF_WORDS = [
    "试管", "人工授精", "人工受孕", "供精", "精子库", "胚胎移植", "代孕",
    "取卵", "体外受精", "辅助生殖", "借种", "借腹生子",
]

# 魔法/非自然诞生关键词
MAGIC_WORDS = [
    "单性繁殖", "无性繁殖", "孤雌生殖", "分裂", "神明赐予", "天降",
    "转生", "克隆", "复制", "魔法", "法术", "功法", "造物", "化身",
    "凭空出现", "突然出现", "从天而降",
]

# 劝说/假设/类比语气词（表明不是事实陈述）
PERSUASION_WORDS = [
    "说不定", "难道", "想让", "应该", "可以", "不如", "要不",
    "更像", "差不多", "迟早", "可能", "也许", "大概", "估计",
    "如果", "假如", "万一", "要是", "倘若", "若是",
    "莫非", "怎么会", "不会吧", "听说", "据说", "有人说", "传言",
    "猜测", "推测", "怀疑", "觉得", "认为", "以为",
]

# 身世倒装模式（"{name}是某人的女儿"表示女主的身世，不是女主的孩子）
# 用于运行时构建正则
IDENTITY_TRAP_PATTERN_TEMPLATES = [
    r"{name}是.{{0,10}}(女儿|孩子|儿子|闺女)",
    r"(生下|产下).{{0,10}}{name}",
    r"{name}的(母亲|父亲|爹|娘|亲生父母)",
    r"(她|他)是{name}的(母亲|父亲|爹|娘)",
]

# 男主孩子的强模式（用于快速判定父亲是男主）
# {male_lead} 在运行时替换
MALE_LEAD_CHILD_PATTERN_TEMPLATES = [
    r"(怀了|怀上|怀的是|有了){male_lead}的(孩子|种|骨肉)",
    r"(孩子|孩子们)是{male_lead}的",
    r"{male_lead}的(孩子|儿子|女儿|种|骨肉)",
    r"(给|替){male_lead}(生|怀)(了|上|下)",
]


def _classify_child_record_heuristic(
    heroine_name: str,
    record: Dict[str, Any],
    male_lead: str = "",
) -> Tuple[str, str]:
    """
    对单条 children_info 记录进行规则分类（纯 heuristic，不调用 LLM）
    
    返回: (category, reason)
    category:
      - "CHILD_EXISTS_STRONG": 孩子已存在/出生/收养等强证据
      - "PREGNANCY_ONLY_OR_WEAK": 仅怀孕提及/肚子里孩子/劝说类比/主体不锚定/证据弱
      - "NOT_A_CHILD_FACT": 身世倒装（"某人生下了{name}"，"{name}是某人的女儿"）
      - "MALE_LEAD_CHILD": 明确是男主的孩子（用于快速判定，规则优先）
    """
    import re
    
    evidence = str(record.get("evidence", "") or "").strip()
    detail = str(record.get("detail", "") or "").strip()
    origin = str(record.get("origin", "") or "").strip()
    child_name = str(record.get("child_name", "") or "").strip()
    father = str(record.get("father", "") or "").strip()
    
    # 新增字段（向后兼容，可能缺失）
    speech_act = _normalize_speech_act(record.get("speech_act", ""))
    evidence_strength = str(record.get("evidence_strength", "") or "").strip().lower()
    child_status = str(record.get("child_status", "") or "").strip().lower()
    
    combined_text = f"{evidence} {detail} {origin}"
    combined_lower = combined_text.lower()
    
    # ===== 1. 身世倒装检测（最高优先级）=====
    # 如果证据说的是"某人生下了{heroine_name}"或"{heroine_name}是某人的女儿"，这不是女主的孩子
    for template in IDENTITY_TRAP_PATTERN_TEMPLATES:
        pattern = template.format(name=re.escape(heroine_name))
        try:
            if re.search(pattern, combined_text, re.IGNORECASE):
                return ("NOT_A_CHILD_FACT", f"身世倒装：证据描述的是{heroine_name}的出身，而非{heroine_name}的孩子")
        except Exception:
            continue

    # ===== 1b. 亲属关系误抽检测（妹妹/姐姐等）=====
    if _looks_like_non_child_kinship_fact(heroine_name, combined_text, child_name):
        return ("NOT_A_CHILD_FACT", "亲属关系词（妹妹/姐姐/弟弟等），非子女事实")
    
    # ===== 2. 语用类型过滤（来自 scan prompt 的新字段） =====
    # 如果 speech_act 不是 asserted_fact，则视为弱证据
    if speech_act and speech_act not in ("asserted_fact", ""):
        return ("PREGNANCY_ONLY_OR_WEAK", f"语用类型非事实陈述：speech_act={speech_act}")
    
    # 如果 evidence_strength 是 weak，则视为弱证据
    if evidence_strength == "weak":
        return ("PREGNANCY_ONLY_OR_WEAK", f"证据强度弱：evidence_strength={evidence_strength}")
    
    # ===== 3. 劝说/假设/类比检测 =====
    persuasion_found = []
    for word in PERSUASION_WORDS:
        if word in combined_text:
            persuasion_found.append(word)
    
    # 如果命中劝说词且同时命中怀孕词，则归类为弱证据
    pregnancy_found = any(w in combined_text for w in PREGNANCY_ONLY_WORDS)
    strong_child_exists_found = _has_strong_child_existence_evidence(combined_text)
    if persuasion_found and pregnancy_found:
        return ("PREGNANCY_ONLY_OR_WEAK", f"劝说/假设语气+怀孕提及：'{', '.join(persuasion_found[:3])}'")
    
    # 仅有劝说词也要过滤
    if persuasion_found and not any(w in combined_text for w in CHILD_EXISTS_WORDS):
        return ("PREGNANCY_ONLY_OR_WEAK", f"疑似劝说/假设语气：'{', '.join(persuasion_found[:3])}'")
    
    # ===== 4. 仅怀孕提及检测（无孩子存在强证据） =====
    # “怀上孩子/怀上小宝宝”里的孩子/宝宝只是胎儿语境，不代表已出生或已存在的子女。
    if child_status == "pregnant" and not strong_child_exists_found:
        return ("PREGNANCY_ONLY_OR_WEAK", f"child_status=pregnant，无出生证据")
    
    if pregnancy_found and not strong_child_exists_found:
        return ("PREGNANCY_ONLY_OR_WEAK", f"仅怀孕提及，无孩子出生/存在证据")
    
    # ===== 5. 男主孩子快速判定 =====
    if male_lead:
        male_lead_escaped = re.escape(male_lead)
        for template in MALE_LEAD_CHILD_PATTERN_TEMPLATES:
            pattern = template.format(male_lead=male_lead_escaped)
            try:
                if re.search(pattern, combined_text, re.IGNORECASE):
                    return ("MALE_LEAD_CHILD", f"证据明确指向男主的孩子：匹配模式'{template}'")
            except Exception:
                continue
        # 额外检查：father 字段直接是男主
        if father:
            father_lower = (father or "").lower()
            male_lead_lower = (male_lead or "").lower()
            if father_lower == male_lead_lower or father in ["男主", "主角"]:
                return ("MALE_LEAD_CHILD", f"father字段明确为男主：{father}")
            if male_lead_lower in father_lower or father_lower in male_lead_lower:
                return ("MALE_LEAD_CHILD", f"father字段疑似男主：{father}")
    
    # ===== 6. 强证据判定（收紧） =====
    # 只有命中明确“孩子存在/收养”词，才算强证据
    if strong_child_exists_found or _has_generic_child_reference(combined_text):
        return ("CHILD_EXISTS_STRONG", "命中孩子存在/收养关键词")

    # 其他情况一律按弱证据处理，避免把噪声当“已生育”
    return ("PREGNANCY_ONLY_OR_WEAK", "无孩子存在强证据，按弱证据处理")


def _preclean_children_info(
    heroine_name: str,
    children_info: List[Dict[str, Any]],
    male_lead: str = "",
) -> Dict[str, Any]:
    """
    对 children_info 进行预清洗（纯规则 heuristic，不调用 LLM）
    
    返回:
    {
        "strong_child_records": [...],      # 可进入合并与 Step1 判定
        "male_lead_child_records": [...],   # 明确是男主孩子的记录（可快速跳过）
        "pregnancy_only_records": [...],    # 仅怀孕提及/弱证据（不进入判定）
        "discarded_records": [...],         # 身世倒装等（直接丢弃）
        "summary": "...",                   # 预清洗摘要
    }
    """
    strong_records = []
    male_lead_records = []
    pregnancy_only_records = []
    discarded_records = []
    
    for record in children_info or []:
        if not isinstance(record, dict):
            continue
        
        category, reason = _classify_child_record_heuristic(heroine_name, record, male_lead)
        
        # 添加分类信息到记录（供调试）
        record_with_meta = dict(record)
        record_with_meta["_preclean_category"] = category
        record_with_meta["_preclean_reason"] = reason
        
        if category == "NOT_A_CHILD_FACT":
            discarded_records.append(record_with_meta)
        elif category == "PREGNANCY_ONLY_OR_WEAK":
            pregnancy_only_records.append(record_with_meta)
        elif category == "MALE_LEAD_CHILD":
            male_lead_records.append(record_with_meta)
        else:  # CHILD_EXISTS_STRONG
            strong_records.append(record_with_meta)
    
    summary_parts = []
    if strong_records:
        summary_parts.append(f"强证据记录{len(strong_records)}条")
    if male_lead_records:
        summary_parts.append(f"男主孩子记录{len(male_lead_records)}条")
    if pregnancy_only_records:
        summary_parts.append(f"仅怀孕/弱证据{len(pregnancy_only_records)}条")
    if discarded_records:
        summary_parts.append(f"身世倒装丢弃{len(discarded_records)}条")
    
    summary = "; ".join(summary_parts) if summary_parts else "无记录"
    
    return {
        "strong_child_records": strong_records,
        "male_lead_child_records": male_lead_records,
        "pregnancy_only_records": pregnancy_only_records,
        "discarded_records": discarded_records,
        "summary": summary,
    }


def _judge_single_child_origin_by_rule(
    heroine_name: str,
    child_name: str,
    child_records: List[Dict[str, Any]],
    male_lead: str,
) -> Optional[Dict[str, Any]]:
    """
    规则优先判定单个孩子的诞生方式（不调用 LLM）
    
    返回:
      - None: 规则无法确定，需要调用 LLM 兜底
      - Dict: 规则已确定的结果
    
    判定优先级：
    1. is_biological=False → needs_male=False, birth_method="收养/非亲生"
    2. 命中 ADOPTION_WORDS → needs_male=False
    3. 命中 IVF_WORDS → needs_male=False
    4. 命中 MAGIC_WORDS → needs_male=False
    5. 明确男主孩子 → needs_male=True, is_father_male_lead=True
    6. 记录冲突/不确定 → 返回 None，调用 LLM
    """
    import re
    
    if not child_records:
        return None
    
    # 收集所有证据文本
    all_text = ""
    has_is_biological_false = False
    has_is_biological_true = False
    has_adoption_word = False
    has_ivf_word = False
    has_magic_word = False
    has_male_lead_pattern = False
    father_mentions = set()
    
    for record in child_records:
        evidence = str(record.get("evidence", "") or "")
        detail = str(record.get("detail", "") or "")
        origin = str(record.get("origin", "") or "")
        father = str(record.get("father", "") or "").strip()
        is_bio = record.get("is_biological")
        
        combined = f"{evidence} {detail} {origin}"
        all_text += " " + combined
        
        if father and father not in ["未知", "未知男性", "不明", "null", "none"]:
            father_mentions.add(father)
        
        # is_biological 标注检查
        if is_bio is False:
            has_is_biological_false = True
        elif is_bio is True:
            has_is_biological_true = True
        
        # 关键词检查
        for w in ADOPTION_WORDS:
            if w in combined:
                has_adoption_word = True
                break
        for w in IVF_WORDS:
            if w in combined:
                has_ivf_word = True
                break
        for w in MAGIC_WORDS:
            if w in combined:
                has_magic_word = True
                break
        
        # 男主孩子模式检查
        if male_lead:
            for template in MALE_LEAD_CHILD_PATTERN_TEMPLATES:
                pattern = template.format(male_lead=re.escape(male_lead))
                try:
                    if re.search(pattern, combined, re.IGNORECASE):
                        has_male_lead_pattern = True
                        break
                except Exception:
                    continue

    # 规则0: 亲属误抽（妹妹/姐姐/弟弟等）直接判为“非子女事实”
    if _looks_like_non_child_kinship_fact(heroine_name, all_text, child_name):
        return {
            "child_name": child_name,
            "needs_male": False,
            "father": "未知",
            "is_father_male_lead": False,
            "birth_method": "非子女亲属关系(规则判定)",
            "reason": "命中妹妹/姐姐/弟弟等亲属称谓，非子女事实",
            "confidence": 0.95,
            "child_exists": False,
            "evidence_is_strong": True,
            "records": child_records,
            "_rule_based": True,
        }
    
    # ===== 规则判定 =====
    
    # 规则1: is_biological=False 明确标注
    if has_is_biological_false and not has_is_biological_true:
        return {
            "child_name": child_name,
            "needs_male": False,
            "father": "未知",
            "is_father_male_lead": False,
            "birth_method": "收养/非亲生(规则判定: is_biological=False)",
            "reason": "记录明确标注 is_biological=false",
            "confidence": 0.9,
            "child_exists": True,
            "evidence_is_strong": True,
            "records": child_records,
            "_rule_based": True,
        }
    
    # 规则2: 命中收养词
    if has_adoption_word:
        return {
            "child_name": child_name,
            "needs_male": False,
            "father": "未知",
            "is_father_male_lead": False,
            "birth_method": "收养/领养/继子女(规则判定)",
            "reason": f"命中收养关键词",
            "confidence": 0.9,
            "child_exists": True,
            "evidence_is_strong": True,
            "records": child_records,
            "_rule_based": True,
        }
    
    # 规则3: 命中试管词
    if has_ivf_word:
        return {
            "child_name": child_name,
            "needs_male": False,
            "father": "未知",
            "is_father_male_lead": False,
            "birth_method": "试管/人工授精/供精(规则判定)",
            "reason": f"命中IVF关键词",
            "confidence": 0.9,
            "child_exists": True,
            "evidence_is_strong": True,
            "records": child_records,
            "_rule_based": True,
        }
    
    # 规则4: 命中魔法词
    if has_magic_word:
        return {
            "child_name": child_name,
            "needs_male": False,
            "father": "未知",
            "is_father_male_lead": False,
            "birth_method": "魔法/神赐/克隆/无性繁殖(规则判定)",
            "reason": f"命中魔法/非自然诞生关键词",
            "confidence": 0.9,
            "child_exists": True,
            "evidence_is_strong": True,
            "records": child_records,
            "_rule_based": True,
        }
    
    # 规则5: 明确男主孩子
    if has_male_lead_pattern:
        return {
            "child_name": child_name,
            "needs_male": True,
            "father": male_lead,
            "is_father_male_lead": True,
            "birth_method": "与男主生育(规则判定)",
            "reason": f"证据明确指向男主的孩子",
            "confidence": 0.9,
            "child_exists": True,
            "evidence_is_strong": True,
            "records": child_records,
            "_rule_based": True,
        }
    
    # 规则5b: father 字段直接是男主
    if male_lead:
        male_lead_lower = male_lead.lower()
        for father in father_mentions:
            father_lower = father.lower()
            if father_lower == male_lead_lower or father in ["男主", "主角"]:
                return {
                    "child_name": child_name,
                    "needs_male": True,
                    "father": male_lead,
                    "is_father_male_lead": True,
                    "birth_method": "与男主生育(规则判定: father字段)",
                    "reason": f"father字段明确为男主: {father}",
                    "confidence": 0.9,
                    "child_exists": True,
                    "evidence_is_strong": True,
                    "records": child_records,
                    "_rule_based": True,
                }
            if male_lead_lower in father_lower or father_lower in male_lead_lower:
                return {
                    "child_name": child_name,
                    "needs_male": True,
                    "father": male_lead,
                    "is_father_male_lead": True,
                    "birth_method": "与男主生育(规则判定: father字段相似)",
                    "reason": f"father字段疑似男主: {father}",
                    "confidence": 0.85,
                    "child_exists": True,
                    "evidence_is_strong": True,
                    "records": child_records,
                    "_rule_based": True,
                }
    
    # 规则6: is_biological=True 且父亲为非男主具体名字 → 需要男性参与（正常生育）
    # 注意：试管婴儿已在规则3(IVF_WORDS)被拦截，收养已在规则2被拦截，不会走到这里
    if has_is_biological_true and father_mentions:
        non_ml_fathers = []
        for f in father_mentions:
            f_lower = f.lower()
            if male_lead and (male_lead.lower() in f_lower or f_lower in male_lead.lower()):
                continue
            if f in ["男主", "主角"]:
                continue
            non_ml_fathers.append(f)
        if non_ml_fathers:
            return {
                "child_name": child_name,
                "needs_male": True,
                "father": non_ml_fathers[0],
                "is_father_male_lead": False,
                "birth_method": "正常生育(规则判定: is_biological=True+非男主父亲)",
                "reason": f"is_biological=True 且父亲为非男主: {non_ml_fathers[0]}",
                "confidence": 0.9,
                "child_exists": True,
                "evidence_is_strong": True,
                "records": child_records,
                "_rule_based": True,
            }

    # 无法用规则确定，需要 LLM 兜底
    return None


def _judge_single_child_origin(
    heroine_name: str,
    child_name: str,
    child_records: List[Dict[str, Any]],
    male_lead: str,
) -> Dict[str, Any]:
    """
    单个孩子诞生方式判定（规则优先 + LLM 兜底）
    
    返回与 _llm_judge_single_child 相同的结构，但优先使用规则判定
    """
    # 1. 先尝试规则判定
    rule_result = _judge_single_child_origin_by_rule(heroine_name, child_name, child_records, male_lead)
    if rule_result is not None:
        logger.info(f"    [孩子来源] {child_name}: 规则判定成功 - {rule_result.get('birth_method', '')}")
        return rule_result
    
    # 2. 规则无法确定，调用 LLM 兜底
    logger.info(f"    [孩子来源] {child_name}: 规则无法确定，调用 LLM 兜底")
    llm_result = _llm_judge_single_child(heroine_name, child_name, child_records, male_lead)
    
    # 3. 对 LLM 结果进行额外校验
    # 如果 LLM 判定 needs_male=True 但 confidence 不足，且缺乏强证据，降级处理
    needs_male = llm_result.get("needs_male", False)
    confidence = llm_result.get("confidence", 0.5)
    child_exists = llm_result.get("child_exists", True)
    evidence_is_strong = llm_result.get("evidence_is_strong", True)
    
    # 如果 needs_male=True 但 confidence<0.65 且 evidence_is_strong=False，则保守处理
    if needs_male and confidence < 0.65 and not evidence_is_strong:
        logger.warning(f"    [孩子来源] {child_name}: LLM判定needs_male=True但confidence={confidence}<0.65且证据不强，保守处理为False")
        llm_result["needs_male"] = False
        llm_result["_downgraded"] = True
        llm_result["_downgrade_reason"] = f"confidence={confidence}<0.65且evidence_is_strong=False"
    
    return llm_result


# ========= 原文回溯：根据 chunk_index 确定伴侣身份 =========
# 全局 chunks 缓存（避免重复读取）
_NOVEL_CHUNKS_CACHE: Dict[str, List[str]] = {}
_NOVEL_CHUNK_MANIFEST_CACHE: Dict[str, Dict[str, Any]] = {}
_CHUNK_SIZE = 6000  # 与 novel_scan.py 保持一致


def _score_decoded_text(text: str, raw_bytes_len: int) -> float:
    """
    评估解码后文本的质量分数（0~1）。
    
    评分因子：
    - replace_count: 替换字符 "�" 数量（越少越好）
    - cjk_ratio: 中文字符占比（过低惩罚）
    - printable_ratio: 可打印字符占比
    - length_ratio: len(text)/raw_bytes_len（过小强惩罚，避免吞字）
    """
    if not text or raw_bytes_len <= 0:
        return 0.0
    
    text_len = len(text)
    
    # 替换字符计数（"�" 是 errors="replace" 产生的占位符）
    replace_count = text.count("�")
    replace_penalty = min(1.0, replace_count / max(1, text_len / 100))  # 每100字超过1个替换字符开始严重惩罚
    
    # 中文字符占比（CJK统一表意文字）
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    cjk_ratio = cjk_count / max(1, text_len)
    
    # 可打印字符占比（排除控制字符）
    printable_count = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
    printable_ratio = printable_count / max(1, text_len)
    
    # 长度比：text 长度 / 原始字节长度
    # 对于中文 UTF-8 编码，每个汉字约3字节，所以 length_ratio 约为 0.33
    # 对于 GBK 编码，每个汉字2字节，所以 length_ratio 约为 0.5
    length_ratio = text_len / max(1, raw_bytes_len)
    
    # 综合评分
    # 1. 替换字符惩罚（权重高，errors="replace" 产生的 "�" 是严重问题）
    score = 1.0 - replace_penalty * 0.5
    
    # 2. 中文占比加成（小说应有较多中文）
    if cjk_ratio < 0.1:
        score *= 0.3  # 中文太少，可能编码错误
    elif cjk_ratio < 0.3:
        score *= 0.7
    else:
        score *= (0.8 + 0.2 * min(1.0, cjk_ratio))
    
    # 3. 可打印字符惩罚
    if printable_ratio < 0.9:
        score *= printable_ratio
    
    # 4. 长度比惩罚（防止吞字）
    # 正常情况下 length_ratio 应该在 0.3~0.6 之间（UTF-8/GBK 中文）
    if length_ratio < 0.1:
        score *= 0.1  # 严重吞字
    elif length_ratio < 0.2:
        score *= 0.5
    
    return max(0.0, min(1.0, score))


def _read_novel_file(file_path: str) -> Optional[str]:
    """
    读取小说原文，自动选择最佳编码。
    
    改进：
    1. 禁止 errors="ignore" 作为主路径（会静默丢字）
    2. 优先使用 errors="strict"，全失败再用 errors="replace" 作为备选
    3. 评分选择最佳解码结果
    4. 若最高分低于阈值，返回 None 并日志说明
    """
    if not os.path.exists(file_path):
        logger.warning(f"小说文件不存在: {file_path}")
        return None
    
    try:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
    except Exception as e:
        logger.warning(f"读取小说文件失败: {file_path}, 错误: {e}")
        return None
    
    raw_bytes_len = len(raw_bytes)
    if raw_bytes_len == 0:
        logger.warning(f"小说文件为空: {file_path}")
        return None
    
    encodings = [
        "utf-8", "utf-8-sig",
        "gb18030", "gbk", "big5",
        "utf-16", "utf-16-le", "utf-16-be",
        "utf-32", "utf-32-le", "utf-32-be",
    ]
    candidates: List[Tuple[float, str, str, int]] = []  # (score, encoding, text, replace_count)
    
    for enc in encodings:
        # 1. 优先尝试 strict 解码（最可靠）
        try:
            text = raw_bytes.decode(enc, errors="strict")
            replace_count = 0
            score = _score_decoded_text(text, raw_bytes_len)
            # strict 成功额外加分
            score = min(1.0, score * 1.2)
            candidates.append((score, enc, text, replace_count))
            continue
        except (UnicodeDecodeError, LookupError):
            pass
        
        # 2. strict 失败，尝试 replace（记录替换字符数）
        try:
            text = raw_bytes.decode(enc, errors="replace")
            replace_count = text.count("�")
            score = _score_decoded_text(text, raw_bytes_len)
            # replace 模式轻微惩罚
            score *= 0.9
            candidates.append((score, enc, text, replace_count))
        except (UnicodeDecodeError, LookupError):
            continue
    
    if not candidates:
        logger.warning(f"所有编码均解码失败: {file_path}")
        return None
    
    # 选择最高分的解码结果
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_enc, best_text, best_replace_count = candidates[0]
    
    # 计算 CJK 比例用于日志
    cjk_count = sum(1 for c in best_text if '\u4e00' <= c <= '\u9fff')
    cjk_ratio = cjk_count / max(1, len(best_text))
    
    logger.info(f"编码选择: {best_enc}, len(text)={len(best_text)}, raw_bytes={raw_bytes_len}, "
                f"replace_count={best_replace_count}, cjk_ratio={cjk_ratio:.2%}, score={best_score:.3f}")
    
    # 检查是否低于阈值
    if best_score < SCORE_THRESHOLD:
        logger.warning(f"编码不可靠（score={best_score:.3f} < {SCORE_THRESHOLD}）: {file_path}")
        return None
    
    # 统一换行符为 \n（全链路只做一次）
    if TEXT_ANCHOR_AVAILABLE:
        best_text = normalize_newlines(best_text)
    else:
        best_text = best_text.replace("\r\n", "\n").replace("\r", "\n")
    
    return best_text


def _split_to_chunks(text: str, chunk_size: int = _CHUNK_SIZE) -> List[str]:
    """按 CHUNK_SIZE 切分文本（与 novel_scan.py 保持一致）"""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start = min(start + chunk_size - 500, len(text))
    return chunks


def _get_novel_chunk_manifest(raw_data_path: str) -> Dict[str, Any]:
    """从 raw_data.json 读取统一 chunk manifest。缺失 manifest 视为错误。"""
    global _NOVEL_CHUNK_MANIFEST_CACHE
    if raw_data_path in _NOVEL_CHUNK_MANIFEST_CACHE:
        return _NOVEL_CHUNK_MANIFEST_CACHE[raw_data_path]

    if not raw_data_path or not os.path.exists(raw_data_path):
        raise FileNotFoundError(f"raw_data.json 不存在: {raw_data_path}")

    with open(raw_data_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    manifest_path = str(raw_data.get("chunk_manifest_file", "") or "").strip()
    if not manifest_path:
        raise FileNotFoundError(f"raw_data.json 缺少 chunk_manifest_file: {raw_data_path}")
    if not os.path.isabs(manifest_path):
        manifest_path = os.path.join(os.path.dirname(raw_data_path), manifest_path)

    manifest = load_chunk_manifest(manifest_path)
    if not manifest.get("chunks"):
        raise ValueError(f"chunk manifest 为空: {manifest_path}")
    _NOVEL_CHUNK_MANIFEST_CACHE[raw_data_path] = manifest
    return manifest


def _get_novel_chunks(raw_data_path: str) -> List[str]:
    """获取 scan 阶段落盘的原文 chunks（带缓存）"""
    global _NOVEL_CHUNKS_CACHE
    if raw_data_path in _NOVEL_CHUNKS_CACHE:
        return _NOVEL_CHUNKS_CACHE[raw_data_path]

    manifest = _get_novel_chunk_manifest(raw_data_path)
    chunks = [str(entry.get("text", "") or "") for entry in manifest.get("chunks", [])]
    _NOVEL_CHUNKS_CACHE[raw_data_path] = chunks
    logger.info(f"已加载 chunk manifest，共 {len(chunks)} 个 chunks")
    return chunks


def _get_context_around_chunk(chunks: List[str], chunk_index: int, expand_range: int = 0) -> str:
    """获取指定 chunk 及其前后 expand_range 个 chunk 的文本"""
    if not chunks or chunk_index < 1:
        return ""
    # chunk_index 是 1-based（scan 输出的）
    idx = chunk_index - 1
    start = max(0, idx - expand_range)
    end = min(len(chunks), idx + expand_range + 1)
    return "\n\n---\n\n".join(chunks[start:end])


def _split_cn_sentences(text: str) -> List[str]:
    """
    粗粒度按中文标点/换行切句，用于截取 evidence 附近上下文。
    不追求完美分句，只要能稳定截取“前后几句”即可。
    """
    import re
    if not text:
        return []
    # 先按换行切一轮，避免把不同段落粘在一起
    lines = [x.strip() for x in str(text).splitlines()]
    lines = [x for x in lines if x]
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


def _find_evidence_anchor(text: str, evidence: str) -> Optional[Tuple[int, int]]:
    """
    在 text 中寻找 evidence 的锚点位置，返回 (start, end)。
    优先精确匹配；失败则用简易“子串片段”回退匹配。
    """
    if not text or not evidence:
        return None
    t = str(text)
    e = str(evidence).strip()
    if not e:
        return None

    # 1) 精确匹配
    pos = t.find(e)
    if pos >= 0:
        return (pos, pos + len(e))

    # 2) 去空白再匹配（保守：仅用于找锚点，不做替换）
    t2 = "".join(t.split())
    e2 = "".join(e.split())
    if e2:
        pos2 = t2.find(e2)
        if pos2 >= 0:
            # 由于压缩空白导致坐标不可逆，这里返回 None 让上层走其他策略
            pass

    # 3) 取 evidence 中较长的连续片段做锚点（例如 10~20 字）
    #    目标：在证据被截断/含空白差异时仍能定位到附近句子
    candidates: List[str] = []
    e3 = e.replace(" ", "").replace("\t", "")
    for L in (20, 16, 12, 10, 8):
        if len(e3) >= L:
            candidates.append(e3[:L])
    # 再补充“中间片段”
    if len(e3) >= 24:
        mid = len(e3) // 2
        candidates.append(e3[max(0, mid - 8) : mid + 8])

    for c in candidates:
        p = t.find(c)
        if p >= 0:
            return (p, p + len(c))

    return None


def _extract_local_context_by_evidence(chunk_text: str, evidence: str, before: int = 3, after: int = 3, max_chars: int = 1800) -> str:
    """
    在单个 chunk 内，根据 evidence 锚点截取“所在句 + 前后几句”的精炼上下文。
    找不到锚点则返回空字符串，交由上层回退到整块/扩展块。
    """
    if not chunk_text or not evidence:
        return ""
    anchor = _find_evidence_anchor(chunk_text, evidence)
    if not anchor:
        return ""
    start, end = anchor
    # 用句子列表做窗口截取
    sents = _split_cn_sentences(chunk_text)
    if not sents:
        return ""
    # 找到包含锚点的句子 index：用累计长度近似定位（足够用于窗口截取）
    acc = 0
    hit_i = None
    for i, s in enumerate(sents):
        acc_next = acc + len(s)
        if acc <= start <= acc_next:
            hit_i = i
            break
        acc = acc_next
    if hit_i is None:
        # 兜底：返回锚点附近字符窗口
        left = max(0, start - 600)
        right = min(len(chunk_text), end + 600)
        return chunk_text[left:right][:max_chars]

    lo = max(0, hit_i - before)
    hi = min(len(sents), hit_i + after + 1)
    ctx = "".join(sents[lo:hi]).strip()
    if len(ctx) > max_chars:
        ctx = ctx[:max_chars]
    return ctx


# ========= 指代消解：候选名单 + 二次截取 + 缓存 =========

# 泛指伴侣噪声过滤集合
_VAGUE_PARTNER_NOISE_SET = {
    "某个男人", "那个男人", "某人", "他", "男人", "未知", "未知男性",
    "男主", "主角", "无", "没有", "无法确定", "不明", "未知对象",
    "某位男性", "一个男人", "那人", "这个人", "那个人", "某男",
    "一名男子", "一个男子", "某男子", "那男人", "这男人",
}

# LLM 指代消解缓存：避免重复调用
# key: (raw_data_path, heroine_name, chunk_index, vague_partner, evidence_hash, stage)
_LLM_RESOLUTION_CACHE: Dict[Tuple[str, str, int, str, str, int], Dict[str, Any]] = {}


def _build_partner_candidates_from_facts(
    heroine_facts: Dict[str, Any],
    male_lead: str,
    male_lead_aliases: Optional[List[str]] = None,
    k: int = 12,
) -> List[str]:
    """
    从 heroine_facts 构建候选伴侣名单（用于指代消解的选择题）。
    
    候选来源（按优先级）：
    1. male_lead + male_lead_aliases（优先级最高）
    2. sexual_relations[].partner
    3. partner_relations[].partner
    4. physical_contacts[].partner
    5. romantic_feelings[].target
    6. children_info[].father
    
    过滤掉泛指/噪声名称，最多返回 k 个候选。
    """
    if male_lead_aliases is None:
        male_lead_aliases = []
    
    candidates: List[str] = []
    seen: set = set()
    
    def _add_if_valid(name: str):
        """添加非噪声的有效名字"""
        s = str(name or "").strip()
        if not s or s in seen:
            return
        # 过滤泛指噪声
        if s.lower() in _VAGUE_PARTNER_NOISE_SET or s in _VAGUE_PARTNER_NOISE_SET:
            return
        if _is_vague_male_name(s):
            return
        seen.add(s)
        candidates.append(s)
    
    # 1. 优先放 male_lead 及其别名
    if male_lead:
        _add_if_valid(male_lead)
    for alias in male_lead_aliases:
        _add_if_valid(alias)
    
    # 2. 从各事实字段收集
    facts = heroine_facts or {}
    
    # sexual_relations[].partner
    for item in facts.get("sexual_relations", []):
        if isinstance(item, dict):
            _add_if_valid(item.get("partner", ""))
    
    # partner_relations[].partner
    for item in facts.get("partner_relations", []):
        if isinstance(item, dict):
            _add_if_valid(item.get("partner", ""))
    
    # physical_contacts[].partner
    for item in facts.get("physical_contacts", []):
        if isinstance(item, dict):
            _add_if_valid(item.get("partner", ""))
    
    # romantic_feelings[].target
    for item in facts.get("romantic_feelings", []):
        if isinstance(item, dict):
            _add_if_valid(item.get("target", ""))
    
    # children_info[].father
    for item in facts.get("children_info", []):
        if isinstance(item, dict):
            _add_if_valid(item.get("father", ""))
    
    # 可选：extra_relations[].male（如果存在）
    for item in facts.get("extra_relations", []):
        if isinstance(item, dict):
            _add_if_valid(item.get("male", ""))
    
    # 截断到 k 个，但确保 male_lead 和别名保留
    if len(candidates) > k:
        # 保留前 k 个（male_lead 及别名已在最前面）
        candidates = candidates[:k]
    
    return candidates


def _refine_context_by_evidence_with_fallback(
    big_context_text: str,
    center_chunk_text: str,
    evidence: str,
    before: int = 3,
    after: int = 3,
    max_chars: int = REFINED_CTX_MAX_CHARS,
    full_text: Optional[str] = None,
    child_name: Optional[str] = None,
    current_heroine: Optional[str] = None,
    father: Optional[str] = None,
) -> str:
    """
    在大窗口文本中基于 evidence 锚点进行二次截取，带正确回退策略。
    
    优先级：
    1. 使用增强的多策略锚定（EXACT -> ELLIPSIS -> FUZZY_ALIGN -> MULTI_FRAGMENT）
    2. 若找到锚点，截取锚点所在句 + 前后 before/after 句
    3. 若 big_context_text 找不到锚点，回退到 center_chunk_text 的"中间窗口"（不是开头！）
    4. 若 center_chunk_text 为空，返回空字符串
    
    注意：绝对禁止"锚定失败 → 返回 big_context_text[:max_chars]"，会导致跑偏误判。
    
    新增参数（用于 MULTI_FRAGMENT 策略）：
    - full_text: 完整原文（用于全文搜索）
    - child_name: 孩子名称（用于片段打分）
    - current_heroine: 当前女主名（用于片段打分）
    - father: 父亲名（用于片段打分）
    """
    # 1. 尝试在大窗口中定位 evidence（使用增强的多策略锚定）
    if big_context_text and evidence:
        # 优先使用 text_anchor 模块的增强锚定
        if TEXT_ANCHOR_AVAILABLE:
            anchor = find_evidence_anchor(
                text=big_context_text,
                evidence=evidence,
                center_pos=len(big_context_text) // 2,
                prefer_span=None,
                full_text=full_text,
                child_name=child_name,
                current_heroine=current_heroine,
                father=father,
            )
            if anchor and anchor.get("start", -1) >= 0:
                start, end = anchor["start"], anchor["end"]
                # 用句子切分做窗口截取
                spans = split_cn_sentences_with_spans(big_context_text)
                if spans:
                    hit_i = sentence_hit_index_by_spans(spans, start)
                    if hit_i is not None:
                        lo = max(0, hit_i - before)
                        hi = min(len(spans), hit_i + after + 1)
                        ctx_start = spans[lo][0]
                        ctx_end = spans[hi - 1][1]
                        ctx = big_context_text[ctx_start:ctx_end]
                        
                        if len(ctx) > max_chars:
                            # 居中截断（围绕锚点）
                            anchor_in_ctx = max(0, min(len(ctx), int(start) - int(ctx_start)))
                            half = max_chars // 2
                            left = max(0, anchor_in_ctx - half)
                            right = min(len(ctx), left + max_chars)
                            if right - left < max_chars and left > 0:
                                left = max(0, right - max_chars)
                            ctx = ctx[left:right]
                        
                        # 后验检查：evidence 是否在 ctx 中
                        present, _ = evidence_in_ctx(ctx, evidence)
                        if not present:
                            # 回退到中心窗口
                            center_mid = (int(start) + int(end)) // 2
                            half = max_chars // 2
                            left = max(0, center_mid - half)
                            right = min(len(big_context_text), center_mid + half)
                            ctx = big_context_text[left:right]
                        
                        return ctx
                
                # 句子切分失败，用字符窗口
                center_mid = (int(start) + int(end)) // 2
                half = max_chars // 2
                left = max(0, center_mid - half)
                right = min(len(big_context_text), center_mid + half)
                return big_context_text[left:right]
        else:
            # 回退到旧的 _find_evidence_anchor
            anchor = _find_evidence_anchor(big_context_text, evidence)
            if anchor:
                start, end = anchor
                # 用句子列表做窗口截取
                sents = _split_cn_sentences(big_context_text)
                if sents:
                    # 找到包含锚点的句子 index
                    acc = 0
                    hit_i = None
                    for i, s in enumerate(sents):
                        acc_next = acc + len(s)
                        if acc <= start <= acc_next:
                            hit_i = i
                            break
                        acc = acc_next
                    
                    if hit_i is not None:
                        lo = max(0, hit_i - before)
                        hi = min(len(sents), hit_i + after + 1)
                        ctx = "".join(sents[lo:hi]).strip()
                        if len(ctx) > max_chars:
                            # 尽量保留锚点附近
                            ctx = ctx[:max_chars]
                        return ctx
                
                # 句子切分失败，用字符窗口
                left = max(0, start - max_chars // 2)
                right = min(len(big_context_text), end + max_chars // 2)
                return big_context_text[left:right][:max_chars]
    
    # 2. 锚定失败，回退到 center_chunk_text 的"中间窗口"
    if center_chunk_text:
        chunk_len = len(center_chunk_text)
        if chunk_len <= max_chars:
            return center_chunk_text
        # 取中间部分，而不是开头
        center = chunk_len // 2
        half_window = max_chars // 2
        left = max(0, center - half_window)
        right = min(chunk_len, center + half_window)
        return center_chunk_text[left:right]
    
    # 3. 无法截取
    return ""


# ================= 孩子母亲归属校验相关函数 =================
# 全局缓存：避免重复 LLM 调用
_CHILD_OWNER_REASSIGN_CACHE: Dict[str, Dict[str, Any]] = {}


def _classify_child_record_owner_anchor(
    current_heroine: str,
    ctx_text: str,
    heroine_names: List[str],
    child_name: str = "",
) -> Tuple[str, str]:
    """
    对孩子记录进行母亲锚定分类（规则优先）。
    
    返回 (category, reason):
    - "STRONG": ctx_text 内仅出现 current_heroine（无其他女主）
    - "MIXED": ctx_text 内同时出现 current_heroine 和其他女主（多人同场，需谨慎）
    - "CONFLICT": ctx_text 内未出现 current_heroine，但出现 heroine_names 中至少一个其他名字
    - "UNANCHORED": ctx_text 内没有出现任何 heroine_names
    
    注意：只做"锚定分类"，不推断谁生的，不做"母亲=谁"的规则结论。
    
    【重要改进】：
    - 新增 MIXED 分类：多人同场对话时，不要把"出现名字"当作强锚定
    - 避免把多人同场的对话/录音误判为母亲归属依据
    """
    if not ctx_text:
        return ("UNANCHORED", "上下文为空")
    
    ctx_lower = ctx_text.lower()
    current_lower = current_heroine.strip().lower() if current_heroine else ""
    
    # 收集所有有效的女主名（去重、过滤空值）
    valid_heroines = [h.strip() for h in heroine_names if h and h.strip()]
    valid_heroines = list(set(valid_heroines))
    
    # 1. 检查当前女主是否出现在上下文中
    current_found = False
    if current_lower and current_lower in ctx_lower:
        current_found = True
    
    # 2. 检查是否出现其他女主名
    other_found: List[str] = []
    child_name_lower = child_name.strip().lower() if child_name else ""
    for h in valid_heroines:
        h_lower = h.lower()
        if h_lower == current_lower:
            continue  # 跳过当前女主
        if child_name_lower and h_lower == child_name_lower:
            continue  # 跳过孩子名（孩子也是女主时，出现在上下文中是正常的）
        if h_lower in ctx_lower:
            other_found.append(h)
    
    # 3. 分类逻辑（增强版）
    if current_found:
        if other_found:
            # 多人同场：current_heroine 和其他女主同时出现
            return ("MIXED", f"上下文中同时出现【{current_heroine}】和其他女主: {', '.join(other_found)}")
        else:
            # 仅 current_heroine 出现
            return ("STRONG", f"上下文中仅出现当前女主名【{current_heroine}】")
    
    if other_found:
        return ("CONFLICT", f"上下文中未出现【{current_heroine}】，但出现其他女主: {', '.join(other_found)}")
    
    # 4. 没有出现任何女主名
    return ("UNANCHORED", "上下文中未出现任何女主名")


# ================= 翻案门控规则（防止多人同场误判）=================
# 触发词列表（怀孕/孩子相关）
_REASSIGN_TRIGGER_WORDS = [
    "怀孕", "孩子", "肚子", "几个月", "生下", "怀了", "孕",
    "产下", "分娩", "临盆", "生产", "流产", "胎儿"
]

# 说话归属 pattern（用于判断引号内容的说话人）
_ATTRIBUTION_PATTERN = re.compile(
    r"(说|道|开口|录音里说|承认|表示|问|答|喊|叫|低声|轻声|冷声|笑着说|哭着说|大声)",
    re.IGNORECASE
)


def _is_reassign_supported(
    target_mother: str,
    evidence: str,
    ctx_text: str,
    supporting_quote: str,
    current_heroine: str,
    anchor_strategy: Optional[str] = None,
    evidence_in_context: bool = True,
) -> Tuple[bool, str]:
    """
    翻案门控函数：判断是否允许将孩子母亲从 current_heroine 改判为 target_mother。
    
    返回 (allowed, reason):
    - allowed: 是否允许翻案
    - reason: 门控判断的原因
    
    门控规则：
    1. 显式同名证据：supporting_quote 或 evidence 命中句中包含 target_mother 名字，且与触发词共现
    2. 说话归属保护：引号内容前缀窗口出现 "{target_mother}(说|道|...)" 等归属 pattern
    3. 第一人称保护：evidence 含"我/我的/肚子里"等第一人称且不含女主名，需明确归属才允许改判
    4. EXACT 锚定保护：若 anchor_strategy == EXACT 且 evidence_in_context == True，且 current_heroine 在命中句附近出现，默认不允许翻案
    """
    if not target_mother or target_mother == "无法确定":
        return False, "目标母亲为空或无法确定"
    
    if target_mother == current_heroine:
        return True, "目标母亲与当前女主相同，无需翻案"
    
    evidence_lower = evidence.lower() if evidence else ""
    quote_lower = supporting_quote.lower() if supporting_quote else ""
    ctx_lower = ctx_text.lower() if ctx_text else ""
    target_lower = target_mother.lower()
    current_lower = current_heroine.lower() if current_heroine else ""
    
    # 规则1：显式同名证据检查
    # target_mother 名字必须出现在 supporting_quote 或 evidence 中，且与触发词共现
    target_in_quote = target_lower in quote_lower
    target_in_evidence = target_lower in evidence_lower
    
    has_trigger_word = False
    combined_text = f"{quote_lower} {evidence_lower}"
    for word in _REASSIGN_TRIGGER_WORDS:
        if word in combined_text:
            has_trigger_word = True
            break
    
    explicit_evidence = (target_in_quote or target_in_evidence) and has_trigger_word
    
    # 规则2：说话归属检查
    # 检查引号前是否有 "{target_mother}(说|道|...)" 的归属 pattern
    attribution_found = False
    if ctx_text and target_mother:
        # 在 ctx_text 中查找引号
        quote_chars = ['"', '"', '「', '『']
        for qc in quote_chars:
            pos = ctx_text.find(qc)
            while pos > 0:
                # 检查引号前 40 字符窗口
                prefix_start = max(0, pos - 40)
                prefix = ctx_text[prefix_start:pos]
                
                # 检查是否有 target_mother + 说话动词
                if target_mother in prefix and _ATTRIBUTION_PATTERN.search(prefix):
                    attribution_found = True
                    break
                
                pos = ctx_text.find(qc, pos + 1)
            
            if attribution_found:
                break
    
    # 规则3：第一人称保护
    # 如果 evidence 含"我/我的/肚子里"等第一人称，且不含任何女主名
    first_person_words = ["我", "我的", "肚子里", "我们", "我肚子"]
    has_first_person = any(w in evidence_lower for w in first_person_words)
    has_any_heroine_name = target_lower in evidence_lower or current_lower in evidence_lower
    
    first_person_risk = has_first_person and not has_any_heroine_name
    
    # 规则4：EXACT 锚定保护
    # 如果 EXACT 锚定成功且 evidence 在 ctx 中，且 current_heroine 在附近，不允许轻易翻案
    exact_anchor_protection = False
    if anchor_strategy == "EXACT" and evidence_in_context:
        if current_lower in ctx_lower:
            exact_anchor_protection = True
    
    # 综合判断
    if explicit_evidence:
        return True, f"显式证据支持：target_mother={target_mother} 在证据中与触发词共现"
    
    if attribution_found:
        return True, f"说话归属支持：检测到 {target_mother} 的说话归属 pattern"
    
    if first_person_risk:
        return False, "第一人称保护：evidence 含第一人称但无明确女主名指向"
    
    if exact_anchor_protection and not explicit_evidence and not attribution_found:
        return False, f"EXACT 锚定保护：current_heroine={current_heroine} 在上下文中出现，无显式证据支持翻案"
    
    # 默认：如果没有明确的支持证据，也没有明确的阻止条件，谨慎允许
    # 但如果是 MIXED 场景（多人同场），应该更谨慎
    return True, "门控通过：无明确阻止条件"


def _get_child_owner_cache_key(
    child_name: str,
    chunk_index: Optional[int],
    evidence: str,
    heroine_candidates: List[str],
) -> str:
    """生成孩子母亲重归属 LLM 调用的缓存 key"""
    evidence_snippet = str(evidence or "")[:200]
    evidence_hash = hashlib.md5(evidence_snippet.encode('utf-8', errors='ignore')).hexdigest()
    candidates_str = ",".join(sorted(heroine_candidates))
    return f"{child_name}|{chunk_index}|{evidence_hash}|{candidates_str}"


def _llm_reassign_child_owner(
    child_name: str,
    evidence: str,
    ctx_text: str,
    heroine_candidates: List[str],
    chunk_index: Optional[int] = None,
) -> Dict[str, Any]:
    """
    调用 LLM 进行孩子母亲重归属（选择题模式）。
    
    返回:
    {
        "mother": "候选女主名 或 '无法确定'",
        "confidence": 0.0-1.0,
        "supporting_quote": "关键句(可选)"
    }
    
    关键约束：
    - mother 只能从 heroine_candidates 中选择，否则输出"无法确定"
    - 必须依据 ctx_text，不允许臆测
    - 不确定时 confidence < 0.7 或 mother="无法确定"
    """
    global _CHILD_OWNER_REASSIGN_CACHE
    
    # 构建缓存 key
    cache_key = _get_child_owner_cache_key(child_name, chunk_index, evidence, heroine_candidates)
    
    # 检查缓存
    if cache_key in _CHILD_OWNER_REASSIGN_CACHE:
        cached = _CHILD_OWNER_REASSIGN_CACHE[cache_key]
        logger.debug(f"[母亲重归属] 命中缓存: {child_name} -> {cached.get('mother', '?')}")
        return cached
    
    # 默认返回值
    default_result: Dict[str, Any] = {
        "mother": "无法确定",
        "confidence": 0.0,
        "supporting_quote": "",
    }
    
    if not ctx_text or not heroine_candidates:
        _CHILD_OWNER_REASSIGN_CACHE[cache_key] = default_result
        return default_result
    
    # 构建候选列表字符串
    candidates_list = "\n".join([f"- {h}" for h in heroine_candidates])
    
    system_prompt = f"""你是一个专业的小说文本分析助手，需要根据上下文判断孩子的母亲是谁。

【任务】：根据下方提供的【上下文】判断孩子【{child_name}】的母亲是谁。

【候选母亲列表】（只能从中选择）：
{candidates_list}

【核心规则】：
1. 你只能从上述【候选母亲列表】中选择一个作为答案
2. 如果无法确定、证据不足或不在列表中，必须输出 mother="无法确定"
3. 你必须严格依据【上下文】原文判断，不允许推测、臆断或使用外部知识
4. 语义方向规则（必须严格执行）：
   - "X生下了Y"/"X产下了Y"/"X诞下了Y"/"X给某人生了孩子" → X是母亲
   - "X是剖腹产生的"/"某人生下了X"/"X是某人的女儿" → X是被生者（孩子），不是母亲
   - 仅出现"X是剖腹产的"且没有明确孩子对象（如儿子/女儿/具体孩子名）→ 证据不足，mother="无法确定"
5. 事实性规则：
   - 假设/劝说/设想语句（如"以后会""如果""你想想"）不算已发生事实
6. 置信度规则：
   - 如果上下文明确写明"XX生下了YY"/"XX的孩子YY" -> confidence=0.9+
   - 如果上下文仅暗示、推测或存在歧义 -> confidence<0.7 且 mother="无法确定"

【输出格式】(JSON)：
{{
    "mother": "候选女主名 或 '无法确定'",
    "confidence": 0.0到1.0之间的数值,
    "supporting_quote": "支持判断的关键原文（不超过50字）"
}}"""

    user_prompt = f"""【孩子名称】：{child_name}

【原始证据】：
{evidence[:300] if evidence else "（无）"}

【上下文】：
{ctx_text[:CHILD_OWNER_CTX_MAX_CHARS]}

请判断这个孩子的母亲是谁。"""

    try:
        data = _call_json_chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )

        mother = str(data.get("mother", "无法确定")).strip()
        confidence = float(data.get("confidence", 0.0))
        supporting_quote = str(data.get("supporting_quote", ""))[:100]
        
        # 校验 mother 是否在候选列表中
        if mother != "无法确定" and mother not in heroine_candidates:
            # 尝试模糊匹配
            matched = None
            for h in heroine_candidates:
                if mother in h or h in mother:
                    matched = h
                    break
            if matched:
                mother = matched
            else:
                logger.warning(f"[母亲重归属] LLM 返回非法母亲名: {mother}，不在候选列表中")
                mother = "无法确定"
                confidence = 0.0
        
        result: Dict[str, Any] = {
            "mother": mother,
            "confidence": confidence,
            "supporting_quote": supporting_quote,
        }
        _CHILD_OWNER_REASSIGN_CACHE[cache_key] = result
        return result
        
    except Exception as e:
        logger.warning(f"[母亲重归属] LLM 调用失败: {e}")
        _CHILD_OWNER_REASSIGN_CACHE[cache_key] = default_result
        return default_result


def _validate_and_reassign_children_records(
    raw_data_path: str,
    current_heroine: str,
    children_info: List[Dict[str, Any]],
    heroine_names: List[str],
    chunks: List[str],
    full_text: Optional[str] = None,
    chunk_entries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    校验孩子记录的母亲归属，并产生可用于 Step1 的 children 记录 + 移入队列。
    
    返回结构：
    {
        "owned_strong_records": [...],      # 当前女主强锚定可用 children 记录（进入 Step1）
        "unanchored_records": [...],        # 无锚定 children（不进入 Step1，仅 debug）
        "conflict_records": [...],          # 冲突 children（不进入 Step1，仅 debug）
        "moved_out_records": [...],         # 从当前女主移出的 records（包含 moved_to 字段）
        "moved_in_by_mother": {             # 若重归属成功：按目标女主聚合，供上层注入
            "某女主": [...],
            ...
        },
        "summary": "..."
    }
    """
    owned_strong_records: List[Dict[str, Any]] = []
    unanchored_records: List[Dict[str, Any]] = []
    conflict_records: List[Dict[str, Any]] = []
    moved_out_records: List[Dict[str, Any]] = []
    moved_in_by_mother: Dict[str, List[Dict[str, Any]]] = {}
    
    if not children_info:
        return {
            "owned_strong_records": [],
            "unanchored_records": [],
            "conflict_records": [],
            "moved_out_records": [],
            "moved_in_by_mother": {},
            "summary": "无孩子记录",
        }
    
    # 准备有效的女主候选列表
    heroine_candidates = [h.strip() for h in heroine_names if h and h.strip()]
    heroine_candidates = list(set(heroine_candidates))
    
    # 辅助函数：检查文本是否包含触发词
    def _contains_trigger_words(text: str) -> bool:
        if not text:
            return False
        for word in CHILD_OWNER_TRIGGER_WORDS:
            if word in text:
                return True
        return False
    
    for record in children_info:
        child_name = str(record.get("child_name", "") or "").strip()
        evidence = str(record.get("evidence", "") or "").strip()
        chunk_index = record.get("chunk_index")
        
        # 复制 record 避免修改原数据
        record_copy = dict(record)
        
        # 1) 无 chunk_index 或无 evidence -> 视为 UNANCHORED
        if chunk_index is None or not evidence:
            record_copy["_owner_anchor"] = "UNANCHORED"
            record_copy["_owner_reason"] = "无 chunk_index 或无 evidence"
            unanchored_records.append(record_copy)
            continue
        
        # 2) 获取上下文
        # chunk_index 是 1-based（scan 输出的）
        if not chunks or chunk_index < 1 or chunk_index > len(chunks):
            record_copy["_owner_anchor"] = "UNANCHORED"
            record_copy["_owner_reason"] = f"chunk_index={chunk_index} 超出范围"
            unanchored_records.append(record_copy)
            continue
        
        # big_context 用 expand_range=1
        if TEXT_ANCHOR_AVAILABLE and full_text and chunk_entries:
            big_context, _center_chunk_from_fulltext, _spans = get_context_around_chunk_from_fulltext(
                full_text,
                None,
                chunk_index,
                expand_range=1,
                chunk_entries=chunk_entries,
            )
        else:
            big_context = _get_context_around_chunk(chunks, chunk_index, expand_range=1)
        center_chunk_text = chunks[chunk_index - 1] if (1 <= chunk_index <= len(chunks)) else ""
        
        # 二次截取 + 正确回退
        ctx_text = _refine_context_by_evidence_with_fallback(
            big_context,
            center_chunk_text,
            evidence,
            before=CHILD_OWNER_BEFORE_SENTS,
            after=CHILD_OWNER_AFTER_SENTS,
            max_chars=CHILD_OWNER_CTX_MAX_CHARS,
            full_text=full_text,
            child_name=child_name or None,
            current_heroine=current_heroine,
            father=str(record.get("father", "") or "") or None,
        )
        
        if not ctx_text:
            record_copy["_owner_anchor"] = "UNANCHORED"
            record_copy["_owner_reason"] = "无法获取有效上下文"
            unanchored_records.append(record_copy)
            continue
        
        # 3) 规则分类
        category, reason = _classify_child_record_owner_anchor(
            current_heroine, ctx_text, heroine_candidates, child_name=child_name
        )
        record_copy["_owner_anchor"] = category
        record_copy["_owner_reason"] = reason
        
        # 4) 分类处理
        if category == "STRONG":
            owned_strong_records.append(record_copy)
        elif category == "MIXED":
            # 多人同场：暂时归入 owned_strong_records，但标记为 MIXED（需要更谨慎的门控）
            owned_strong_records.append(record_copy)
        elif category == "CONFLICT":
            conflict_records.append(record_copy)
        else:  # UNANCHORED
            unanchored_records.append(record_copy)
        
        # 5) 判断是否触发 LLM 重归属
        should_trigger_llm = False
        if category == "CONFLICT":
            should_trigger_llm = True
        elif category == "MIXED":
            # MIXED 场景：多人同场，需要 LLM 判断但门控更严格
            should_trigger_llm = True
        elif category == "UNANCHORED":
            # UNANCHORED 且含触发词才调用 LLM
            if _contains_trigger_words(evidence) or _contains_trigger_words(ctx_text):
                should_trigger_llm = True
        
        # 6) 触发 LLM 重归属
        if should_trigger_llm:
            resp = _llm_reassign_child_owner(
                child_name, evidence, ctx_text, heroine_candidates, chunk_index
            )
            mother = resp.get("mother", "无法确定")
            confidence = resp.get("confidence", 0.0)
            supporting_quote = resp.get("supporting_quote", "")
            
            if mother != "无法确定" and confidence >= CHILD_OWNER_CONF_THRESHOLD:
                # 高置信度重归属成功
                if mother == current_heroine:
                    # 归属到当前女主：从 conflict/unanchored/mixed 移到 owned_strong_records
                    if record_copy in conflict_records:
                        conflict_records.remove(record_copy)
                    if record_copy in unanchored_records:
                        unanchored_records.remove(record_copy)
                    record_copy["_owner_reassigned"] = True
                    record_copy["_owner_reassign_conf"] = confidence
                    record_copy["_supporting_quote"] = supporting_quote
                    if record_copy not in owned_strong_records:
                        owned_strong_records.append(record_copy)
                    logger.info(f"    [母亲归属] {child_name} 重归属确认为【{current_heroine}】(conf={confidence:.2f})")
                else:
                    # ======== 翻案门控检查 ========
                    # 在移出前，检查是否满足门控条件
                    gate_allowed, gate_reason = _is_reassign_supported(
                        target_mother=mother,
                        evidence=evidence,
                        ctx_text=ctx_text,
                        supporting_quote=supporting_quote,
                        current_heroine=current_heroine,
                        anchor_strategy=None,  # 可从 ctx 获取，这里暂时不传
                        evidence_in_context=True,  # 假设 evidence 在 ctx 中
                    )
                    
                    record_copy["_reassign_gate_passed"] = gate_allowed
                    record_copy["_reassign_gate_reason"] = gate_reason
                    
                    # MIXED 场景下，门控更严格
                    if category == "MIXED" and not gate_allowed:
                        # MIXED 场景且门控不通过：不翻案，保持原归属
                        logger.info(
                            f"    [母亲归属] {child_name} MIXED 场景门控拒绝翻案: "
                            f"LLM={mother}(conf={confidence:.2f}), 原因={gate_reason}"
                        )
                        continue
                    
                    if not gate_allowed:
                        # 普通场景门控不通过：记录但不翻案
                        logger.info(
                            f"    [母亲归属] {child_name} 门控拒绝翻案: "
                            f"LLM={mother}(conf={confidence:.2f}), 原因={gate_reason}"
                        )
                        continue
                    
                    # 门控通过，执行翻案
                    # 归属到其他女主：从 owned_strong_records 移除（如有），加入 moved_out
                    if record_copy in owned_strong_records:
                        owned_strong_records.remove(record_copy)
                    if record_copy in conflict_records:
                        conflict_records.remove(record_copy)
                    if record_copy in unanchored_records:
                        unanchored_records.remove(record_copy)
                    
                    record_copy["moved_to"] = mother
                    record_copy["_owner_reassign_conf"] = confidence
                    record_copy["_supporting_quote"] = supporting_quote
                    moved_out_records.append(record_copy)
                    
                    # 放入 moved_in_by_mother（复制一份避免引用问题）
                    if mother not in moved_in_by_mother:
                        moved_in_by_mother[mother] = []
                    moved_in_by_mother[mother].append(dict(record_copy))
                    
                    logger.info(
                        f"    [母亲归属] {child_name} 从【{current_heroine}】移至【{mother}】"
                        f"(conf={confidence:.2f}, chunk={chunk_index}, gate={gate_reason[:30]}...)"
                    )
            else:
                # 低置信度或无法确定：保持在原分类（debug 列表），不惩罚
                logger.debug(
                    f"    [母亲归属] {child_name} LLM判断={mother}(conf={confidence:.2f})，"
                    f"未达阈值，保持{category}"
                )
    
    # 构建 summary
    summary_parts = [
        f"原始{len(children_info)}条",
        f"强锚定{len(owned_strong_records)}条",
        f"冲突{len(conflict_records)}条",
        f"无锚定{len(unanchored_records)}条",
        f"移出{len(moved_out_records)}条",
    ]
    if moved_out_records:
        moved_to_stats: Dict[str, int] = {}
        for r in moved_out_records:
            mt = r.get("moved_to", "?")
            moved_to_stats[mt] = moved_to_stats.get(mt, 0) + 1
        summary_parts.append(f"移出目标: {moved_to_stats}")
    summary = ", ".join(summary_parts)
    
    # 日志输出
    logger.info(f"    [母亲归属] 【{current_heroine}】校验结果: {summary}")
    
    return {
        "owned_strong_records": owned_strong_records,
        "unanchored_records": unanchored_records,
        "conflict_records": conflict_records,
        "moved_out_records": moved_out_records,
        "moved_in_by_mother": moved_in_by_mother,
        "summary": summary,
    }


def _get_resolution_cache_key(
    raw_data_path: str,
    heroine_name: str,
    chunk_index: int,
    vague_partner: str,
    evidence: str,
    stage: int,
) -> Tuple[str, str, int, str, str, int]:
    """生成指代消解 LLM 调用的缓存 key"""
    # evidence_hash: 取前200字符的 MD5
    evidence_snippet = str(evidence or "")[:200]
    evidence_hash = hashlib.md5(evidence_snippet.encode('utf-8', errors='ignore')).hexdigest()
    return (raw_data_path, heroine_name, chunk_index, vague_partner, evidence_hash, stage)


def _llm_identify_vague_partner(
    heroine_name: str,
    vague_partner: str,
    evidence: str,
    relationship: str,
    context_text: str,
    male_lead: str,
    male_lead_aliases: Optional[List[str]] = None,
    candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    调用 LLM 回溯原文，尝试确定泛指伴侣的真实身份。
    
    返回结构化 JSON：
    {
        "identified_partner": "候选之一 或 '无法确定'",
        "is_male_lead": true/false,
        "confidence": 0.0-1.0,
        "supporting_quote": "用于判定的一句原文(可选)",
        "speech_act": "asserted_fact/hypothesis/advice_persuasion/comparison/rumor/question/unknown",
        "is_fact_statement": true/false,
        "nonfact_reason": "若非事实，说明原因"
    }
    """
    default_result = {
        "identified_partner": "无法确定",
        "is_male_lead": False,
        "confidence": 0.0,
        "supporting_quote": "",
        "speech_act": "unknown",
        "is_fact_statement": False,
        "nonfact_reason": "",
    }
    
    if not API_KEY_POOL or not context_text:
        return default_result
    
    if male_lead_aliases is None:
        male_lead_aliases = []
    if candidates is None:
        candidates = []
    
    # 构建男主及别名集合，用于判断 is_male_lead
    male_lead_set = {male_lead} if male_lead else set()
    male_lead_set.update(a for a in male_lead_aliases if a)
    male_lead_set.discard("")
    
    # 构建候选名单字符串（用于 prompt）
    if candidates:
        candidates_str = "、".join(candidates[:12])
    else:
        candidates_str = "（无候选名单，请从上下文推断）"
    
    system_prompt = f"""你是严格的小说信息回溯专家。你的任务是根据上下文确定女主【{heroine_name}】某段伴侣关系记录中"对象"的真实身份。

【任务】
原始记录中的 partner（伴侣对象）为泛指词（如"某个男人"、"他"），现在需要你根据原文上下文，判断这个人到底是谁。

【候选名单】
{candidates_str}

【输出规范】
0. 先判断该记录语用类型 speech_act：
   - asserted_fact：已发生的事实陈述
   - hypothesis：假设/条件推演（如“如果/就算/假如/万一/要是”）
   - advice_persuasion：说服/劝导/刺激（如“你想想……会不会……”）
   - comparison：类比/比喻
   - rumor：传闻（听说/据说/传言）
   - question：设问/反问
1. identified_partner：必须从候选名单中选择一个，或者输出"无法确定"
   - 若上下文能明确指向某个候选，输出该候选名
   - 若能确定是男主【{male_lead}】或其别名，输出男主名字"{male_lead}"
   - 若完全无法推断或信息不足，必须输出"无法确定"
2. is_male_lead：若确定的对象是男主或男主别名，输出 true；否则 false
3. confidence：你对判断的置信度（0.0~1.0）
   - 1.0: 上下文明确提到了对象名字
   - 0.8~0.9: 上下文强烈暗示是某个候选
   - 0.5~0.7: 有一定依据但不确定
   - <0.5: 基本靠猜测，应输出"无法确定"
4. supporting_quote：用于判定的一句关键原文（可选，最多50字）

【重要】
- identified_partner 只能从候选名单中选择，否则输出"无法确定"
- 若 evidence/context 只是在猜测、或信息不足，必须输出"无法确定"且 confidence < 0.5
- 若 speech_act 不是 asserted_fact，必须输出：
  identified_partner="无法确定", is_male_lead=false, is_fact_statement=false, confidence<=0.3
- 以后/将来/就算后来/你想想那样的场景 等内容属于假设或说服，不是事实

【已明确伴侣的处理】
- 若 partner 已是具体人名（非 某个男人/他/对方/那个人 等泛指），且 relationship 含明确关系词
  （丈夫/前夫/男友/恋人/夫君等），则该记录天然倾向于 asserted_fact，除非上下文有强反证
  （如明确出现在 如果/假如/想象 句式中）。
- 已故/去世/过世/死亡/不在了 等描述不影响事实性——提到已故的丈夫/前夫是在陈述过去的事实。
- 若 partner 已是具体人名且上下文中能找到该人名或关系描述，confidence 不应低于 0.7。

- 只输出一个 JSON 对象，不要解释"""

    user_prompt = f"""请根据以下信息确定伴侣身份：

【女主】{heroine_name}
【男主】{male_lead}
【男主别名】{', '.join(male_lead_aliases) if male_lead_aliases else '无'}
【候选名单】{candidates_str}
【原始记录】
- partner（待确定）: {vague_partner}
- 关系类型: {relationship}
- 证据: {evidence}

【原文上下文】
{context_text[:]}

请输出 JSON：
{{
  "identified_partner": "从候选中选择或'无法确定'",
  "is_male_lead": true或false,
  "confidence": 0.0到1.0,
  "supporting_quote": "关键原文片段",
  "speech_act": "asserted_fact/hypothesis/advice_persuasion/comparison/rumor/question/unknown",
  "is_fact_statement": true或false,
  "nonfact_reason": "若非事实，简述原因"
}}"""

    try:
        data = _call_json_chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
        )
        if data:
            identified = str(data.get("identified_partner", "") or "").strip()
            is_male_lead = bool(data.get("is_male_lead", False))
            confidence = float(data.get("confidence", 0.0))
            supporting_quote = str(data.get("supporting_quote", "") or "")[:100]
            speech_act = str(data.get("speech_act", "") or "").strip().lower()
            allowed_speech_act = {
                "asserted_fact", "hypothesis", "advice_persuasion",
                "comparison", "rumor", "question", "unknown",
            }
            if speech_act not in allowed_speech_act:
                speech_act = "unknown"
            is_fact_statement = bool(data.get("is_fact_statement", speech_act == "asserted_fact"))
            nonfact_reason = str(data.get("nonfact_reason", "") or "")[:120]
            
            # 校验 identified_partner 是否在候选中（或为"无法确定"）
            if identified and identified != "无法确定":
                # 检查是否在候选名单中（模糊匹配）
                in_candidates = False
                for c in candidates:
                    if c and (c in identified or identified in c or c == identified):
                        in_candidates = True
                        break
                # 检查是否为男主/别名
                is_male = identified in male_lead_set or any(
                    (a and (a in identified or identified in a)) for a in male_lead_set
                )
                if is_male:
                    in_candidates = True
                    is_male_lead = True
                
                if not in_candidates:
                    # 不在候选中，视为无法确定
                    logger.debug(f"LLM 返回的 '{identified}' 不在候选名单中，视为无法确定")
                    identified = "无法确定"
                    confidence = min(confidence, 0.3)

            # 非事实语用门控：避免把假设/说服场景写回成真实伴侣
            if (speech_act and speech_act != "asserted_fact") or (not is_fact_statement):
                identified = "无法确定"
                is_male_lead = False
                confidence = min(confidence, 0.3)
                if not nonfact_reason:
                    nonfact_reason = f"speech_act={speech_act or 'unknown'}"

            # 本地规则兜底：即使 LLM 漏判，也按规则识别“假设/劝说/类比”
            if _is_nonfact_relation_or_feeling_record({
                "relationship": relationship,
                "detail": "",
                "evidence": evidence,
                "speech_act": speech_act,
            }):
                identified = "无法确定"
                is_male_lead = False
                confidence = min(confidence, 0.3)
                if not nonfact_reason:
                    nonfact_reason = "heuristic_nonfact_relation_or_feeling"
            
            return {
                "identified_partner": identified if identified else "无法确定",
                "is_male_lead": is_male_lead,
                "confidence": max(0.0, min(1.0, confidence)),
                "supporting_quote": supporting_quote,
                "speech_act": speech_act if speech_act else "unknown",
                "is_fact_statement": bool(is_fact_statement and speech_act == "asserted_fact"),
                "nonfact_reason": nonfact_reason,
            }
    except Exception as e:
        logger.warning(f"回溯确定伴侣身份失败: {e}")
    return default_result


def _resolve_vague_partners_by_context(
    heroine_name: str,
    partner_relations: List[Dict[str, Any]],
    male_lead: str,
    chunks: List[str],
    male_lead_aliases: Optional[List[str]] = None,
    heroine_facts: Optional[Dict[str, Any]] = None,
    raw_data_path: Optional[str] = None,
    full_text: Optional[str] = None,
    chunk_starts: Optional[List[int]] = None,
    chunk_entries: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    对 partner_relations 中的伴侣对象进行回溯原文确定身份。
    
    改进：
    1. 使用候选名单约束 LLM 输出
    2. 使用二次截取（evidence 锚定 + 正确回退）减少噪声
    3. 使用置信度门槛早停
    4. 使用缓存避免重复 LLM 调用
    5. 【新增】使用增强的多策略锚定（EXACT -> ELLIPSIS -> FUZZY -> MULTI_FRAGMENT）
    6. 【新增】sanity check：evidence_in_ctx 检查，命中句所在 chunk 与 record.chunk_index 距离检查
    
    流程：
    1. 遍历所有 partner_relations
    2. 对每条伴侣记录，根据 chunk_index 回溯原文
    3. 阶段0~3 逐步扩大窗口，达到置信度门槛则早停
    4. 若最终仍无法确定，标记 _resolution_failed=True
    
    新增参数：
    - full_text: 规范化后的完整原文（用于增强锚定）
    - chunk_starts: chunk 起始位置列表（用于增强锚定）
    """
    if not partner_relations or not chunks:
        return partner_relations
    
    if male_lead_aliases is None:
        male_lead_aliases = []
    if heroine_facts is None:
        heroine_facts = {}
    
    # 构建候选名单
    candidates = _build_partner_candidates_from_facts(
        heroine_facts, male_lead, male_lead_aliases, k=12
    )
    
    # 构建男主及别名集合
    male_lead_set = {male_lead} if male_lead else set()
    male_lead_set.update(a for a in male_lead_aliases if a)
    male_lead_set.discard("")
    
    result: List[Dict[str, Any]] = []
    cache_path = raw_data_path or ""
    
    for pr in partner_relations:
        if not isinstance(pr, dict):
            result.append(pr)
            continue
        
        pr2 = dict(pr)
        partner = str(pr2.get("partner", "") or "").strip()
        
        # 统一对所有伴侣记录进行回溯，不再只处理泛指对象
        
        chunk_index = pr2.get("chunk_index")
        if not chunk_index or not isinstance(chunk_index, int) or chunk_index < 1:
            result.append(pr2)
            continue
        
        evidence = str(pr2.get("evidence", "") or "")
        relationship = str(pr2.get("relationship") or pr2.get("relation_type") or pr2.get("relation") or "")

        # 获取当前 chunk 文本
        center_chunk_text = chunks[chunk_index - 1] if 0 <= (chunk_index - 1) < len(chunks) else ""
        
        best_result: Optional[Dict[str, Any]] = None
        best_confidence = 0.0
        resolution_method = ""
        
        # 阶段定义：(stage_id, expand_range, description)
        stages = [
            (0, -1, "local_ctx"),      # 阶段0：当前 chunk 内 evidence 定位
            (1, 0, "expand_0"),        # 阶段1：当前 chunk
            (2, 1, "expand_1"),        # 阶段2：前后各 1 个 chunk
            (3, 2, "expand_2"),        # 阶段3：前后各 2 个 chunk
        ]
        
        for stage_id, expand_range, stage_desc in stages:
            # 检查缓存
            cache_key = _get_resolution_cache_key(
                cache_path, heroine_name, chunk_index, partner, evidence, stage_id
            )
            if cache_key in _LLM_RESOLUTION_CACHE:
                cached = _LLM_RESOLUTION_CACHE[cache_key]
                confidence = cached.get("confidence", 0.0)
                cached_speech_act = str(cached.get("speech_act", "") or "").strip().lower()
                cached_is_fact = bool(cached.get("is_fact_statement", cached_speech_act == "asserted_fact"))
                cached_nonfact = (cached_speech_act and cached_speech_act != "asserted_fact") or (not cached_is_fact)
                if cached_nonfact:
                    confidence = min(confidence, 0.3)
                    pr2["_resolution_nonfact_speech_act"] = cached_speech_act or "unknown"
                    pr2["_resolution_nonfact_reason"] = str(cached.get("nonfact_reason", "") or "")
                cached_identified = str(cached.get("identified_partner", "无法确定") or "无法确定")
                if confidence >= CONFIDENCE_THRESHOLD and (not cached_nonfact) and cached_identified != "无法确定":
                    best_result = cached
                    best_confidence = confidence
                    resolution_method = f"cached_{stage_desc}"
                    break
                elif confidence > best_confidence:
                    best_result = cached
                    best_confidence = confidence
                    resolution_method = f"cached_{stage_desc}"
                continue
            
            # 根据阶段获取上下文
            if stage_id == 0:
                # 阶段0：当前 chunk 内 evidence 定位
                local_ctx = _extract_local_context_by_evidence(
                    center_chunk_text, evidence, before=3, after=3, max_chars=1800
                )
                if not local_ctx:
                    continue
                refined_context = local_ctx
                anchor_strategy = "LOCAL"
            else:
                # 阶段1-3：扩大窗口 + 二次截取（使用增强锚定）
                if TEXT_ANCHOR_AVAILABLE and full_text and chunk_entries:
                    big_context, _center_chunk_from_fulltext, _spans = get_context_around_chunk_from_fulltext(
                        full_text,
                        chunk_starts,
                        chunk_index,
                        expand_range=expand_range,
                        chunk_entries=chunk_entries,
                    )
                else:
                    big_context = _get_context_around_chunk(chunks, chunk_index, expand_range=expand_range)
                refined_context = _refine_context_by_evidence_with_fallback(
                    big_context, center_chunk_text, evidence,
                    before=3, after=3, max_chars=REFINED_CTX_MAX_CHARS,
                    full_text=full_text,
                    child_name=None,
                    current_heroine=heroine_name,
                    father=None,
                )
                if not refined_context:
                    continue
                anchor_strategy = f"STAGE_{stage_id}"
            
            # Sanity check：evidence_in_ctx 检查
            if TEXT_ANCHOR_AVAILABLE and refined_context and evidence:
                ev_present, ev_meta = evidence_in_ctx(refined_context, evidence)
                if not ev_present:
                    # evidence 不在 ctx 中，降低置信度
                    logger.debug(f"    [伴侣回溯] {heroine_name}/{partner}: evidence 不在 ctx 中 (stage={stage_desc})")
                    pr2["_evidence_in_ctx"] = False
                else:
                    pr2["_evidence_in_ctx"] = True
                    pr2["_evidence_match_mode"] = ev_meta.get("matched_side", "unknown")
            
            # 调用 LLM
            llm_result = _llm_identify_vague_partner(
                heroine_name, partner, evidence, relationship, refined_context, male_lead,
                male_lead_aliases=male_lead_aliases, candidates=candidates
            )
            
            # 缓存结果
            _LLM_RESOLUTION_CACHE[cache_key] = llm_result
            
            confidence = llm_result.get("confidence", 0.0)
            identified = llm_result.get("identified_partner", "无法确定")
            speech_act = str(llm_result.get("speech_act", "") or "").strip().lower()
            is_fact_statement = bool(llm_result.get("is_fact_statement", speech_act == "asserted_fact"))
            is_nonfact = (speech_act and speech_act != "asserted_fact") or (not is_fact_statement)
            if is_nonfact:
                # 保护：原始记录为 asserted_fact 且含明确关系词时，不覆盖事实性
                orig_sa = _normalize_speech_act(pr2.get("speech_act", ""))
                rel_blob = "\n".join([
                    str(pr2.get("relationship") or pr2.get("relation_type") or pr2.get("relation") or ""),
                    str(pr2.get("evidence", "") or ""),
                ])
                has_explicit_rel = any(k in rel_blob for k in _EXPLICIT_PARTNER_ROLE_HINTS)
                if orig_sa == "asserted_fact" and has_explicit_rel:
                    logger.debug(f"    [回溯保护] {partner}: 原始为 asserted_fact 且含明确关系词，跳过 nonfact 覆盖")
                else:
                    confidence = min(confidence, 0.3)
                    pr2["_resolution_nonfact_speech_act"] = speech_act or "unknown"
                    pr2["_resolution_nonfact_reason"] = str(llm_result.get("nonfact_reason", "") or "")
            
            # 更新最佳结果
            if confidence > best_confidence:
                best_result = llm_result
                best_confidence = confidence
                resolution_method = stage_desc
            
            # 置信度门槛早停
            if confidence >= CONFIDENCE_THRESHOLD and (not is_nonfact) and identified != "无法确定":
                break
        
        # 根据最佳结果决定是否写回
        if best_result and best_confidence >= CONFIDENCE_THRESHOLD:
            identified = best_result.get("identified_partner", "无法确定")
            best_speech_act = str(best_result.get("speech_act", "") or "").strip().lower()
            best_is_fact = bool(best_result.get("is_fact_statement", best_speech_act == "asserted_fact"))
            best_nonfact = (best_speech_act and best_speech_act != "asserted_fact") or (not best_is_fact)
            if best_nonfact:
                # 保护：原始记录为 asserted_fact 且含明确关系词时，不覆盖事实性
                orig_sa = _normalize_speech_act(pr2.get("speech_act", ""))
                rel_blob = "\n".join([
                    str(pr2.get("relationship") or pr2.get("relation_type") or pr2.get("relation") or ""),
                    str(pr2.get("evidence", "") or ""),
                ])
                has_explicit_rel = any(k in rel_blob for k in _EXPLICIT_PARTNER_ROLE_HINTS)
                if orig_sa == "asserted_fact" and has_explicit_rel:
                    logger.debug(
                        f"    [回溯保护] {heroine_name} 的伴侣 '{partner}': "
                        f"原始为 asserted_fact 且含明确关系词，跳过 nonfact 覆盖"
                    )
                else:
                    pr2["_resolution_failed"] = True
                    pr2["_resolution_confidence"] = min(best_confidence, 0.3)
                    pr2["_resolution_nonfact_speech_act"] = best_speech_act or "unknown"
                    pr2["_resolution_nonfact_reason"] = str(best_result.get("nonfact_reason", "") or "")
                    logger.info(
                        f"    [回溯失败] {heroine_name} 的伴侣 '{partner}' 为非事实语气"
                        f"(speech_act={best_speech_act or 'unknown'})，拒绝回写"
                    )
            elif identified and identified != "无法确定":
                logger.info(f"    [回溯确定] {heroine_name} 的伴侣 '{partner}' → '{identified}' (confidence={best_confidence:.2f})")
                pr2["partner"] = identified
                pr2["_partner_resolved_from"] = partner
                pr2["_resolution_confidence"] = best_confidence
                pr2["_resolution_method"] = resolution_method
                pr2["_supporting_quote"] = best_result.get("supporting_quote", "")
                pr2["_resolution_speech_act"] = best_speech_act if best_speech_act else "unknown"
                
                # 判断是否为男主（基于精确集合匹配，不用子串包含）
                is_male_lead = best_result.get("is_male_lead", False)
                if not is_male_lead:
                    # 再检查 identified 是否精确匹配男主或别名
                    is_male_lead = identified in male_lead_set
                if is_male_lead:
                    pr2["is_male_lead"] = True
            else:
                # 虽然置信度高，但 identified 为"无法确定"
                logger.info(f"    [回溯失败] {heroine_name} 的伴侣 '{partner}' 无法确定（置信度={best_confidence:.2f}）")
                pr2["_resolution_failed"] = True
                pr2["_resolution_confidence"] = best_confidence
        else:
            # 置信度不足或无结果
            logger.info(f"    [回溯失败] {heroine_name} 的伴侣 '{partner}' 置信度不足({best_confidence:.2f}<{CONFIDENCE_THRESHOLD})，保留原值")
            pr2["_resolution_failed"] = True
            pr2["_resolution_confidence"] = best_confidence
        
        result.append(pr2)
    
    # 统计日志：伴侣回溯 anchor_strategy 分布、evidence_in_ctx=false 数量
    if result:
        resolved_count = sum(1 for r in result if r.get("_partner_resolved_from"))
        failed_count = sum(1 for r in result if r.get("_resolution_failed"))
        evidence_missing_count = sum(1 for r in result if r.get("_evidence_in_ctx") is False)
        if resolved_count > 0 or failed_count > 0:
            logger.info(f"    [伴侣回溯统计] {heroine_name}: 成功={resolved_count}, 失败={failed_count}, evidence_missing={evidence_missing_count}")
    
    return result


_EXPLICIT_PARTNER_ROLE_HINTS = [
    "丈夫", "夫君", "夫婿", "相公", "老公",
    "前夫", "未婚夫", "男友", "男朋友", "恋人",
    "婚约", "订婚", "成亲", "完婚", "结婚", "离婚", "再婚",
]


def _sanitize_partner_relations_for_purity(partner_relations: List[Dict[str, Any]], male_lead: str) -> List[Dict[str, Any]]:
    """
    清洗 partner_relations 中“对象名泛指”导致的误判：
    - 诸如 partner="某个男人"/"他" 且关系词不明确（如“亲密关系”）时，视为低置信度噪声，默认剔除
    - 若关系词本身很明确（丈夫/前夫/未婚夫等），允许把 partner 归一为该身份标签（例如 partner="丈夫"）
    """
    if not partner_relations:
        return []
    out: List[Dict[str, Any]] = []
    ml = str(male_lead or "").strip()

    for pr in partner_relations:
        if not isinstance(pr, dict):
            continue
        pr2 = dict(pr)
        partner = str(pr2.get("partner", "") or "").strip()
        # 统一 relationship 字段（兼容 relation/relation_type）
        relationship = str(pr2.get("relationship") or pr2.get("relation") or pr2.get("relation_type") or "").strip()
        status = str(pr2.get("status", "") or "").strip()
        evidence = str(pr2.get("evidence", "") or "")
        detail = str(pr2.get("detail", "") or "")
        blob = "\n".join([partner, relationship, status, evidence, detail])

        # 过滤假设/劝说/类比语句，避免把"想象场景"当成真实伴侣关系
        if _is_nonfact_relation_or_feeling_record(pr2):
            logger.debug(f"    [伴侣清洗] 过滤非事实记录: partner={partner}, speech_act={pr2.get('speech_act')}, _resolution_nonfact={pr2.get('_resolution_nonfact_speech_act')}")
            continue

        # 若文本明显提到男主，则强制视为男主相关（不纳入非男主伴侣）
        if ml and _text_contains_male_lead(blob, ml):
            pr2["is_male_lead"] = True
            out.append(pr2)
            continue

        # 对象名是泛指：只在“关系词很明确”时保留，否则剔除
        if _is_vague_male_name(partner):
            if any(h in (relationship + status) for h in _EXPLICIT_PARTNER_ROLE_HINTS):
                # 用身份标签替代“某个男人”，避免后续误当作“另一个男人”
                role = next((h for h in _EXPLICIT_PARTNER_ROLE_HINTS if h in (relationship + status)), "丈夫")
                pr2["partner"] = role
                pr2["_partner_was_vague"] = True
                out.append(pr2)
            else:
                # 典型如：partner="某个男人" + relation_type="亲密关系"（证据片面且不指向明确对象）
                # 为避免误判，默认剔除这类低置信度条目
                continue
        else:
            out.append(pr2)
    return out

# 非亲生孩子关键词列表（用于判断孩子是否为亲生）
NON_BIOLOGICAL_KEYWORDS = [
    # 收养/领养类
    "收养", "领养", "养女", "养子", "继女", "继子", "继子女",
    "过继", "寄养", "托孤", "遗孤", "孤儿",
    # 非亲生关系
    "认作女儿", "当作女儿", "认作儿子", "当作儿子",
    "不是亲生", "非亲生", "并非亲生", "不是她的孩子",
    "不是她亲生", "不是自己的孩子", "不是亲女儿", "不是亲儿子",
    # 其他关系误判
    "捡来", "救下后", "师徒", "弟子", "徒弟", "学生",
    "妹妹", "姐姐", "侄女", "外甥女",
    # 特殊来源
    "单性", "无性", "孤雌", "功法", "法术", "魔法",
    # 辅助生殖/非性行为怀孕（处女妈妈豁免）
    "试管", "试管婴儿", "人工授精", "人工受精", "人工受孕", "辅助生殖",
    "供精", "捐精", "精子库", "冻精", "胚胎移植", "移植胚胎", "取卵", "授精",
    "代孕",
    # 其他奇幻来源
    "神明", "天降", "转生", "克隆", "复制", "分裂",
    "凭空", "造物", "创造", "召唤"
]


def _derive_partners_from_children_info(
    children_info: List[Dict[str, Any]],
    male_lead: str,
    existing_partner_relations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    从 children_info 推导隐含伴侣：
    - 遍历 children_info 找 is_biological=True 的亲生孩子
    - 提取 father 字段作为隐含伴侣
    - 过滤：跳过 placeholder、男主、非亲生关键词
    - 去重：跳过 existing_partner_relations 中已有的同名伴侣
    - 返回合成的 partner 记录列表（标记 _derived_from_children=True）
    """
    if not children_info:
        return []

    ml = str(male_lead or "").strip()

    # 构建已有伴侣归一化名集合（用于去重）
    existing_keys: set = set()
    for pr in (existing_partner_relations or []):
        p = str(pr.get("partner", "") or "").strip()
        if p:
            existing_keys.add(_normalize_partner_key(p))

    derived: List[Dict[str, Any]] = []
    seen_fathers: set = set()  # 避免同一父亲多次推导

    for child in children_info:
        if not isinstance(child, dict):
            continue

        # 只处理亲生孩子
        is_bio = child.get("is_biological")
        if is_bio is not True:
            continue

        father_raw = str(child.get("father", "") or "").strip()
        if not father_raw:
            continue

        # 过滤 placeholder
        if _is_placeholder(father_raw):
            continue

        # 从 father 字段解析状态信息（如"藤原（已故）" → name="藤原", status="已故"）
        father_name = father_raw
        father_status = ""
        for bracket_open, bracket_close in [("（", "）"), ("(", ")")]:
            if bracket_open in father_raw and bracket_close in father_raw:
                idx_open = father_raw.index(bracket_open)
                idx_close = father_raw.index(bracket_close)
                if idx_close > idx_open:
                    father_status = father_raw[idx_open + 1:idx_close].strip()
                    father_name = father_raw[:idx_open].strip()
                    break

        if not father_name or _is_placeholder(father_name):
            continue

        # 过滤男主
        if ml and _text_contains_male_lead(father_name, ml):
            continue

        # 检查 origin/evidence/detail 是否含非亲生关键词
        origin = str(child.get("origin", "") or "")
        raw_evidence = str(child.get("evidence", "") or "")
        raw_detail = str(child.get("detail", "") or "")
        child_blob = "\n".join([origin, raw_evidence, raw_detail])
        if any(kw in child_blob for kw in NON_BIOLOGICAL_KEYWORDS):
            continue

        # 去重：已有同名伴侣则跳过
        father_key = _normalize_partner_key(father_name)
        if father_key in existing_keys or father_key in seen_fathers:
            continue
        seen_fathers.add(father_key)

        child_name = str(child.get("child_name", "") or "")
        chunk_index = child.get("chunk_index")
        evidence = _clip_llm_text(raw_evidence, 180)

        derived.append({
            "partner": father_name,
            "is_male_lead": False,
            "relationship": "孩子的父亲",
            "status": father_status or "未知",
            "forced": None,
            "evidence": evidence,
            "detail": f"从亲生孩子{child_name}推导：{evidence[:80]}" if evidence else f"从亲生孩子{child_name}推导",
            "speech_act": "asserted_fact",
            "evidence_strength": str(child.get("evidence_strength", "strong") or "strong"),
            "is_fact_statement": True,
            "chunk_index": chunk_index,
            "_derived_from_children": True,
        })

    return derived


def _is_placeholder(value: str) -> bool:
    """检查值是否为占位符（不是真正的角色名）"""
    if not value:
        return True
    value_lower = value.strip().lower()
    if value_lower in PLACEHOLDER_NAMES:
        return True
    # 单字符检查（如单独的"无"）
    if len(value.strip()) <= 1 and value.strip() in "无没空":
        return True
    return False


def judge_purity_by_facts(
    name: str,
    facts: Dict[str, Any],
    male_lead: str,
    female_role_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    基于结构化事实进行规则判定，不依赖 LLM
    
    输入：
    - name: 女主名称
    - facts: 结构化事实，包含 sexual_relations, children_info, physical_contacts, romantic_feelings, partner_relations
    - male_lead: 男主名称
    
    输出：
    - 五维洁度判定结果（含前世/原故事线与接触等级补充）
    """
    # 先清洗一遍，避免“身世”误当“生育”
    facts = _sanitize_purity_facts_for_heroine(name, facts)
    # 与 stepwise 路径保持一致：清洗泛指/非事实伴侣关系，避免规则路径误判
    try:
        facts["partner_relations"] = _sanitize_partner_relations_for_purity(
            facts.get("partner_relations", []), male_lead
        )
    except Exception:
        pass

    result = {
        "name": name,
        "is_virgin": True,
        # 注意：本项目“处女”是【排他性处女】——仅与男主发生关系也依然判✅
        "virgin_status": "✅ 处女（排他性：仅男主不算非处）",
        "has_other_contact": False,
        "contact_status": "✅ 无接触（无非男主接触记录）",
        "no_partner": True,
        "partner_status": "✅ 无男伴（无非男主伴侣记录）",
        "is_spirit_clean": True,
        # 注意：本项目“精神洁”是【排他性精神洁】——出现非男主性关系/亲生孩子/正式伴侣即判❌
        "spirit_status": "✅ 精神洁（无非男主关系/性/情记录）",
        # 规则维度理由（用于与 LLM 对照展示）
        "rule_virgin_reason": "",
        "rule_contact_reason": "",
        "rule_partner_reason": "",
        "rule_spirit_reason": "",
        "summary": "",
        "is_clean": True,
        "evidence_used": [],
    }
    
    reasons = []
    virgin_reasons: List[str] = []
    contact_reasons: List[str] = []
    partner_reasons: List[str] = []
    spirit_reasons: List[str] = []
    
    # ========== 维度1：是否处女 ==========
    sexual_relations = facts.get("sexual_relations", [])
    children_info = facts.get("children_info", [])
    
    # 检查性关系（过滤占位符）
    non_ml_sex = []
    for sr in sexual_relations:
        partner = sr.get("partner", "")
        if sr.get("is_male_lead", False):
            continue
        if not _is_placeholder(partner):
            non_ml_sex.append(sr)
            continue
        # partner 为“未知/空”等占位符时，若证据/细节强烈指向性行为，也应算作非男主性关系
        detail = str(sr.get("detail", "") or "")
        evidence = str(sr.get("evidence", "") or "")
        if _contains_any(detail + "\n" + evidence, SEX_ACT_HINT_KEYWORDS):
            sr2 = dict(sr)
            sr2["partner"] = sr2.get("partner") or "未知男性"
            non_ml_sex.append(sr2)
    
    if non_ml_sex:
        result["is_virgin"] = False
        partners = [sr.get("partner", "未知") for sr in non_ml_sex]
        evidence = non_ml_sex[0].get("evidence", "")[:50]
        result["virgin_status"] = f"❌ 非处（与非男主{','.join(partners)}有性关系）"
        reason_text = f"与非男主有性关系: {evidence}"
        reasons.append(reason_text)
        virgin_reasons.append(reason_text)
        result["evidence_used"].append(evidence)
    
    # 检查孩子来源（仅当孩子是亲生且父亲非男主时判定为非处）
    non_ml_bio_children: List[Dict[str, Any]] = []
    for child in children_info:
        father = child.get("father", "")
        origin = (child.get("origin") or "").lower()
        evidence_text = (child.get("evidence") or "").lower()
        detail_text = (child.get("detail") or "").lower()
        child_name = str(child.get("child_name", "") or "")

        # 防呆：如果这条“孩子信息”其实是在描述“她是谁的女儿/谁生下了她”，则跳过
        raw_evidence = str(child.get("evidence", "") or "") + "\n" + str(child.get("detail", "") or "")
        if _looks_like_parentage_as_child(name, raw_evidence):
            continue
        if _looks_like_non_child_kinship_fact(name, raw_evidence, child_name):
            continue
        child_category, _child_reason = _classify_child_record_heuristic(name, child, male_lead)
        if child_category in ("NOT_A_CHILD_FACT", "PREGNANCY_ONLY_OR_WEAK", "MALE_LEAD_CHILD"):
            continue

        # 【核心判断】检查是否为亲生孩子
        is_biological = child.get("is_biological", None)
        
        # 如果明确标注为非亲生，直接跳过
        if is_biological is False:
            continue
        
        # 检查 origin、evidence、detail 中是否有非亲生关键词
        all_text = f"{origin} {evidence_text} {detail_text}"
        is_non_biological = any(kw in all_text for kw in NON_BIOLOGICAL_KEYWORDS)
        
        # 如果检测到非亲生关键词，跳过
        if is_non_biological:
            continue
        
        # 判断父亲是否是男主
        is_father_male_lead = False
        if father:
            father_lower = (father or "").lower()
            male_lead_lower = (male_lead or "").lower()
            if male_lead_lower and (male_lead_lower in father_lower or father_lower in male_lead_lower):
                is_father_male_lead = True
            if father in ["男主", "主角"]:
                is_father_male_lead = True

        # father 可能为”未知/空”等占位符：若文本强提示”正常生育/怀孕/亲生”等，仍应视为非男主亲生孩子
        father_label = father
        if _is_placeholder(father_label):
            # 若明显是”意愿/请求/计划”，不视为已生育
            is_intent_only = _contains_any(all_text, INTENT_ONLY_KEYWORDS) or ("让" in all_text and "怀孕" in all_text)
            # 原有检查：文本中有生育关键词
            has_birth_hint = _contains_any(all_text, BIOLOGICAL_BIRTH_HINT_KEYWORDS)
            # 新增检查：conception_method=”sex” 且 is_biological=True（双重条件防误触）
            conception = str(child.get("conception_method", "") or "").lower()
            has_conception_hint = (conception == "sex" and is_biological is True)
            if (not is_intent_only) and (has_birth_hint or has_conception_hint):
                father_label = "未知男性"
            else:
                continue

        # 只有确认是亲生 + 父亲非男主时才判定为非处
        if not is_father_male_lead and father_label:
            result["is_virgin"] = False
            evidence = child.get("evidence", "")[:50]
            result["virgin_status"] = f"❌ 非处（与非男主{father_label}有亲生孩子）"
            reason_text = f"与非男主有亲生孩子: {evidence}"
            reasons.append(reason_text)
            virgin_reasons.append(reason_text)
            result["evidence_used"].append(evidence)
            non_ml_bio_children.append({"father": father_label, "evidence": evidence, "raw": child})
    
    # ========== 维度2：有无非男主肉体接触 ==========
    female_name_norm_set = _build_name_norm_set((female_role_names or []) + [name])
    physical_contacts = facts.get("physical_contacts", [])
    non_ml_contacts = []
    for pc in physical_contacts:
        partner = pc.get("partner", "")
        if pc.get("is_male_lead", False) or _is_placeholder(partner):
            continue
        male_hint = _is_likely_male_counterpart(
            str(partner or ""),
            " ".join([
                str(pc.get("contact_type", "") or ""),
                str(pc.get("evidence", "") or ""),
                str(pc.get("detail", "") or ""),
            ]),
            female_name_norm_set,
        )
        # 规则模式下仅排除“明确女性”；其余沿用原有行为，避免漏判
        if male_hint is not False:
            non_ml_contacts.append(pc)
    
    if non_ml_contacts:
        result["has_other_contact"] = True
        partners = [pc.get("partner", "未知") for pc in non_ml_contacts]
        contact_types = [pc.get("contact_type", "") for pc in non_ml_contacts]
        evidence = non_ml_contacts[0].get("evidence", "")[:50]
        result["contact_status"] = f"❌ 有接触（被非男主{','.join(partners)}{','.join(contact_types)}）"
        reason_text = f"被非男主接触: {evidence}"
        reasons.append(reason_text)
        contact_reasons.append(reason_text)
        result["evidence_used"].append(evidence)

    # 性关系/亲生孩子必然包含实际身体接触（即使 physical_contacts 没抽到，也应判为“有接触”）
    if not result["has_other_contact"]:
        if non_ml_sex:
            partners = [sr.get("partner", "未知") for sr in non_ml_sex]
            evidence = (non_ml_sex[0].get("evidence") or non_ml_sex[0].get("detail") or "")[:50]
            result["has_other_contact"] = True
            result["contact_status"] = f"❌ 有接触（与非男主{','.join(partners)}发生性关系）"
            reason_text = f"与非男主发生性关系: {evidence}"
            reasons.append(reason_text)
            contact_reasons.append(reason_text)
            result["evidence_used"].append(evidence)
        elif non_ml_bio_children:
            father_label = non_ml_bio_children[0].get("father", "未知男性")
            evidence = (non_ml_bio_children[0].get("evidence") or "")[:50]
            result["has_other_contact"] = True
            result["contact_status"] = f"❌ 有接触（与非男主{father_label}生育亲生孩子）"
            reason_text = f"与非男主生育亲生孩子: {evidence}"
            reasons.append(reason_text)
            contact_reasons.append(reason_text)
            result["evidence_used"].append(evidence)
    
    # ========== 维度3：有无非男主伴侣 ==========
    partner_relations = facts.get("partner_relations", [])
    logger.debug(f"    [规则-伴侣] 输入 partner_relations 共 {len(partner_relations)} 条")
    non_ml_partners = []
    for pr in partner_relations:
        partner = pr.get("partner", "")
        # 过滤占位符
        if _is_placeholder(partner):
            continue
        if not pr.get("is_male_lead", False):
            gender_hint = _is_likely_male_counterpart(
                str(partner or ""),
                " ".join([
                    str(pr.get("relationship", "") or ""),
                    str(pr.get("status", "") or ""),
                    str(pr.get("evidence", "") or ""),
                    str(pr.get("detail", "") or ""),
                    str(pr.get("analysis_reason", "") or ""),
                ]),
                female_name_norm_set,
            )
            # 明确女性对象不计入“男伴”
            if gender_hint is False:
                continue
            # 检查是否为"被迫订婚且未完婚"的豁免情况
            status = (pr.get("status") or "").lower()
            forced = pr.get("forced", False)
            is_unconsummated_forced = forced and any(kw in status for kw in ["未完婚", "订婚", "婚约解除", "逃婚", "未婚"])
            
            if not is_unconsummated_forced:
                non_ml_partners.append(pr)
    
    if non_ml_partners:
        result["no_partner"] = False
        partners = [pr.get("partner", "未知") for pr in non_ml_partners]
        relationships = [pr.get("relationship", "") for pr in non_ml_partners]
        evidence = non_ml_partners[0].get("evidence", "")[:50]
        result["partner_status"] = f"❌ 有男伴（有非男主{','.join(relationships)}{','.join(partners)}）"
        reason_text = f"有非男主伴侣: {evidence}"
        reasons.append(reason_text)
        partner_reasons.append(reason_text)
        result["evidence_used"].append(evidence)

    # 兜底：partner_relations 未检测到伴侣，但有非男主亲生孩子 → 推导有伴侣
    if result["no_partner"] and non_ml_sex:
        for item in non_ml_sex:
            father = item.get("partner", "")
            if father and not _is_placeholder(father):
                result["no_partner"] = False
                result["partner_status"] = f"❌ 有男伴（从亲生孩子推导：{father}）"
                reason_text = f"从亲生孩子推导伴侣: {father}"
                reasons.append(reason_text)
                partner_reasons.append(reason_text)
                break

    # 精神维度专用：伴侣关系豁免判定（不限性别，且不影响"男伴"维度）
    spirit_non_exempt_partner_history: List[Dict[str, Any]] = []
    spirit_exempt_partner_notes: List[str] = []
    rule_partner_analysis_map = _build_partner_analysis_map(_simple_merge_partner_relations(partner_relations or []))
    has_non_ml_sex_evidence_for_spirit = bool(non_ml_sex)
    for pr in partner_relations:
        if not isinstance(pr, dict):
            continue
        partner = str(pr.get("partner", "") or "").strip()
        if _is_placeholder(partner):
            continue
        if pr.get("is_male_lead", False):
            continue
        try:
            if _is_nonfact_relation_or_feeling_record(pr):
                continue
        except Exception:
            pass
        is_exempt, exempt_reason = _is_spirit_exempt_partner_relation(
            pr,
            analyzed_partner_map=rule_partner_analysis_map,
            has_non_ml_sex_evidence=has_non_ml_sex_evidence_for_spirit,
        )
        if is_exempt:
            ev = str(pr.get("evidence", "") or "")[:50]
            spirit_exempt_partner_notes.append(
                f"{partner}命中豁免({exempt_reason})" + (f"，证据:{ev}" if ev else "")
            )
        else:
            spirit_non_exempt_partner_history.append(pr)
    
    # ========== 维度4：精神洁度 ==========
    romantic_feelings = facts.get("romantic_feelings", [])
    non_ml_feelings = []
    non_ml_positive_feelings = []
    spirit_non_positive_feeling_notes: List[str] = []
    for rf in romantic_feelings:
        target = rf.get("target", "")
        # 过滤占位符
        if _is_placeholder(target):
            continue
        if not rf.get("is_male_lead", False):
            non_ml_feelings.append(rf)
            if _is_positive_non_ml_feeling_record(rf):
                non_ml_positive_feelings.append(rf)
            else:
                ev = str(rf.get("evidence", "") or "")[:50]
                spirit_non_positive_feeling_notes.append(
                    f"{target}缺少正向动心证据" + (f"，证据:{ev}" if ev else "")
                )
    
    if non_ml_positive_feelings:
        result["is_spirit_clean"] = False
        targets = [rf.get("target", "未知") for rf in non_ml_positive_feelings]
        evidence = non_ml_positive_feelings[0].get("evidence", "")[:50]
        result["spirit_status"] = f"❌ 精神非初（爱过非男主{','.join(targets)}）"
        reason_text = f"爱过非男主（明确正向动心证据）: {evidence}"
        reasons.append(reason_text)
        spirit_reasons.append(reason_text)
        result["evidence_used"].append(evidence)

    # 排他性精神洁（含豁免）：仅“非豁免伴侣”或非男主性关系/亲生孩子可判❌精神非初
    if result["is_spirit_clean"]:
        if spirit_non_exempt_partner_history:
            partner = spirit_non_exempt_partner_history[0].get("partner", "未知")
            evidence = (spirit_non_exempt_partner_history[0].get("evidence") or "")[:50]
            result["is_spirit_clean"] = False
            result["spirit_status"] = f"❌ 精神非初（有非男主伴侣：{partner}）"
            reason_text = f"有非男主伴侣(非豁免，精神不洁): {evidence}"
            reasons.append(reason_text)
            spirit_reasons.append(reason_text)
            result["evidence_used"].append(evidence)
        elif non_ml_sex:
            partner = non_ml_sex[0].get("partner", "未知")
            evidence = (non_ml_sex[0].get("evidence") or non_ml_sex[0].get("detail") or "")[:50]
            result["is_spirit_clean"] = False
            result["spirit_status"] = f"❌ 精神非初（与非男主{partner}发生性关系）"
            reason_text = f"与非男主发生性关系(精神不洁): {evidence}"
            reasons.append(reason_text)
            spirit_reasons.append(reason_text)
            result["evidence_used"].append(evidence)
        elif non_ml_bio_children:
            father_label = non_ml_bio_children[0].get("father", "未知男性")
            evidence = (non_ml_bio_children[0].get("evidence") or "")[:50]
            result["is_spirit_clean"] = False
            result["spirit_status"] = f"❌ 精神非初（与非男主{father_label}生育亲生孩子）"
            reason_text = f"与非男主生育亲生孩子(精神不洁): {evidence}"
            reasons.append(reason_text)
            spirit_reasons.append(reason_text)
            result["evidence_used"].append(evidence)
        elif spirit_exempt_partner_notes:
            spirit_reasons.append(
                "规则豁免：非男主伴侣记录仅体现被迫/受害或无性行为受孕，且缺少正向动心与非男主性关系证据；"
                f"样例:{spirit_exempt_partner_notes[0][:80]}"
            )

    if result["is_spirit_clean"] and (not spirit_reasons):
        if spirit_non_positive_feeling_notes:
            spirit_reasons.append(
                "规则豁免：存在非男主感情条目但无正向动心证据；"
                f"样例:{spirit_non_positive_feeling_notes[0][:80]}"
            )
    
    # ========== 综合判定 ==========
    result["is_clean"] = (
        result["is_virgin"] and 
        not result["has_other_contact"] and 
        result["no_partner"] and 
        result["is_spirit_clean"]
    )
    
    if reasons:
        result["summary"] = "; ".join(reasons[:3])  # 最多3条理由
    else:
        result["summary"] = "无不洁记录，默认全初"
    
    result["rule_virgin_reason"] = "; ".join(virgin_reasons[:2]) if virgin_reasons else "规则判断：未发现非男主性关系或非男主亲生孩子。"
    result["rule_contact_reason"] = "; ".join(contact_reasons[:2]) if contact_reasons else "规则判断：未发现非男主实际身体接触。"
    result["rule_partner_reason"] = "; ".join(partner_reasons[:2]) if partner_reasons else "规则判断：未发现非男主正式男伴。"
    result["rule_spirit_reason"] = "; ".join(spirit_reasons[:2]) if spirit_reasons else "规则判断：未发现非男主对象的感情/伴侣/性关系/非男主亲生孩子。"
    
    # 生成兼容旧版的 body_status
    result["body_status"] = f"处女:{result['virgin_status']} | 接触:{result['contact_status']} | 男伴:{result['partner_status']}"
    
    return _normalize_purity_result_consistency(result)


def judge_character_purity_by_facts(name: str, scan_facts: Dict[str, Any], detail_facts: Dict[str, Any], male_lead: str) -> Dict[str, Any]:
    """
    综合 scan 和 detail 的结构化事实进行判定
    """
    # 合并事实
    merged_facts = _empty_purity_fact_bucket()
    
    # 从 scan_facts 合并
    for key in _FACT_DIMENSIONS:
        merged_facts[key].extend(scan_facts.get(key, []))
    
    # 从 detail_facts.purity_facts 合并
    purity_facts = detail_facts.get("purity_facts", {})
    for key in _FACT_DIMENSIONS:
        merged_facts[key].extend(purity_facts.get(key, []))
    
    # 去重（基于 evidence）
    merged_facts = _dedupe_fact_bucket_by_evidence(merged_facts)
    
    return judge_purity_by_facts(name, merged_facts, male_lead)


# --- 3. LLM 校验函数：验证程序判定结果 ---

def verify_purity_by_llm(name: str, facts: Dict[str, Any], program_result: Dict[str, Any], male_lead: str) -> Dict[str, Any]:
    """
    让 LLM 校验程序的判定结果是否正确
    
    返回：
    - agree: bool - LLM 是否同意程序判定
    - llm_result: dict - LLM 的判定结果
    - reason: str - 不同意的理由
    """
    # 清洗事实，避免“她是谁的女儿/某人生下她”被当作“她有孩子”
    facts = _sanitize_purity_facts_for_heroine(name, facts)

    # 构建事实描述
    facts_text = []
    for sr in facts.get("sexual_relations", []):
        partner = sr.get("partner", "未知")
        is_ml = "男主" if sr.get("is_male_lead") else "非男主"
        detail = _clip_llm_text(sr.get("detail", ""), 120)
        evidence = _clip_llm_text(sr.get("evidence", ""), 120)
        facts_text.append(f"[性关系] 与{partner}({is_ml}): {detail} | 证据: {evidence}")
    
    for ci in facts.get("children_info", []):
        # 双保险：即使上游没清干净，这里也跳过明显“身世句”
        try:
            blob = (
                str(ci.get("evidence", "") or "")
                + "\n"
                + str(ci.get("detail", "") or "")
                + "\n"
                + str(ci.get("origin", "") or "")
                + "\n"
                + str(ci.get("child_name", "") or "")
            )
            if _looks_like_parentage_as_child(name, blob):
                continue
            if _looks_like_non_child_kinship_fact(name, blob, str(ci.get("child_name", "") or "")):
                continue
        except Exception:
            pass
        father = ci.get("father", "未知")
        origin = ci.get("origin", "未知")
        evidence = _clip_llm_text(ci.get("evidence", ""), 120)
        facts_text.append(f"[孩子] 父亲:{father}, 来源:{origin} | 证据: {evidence}")
    
    for pc in facts.get("physical_contacts", []):
        partner = pc.get("partner", "未知")
        is_ml = "男主" if pc.get("is_male_lead") else "非男主"
        ctype = pc.get("contact_type", "")
        evidence = _clip_llm_text(pc.get("evidence", ""), 120)
        facts_text.append(f"[肉体接触] 与{partner}({is_ml}){ctype} | 证据: {evidence}")
    
    for rf in facts.get("romantic_feelings", []):
        target = rf.get("target", "未知")
        is_ml = "男主" if rf.get("is_male_lead") else "非男主"
        evidence = _clip_llm_text(rf.get("evidence", ""), 120)
        facts_text.append(f"[感情] 对{target}({is_ml})有感情 | 证据: {evidence}")
    
    for pr in facts.get("partner_relations", []):
        partner = pr.get("partner", "未知")
        is_ml = "男主" if pr.get("is_male_lead") else "非男主"
        rel = pr.get("relationship", "")
        status = pr.get("status", "")
        forced_flag = pr.get("forced", None)
        forced = "被迫" if forced_flag is True else ("自愿" if forced_flag is False else "未知")
        evidence = _clip_llm_text(pr.get("evidence", ""), 120)
        facts_text.append(f"[伴侣] 与{partner}({is_ml})的{rel}关系, 状态:{status}, {forced} | 证据: {evidence}")

    _append_extended_relationship_facts_text(facts_text, facts)
    
    if not facts_text:
        facts_text.append("（无任何事实记录）")
    
    facts_str = _compact_llm_lines(facts_text, label="结构化事实")
    
    # 构建程序判定结果描述
    program_str = f"""
程序判定结果：
- 是否处女: {program_result.get('virgin_status', '未知')}
- 非男主接触: {program_result.get('contact_status', '未知')}
- 有无男伴: {program_result.get('partner_status', '未知')}
- 精神洁: {program_result.get('spirit_status', '未知')}
- 综合: {'全初' if program_result.get('is_clean') else '有瑕'}
- 依据: {program_result.get('summary', '无')}
"""
    
    system_prompt = f"""你是一个严格的校验专家。你的任务是：
1. 查看结构化事实数据
2. 查看程序的判定结果
3. 判断程序的判定是否正确

【男主】：{male_lead}

【重要定义（本项目为“独占/排他”口径，不按现实语义）】：
- “处女”=【排他性处女】：只要所有性关系都发生在男主身上，也必须判 ✅处女（仅男主）。
  严禁把“与男主发生关系”判成 ❌非处！
- “精神洁”=【排他性精神洁】：只要存在非男主的性关系/亲生孩子/正式伴侣（男友/丈夫/前夫等）/明确爱过，就判 ❌精神非初。
  仅“被迫订婚且未完婚/未圆房”等可豁免。

【五维判定规则（核心四项 + 前世/接触等级补充）】：
1. 是否处女：仅与男主发生性关系/或无性关系 = ✅处女；任何与非男主的性关系/亲生孩子(父亲非男主或未知且为正常生育) = ❌非处
2. 非男主接触：被非男主实际触碰 = ❌有接触；若存在与非男主性关系或亲生孩子，也视为 ❌有接触（即使 physical_contacts 没抽到）
3. 有无男伴：无非男主正式伴侣 = ✅无男伴；有非男主男友/丈夫 = ❌有男伴（被迫订婚未完婚豁免）
4. 精神洁：无非男主恋爱/伴侣/性关系/亲生孩子 = ✅精神洁；存在任一则 ❌精神非初

【关键】：
- is_male_lead=true 表示与男主相关，不影响洁度
- is_male_lead=false 表示与非男主相关，才可能影响洁度
- 无事实记录 = 默认✅洁
- 经济依附、权力关系、政治联姻、受害/胁迫记录是辅助事实；不能把“未遂/计划中/未圆房/被迫且无感情”的记录直接当成已发生性关系。
- 只允许基于事实判断，禁止“推定/脑补”。

【重要防呆（必须严格执行）】：
- 若 children_info / 证据描述的是“{name}是谁的女儿/之女/某人生下了{name}”，这是【身世/出身】信息，
  不代表 {name} “生过孩子”，必须忽略这类条目，严禁据此判 ❌非处 / ❌精神非初 / ❌有接触。

输出 JSON：
{{
  "agree": true/false,           // 是否同意程序判定
  "reason": "若不同意，说明哪个维度判错了，为什么",
  "is_virgin": true/false,       // 你的判定
  "virgin_status": "简短结论",
  "has_other_contact": true/false,
  "contact_status": "简短结论",
  "no_partner": true/false,
  "partner_status": "简短结论",
  "is_spirit_clean": true/false,
  "spirit_status": "简短结论"
}}"""

    user_prompt = f"""请校验以下判定：

【角色】：{name}

【结构化事实】：
{facts_str}

{program_str}

请判断程序的判定是否正确，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        return data
    except Exception as e:
        logger.warning(f"LLM 校验失败: {e}")
        return {"agree": True, "reason": f"校验失败: {e}"}


def verify_purity_second_round(name: str, facts: Dict[str, Any], program_result: Dict[str, Any], 
                                first_llm_result: Dict[str, Any], male_lead: str) -> Dict[str, Any]:
    """
    第二轮校验：当 LLM 与程序不一致时，再次让 LLM 仔细判断
    """
    # 清洗事实，避免“身世句”进入二次裁决
    facts = _sanitize_purity_facts_for_heroine(name, facts)

    # 构建事实描述（同上）
    facts_text = []
    for sr in facts.get("sexual_relations", []):
        partner = sr.get("partner", "未知")
        is_ml = "男主" if sr.get("is_male_lead") else "非男主"
        detail = _clip_llm_text(sr.get("detail", ""), 120)
        evidence = _clip_llm_text(sr.get("evidence", ""), 120)
        facts_text.append(f"[性关系] 与{partner}({is_ml}): {detail} | 证据: {evidence}")
    
    for ci in facts.get("children_info", []):
        # 双保险：跳过明显“身世句”
        try:
            blob = (
                str(ci.get("evidence", "") or "")
                + "\n"
                + str(ci.get("detail", "") or "")
                + "\n"
                + str(ci.get("origin", "") or "")
                + "\n"
                + str(ci.get("child_name", "") or "")
            )
            if _looks_like_parentage_as_child(name, blob):
                continue
            if _looks_like_non_child_kinship_fact(name, blob, str(ci.get("child_name", "") or "")):
                continue
        except Exception:
            pass
        father = ci.get("father", "未知")
        origin = ci.get("origin", "未知")
        evidence = _clip_llm_text(ci.get("evidence", ""), 120)
        facts_text.append(f"[孩子] 父亲:{father}, 来源:{origin} | 证据: {evidence}")
    
    for pc in facts.get("physical_contacts", []):
        partner = pc.get("partner", "未知")
        is_ml = "男主" if pc.get("is_male_lead") else "非男主"
        ctype = pc.get("contact_type", "")
        evidence = _clip_llm_text(pc.get("evidence", ""), 120)
        facts_text.append(f"[肉体接触] 与{partner}({is_ml}){ctype} | 证据: {evidence}")
    
    for rf in facts.get("romantic_feelings", []):
        target = rf.get("target", "未知")
        is_ml = "男主" if rf.get("is_male_lead") else "非男主"
        evidence = _clip_llm_text(rf.get("evidence", ""), 120)
        facts_text.append(f"[感情] 对{target}({is_ml})有感情 | 证据: {evidence}")
    
    for pr in facts.get("partner_relations", []):
        partner = pr.get("partner", "未知")
        is_ml = "男主" if pr.get("is_male_lead") else "非男主"
        rel = pr.get("relationship", "")
        status = pr.get("status", "")
        forced_flag = pr.get("forced", None)
        forced = "被迫" if forced_flag is True else ("自愿" if forced_flag is False else "未知")
        evidence = _clip_llm_text(pr.get("evidence", ""), 120)
        facts_text.append(f"[伴侣] 与{partner}({is_ml})的{rel}关系, 状态:{status}, {forced} | 证据: {evidence}")

    _append_extended_relationship_facts_text(facts_text, facts)
    
    if not facts_text:
        facts_text.append("（无任何事实记录）")
    
    facts_str = _compact_llm_lines(facts_text, label="结构化事实")
    
    system_prompt = f"""你是一个严格的二次校验专家。第一轮校验发现程序与LLM判定不一致，现在请你再次仔细判断。

【男主】：{male_lead}

【重要定义（本项目为“独占/排他”口径，不按现实语义）】：
- “处女”=【排他性处女】：仅与男主发生性关系也必须判 ✅处女（仅男主）；严禁判成 ❌非处！
- “精神洁”=【排他性精神洁】：出现非男主性关系/亲生孩子/正式伴侣/明确爱过 → 直接判 ❌精神非初。

【五维判定规则（核心四项 + 前世/接触等级补充）】：
1. 是否处女：仅与男主发生性关系/或无性关系 = ✅处女；任何与非男主的性关系/亲生孩子(父亲非男主或未知且为正常生育) = ❌非处
2. 非男主接触：被非男主实际触碰 = ❌有接触；若存在与非男主性关系或亲生孩子，也视为 ❌有接触（即使 physical_contacts 没抽到）
3. 有无男伴：无非男主正式伴侣 = ✅无男伴；有非男主男友/丈夫 = ❌有男伴（被迫订婚未完婚豁免）
4. 精神洁：无非男主恋爱/伴侣/性关系/亲生孩子 = ✅精神洁；存在任一则 ❌精神非初

【关键判定原则】：
- is_male_lead=true 的事实 → 与男主相关 → 不影响洁度 → 该维度应为✅
- is_male_lead=false 的事实 → 与非男主相关 → 可能影响洁度
- 无事实记录 → 默认✅洁
- 仔细核对每个事实的 is_male_lead 字段！
- 经济依附、权力关系、政治联姻、受害/胁迫记录是辅助事实；不能把“未遂/计划中/未圆房/被迫且无感情”的记录直接当成已发生性关系。
- 只允许基于事实判断，禁止“推定/脑补”。

【重要防呆（必须严格执行）】：
- 若 children_info / 证据描述的是“{name}是谁的女儿/之女/某人生下了{name}”，这是【身世/出身】信息，
  不代表 {name} “生过孩子”，必须忽略这类条目，严禁据此判 ❌非处 / ❌精神非初 / ❌有接触。

输出 JSON：
{{
  "final_is_virgin": true/false,
  "final_virgin_status": "最终结论",
  "final_has_other_contact": true/false,
  "final_contact_status": "最终结论",
  "final_no_partner": true/false,
  "final_partner_status": "最终结论",
  "final_is_spirit_clean": true/false,
  "final_spirit_status": "最终结论",
  "final_reason": "最终判定理由，说明为何做出此判定"
}}"""

    user_prompt = f"""请进行二次校验：

【角色】：{name}

【结构化事实】：
{facts_str}

【程序判定】：
- 是否处女: {program_result.get('virgin_status', '未知')}
- 非男主接触: {program_result.get('contact_status', '未知')}
- 有无男伴: {program_result.get('partner_status', '未知')}
- 精神洁: {program_result.get('spirit_status', '未知')}

【第一轮LLM校验结果】：
- 是否同意程序: {first_llm_result.get('agree', '未知')}
- 不同意理由: {first_llm_result.get('reason', '无')}
- LLM判定是否处女: {first_llm_result.get('virgin_status', '未知')}
- LLM判定非男主接触: {first_llm_result.get('contact_status', '未知')}
- LLM判定有无男伴: {first_llm_result.get('partner_status', '未知')}
- LLM判定精神洁: {first_llm_result.get('spirit_status', '未知')}

请仔细核对事实数据，做出最终判定。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        return data
    except Exception as e:
        logger.warning(f"LLM 二次校验失败: {e}")
        return {"final_reason": f"二次校验失败: {e}"}


def judge_with_verification(name: str, facts: Dict[str, Any], male_lead: str) -> Dict[str, Any]:
    """
    完整的判定流程：程序判定 → LLM校验 → 若不一致再次校验 → 输出最终结果
    """
    # 统一清洗：避免“身世句”影响程序/LLM
    facts = _sanitize_purity_facts_for_heroine(name, facts)

    # 第一步：程序规则判定
    program_result = judge_purity_by_facts(name, facts, male_lead)
    
    # 第二步：LLM 校验
    llm_verify = verify_purity_by_llm(name, facts, program_result, male_lead)
    
    if llm_verify.get("agree", True):
        # LLM 同意程序判定，直接返回
        program_result["verification"] = {
            "method": "program_with_llm_agree",
            "llm_agreed": True,
            "rounds": 1,
        }
        return _normalize_purity_result_consistency(program_result)
    
    # LLM 不同意，记录第一轮分歧
    first_disagreement = llm_verify.get("reason", "")
    
    # 第三步：二次校验
    second_verify = verify_purity_second_round(name, facts, program_result, llm_verify, male_lead)
    
    # 检查二次校验结果是否与程序一致
    second_agrees = (
        second_verify.get("final_is_virgin", program_result["is_virgin"]) == program_result["is_virgin"] and
        second_verify.get("final_has_other_contact", program_result["has_other_contact"]) == program_result["has_other_contact"] and
        second_verify.get("final_no_partner", program_result["no_partner"]) == program_result["no_partner"] and
        second_verify.get("final_is_spirit_clean", program_result["is_spirit_clean"]) == program_result["is_spirit_clean"]
    )
    
    if second_agrees:
        # 二次校验后一致，采用程序结果
        program_result["verification"] = {
            "method": "program_confirmed_after_second_round",
            "llm_agreed": True,
            "rounds": 2,
            "first_disagreement": first_disagreement,
            # 解释：第一轮可能“不同意”，第二轮复核后又“同意”，这里记录二轮理由避免观感矛盾
            "second_reason": second_verify.get("final_reason", ""),
        }
        return _normalize_purity_result_consistency(program_result)
    
    # 仍然不一致：采用 LLM 的二次校验结果作为最终裁决（同时保留程序结果供回溯）
    final_result = {
        "name": name,
        # 最终采用 LLM 的二次校验结果（但同时保留程序结果供参考）
        "is_virgin": second_verify.get("final_is_virgin", program_result["is_virgin"]),
        "virgin_status": second_verify.get("final_virgin_status", program_result["virgin_status"]),
        "has_other_contact": second_verify.get("final_has_other_contact", program_result["has_other_contact"]),
        "contact_status": second_verify.get("final_contact_status", program_result["contact_status"]),
        "no_partner": second_verify.get("final_no_partner", program_result["no_partner"]),
        "partner_status": second_verify.get("final_partner_status", program_result["partner_status"]),
        "is_spirit_clean": second_verify.get("final_is_spirit_clean", program_result["is_spirit_clean"]),
        "spirit_status": second_verify.get("final_spirit_status", program_result["spirit_status"]),
        "summary": second_verify.get("final_reason", program_result.get("summary", "")),
        "is_clean": (
            second_verify.get("final_is_virgin", True) and
            not second_verify.get("final_has_other_contact", False) and
            second_verify.get("final_no_partner", True) and
            second_verify.get("final_is_spirit_clean", True)
        ),
        "body_status": f"处女:{second_verify.get('final_virgin_status', '?')} | 接触:{second_verify.get('final_contact_status', '?')} | 男伴:{second_verify.get('final_partner_status', '?')}",
        "verification": {
            "method": "llm_override_after_disagreement",
            "llm_agreed": False,
            "rounds": 2,
            "first_disagreement": first_disagreement,
            "second_reason": second_verify.get("final_reason", ""),
            "program_result": {
                "is_virgin": program_result["is_virgin"],
                "virgin_status": program_result["virgin_status"],
                "has_other_contact": program_result["has_other_contact"],
                "contact_status": program_result["contact_status"],
                "no_partner": program_result["no_partner"],
                "partner_status": program_result["partner_status"],
                "is_spirit_clean": program_result["is_spirit_clean"],
                "spirit_status": program_result["spirit_status"],
            },
            "llm_first_result": {
                "is_virgin": llm_verify.get("is_virgin"),
                "virgin_status": llm_verify.get("virgin_status"),
                "has_other_contact": llm_verify.get("has_other_contact"),
                "contact_status": llm_verify.get("contact_status"),
                "no_partner": llm_verify.get("no_partner"),
                "partner_status": llm_verify.get("partner_status"),
                "is_spirit_clean": llm_verify.get("is_spirit_clean"),
                "spirit_status": llm_verify.get("spirit_status"),
            },
        },
    }
    return _normalize_purity_result_consistency(final_result)


# ================= 新版：分步骤LLM五维纯洁度判断 =================
# 五个步骤顺序：
# Step 0: 孩子来源判断（最复杂，单独判断）
# Step 1: 是否有男伴（最简单）
# Step 2: 是否有非男主肉体接触
# Step 3: 精神纯洁度
# Step 4: 是否处女（综合判断）


# ========== 孩子记录合并（由大模型判断） ==========

def _llm_merge_children_records(
    heroine_name: str,
    children_info: List[Dict[str, Any]],
    male_lead: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    使用大模型判断哪些孩子记录应该合并为同一个孩子
    
    核心任务：
    1. 分析所有孩子记录，判断哪些记录描述的是同一个孩子
    2. 考虑：名称变体、时间线（怀孕→出生→成长）、双胞胎/多胞胎等情况
    3. 返回合并后的分组
    
    返回: { "孩子名/组名": [记录列表], ... }
    """
    if not children_info:
        return {}
    
    # 如果只有1-2条记录，直接简单分组
    if len(children_info) <= 2:
        result: Dict[str, List[Dict[str, Any]]] = {}
        for child in children_info:
            child_name = str(child.get("child_name", "") or "").strip() or "未命名孩子"
            if child_name not in result:
                result[child_name] = []
            result[child_name].append(child)
        return result
    
    # 构建记录摘要供 LLM 分析
    records_text = []
    for i, child in enumerate(children_info):
        child_name = child.get("child_name", "未知")
        father = child.get("father", "未知")
        origin = child.get("origin", "未知")
        evidence = str(child.get("evidence", "") or "")
        detail = str(child.get("detail", "") or "")
        records_text.append(
            f"记录{i+1}: child_name=\"{child_name}\", father=\"{father}\", origin=\"{origin}\"\n"
            f"  evidence: {evidence if evidence else '无'}\n"
            f"  detail: {detail if detail else '无'}"
        )
    records_str = _compact_llm_lines(records_text, label="孩子记录")
    
    system_prompt = f"""你是小说角色分析专家。你的任务是分析女性角色【{heroine_name}】的孩子记录，判断哪些记录描述的是同一个孩子。

【男主】：{male_lead}

【核心任务】：
将下方的孩子记录分组，每组代表一个真实的孩子（或一组双胞胎/多胞胎）。

【合并判断规则】：
1. 名称变体：以下情况应该合并为同一个孩子：
   - "未知"/"未命名"/"孩子"/"女儿" + 有具体名字的记录 → 可能是同一个孩子的不同称呼
   - "未命名胎儿"/"未出生孩子" + 后来有名字的孩子 → 怀孕时期 vs 出生后
   - 相似的名字变体（如"宁清歌"和"清歌"）

2. 双胞胎/多胞胎处理：
   - "双胞胎"/"双胞胎女儿" + "宁清歌、宁清舞" → 应该合并为一组
   - "双胞胎"的多条记录应该合并
   - 注意：双胞胎作为一组处理，不要拆分

3. 时间线一致性：
   - 怀孕记录 + 出生记录 + 成长记录 → 如果描述的是同一个孩子，应该合并
   - 检查 father/origin 是否一致

4. 不应该合并的情况：
   - 明确不同名字的孩子（除非有证据表明是同一人）
   - 父亲明确不同的孩子
   - 来源明确不同的孩子（如一个是亲生，一个是收养）

【输出格式】：
输出 JSON，每个分组用一个代表性名称：
{{
  "groups": [
    {{
      "group_name": "代表性名称（如有名字用名字，双胞胎用'双胞胎:名1、名2'，无名用'未命名孩子'）",
      "record_indices": [1, 3, 5],  // 属于这个孩子的记录编号（从1开始）
      "reason": "合并理由"
    }},
    ...
  ],
  "total_children_count": 2,  // 实际有几个不同的孩子（双胞胎算1组）
  "merge_summary": "合并摘要说明"
}}"""

    user_prompt = f"""请分析【{heroine_name}】的孩子记录，判断哪些记录描述的是同一个孩子：

【共 {len(children_info)} 条孩子记录】：
{records_str}

请分析并输出分组 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )

        # 根据 LLM 返回的分组构建结果
        result: Dict[str, List[Dict[str, Any]]] = {}
        assigned_indices = set()
        
        for group in data.get("groups", []):
            group_name = group.get("group_name", "未命名孩子")
            record_indices = group.get("record_indices", [])
            
            if not record_indices:
                continue
            
            # 收集该组的记录
            group_records = []
            for idx in record_indices:
                # 记录编号从1开始，转换为0开始的索引
                real_idx = idx - 1
                if 0 <= real_idx < len(children_info) and real_idx not in assigned_indices:
                    group_records.append(children_info[real_idx])
                    assigned_indices.add(real_idx)
            
            if group_records:
                if group_name in result:
                    result[group_name].extend(group_records)
                else:
                    result[group_name] = group_records
        
        # 处理未被分配的记录
        for i, child in enumerate(children_info):
            if i not in assigned_indices:
                child_name = str(child.get("child_name", "") or "").strip() or "未分配记录"
                if child_name not in result:
                    result[child_name] = []
                result[child_name].append(child)
        
        logger.info(f"    [孩子合并] LLM分组结果: {list(result.keys())}")
        return result
        
    except Exception as e:
        logger.warning(f"孩子合并LLM判断异常: {e}，使用简单分组")
        return _simple_merge_children_records(children_info)


def _simple_merge_children_records(children_info: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    简单的孩子记录合并（备选方案）：
    - 按孩子名称分组
    - 名称不明确的合并到其他明确命名的孩子
    """
    if not children_info:
        return {}
    
    UNCLEAR_NAMES = {
        "孩子", "未知", "不明", "无名", "某孩子", "孩子们", "子女", "儿女",
        "女儿", "儿子", "小孩", "婴儿", "宝宝", "娃娃", "幼儿", "孩童",
        "", "null", "none", "unknown", "unnamed", "未命名", "未命名胎儿",
        "未出生孩子", "未命名双胞胎", "双胞胎",
    }
    
    def is_unclear(name: str) -> bool:
        if not name:
            return True
        name_lower = name.strip().lower()
        if name_lower in UNCLEAR_NAMES:
            return True
        if len(name.strip()) <= 1:
            return True
        return False
    
    named_children: Dict[str, List[Dict[str, Any]]] = {}
    unnamed_records: List[Dict[str, Any]] = []
    
    for child in children_info:
        child_name = str(child.get("child_name", "") or "").strip()
        if is_unclear(child_name):
            unnamed_records.append(child)
        else:
            if child_name not in named_children:
                named_children[child_name] = []
            named_children[child_name].append(child)
    
    # 如果有明确命名的孩子，将不明确的记录合并到第一个明确命名的孩子
    if named_children and unnamed_records:
        first_named = list(named_children.keys())[0]
        named_children[first_named].extend(unnamed_records)
    elif unnamed_records:
        named_children["未命名孩子"] = unnamed_records
    
    return named_children


# ========== 伴侣关系 forced 推断与聚合（由大模型逐条判断 + 聚合） ==========

def _normalize_partner_relation(pr: Dict[str, Any]) -> Dict[str, Any]:
    """兼容不同字段名，把 relationship/status/evidence/detail/forced 统一起来。"""
    out = dict(pr or {})
    if not out.get("relationship"):
        out["relationship"] = out.get("relation") or out.get("relation_type") or "未知"
    if "status" not in out:
        out["status"] = out.get("marital_status") or out.get("state") or "未知"
    if "evidence" not in out:
        out["evidence"] = out.get("proof") or ""
    if "detail" not in out:
        out["detail"] = ""
    return out


def _normalize_partner_key(partner: str) -> str:
    """
    归一化 partner 用于聚合：
    - 去除空白
    - 去除常见助词/标点（如"的"）
    目的：把"墨水心的父亲"与"墨水心父亲"等视为同一对象，避免 forced 证据分散导致误判。
    """
    s = str(partner or "").strip()
    if not s:
        return "未知"
    # 去空白
    s = "".join(s.split())
    # 去常见助词/连接符/标点
    for ch in ["的", "·", "・", "-", "—", "_", "（", "）", "(", ")", "[", "]", "【", "】", "：", ":", "，", ",", "。", "."]:
        s = s.replace(ch, "")
    s = s.strip()
    return s or "未知"


def _llm_group_partner_relations(
    heroine_name: str,
    analyzed_records: List[Dict[str, Any]],
    male_lead: str,
) -> Optional[Dict[str, Any]]:
    """
    使用大模型对伴侣关系记录进行“同一对象”分组（归一化/聚合前置）。

    输入：analyzed_records（已逐条判定 is_forced/has_feelings 的记录列表）
    输出（LLM JSON 解析后的 dict）示例：
    {
      "groups": [
        {"group_name": "...", "record_indices": [1,2,5], "reason": "..."},
        ...
      ],
      "merge_summary": "..."
    }

    返回 None 表示分组失败（调用失败或 JSON 解析失败）。
    """
    if not analyzed_records:
        return {"groups": [], "merge_summary": "无伴侣记录"}
    if len(analyzed_records) == 1:
        partner = analyzed_records[0].get("_partner_raw") or analyzed_records[0].get("partner") or "未知"
        return {"groups": [{"group_name": str(partner), "record_indices": [1], "reason": "仅一条记录"}], "merge_summary": "仅一条记录无需分组"}

    # 构建输入摘要（尽量短，避免 token 过大）
    lines: List[str] = []
    for i, rec in enumerate(analyzed_records):
        pr = rec.get("original_record", {}) if isinstance(rec, dict) else {}
        partner_raw = str(rec.get("_partner_raw", "") or rec.get("partner", "") or pr.get("partner", "") or "未知").strip() or "未知"
        relationship = str(pr.get("relationship", "") or rec.get("relationship", "") or "未知")
        status = str(pr.get("status", "") or "未知")
        evidence = _clip_llm_text(pr.get("evidence", ""), 180)
        detail = _clip_llm_text(pr.get("detail", ""), 180)
        is_forced = rec.get("is_forced", None)
        has_feelings = rec.get("has_feelings", None)
        # 为分组提供“语义锚点”，但不要求模型相信这些结论（分组只看同一对象）
        hint = f"forced={is_forced}, feelings={has_feelings}"
        lines.append(
            f"记录{i+1}: partner=\"{partner_raw}\", relationship=\"{relationship}\", status=\"{status}\", {hint}\n"
            f"  evidence: {evidence if evidence else '无'}\n"
            f"  detail: {detail if detail else '无'}"
        )
    records_str = _compact_llm_lines(lines, label="伴侣分组记录")

    system_prompt = f"""你是严格的小说信息归一化专家。你的任务是把女性角色【{heroine_name}】的伴侣关系记录按“是否指向同一个真实对象”进行分组（归一化/聚合）。

【男主】：{male_lead}

【分组目标】：
- 每一组代表同一个“人/对象”（如同一位丈夫/同一位施暴者/同一位前男友）。
- 你只负责“同一对象分组”，不需要判断处女/精神洁等结论。

【必须遵守】：
1) 高精度合并：只有在高度确信是同一对象时才合并；不确定就分开（宁可不合并）。
2) 名称变体应合并：如“墨水心的父亲”≈“墨水心父亲”，仅差助词/空格/标点，应视为同一对象。
3) 指代/身份应谨慎合并：如“某个男人/他/丈夫/姐夫/施暴者”，只有当证据文本明显指向同一人时才合并。
4) 男主不得与非男主混合：凡记录明显指向男主【{male_lead}】（名称或证据出现男主名），必须单独成组，不能与其他对象合并。
5) 不要遗漏：每条记录必须且只能属于一个组。

【输出 JSON】：
{{
  "groups": [
    {{
      "group_name": "组名（尽量用该组里最具体的称呼；没有就用身份如“未知男性/丈夫/施暴者”）",
      "record_indices": [1, 3, 5],
      "reason": "为什么这些记录是同一对象（简短）"
    }}
  ],
  "merge_summary": "一句话总结分组情况"
}}"""

    user_prompt = f"""请对【{heroine_name}】的伴侣记录做同一对象分组（共{len(analyzed_records)}条）：

{records_str}

请输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        if not isinstance(data, dict) or "groups" not in data:
            logger.warning("伴侣关系归一化分组返回格式异常，缺少 groups")
            return None
        return data
    except Exception as e:
        logger.warning(f"伴侣关系归一化分组异常: {e}")
        return None


def _llm_judge_single_partner_forced(
    heroine_name: str,
    record_index: int,
    pr: Dict[str, Any],
    male_lead: str,
) -> Dict[str, Any]:
    """
    使用大模型判断单条伴侣关系记录是否被迫
    
    返回: {
        "partner": str,
        "relationship": str,
        "is_forced": bool,  # 是否被迫
        "has_feelings": bool,  # 是否有感情投入
        "reason": str,
        "original_record": dict,
    }
    """
    partner = pr.get("partner", "未知")
    relationship = pr.get("relationship", "未知")
    status = pr.get("status", "未知")
    evidence = _clip_llm_text(pr.get("evidence", ""), 600)
    detail = _clip_llm_text(pr.get("detail", ""), 400)
    forced_tag = pr.get("forced", None)
    is_ml = pr.get("is_male_lead", False)
    heuristic_is_fact = not _is_nonfact_relation_or_feeling_record({
        "speech_act": pr.get("speech_act", ""),
        "evidence_strength": pr.get("evidence_strength", ""),
        "relationship": relationship,
        "status": status,
        "detail": detail,
        "evidence": evidence,
    })
    
    # 如果是男主，直接跳过 LLM 判断
    if is_ml:
        return {
            "partner": partner,
            "relationship": relationship,
            "is_forced": False,
            "has_feelings": True,  # 对男主有感情是正常的
            "is_male_lead": True,
            "speech_act": "asserted_fact",
            "is_fact_statement": True,
            "reason": "男主关系，不影响洁度判定",
            "original_record": pr,
        }
    
    system_prompt = f"""你是严格的小说角色分析专家。判断女性角色【{heroine_name}】与某人的伴侣关系是否【被迫】以及是否【有感情投入】。

【男主】：{male_lead}

【核心任务】：
分析这条伴侣关系记录，判断：
1. 这条记录是否是“已发生事实”（is_fact_statement）
2. 这段关系是否是被迫的（forced）
3. {heroine_name}是否对这个人有感情投入（has_feelings）

【事实性识别（最高优先级）】：
- 事实（is_fact_statement=true）：
  · 叙述已发生的关系事实（曾经/已经/当时/确实发生）
  · 对话中对当前关系状态的描述（如"你有丈夫""你的丈夫""他是我丈夫"）
  · 第一人称对自身关系的陈述（如"有丈夫跟没丈夫差不多""我嫁给了他"）
  · 第三方对关系的观察/描述（如"丈夫还健在""她的丈夫是..."）
  · 关键：只要证据中明确提到了"丈夫/妻子/男友/恋人"等关系词，
    且不是在假设/想象场景中使用，就应判定为事实
- 非事实（is_fact_statement=false）：
  · 仅限纯假设/想象/劝说场景（"如果你嫁给别人""你想想以后..."）
  · 关键：对话中提到实际存在的关系（如"你丈夫""你的男友"）不算假设
- 若 is_fact_statement=false：
  · 必须输出 has_feelings=false
  · is_forced 优先按 false（除非文本明确是“已发生的强迫行为”事实）

【被迫（is_forced=true）的情况】：
- 强奸/强暴/性侵/强迫发生关系
- 逼迫/胁迫/威胁建立的关系
- 被迫订婚/包办婚姻/政治联姻（非自愿）
- 被下药/迷奸
- 任何非自愿的关系

【非被迫（is_forced=false）的情况】：
- 自由恋爱/自愿交往
- 双方自愿的婚姻
- 主动追求/主动接受

【有感情投入（has_feelings=true）的情况】：
- 明确说爱过/喜欢过/动心过
- 有舍不得/放不下/心动等描述
- 自愿付出感情

【无感情投入（has_feelings=false）的情况】：
- 被迫且明确厌恶/抗拒
- 有名无实/假结婚/未圆房且无感情描述
- 政治联姻但完全无感情
- 仅仅是名义关系

【创伤/厌恶信号（强提示无感情）】：
- 若证据出现“讨厌到想吐/恶心/厌恶/仇恨/恨/畜生/被伤害/家暴/施暴”等，
  且没有“爱上/喜欢/动心/真心/舍不得/放不下”等正向感情证据，
  应优先判断 has_feelings=false。
- “已完婚/丈夫/前夫”本身不等于有感情投入，必须看是否存在“真心/动心”证据。

【重要】：
★ 如果证据中出现"强奸/强暴/胁迫/被迫/未遂"等词，即使字段标注forced=false，也应判断is_forced=true
★ 如果关系是被迫的且没有感情描述，应判断has_feelings=false
★ 若 evidence/detail 与 forced 字段冲突，优先采信 evidence/detail 原文语义
★ 证据不足时，默认is_forced=false, has_feelings判断要谨慎（需要有明确的感情描述才判true）

输出 JSON：
{{
  "speech_act": "asserted_fact/hypothesis/advice_persuasion/comparison/rumor/question/unknown",
  "is_fact_statement": true/false,  // 该记录是否已发生事实
  "is_forced": true/false,  // 这段关系是否被迫
  "has_feelings": true/false,  // 是否对这个人有感情投入
  "reason": "判断理由，引用关键证据"
}}"""

    user_prompt = f"""请判断【{heroine_name}】与【{partner}】的关系：

【记录信息】：
- 对象: {partner}
- 关系类型: {relationship}
- 状态: {status}
- forced字段标注: {forced_tag}
- evidence: {evidence if evidence else '无'}
- detail: {detail if detail else '无'}

请判断这段关系是否被迫、是否有感情投入，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )

        return {
            "partner": partner,
            "relationship": relationship,
            "is_forced": data.get("is_forced", False),
            "has_feelings": data.get("has_feelings", None),
            "is_male_lead": False,
            "speech_act": str(data.get("speech_act", "") or "unknown"),
            "is_fact_statement": _to_bool(data.get("is_fact_statement", heuristic_is_fact), heuristic_is_fact),
            "reason": data.get("reason", ""),
            "original_record": pr,
        }
    except Exception as e:
        logger.warning(f"伴侣关系forced判断异常[{partner}]: {e}")
        return {
            "partner": partner,
            "relationship": relationship,
            "is_forced": False,
            "has_feelings": None,
            "is_male_lead": False,
            "speech_act": "unknown",
            "is_fact_statement": heuristic_is_fact,
            "reason": f"LLM调用失败: {e}",
            "original_record": pr,
        }


def _llm_analyze_and_merge_partner_relations(
    heroine_name: str,
    partner_relations: List[Dict[str, Any]],
    male_lead: str,
) -> List[Dict[str, Any]]:
    """
    两阶段处理伴侣关系：
    1. 第一阶段：逐条让大模型判断每条记录是否被迫、是否有感情
    2. 第二阶段：调用大模型进行“同一对象分组”（归一化），再聚合 forced/has_feelings 判定
    
    forced 合并规则：True > False > None（只要任何一条被判定为被迫，整体就是被迫）
    has_feelings 合并规则：True > None > False（只要有一条有感情，整体就有感情）
    
    返回: 聚合后的伴侣关系列表
    """
    if not partner_relations:
        return []
    
    logger.info(f"    [伴侣关系] 开始分析，共 {len(partner_relations)} 条记录")
    
    # ===== 第一阶段：逐条判断 =====
    analyzed_records: List[Dict[str, Any]] = []
    for i, pr in enumerate(partner_relations):
        pr_normalized = _normalize_partner_relation(pr if isinstance(pr, dict) else {})
        partner_raw = str(pr_normalized.get("partner", "") or "未知").strip() or "未知"
        
        logger.info(f"    [伴侣关系] 判断第{i+1}条: {partner_raw}")
        result = _llm_judge_single_partner_forced(heroine_name, i, pr_normalized, male_lead)
        result["_partner_raw"] = partner_raw
        result["_partner_key"] = _normalize_partner_key(partner_raw)
        analyzed_records.append(result)
    
    # ===== 第二阶段：由 LLM 做“同一对象分组”（归一化聚合）=====
    groups: Dict[str, List[Dict[str, Any]]] = {}
    group_meta: Dict[str, str] = {}  # group_key -> reason
    llm_grouping = _llm_group_partner_relations(heroine_name, analyzed_records, male_lead)
    if llm_grouping and isinstance(llm_grouping.get("groups"), list):
        assigned: set = set()
        used_names: Dict[str, int] = {}

        for g in llm_grouping.get("groups", []):
            try:
                group_name = str(g.get("group_name", "") or "").strip() or "未命名对象"
                idxs = g.get("record_indices", []) or []
                reason = str(g.get("reason", "") or "")
            except Exception:
                continue
            # 收集该组记录
            items: List[Dict[str, Any]] = []
            for idx in idxs:
                try:
                    real_idx = int(idx) - 1
                except Exception:
                    continue
                if real_idx < 0 or real_idx >= len(analyzed_records):
                    continue
                if real_idx in assigned:
                    continue
                items.append(analyzed_records[real_idx])
                assigned.add(real_idx)

            if not items:
                continue

            # 防止同名 key 覆盖：加后缀
            key_base = group_name
            n = used_names.get(key_base, 0)
            used_names[key_base] = n + 1
            group_key = key_base if n == 0 else f"{key_base}#{n+1}"

            groups[group_key] = items
            group_meta[group_key] = reason

        # 兜底：未被 LLM 分到任何组的记录，各自成组
        for i, rec in enumerate(analyzed_records):
            if i in assigned:
                continue
            fallback_name = str(rec.get("_partner_raw", "") or rec.get("partner", "") or f"未分组记录{i+1}").strip() or f"未分组记录{i+1}"
            key_base = fallback_name
            n = used_names.get(key_base, 0)
            used_names[key_base] = n + 1
            group_key = key_base if n == 0 else f"{key_base}#{n+1}"
            groups[group_key] = [rec]
            group_meta[group_key] = "LLM未分配，单独成组"

        logger.info(f"    [伴侣关系] LLM分组完成，共 {len(groups)} 组")
    else:
        # 回退：按简单归一化 key 聚合（不走 LLM 分组）
        logger.info("    [伴侣关系] LLM分组失败，回退到规则归一化聚合")
        for record in analyzed_records:
            partner_key = record.get("_partner_key", "未知")
            groups.setdefault(partner_key, []).append(record)
    
    merged: List[Dict[str, Any]] = []
    for partner_key, items in groups.items():
        # 展示名：优先选择更具体、更长的原始 partner 名
        raw_names = [str(i.get("_partner_raw", "") or "").strip() for i in items]
        raw_names = [n for n in raw_names if n and n != "未知"]
        partner_display = max(raw_names, key=len) if raw_names else str(partner_key or "未知")
        
        # 基础字段
        relationship = next((i.get("relationship") for i in items if i.get("relationship") and i.get("relationship") != "未知"), "未知")
        is_ml = any(bool(i.get("is_male_lead", False)) for i in items)
        
        # 获取原始记录信息
        original_records = [i.get("original_record", {}) for i in items]
        status = next((r.get("status") for r in original_records if r.get("status") and r.get("status") != "未知"), "未知")
        
        # forced 合并：True > False > None
        forced_values = [i.get("is_forced") for i in items]
        if any(f is True for f in forced_values):
            forced_final = True
        elif any(f is False for f in forced_values):
            forced_final = False
        else:
            forced_final = None
        
        # has_feelings 合并：True > None > False
        feelings_values = [i.get("has_feelings") for i in items]
        if any(f is True for f in feelings_values):
            has_feelings_final = True
        elif all(f is False for f in feelings_values if f is not None):
            has_feelings_final = False
        else:
            has_feelings_final = None

        # 事实性合并：True > None > False（只要有一条明确事实，就视为可作为事实来源）
        fact_values = [i.get("is_fact_statement") for i in items]
        if any(f is True for f in fact_values):
            is_fact_statement_final = True
        elif all(f is False for f in fact_values if f is not None) and any(f is not None for f in fact_values):
            is_fact_statement_final = False
        else:
            is_fact_statement_final = None
        speech_acts = []
        for i in items:
            sa = str(i.get("speech_act", "") or "").strip()
            if sa and sa not in speech_acts:
                speech_acts.append(sa)
        speech_act_final = speech_acts[0] if speech_acts else "unknown"
        
        # 理由合并
        reasons = [i.get("reason", "") for i in items if i.get("reason")]
        reason_merged = " | ".join(_clip_llm_text(item, 120) for item in reasons[:20])
        
        # 证据合并
        evidences = []
        for r in original_records:
            for key in ("evidence", "detail"):
                s = str(r.get(key, "") or "").strip()
                if s:
                    evidences.append(_clip_llm_text(s, 180))
        seen = set()
        dedup = []
        for s in evidences:
            if s not in seen:
                seen.add(s)
                dedup.append(s)
        evidence_merged = _compact_llm_lines(dedup, max_chars=1600, max_items=12, label="伴侣证据")
        
        merged.append({
            "partner": partner_display,
            "is_male_lead": is_ml,
            "relationship": relationship,
            "status": status,
            "forced": forced_final,
            "has_feelings": has_feelings_final,
            "is_fact_statement": is_fact_statement_final,
            "speech_act": speech_act_final,
            "evidence": evidence_merged,
            "analysis_reason": reason_merged,
            "group_reason": group_meta.get(partner_key, ""),
            "raw_items": items,
        })
        
        logger.info(f"    [伴侣关系] 聚合 {partner_display}: forced={forced_final}, has_feelings={has_feelings_final}")
    
    logger.info(f"    [伴侣关系] 聚合后共 {len(merged)} 个伴侣")
    return merged


def _simple_merge_partner_relations(partner_relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    简单的伴侣关系聚合（备选方案，不调用 LLM）
    """
    COERCION_KEYWORDS = [
        "强奸", "强暴", "强迫", "逼迫", "胁迫", "威胁", "要挟", "下药", "迷奸",
        "侵犯", "施暴", "性侵", "猥亵", "强行", "未遂",
    ]
    
    def infer_forced(pr: Dict[str, Any]) -> Optional[bool]:
        forced_flag = pr.get("forced", None)
        if forced_flag is True:
            return True
        blob = " ".join([
            str(pr.get("relationship", "") or ""),
            str(pr.get("relation", "") or ""),
            str(pr.get("status", "") or ""),
            str(pr.get("detail", "") or ""),
            str(pr.get("evidence", "") or ""),
        ])
        if any(kw in blob for kw in COERCION_KEYWORDS):
            return True
        return False if forced_flag is False else None
    
    if not partner_relations:
        return []
    
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for pr in partner_relations:
        pr2 = _normalize_partner_relation(pr if isinstance(pr, dict) else {})
        partner_raw = str(pr2.get("partner", "") or "未知").strip() or "未知"
        partner_key = _normalize_partner_key(partner_raw)
        pr2["_partner_raw"] = partner_raw
        groups.setdefault(partner_key, []).append(pr2)
    
    merged: List[Dict[str, Any]] = []
    for partner_key, items in groups.items():
        raw_names = [str(i.get("_partner_raw", "") or "").strip() for i in items]
        raw_names = [n for n in raw_names if n and n != "未知"]
        partner_display = max(raw_names, key=len) if raw_names else "未知"
        
        relationship = next((i.get("relationship") for i in items if i.get("relationship") and i.get("relationship") != "未知"), "未知")
        status = next((i.get("status") for i in items if i.get("status") and i.get("status") != "未知"), "未知")
        is_ml = any(bool(i.get("is_male_lead", False)) for i in items)
        
        forced_candidates = [infer_forced(i) for i in items]
        if any(fc is True for fc in forced_candidates):
            forced_final = True
        elif any(fc is False for fc in forced_candidates):
            forced_final = False
        else:
            forced_final = None
        
        evidences = []
        for i in items:
            for key in ("evidence", "detail"):
                s = str(i.get(key, "") or "").strip()
                if s:
                    evidences.append(_clip_llm_text(s, 180))
        seen = set()
        dedup = [s for s in evidences if not (s in seen or seen.add(s))]
        evidence_merged = _compact_llm_lines(dedup, max_chars=1600, max_items=12, label="伴侣证据")
        
        merged.append({
            "partner": partner_display,
            "is_male_lead": is_ml,
            "relationship": relationship,
            "status": status,
            "forced": forced_final,
            "has_feelings": None,
            "evidence": evidence_merged,
            "raw_items": items,
        })
    return merged


def _extract_father_statistics(child_records: List[Dict[str, Any]], male_lead: str) -> Dict[str, Any]:
    """
    从所有记录中统计父亲信息，用于辅助 LLM 判断
    
    返回: {
        "known_fathers": set,  # 已知的父亲名
        "male_lead_mention_count": int,  # evidence 中提到男主的次数
        "explicit_male_lead_father_count": int,  # father 字段明确是男主的记录数
        "unknown_father_count": int,  # father 字段是"未知"的记录数
        "male_lead_evidence_samples": list,  # 提到男主的 evidence 样本
        "summary": str,  # 统计摘要
    }
    """
    known_fathers = set()
    male_lead_lower = male_lead.lower() if male_lead else ""
    male_lead_mention_count = 0
    explicit_male_lead_father_count = 0
    unknown_father_count = 0
    male_lead_evidence_samples = []
    
    for record in child_records:
        father = str(record.get("father", "") or "").strip()
        father_lower = father.lower()
        evidence = str(record.get("evidence", "") or "")
        detail = str(record.get("detail", "") or "")
        origin = str(record.get("origin", "") or "")
        
        # 统计 father 字段
        if father_lower in ["未知", "unknown", "无", "", "不明", "null", "none"]:
            unknown_father_count += 1
        else:
            known_fathers.add(father)
            # 检查是否是男主
            if male_lead_lower and (male_lead_lower in father_lower or father_lower in male_lead_lower):
                explicit_male_lead_father_count += 1
            elif father in ["男主", "主角"]:
                explicit_male_lead_father_count += 1
        
        # 从 evidence/detail/origin 中检测男主名
        all_text = f"{evidence} {detail} {origin}"
        if male_lead and male_lead in all_text:
            male_lead_mention_count += 1
            known_fathers.add(male_lead)
            # 收集样本
            if len(male_lead_evidence_samples) < 3:
                sample = _clip_llm_text(evidence if evidence else detail, 120)
                if sample:
                    male_lead_evidence_samples.append(sample)
    
    # 构建摘要
    total = len(child_records)
    summary_parts = []
    if explicit_male_lead_father_count > 0:
        summary_parts.append(f"father字段明确为男主的记录: {explicit_male_lead_father_count}/{total}")
    if male_lead_mention_count > 0:
        summary_parts.append(f"evidence中提到男主的记录: {male_lead_mention_count}/{total}")
    if unknown_father_count > 0:
        summary_parts.append(f"father字段为'未知'的记录: {unknown_father_count}/{total}")
    
    return {
        "known_fathers": known_fathers,
        "male_lead_mention_count": male_lead_mention_count,
        "explicit_male_lead_father_count": explicit_male_lead_father_count,
        "unknown_father_count": unknown_father_count,
        "male_lead_evidence_samples": male_lead_evidence_samples,
        "summary": "; ".join(summary_parts) if summary_parts else "无统计信息",
    }


def _llm_judge_single_child(
    name: str,
    child_name: str,
    child_records: List[Dict[str, Any]],
    male_lead: str,
) -> Dict[str, Any]:
    """
    第一轮：逐条判断单个孩子是否"非男性参与诞生"
    
    核心问题：该孩子的诞生是否不需要男性参与（试管/人工授精/收养/魔法等）
    
    返回: {
        "child_name": str,
        "needs_male": bool,  # 是否需要男性参与（True=正常性交生育，False=非男性参与）
        "father": str,       # 从证据推断的父亲
        "is_father_male_lead": bool,  # 父亲是否是男主
        "reason": str,
        "records": [...],  # 原始记录
    }
    """
    # 统计父亲信息
    father_stats = _extract_father_statistics(child_records, male_lead)
    
    # 构建该孩子的所有证据
    evidence_list = []
    for i, record in enumerate(child_records):
        father = record.get("father", "未知")
        origin = record.get("origin", "未知")
        detail = _clip_llm_text(record.get("detail", ""), 180)
        evidence = _clip_llm_text(record.get("evidence", ""), 180)
        is_bio_tag = record.get("is_biological", "未标注")
        evidence_list.append(
            f"记录{i+1}: 父亲={father}, 来源={origin}, is_biological标注={is_bio_tag}\n"
            f"  detail: {detail if detail else '无'}\n"
            f"  evidence: {evidence if evidence else '无'}"
        )
    evidence_str = _compact_llm_lines(evidence_list, label="孩子来源证据")
    
    # 添加父亲统计信息（关键！）
    stats_hint = f"\n\n【⚠️ 父亲信息统计 - 非常重要！】：\n{father_stats['summary']}"
    if father_stats["male_lead_mention_count"] > 0:
        stats_hint += f"\n★★★ 警告：有 {father_stats['male_lead_mention_count']} 条记录的 evidence 中提到了男主【{male_lead}】！"
        stats_hint += f"\n    这强烈暗示孩子的父亲就是男主，请务必仔细检查！"
        if father_stats["male_lead_evidence_samples"]:
            stats_hint += f"\n    样本证据: " + " | ".join(
                _clip_llm_text(item, 120) for item in father_stats["male_lead_evidence_samples"]
            )
    if father_stats["explicit_male_lead_father_count"] > 0:
        stats_hint += f"\n★★★ 已有 {father_stats['explicit_male_lead_father_count']} 条记录的 father 字段明确为男主！"
    if father_stats["known_fathers"]:
        stats_hint += f"\n    从所有记录提取到的父亲: {', '.join(father_stats['known_fathers'])}"
    
    evidence_str += stats_hint
    
    system_prompt = f"""你是严格的小说角色分析专家。你的任务是判断女性角色【{name}】的孩子【{child_name}】的诞生方式。

【男主】：{male_lead}

【核心问题】：
1. 该孩子是否确实存在（已出生/已收养），还是仅仅是怀孕提及/假设/劝说？
2. 该孩子的诞生是否【不需要男性参与】？
3. 如果需要男性参与，父亲是谁？（重点：必须从 evidence/detail 原文中推断！）

【⚠️ 最重要：孩子存在性判断】：
★★★ "仅怀孕提及"不等于"孩子存在"！以下情况 child_exists=false：
  - "我们都怀孕了" "肚子里的孩子" "怀上了" → 仅表示怀孕状态，孩子可能未出生
  - "说不定会怀孕" "可能怀了" "如果怀孕" → 假设/推测，不是事实
  - "想让她怀孕" "让她生个孩子" → 劝说/建议，不是事实
★★★ 只有明确"生下/出生/分娩/已经X岁/收养"等才算 child_exists=true
★★★ 若 child_name 或证据是“妹妹/姐姐/弟弟/哥哥/侄女/外甥女”等亲属称谓，
    且没有“{name}生下/收养该人”的明确信息，这是亲属关系，不是{name}的孩子：
    必须输出 child_exists=false 且 needs_male=false

【⚠️ 生出孩子 vs 被生出来（语义方向，必须判断）】：
★★★ 必须区分“她生了孩子”和“她是被生出来的”：
  - "X生下/产下/诞下Y"、"X给某人生了孩子" → X是母亲（可用于 child_exists=true）
  - "X是剖腹产生的"、"某人生下了X"、"X是某人的女儿" → X是孩子/身世信息，不代表X自己有孩子
★★★ 若仅出现"X是剖腹产的"但没有明确孩子对象（如孩子名/儿子/女儿/宝宝），
    必须按歧义或身世信息处理：child_exists=false, evidence_is_strong=false

【证据优先级】：
★★★ 直接引文 evidence 高于 detail/origin 摘要字段。
★★★ 如果 detail/origin 与 evidence 原文冲突，必须以 evidence 为准，并降低 confidence。

【⚠️ 父亲身份推断规则 - 最最重要！务必仔细阅读！】：
★★★ 很多记录的 father 字段是"未知"，但这只是数据抽取不完整！你必须从 evidence/detail 原文中推断真正的父亲！
★★★ 如果 evidence 中明确提到孩子是男主【{male_lead}】的，即使 father 字段写的是"未知"，也必须输出：
      father="{male_lead}" 和 is_father_male_lead=true
★★★ 常见的男主孩子表述（只要 evidence 中出现这些，父亲就是男主）：
  - "孩子是{male_lead}的" / "{male_lead}的孩子" / "{male_lead}的种"
  - "怀了{male_lead}的孩子" / "怀上{male_lead}的" / "怀了他的孩子"
  - "姐夫的孩子" / "老公的孩子" / "丈夫的孩子"（结合上下文判断是否指男主）
★★★ 多条记录综合判断：如果有任何一条记录能确定父亲是男主，则整个孩子的父亲就是男主！

【父亲名字无法确定时的处理】：
★ 如果能推断出父亲的身份/关系但不知道具体名字，应输出身份描述而非"未知"：
  - 证据提到"嫁过去/丈夫/老公" → father="丈夫"
  - 证据提到"前夫/离婚" → father="前夫"  
  - 证据提到被强迫/强奸 → father="施暴者"
★ 只有当完全无法从任何记录推断父亲身份时，才输出 father="未知男性"

【判定为"不需要男性参与"（needs_male=false）的情况】：
1. 试管婴儿/人工授精/供精/精子库/胚胎移植/代孕/取卵/体外受精
2. 收养/领养/继子女/养子女/过继/认作孩子/捡来的
3. 单性繁殖/无性繁殖/孤雌生殖/分裂
4. 魔法/法术/功法/神明赐予/天降/转生/克隆/复制
5. 他人所生但由{name}抚养
6. 任何明确说明"非性行为怀孕"的情况
7. 仅怀孕提及（child_exists=false时自动 needs_male=false）

【判定为"需要男性参与"（needs_male=true）的情况】：
1. 明确描述与某男性发生性行为后怀孕/生育
2. 有明确的父亲且是通过正常性交怀孕
3. 即使 father 字段是"未知"，但 evidence 表明是正常怀孕（此时需推断父亲）
★★★ 但必须同时满足 child_exists=true！仅怀孕提及不算！

【重要规则】：
★ "亲生女儿/母女关系/剖腹产/分娩" 只能说明{name}生过这个孩子，不能自动推出发生过性行为
★ 若证据中出现"试管/人工授精/供精/代孕"等，即使同时有"亲生/分娩"描述，也判定 needs_male=false
★ 证据不足/不明确时，按无罪推定 → needs_male=false, child_exists=false

【防呆】：
★ 如果证据说的是"某人生下了{name}"或"{name}是某人的女儿"，这是{name}的身世，不是{name}的孩子！
★ 如果证据是"{name}是剖腹产的"，且没有明确孩子对象，不得据此判定{name}已生育！

输出 JSON：
{{
  "child_exists": true/false,  // 孩子是否确实存在（已出生/收养），仅怀孕提及=false
  "evidence_is_strong": true/false,  // 证据是否充分（明确出生/收养证据=true，仅怀孕/假设=false）
  "confidence": 0.0-1.0,  // 判断置信度（0.9以上=非常确定，0.65以上=较确定，低于0.65=不确定）
  "needs_male": true/false,  // 该孩子诞生是否需要男性参与（child_exists=false时必须为false）
  "father": "父亲名（如果是男主必须写'{male_lead}'，完全未知写'未知男性'）",
  "is_father_male_lead": true/false,  // 父亲是否是男主{male_lead}【关键字段！】
  "birth_method": "诞生方式描述",
  "reason": "判断理由，必须引用关键证据"
}}"""

    user_prompt = f"""请判断【{name}】的孩子【{child_name}】的诞生方式：

【该孩子的所有相关记录（共{len(child_records)}条）】：
{evidence_str}

请仔细分析：
1. 该孩子的诞生是否需要男性参与（正常性交生育）
2. 如果需要，父亲是谁（★重点：从evidence原文推断，不要只看father字段！）
3. 父亲是否是男主【{male_lead}】（★这是关键判断！）

输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )

        # 解析新字段（向后兼容，提供默认值）
        child_exists = data.get("child_exists", True)  # 默认 True（旧版不输出此字段）
        evidence_is_strong = data.get("evidence_is_strong", True)  # 默认 True
        confidence = data.get("confidence", 0.5)  # 默认中等置信度
        needs_male = data.get("needs_male", False)
        
        # 关键约束：如果 child_exists=False，则强制 needs_male=False
        if not child_exists:
            needs_male = False
        
        return {
            "child_name": child_name,
            "child_exists": child_exists,
            "evidence_is_strong": evidence_is_strong,
            "confidence": confidence,
            "needs_male": needs_male,
            "father": data.get("father", "未知"),
            "is_father_male_lead": data.get("is_father_male_lead", False),
            "birth_method": data.get("birth_method", "未知"),
            "reason": data.get("reason", ""),
            "records": child_records,
        }
    except Exception as e:
        logger.warning(f"单个孩子来源判断异常[{child_name}]: {e}")
        return {
            "child_name": child_name,
            "child_exists": False,  # 异常时保守处理
            "evidence_is_strong": False,
            "confidence": 0.0,
            "needs_male": False,
            "father": "未知",
            "is_father_male_lead": False,
            "birth_method": "判断异常",
            "reason": f"LLM调用失败: {e}",
            "records": child_records,
        }


def _llm_verify_non_male_children(
    name: str,
    non_male_children: List[Dict[str, Any]],
    virginity_mentions: List[str],
    male_lead_intimacy: List[str],
    male_lead: str,
) -> Dict[str, Any]:
    """
    第二轮：汇总验证所有"非男性参与诞生"的孩子
    
    验证逻辑自洽性：
    - 处女暗示是否与孩子来源矛盾
    - 多个孩子的来源是否一致
    - 是否有遗漏的正常生育证据
    
    返回: {
        "verified_non_male": [...],  # 确认为非男性参与的孩子
        "reclassified_male": [...],  # 重新判定为需要男性参与的孩子
        "verification_reason": str,
    }
    """
    if not non_male_children:
        return {
            "verified_non_male": [],
            "reclassified_male": [],
            "verification_reason": "无需验证：没有非男性参与诞生的孩子记录",
        }
    
    # 构建非男性参与孩子的信息
    children_text = []
    for child in non_male_children:
        child_name = child.get("child_name", "未知")
        birth_method = child.get("birth_method", "未知")
        father = child.get("father", "未知")
        reason = _clip_llm_text(child.get("reason", ""), 180)
        # 提取原始证据
        records = child.get("records", [])
        evidences = [_clip_llm_text(r.get("evidence", ""), 160) for r in records if r.get("evidence")]
        children_text.append(
            f"孩子【{child_name}】：\n"
            f"  第一轮判定：诞生方式={birth_method}, 父亲={father}\n"
            f"  判定理由：{reason}\n"
            f"  原始证据：{' | '.join(evidences) if evidences else '无'}"
        )
    children_str = _compact_llm_lines(children_text, label="非男性参与孩子")
    
    # 构建处女暗示信息
    virgin_hints = []
    if virginity_mentions:
        virgin_hints.extend([f"[处女提及] {_clip_llm_text(v, 160)}" for v in virginity_mentions])
    if male_lead_intimacy:
        virgin_hints.extend([f"[与男主亲密] {_clip_llm_text(m, 160)}" for m in male_lead_intimacy])
    virgin_str = _compact_llm_lines(virgin_hints, label="处女相关记录", empty="（无处女相关记录）")
    
    system_prompt = f"""你是严格的小说角色分析专家。你的任务是验证女性角色【{name}】的孩子来源判定是否逻辑自洽。

【男主】：{male_lead}

【背景】：
第一轮判断已将以下孩子判定为"非男性参与诞生"（如试管/收养/魔法等）。
现在需要你验证这些判定是否合理、逻辑是否自洽。

【验证要点】：
1. 处女暗示验证：
   - 如果有与男主的初夜/破处描写，说明{name}之前是处女
   - 处女+有孩子 → 孩子应该是非男性参与诞生的，这是自洽的
   - 如果第一轮判为"需要男性参与"但有处女暗示，需要重新考虑

2. 逻辑自洽性验证：
   - 检查是否有矛盾的证据（如既说是试管又说是和某人生的）
   - 如果证据明确说"与某男性发生关系后怀孕"，应判定为需要男性参与
   - "亲生/母女/分娩"不等于"与男性发生过性行为"
   - 必须区分“生出孩子”与“被生出来”：若证据是"{name}是剖腹产生的/某人生下了{name}"，这是{name}的身世，不是{name}生育
   - 若仅有"{name}是剖腹产的"且无明确孩子对象，不能据此认定其已有亲生子女

3. 遗漏检查：
   - 是否有被忽略的"正常性交生育"证据
   - 父亲身份是否有更明确的信息

【输出要求】：
对每个孩子，确认或修正其判定。只有当证据明确指向"与非男主男性发生性行为导致怀孕"时，才重新判定为需要男性参与。

输出 JSON：
{{
  "verification_results": [
    {{
      "child_name": "孩子名",
      "final_needs_male": true/false,  // 最终判定：是否需要男性参与
      "verification_status": "confirmed/reclassified",  // confirmed=确认原判定，reclassified=重新判定
      "reason": "验证理由"
    }}
  ],
  "overall_consistent": true/false,  // 整体逻辑是否自洽
  "verification_summary": "综合验证结论"
}}"""

    user_prompt = f"""请验证【{name}】的孩子来源判定：

【第一轮判定为"非男性参与诞生"的孩子】：
{children_str}

【处女痕迹/初夜信息】（用于逻辑验证）：
{virgin_str}

请验证这些判定是否逻辑自洽，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )

        # 根据验证结果分类
        verified_non_male = []
        reclassified_male = []
        
        verification_results = data.get("verification_results", [])
        # 构建名称到原始数据的映射
        child_map = {c.get("child_name", ""): c for c in non_male_children}
        
        for vr in verification_results:
            child_name = vr.get("child_name", "")
            original_child = child_map.get(child_name)
            if not original_child:
                continue
            
            if vr.get("final_needs_male", False):
                # 重新判定为需要男性参与
                reclassified = dict(original_child)
                reclassified["needs_male"] = True
                reclassified["verification_reason"] = vr.get("reason", "")
                reclassified_male.append(reclassified)
            else:
                # 确认为非男性参与
                verified = dict(original_child)
                verified["needs_male"] = False
                verified["verification_reason"] = vr.get("reason", "")
                verified_non_male.append(verified)
        
        return {
            "verified_non_male": verified_non_male,
            "reclassified_male": reclassified_male,
            "verification_reason": data.get("verification_summary", ""),
            "overall_consistent": data.get("overall_consistent", True),
        }
    except Exception as e:
        logger.warning(f"孩子来源验证异常: {e}")
        return {
            "verified_non_male": non_male_children,
            "reclassified_male": [],
            "verification_reason": f"LLM验证失败: {e}",
        }


def _llm_judge_child_origin(
    name: str,
    children_info: List[Dict[str, Any]],
    virginity_mentions: List[str],
    male_lead_intimacy: List[str],
    male_lead: str,
    heroine_names: Optional[List[str]] = None,
    raw_data_path: Optional[str] = None,
    chunks: Optional[List[str]] = None,
    chunk_manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Step 0: 孩子来源判断（母亲归属校验 + 预清洗 + 规则优先 + LLM兜底 + 孩子合并）
    
    改造后流程：
    -1. 【新增】母亲归属校验：过滤/重归属错误挂到当前女主名下的孩子记录
    0. 预清洗：过滤身世倒装、仅怀孕提及、劝说假设等弱证据
    1. 预处理：合并孩子记录（名称不明确的合并到明确命名的）
    2. 第一轮：规则优先判断，仅必要时调用 LLM 兜底
    3. 第二轮：汇总验证所有"非男性参与"记录的逻辑自洽性
    4. 整理最终结果（收紧 has_biological_children 触发门槛）
    
    新增参数：
    - heroine_names: 全体女主名列表（用于母亲归属校验）
    - raw_data_path: raw_data.json 路径（用于获取 chunks）
    - chunks: 预加载的 chunks（优先使用，避免重复读取）
    
    返回: {
        "has_biological_children": bool,  # 是否有需要男性参与诞生的孩子（父亲非男主）
        "biological_children": [...],     # 需要男性参与的孩子列表
        "adopted_children": [...],        # 非男性参与的孩子列表
        "child_origin_reason": "...",     # 判断理由
        "pregnancy_only_records": [...],  # 仅怀孕提及/弱证据（供调试）
        "discarded_records": [...],       # 身世倒装等丢弃记录（供调试）
        "uncertain_children": [...],      # 不确定的孩子（needs_male但证据弱）
        # 新增调试字段
        "owner_unanchored_records": [...],  # 无锚定的孩子记录
        "owner_conflict_records": [...],    # 冲突的孩子记录
        "owner_moved_out_records": [...],   # 从当前女主移出的记录
        "owner_moved_in_by_mother": {...},  # 重归属成功的记录（按目标母亲聚合）
        "owner_summary": "...",             # 母亲归属校验摘要
    }
    """
    # 初始化调试字段
    owner_debug_info: Dict[str, Any] = {
        "owner_unanchored_records": [],
        "owner_conflict_records": [],
        "owner_moved_out_records": [],
        "owner_moved_in_by_mother": {},
        "owner_summary": "",
    }
    
    if not children_info:
        return {
            "has_biological_children": False,
            "biological_children": [],
            "adopted_children": [],
            "child_origin_reason": "无孩子信息记录",
            "pregnancy_only_records": [],
            "discarded_records": [],
            "uncertain_children": [],
            **owner_debug_info,
        }
    
    logger.info(f"    [孩子来源] 开始判断，共 {len(children_info)} 条记录")
    
    # ===== 阶段-1：母亲归属校验（新增） =====
    children_for_step1 = children_info  # 默认使用原始记录
    
    if heroine_names and len(heroine_names) > 1:
        # 获取 chunks
        actual_chunks = chunks
        actual_manifest = chunk_manifest
        if actual_manifest is None and raw_data_path:
            try:
                actual_manifest = _get_novel_chunk_manifest(raw_data_path)
            except Exception as e:
                logger.warning(f"    [母亲归属] 获取 chunk manifest 失败: {e}")
        if not actual_chunks and raw_data_path:
            try:
                actual_chunks = _get_novel_chunks(raw_data_path)
            except Exception as e:
                logger.warning(f"    [母亲归属] 获取 chunks 失败: {e}")
        
        if actual_chunks:
            logger.info(f"    [母亲归属] 开始校验【{name}】的 {len(children_info)} 条孩子记录...")
            owner_chk = _validate_and_reassign_children_records(
                raw_data_path or "",
                name,
                children_info,
                heroine_names,
                actual_chunks,
                full_text=(actual_manifest or {}).get("full_text"),
                chunk_entries=(actual_manifest or {}).get("chunks"),
            )
            
            children_for_step1 = owner_chk.get("owned_strong_records", [])
            owner_debug_info["owner_unanchored_records"] = owner_chk.get("unanchored_records", [])
            owner_debug_info["owner_conflict_records"] = owner_chk.get("conflict_records", [])
            owner_debug_info["owner_moved_out_records"] = owner_chk.get("moved_out_records", [])
            owner_debug_info["owner_moved_in_by_mother"] = owner_chk.get("moved_in_by_mother", {})
            owner_debug_info["owner_summary"] = owner_chk.get("summary", "")
            
            logger.info(f"    [母亲归属] 校验完成: {owner_chk.get('summary', '')}")
            
            # 【重要】如果校验后 children_for_step1 为空，直接返回无孩子
            if not children_for_step1:
                logger.info(f"    [母亲归属] 【{name}】校验后无可用孩子记录，跳过后续判断")
                return {
                    "has_biological_children": False,
                    "biological_children": [],
                    "adopted_children": [],
                    "child_origin_reason": "母亲归属校验后无可用孩子记录（不惩罚）",
                    "pregnancy_only_records": [],
                    "discarded_records": [],
                    "uncertain_children": [],
                    "first_round_results": [],
                    **owner_debug_info,
                }
        else:
            logger.debug(f"    [母亲归属] 无 chunks，跳过母亲归属校验")
    else:
        logger.debug(f"    [母亲归属] heroine_names 为空或仅有 1 人，跳过母亲归属校验")
    
    # ===== 阶段0：预清洗 children_info =====
    logger.info(f"    [孩子来源] 执行预清洗（共 {len(children_for_step1)} 条经母亲归属校验的记录）...")
    cleaned = _preclean_children_info(name, children_for_step1, male_lead)
    logger.info(f"    [孩子来源] 预清洗结果: {cleaned['summary']}")
    
    children_for_merge = cleaned["strong_child_records"]
    male_lead_from_preclean = cleaned["male_lead_child_records"]
    pregnancy_only = cleaned["pregnancy_only_records"]
    discarded = cleaned["discarded_records"]
    
    # 如果预清洗后没有强证据记录，直接返回
    if not children_for_merge and not male_lead_from_preclean:
        return {
            "has_biological_children": False,
            "biological_children": [],
            "adopted_children": [],
            "male_lead_children": [],
            "child_origin_reason": "无足够强的孩子存在证据（仅怀孕/弱语句/身世倒装）",
            "pregnancy_only_records": pregnancy_only,
            "discarded_records": discarded,
            "uncertain_children": [],
            "first_round_results": [],
            **owner_debug_info,
        }
    
    # ===== 阶段1：合并孩子记录（仅对强证据记录） =====
    # 把男主孩子记录也加入合并（但后续会单独处理）
    all_records_for_merge = children_for_merge + male_lead_from_preclean
    
    if len(all_records_for_merge) <= 2:
        # 少量记录直接简单分组，不调用 LLM
        merged_children: Dict[str, List[Dict[str, Any]]] = {}
        for record in all_records_for_merge:
            child_name = str(record.get("child_name", "") or "").strip() or "未命名孩子"
            if child_name not in merged_children:
                merged_children[child_name] = []
            merged_children[child_name].append(record)
        logger.info(f"    [孩子来源] 记录较少，直接分组: {list(merged_children.keys())}")
    else:
        logger.info(f"    [孩子来源] 调用 LLM 判断孩子记录合并...")
        merged_children = _llm_merge_children_records(name, all_records_for_merge, male_lead)
        logger.info(f"    [孩子来源] 合并后共 {len(merged_children)} 个孩子: {list(merged_children.keys())}")
    
    # ===== 阶段2：逐条判断每个孩子（规则优先 + LLM兜底） =====
    first_round_results: List[Dict[str, Any]] = []
    for child_name, records in merged_children.items():
        logger.info(f"    [孩子来源] 判断孩子: {child_name} ({len(records)}条记录)")
        # 使用规则优先判定
        result = _judge_single_child_origin(name, child_name, records, male_lead)
        first_round_results.append(result)
    
    # 分类：需要男性参与 vs 不需要男性参与
    needs_male_children = [c for c in first_round_results if c.get("needs_male", False)]
    non_male_children = [c for c in first_round_results if not c.get("needs_male", False)]
    
    logger.info(f"    [孩子来源] 第一轮结果: 需男性参与={len(needs_male_children)}, 非男性参与={len(non_male_children)}")
    
    # ===== 阶段2：验证非男性参与的孩子 =====
    if non_male_children:
        logger.info(f"    [孩子来源] 第二轮验证: {len(non_male_children)} 个非男性参与孩子")
        verification = _llm_verify_non_male_children(
            name, non_male_children, virginity_mentions, male_lead_intimacy, male_lead
        )
        
        # 更新分类
        verified_non_male = verification.get("verified_non_male", [])
        reclassified_male = verification.get("reclassified_male", [])
        
        if reclassified_male:
            logger.info(f"    [孩子来源] 验证后重新判定: {len(reclassified_male)} 个孩子改为需男性参与")
            needs_male_children.extend(reclassified_male)
        
        non_male_children = verified_non_male
    
    # ===== 阶段3：判断父亲是否为男主 =====
    # 需要男性参与的孩子中，父亲是男主的不算"非男主亲生孩子"
    biological_children = []  # 非男主亲生
    male_lead_children = []   # 男主的孩子（豁免）
    
    male_lead_lower = male_lead.lower() if male_lead else ""
    
    for child in needs_male_children:
        # 优先使用 LLM 判断的 is_father_male_lead 字段
        is_father_ml = child.get("is_father_male_lead", False)
        
        # 如果 LLM 没有明确判断，则用字符串匹配作为备选
        if not is_father_ml:
            father = str(child.get("father", "") or "").strip()
            father_lower = father.lower()
            
            if male_lead_lower and (male_lead_lower in father_lower or father_lower in male_lead_lower):
                is_father_ml = True
            if father in ["男主", "主角"]:
                is_father_ml = True
        
        if is_father_ml:
            male_lead_children.append(child)
            logger.info(f"    [孩子来源] {child.get('child_name', '未知')} 父亲是男主，豁免")
        else:
            biological_children.append(child)
            logger.info(f"    [孩子来源] {child.get('child_name', '未知')} 父亲={child.get('father', '未知')}，非男主")
    
    # ===== 整理最终结果（收紧 has_biological_children 触发门槛） =====
    # 收紧条件：必须同时满足 child_exists=True, needs_male=True, is_father_male_lead=False, confidence>=0.65
    confirmed_biological = []  # 确认的非男主亲生孩子
    uncertain_children = []    # 不确定的孩子（证据弱）
    
    for child in biological_children:
        child_exists = child.get("child_exists", True)
        confidence = child.get("confidence", 0.5)
        evidence_is_strong = child.get("evidence_is_strong", True)
        
        # 收紧条件：child_exists 必须为 True，且 (confidence>=0.65 或 evidence_is_strong=True)
        if not child_exists:
            uncertain_children.append(child)
            logger.info(f"    [孩子来源] {child.get('child_name', '未知')} child_exists=False，归入不确定")
        elif confidence >= 0.65 or evidence_is_strong:
            confirmed_biological.append(child)
            logger.info(f"    [孩子来源] {child.get('child_name', '未知')} 确认为非男主亲生 (confidence={confidence})")
        else:
            uncertain_children.append(child)
            logger.info(f"    [孩子来源] {child.get('child_name', '未知')} confidence={confidence}<0.65且evidence_is_strong=False，归入不确定")
    
    has_biological = len(confirmed_biological) > 0
    
    # 构建理由
    reason_parts = []
    if confirmed_biological:
        names = [c.get("child_name", "未知") for c in confirmed_biological]
        reason_parts.append(f"与非男主男性生育的孩子: {', '.join(names)}")
    if male_lead_children:
        names = [c.get("child_name", "未知") for c in male_lead_children]
        reason_parts.append(f"与男主生育的孩子(豁免): {', '.join(names)}")
    if non_male_children:
        names = [c.get("child_name", "未知") for c in non_male_children]
        methods = [c.get("birth_method", "非男性参与") for c in non_male_children]
        reason_parts.append(f"非男性参与诞生的孩子: {', '.join([f'{n}({m})' for n, m in zip(names, methods)])}")
    if uncertain_children:
        names = [c.get("child_name", "未知") for c in uncertain_children]
        reason_parts.append(f"证据不足/不确定的孩子(未计入): {', '.join(names)}")
    
    final_reason = "; ".join(reason_parts) if reason_parts else "无孩子"
    
    logger.info(f"    [孩子来源] 最终结果: has_biological={has_biological}, 理由={final_reason[:100]}")
    
    return {
        "has_biological_children": has_biological,
        "biological_children": confirmed_biological,  # 只返回确认的
        "adopted_children": non_male_children,
        "male_lead_children": male_lead_children,
        "child_origin_reason": final_reason,
        "first_round_results": first_round_results,
        "pregnancy_only_records": pregnancy_only,
        "discarded_records": discarded,
        "uncertain_children": uncertain_children,
        **owner_debug_info,
    }


def _llm_judge_partner(
    name: str,
    romantic_feelings: List[Dict[str, Any]],
    partner_relations: List[Dict[str, Any]],
    non_male_male_interactions: List[str],
    male_lead: str,
    analyzed_partner_relations: Optional[List[Dict[str, Any]]] = None,
    female_role_names: Optional[List[str]] = None,
    has_bio_children: bool = False,
    bio_children: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Step 1: 是否有男伴（最简单的判断）
    
    使用 romantic_feelings + partner_relations 判断是否有非男主的正式伴侣关系
    
    返回: {
        "no_partner": bool,        # 是否无非男主男伴
        "partner_status": "...",   # 状态描述
        "partner_list": [...],     # 非男主伴侣列表（含 forced/has_feelings 分析结果）
        "partner_reason": "...",   # 判断理由
        "analyzed_partners": [...],  # 已分析的伴侣关系（供后续 Step 使用）
    }
    """
    # 如果已有分析结果则直接使用，否则调用 LLM 分析
    if analyzed_partner_relations is not None:
        partner_relations = analyzed_partner_relations
    else:
        # 使用 LLM 逐条分析并聚合伴侣关系
        partner_relations = _llm_analyze_and_merge_partner_relations(name, partner_relations or [], male_lead)

    female_role_names = [str(x).strip() for x in (female_role_names or []) if str(x).strip()]
    female_name_norm_set = _build_name_norm_set(female_role_names + [name])

    # 构建伴侣关系信息
    partner_text = []
    filtered_partner_relations: List[Dict[str, Any]] = []
    dropped_female_partner_records = 0
    dropped_female_feelings = 0
    dropped_female_interactions = 0

    for pr in partner_relations:
        partner = str(pr.get("partner", "未知") or "未知")
        is_ml = "男主" if pr.get("is_male_lead") else "非男主"
        rel = pr.get("relationship", "未知")
        status = pr.get("status", "未知")
        forced_flag = pr.get("forced", None)
        forced = "被迫" if forced_flag is True else ("自愿" if forced_flag is False else "未知")
        speech_act = str(pr.get("speech_act", "") or "").strip() or "未知"
        fact_flag = pr.get("is_fact_statement", None)
        fact_label = "事实" if fact_flag is True else ("非事实/假设" if fact_flag is False else "未知")
        evidence = _clip_llm_text(pr.get("evidence", ""), 180)
        gender_hint = _is_likely_male_counterpart(
            partner,
            " ".join([
                str(rel or ""),
                str(status or ""),
                str(pr.get("evidence", "") or ""),
                str(pr.get("detail", "") or ""),
                str(pr.get("analysis_reason", "") or ""),
            ]),
            female_name_norm_set,
        )
        if (not pr.get("is_male_lead", False)) and gender_hint is False:
            dropped_female_partner_records += 1
            continue
        filtered_partner_relations.append(pr)
        gender_label = "男性" if gender_hint is True else "性别未知"
        partner_text.append(
            f"[伴侣] {partner}({is_ml},{gender_label}), 关系:{rel}, 状态:{status}, {forced}, "
            f"语用:{speech_act}, 事实性:{fact_label}, 证据:{evidence}"
        )
    
    for rf in romantic_feelings:
        target = str(rf.get("target", "未知") or "未知")
        is_ml = "男主" if rf.get("is_male_lead") else "非男主"
        evidence = _clip_llm_text(rf.get("evidence", ""), 180)
        gender_hint = _is_likely_male_counterpart(
            target,
            " ".join([
                str(rf.get("detail", "") or ""),
                str(rf.get("evidence", "") or ""),
            ]),
            female_name_norm_set,
        )
        if (not rf.get("is_male_lead", False)) and gender_hint is False:
            dropped_female_feelings += 1
            continue
        gender_label = "男性" if gender_hint is True else "性别未知"
        partner_text.append(f"[感情] 对{target}({is_ml},{gender_label})有感情, 证据:{evidence}")

    # 关键补充：很多“男友/对象/前任”等信息会落在旧字段 non_male_male_interactions 里
    for it in (non_male_male_interactions or []):
        s = _clip_llm_text(it, 180)
        if s:
            partner = _extract_partner_from_contact_interaction(s)
            gender_hint = _is_likely_male_counterpart(partner, s, female_name_norm_set)
            if gender_hint is False:
                dropped_female_interactions += 1
                continue
            gender_label = "男性" if gender_hint is True else "性别未知"
            if partner:
                partner_text.append(f"[非男主互动] 对象:{partner}({gender_label}) | {s}")
            else:
                partner_text.append(f"[非男主互动] 对象:未知({gender_label}) | {s}")

    # 补充亲生孩子信息，帮助 LLM 推断隐含伴侣
    if has_bio_children and bio_children:
        for child in (bio_children or []):
            if not isinstance(child, dict):
                continue
            child_name = child.get("child_name", "未知")
            father = child.get("father", "未知")
            child_evidence = _clip_llm_text(child.get("evidence", child.get("reason", "")), 180)
            partner_text.append(
                f"[亲生孩子] {child_name}, 父亲:{father}, 证据:{child_evidence}"
            )

    if not partner_text:
        reason = "无任何伴侣关系或感情记录"
        dropped_total = dropped_female_partner_records + dropped_female_feelings + dropped_female_interactions
        if dropped_total:
            reason += f"（已过滤{dropped_total}条女性相关记录）"
        return {
            "no_partner": True,
            "partner_status": "✅ 无男伴（无伴侣关系记录）",
            "partner_list": [],
            "partner_reason": reason,
            "analyzed_partners": filtered_partner_relations,  # 仅保留非女性对象，供后续Step使用
        }
    
    partner_str = _compact_llm_lines(partner_text, label="伴侣/感情信息")
    female_name_hint = "、".join(female_role_names[:40]) if female_role_names else "无"
    
    system_prompt = f"""你是严格的小说角色分析专家。判断女性角色【{name}】是否有非男主的正式男伴。

【男主】：{male_lead}
【已知女性角色名单】{female_name_hint}

【判定规则】：
- ✅ 无男伴：从未有过非男主的正式伴侣（男朋友/丈夫/恋人等）
- ✅ 无男伴（豁免）：
  · 单方面被追求但从未接受
  · 被迫订婚/包办婚姻但最终未完婚（婚约解除/逃婚/对方死亡等）
- ❌ 有男伴：曾有过非男主的正式交往关系（前男友/前夫/前恋人）
- ❌ 有男伴：与非男主已完婚的婚姻（无论有名无实/未圆房/假结婚）
- ❌ 有男伴：有亲生孩子且其父亲是非男主（生育亲生孩子意味着曾存在伴侣关系）
- ✅ 无男伴：对象为女性（如百合/女友/女性恋人）不算"男伴"

【关键】：
- is_male_lead=true 或 partner 明确等于男主名字 → 不算"非男主男伴"
- 对象若为女性（包括在“已知女性角色名单”中的名字）→ 不算"男伴"
- 证据不足 → 默认 ✅ 无男伴
- 【事实性硬规则（最高优先级）】：
  1) 只统计"已发生事实"，不统计"劝说/假设/未来场景"。
  2) 以下语气默认视为非事实：如果/假如/就算/以后/将来/可能会/你想想/你觉得/那样的场景/会不会。
  3) 关系词（男友/恋爱/结婚/在一起）只有在"事实陈述"里才生效；若出现在假设句中，不得判❌有男伴。
  4) 若输入里带有 `is_fact_statement=false` 且事实性标注为"非事实/假设"：
     · 若证据中不含关系词（丈夫/男友/恋人等），按非事实忽略
     · 若证据中明确包含关系词（丈夫/男友/恋人/前夫/未婚夫等）且对象是非男主，
       不得忽略——应重新审视该记录的事实性，以证据内容为准
- 【硬规则】只要证据中出现明确关系词（如“男友/男朋友/对象/恋人/前男友/前夫/未婚夫/丈夫/交往/谈恋爱/情侣”）
  且对象是非男主男性，并且该证据是“已发生事实”，则必须判定为 ❌有男伴（no_partner=false）。

输出 JSON：
{{
  "no_partner": true/false,
  "partner_status": "简短状态描述",
  "partner_list": [  // 非男主伴侣列表
    {{"name": "伴侣名", "relationship": "关系类型", "forced": true/false/null}}
  ],
  "partner_reason": "判断理由"
}}"""

    user_prompt = f"""请判断角色【{name}】是否有非男主男伴：

【伴侣/感情信息】：
{partner_str}

请判断是否有非男主的正式男伴，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        raw_partner_list = data.get("partner_list", [])
        if not isinstance(raw_partner_list, list):
            raw_partner_list = []
        normalized_partner_list = []
        dropped_female_from_llm = 0
        for item in raw_partner_list:
            if not isinstance(item, dict):
                continue
            pname = str(item.get("name", "") or "").strip()
            gender_hint = _is_likely_male_counterpart(
                pname,
                " ".join([
                    str(item.get("relationship", "") or ""),
                    str(item.get("status", "") or ""),
                    str(item.get("reason", "") or ""),
                ]),
                female_name_norm_set,
            )
            if gender_hint is False:
                dropped_female_from_llm += 1
                continue
            normalized_partner_list.append(item)
        no_partner = _to_bool(data.get("no_partner", True), True)
        partner_status = str(data.get("partner_status", "❓ 未知") or "❓ 未知")
        partner_reason = str(data.get("partner_reason", "") or "")
        if dropped_female_from_llm > 0:
            extra = f"已过滤{dropped_female_from_llm}条女性伴侣项"
            partner_reason = f"{partner_reason}；{extra}" if partner_reason else extra
        if (not no_partner) and (not normalized_partner_list) and dropped_female_from_llm > 0:
            no_partner = True
            partner_status = "✅ 无男伴（仅女性/非男性关系）"
            if not partner_reason:
                partner_reason = "仅发现女性或非男性关系，不计入男伴"
        return {
            "no_partner": no_partner,
            "partner_status": partner_status,
            "partner_list": normalized_partner_list,
            "partner_reason": partner_reason,
            "analyzed_partners": filtered_partner_relations,  # 仅保留非女性对象
        }
    except Exception as e:
        logger.warning(f"男伴LLM判断异常: {e}")
        return {
            "no_partner": True,
            "partner_status": "❓ 判断失败",
            "partner_list": [],
            "partner_reason": f"LLM调用失败: {e}",
            "analyzed_partners": filtered_partner_relations,  # 仅保留非女性对象
        }


def _llm_judge_contact(
    name: str,
    physical_contacts: List[Dict[str, Any]],
    non_male_male_interactions: List[str],
    has_biological_children: bool,
    biological_children: List[Dict[str, Any]],
    male_lead: str,
    female_role_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Step 2: 是否有非男主肉体接触
    
    使用 physical_contacts + non_male_male_interactions
    注意：有亲生孩子必定被非男主触碰过
    
    返回: {
        "has_other_contact": bool,   # 是否有非男主接触
        "contact_status": "...",     # 状态描述
        "contact_list": [...],       # 接触事件列表
        "contact_reason": "...",     # 判断理由
    }
    """
    # 如果有亲生孩子，直接判定有接触
    if has_biological_children and biological_children:
        fathers = [c.get("father", "未知男性") for c in biological_children]
        return {
            "has_other_contact": True,
            "contact_status": f"❌ 有接触（与非男主{','.join(fathers)}生育亲生孩子）",
            "contact_list": [{"type": "生育", "partner": f, "reason": "亲生孩子必然有性接触"} for f in fathers],
            "contact_reason": "有非男主亲生孩子，必然存在肉体接触",
        }
    
    female_role_names = [str(x).strip() for x in (female_role_names or []) if str(x).strip()]
    female_name_norm_set = _build_name_norm_set(female_role_names + [name])

    # 构建接触信息（先过滤明确女性对象，避免误计入“非男主男性接触”）
    contact_text = []
    dropped_female_events = 0

    for pc in physical_contacts:
        partner = str(pc.get("partner", "未知") or "未知")
        context_blob = " ".join([
            str(pc.get("contact_type", "") or ""),
            str(pc.get("evidence", "") or ""),
            str(pc.get("detail", "") or ""),
        ])
        male_hint = _is_likely_male_counterpart(partner, context_blob, female_name_norm_set)
        if male_hint is False:
            dropped_female_events += 1
            continue
        is_ml = "男主" if pc.get("is_male_lead") else "非男主"
        ctype = pc.get("contact_type", "未知")
        evidence = _clip_llm_text(pc.get("evidence", ""), 180)
        gender_hint = "男性" if male_hint is True else "性别未知"
        contact_text.append(f"[肉体接触] 与{partner}({is_ml},{gender_hint}){ctype}, 证据:{evidence}")
    
    for interaction in (non_male_male_interactions or []):
        interaction_text = _clip_llm_text(interaction, 180)
        if not interaction_text:
            continue
        partner = _extract_partner_from_contact_interaction(interaction_text)
        male_hint = _is_likely_male_counterpart(partner, interaction_text, female_name_norm_set)
        if male_hint is False:
            dropped_female_events += 1
            continue
        gender_hint = "男性" if male_hint is True else "性别未知"
        if partner:
            contact_text.append(f"[非男主互动] 对象:{partner}({gender_hint}) | {interaction_text}")
        else:
            contact_text.append(f"[非男主互动] 对象:未知({gender_hint}) | {interaction_text}")
    
    if not contact_text:
        reason = "无任何非男主男性肉体接触记录"
        if dropped_female_events:
            reason += f"（已过滤{dropped_female_events}条女性互动）"
        return {
            "has_other_contact": False,
            "contact_status": "✅ 无接触（无非男主接触记录）",
            "contact_list": [],
            "contact_reason": reason,
        }
    
    contact_str = _compact_llm_lines(contact_text, label="接触/互动信息")
    female_name_hint = "、".join(female_role_names[:40]) if female_role_names else "无"
    
    system_prompt = f"""你是严格的小说角色分析专家。判断女性角色【{name}】是否被非男主男性实际触碰过身体。

【男主】：{male_lead}
【已知女性角色名单】{female_name_hint}

【判定规则】：
- ✅ 无接触：从未被非男主男性实际触碰身体
- ✅ 无接触：仅被男主接触
- ✅ 无接触：仅被围观/注视/议论/目光凝视（不算肉体接触）
- ❌ 有接触：被非男主男性实际触碰身体（猥亵/深吻/爱抚/舌吻/摸胸/强抱/性行为等）

【关键】：
- is_male_lead=true 或 partner 明确等于男主名字 → 不算"非男主接触"
- 对方若为女性（包括在“已知女性角色名单”中的名字）→ 不算"非男主男性接触"
- 被围观/注视 ≠ 肉体接触
- 性别不明 → 默认不算接触（除非证据明确“男性+实际触碰”）
- 证据不足 → 默认 ✅ 无接触
- 【事实性硬规则（最高优先级）】：
  1) 只统计“已发生的身体触碰事实”。
  2) 劝说/假设/未来场景（如“如果/就算/以后/将来/可能会/你想想/那样的场景”）一律不算接触证据。
  3) 不能把“可能会被欺负/会不会怎样”推断为已发生触碰。

输出 JSON：
{{
  "has_other_contact": true/false,
  "contact_status": "简短状态描述",
  "contact_list": [  // 非男主接触事件列表
    {{"partner": "对方名", "contact_type": "接触类型", "evidence": "证据摘要"}}
  ],
  "contact_reason": "判断理由"
}}"""

    user_prompt = f"""请判断角色【{name}】是否有非男主肉体接触：

【接触/互动信息】：
{contact_str}

请判断是否被非男主男性实际触碰过，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        return {
            "has_other_contact": data.get("has_other_contact", False),
            "contact_status": data.get("contact_status", "❓ 未知"),
            "contact_list": data.get("contact_list", []),
            "contact_reason": data.get("contact_reason", ""),
        }
    except Exception as e:
        logger.warning(f"接触LLM判断异常: {e}")
        return {
            "has_other_contact": False,
            "contact_status": "❓ 判断失败",
            "contact_list": [],
            "contact_reason": f"LLM调用失败: {e}",
        }


def _llm_judge_spirit(
    name: str,
    no_partner: bool,
    partner_list: List[Dict[str, Any]],
    has_biological_children: bool,
    biological_children: List[Dict[str, Any]],
    romantic_feelings: List[Dict[str, Any]],
    male_lead: str,
    analyzed_partners: Optional[List[Dict[str, Any]]] = None,
    all_partner_relations: Optional[List[Dict[str, Any]]] = None,
    sexual_relations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Step 3: 精神纯洁度判断
    
    综合考虑：
    1. 有无非男主伴侣 - 如果有，看是否对其动心、是否被迫
    2. 有无亲生孩子 - 如果有，直接判定精神不纯洁
    3. 有无对非男主对象动心（不区分性别）
    
    返回: {
        "is_spirit_clean": bool,    # 是否精神洁
        "spirit_status": "...",     # 状态描述
        "spirit_reason": "...",     # 判断理由
    }
    """
    # 有亲生孩子直接判定精神不纯洁
    if has_biological_children and biological_children:
        fathers = [c.get("father", "未知男性") for c in biological_children]
        return {
            "is_spirit_clean": False,
            "spirit_status": f"❌ 精神非初（与非男主{','.join(fathers)}生育亲生孩子）",
            "spirit_reason": "有非男主亲生孩子，必然存在情感/肉体关系",
        }
    
    # 使用已分析的伴侣关系（来自 Step 1），包含 forced 和 has_feelings 判定
    # 如果没有传入，则使用简单合并（备选方案）
    if analyzed_partners is not None:
        analyzed_list = analyzed_partners
    else:
        analyzed_list = _simple_merge_partner_relations(partner_list or [])
    
    all_partner_relations = all_partner_relations or []
    sexual_relations = sexual_relations or []
    analyzed_partner_map = _build_partner_analysis_map(analyzed_list)
    non_ml_sex_evidence = _collect_non_ml_sex_evidence_for_spirit(sexual_relations, male_lead)
    has_non_ml_sex_evidence = bool(non_ml_sex_evidence)
    
    # 构建情感信息
    spirit_text = []
    
    # 男伴信息（使用已分析的 forced 和 has_feelings）
    if not no_partner and analyzed_list:
        for p in analyzed_list:
            pname = p.get("partner", p.get("name", "未知"))
            rel = p.get("relationship", "未知")
            forced_flag = p.get("forced", None)
            has_feelings_flag = p.get("has_feelings", None)
            forced = "被迫" if forced_flag is True else ("自愿" if forced_flag is False else "未知")
            feelings = "有感情" if has_feelings_flag is True else ("无感情" if has_feelings_flag is False else "未知")
            analysis_reason = _clip_llm_text(p.get("analysis_reason", ""), 180)
            spirit_text.append(f"[男伴] {pname}, 关系:{rel}, {forced}, {feelings}" + (f", 分析:{analysis_reason}" if analysis_reason else ""))
    
    # 非男主伴侣（不限性别）：用于精神判定，不能只看“男伴”维度
    non_ml_partner_history = []
    non_exempt_partner_history = []
    exempt_partner_notes: List[Dict[str, str]] = []
    for pr in all_partner_relations:
        if pr.get("is_male_lead", False):
            continue
        partner = str(pr.get("partner", "") or "").strip()
        if _is_placeholder(partner):
            continue
        speech_act = str(pr.get("speech_act", "") or "").strip().lower()
        if speech_act and speech_act != "asserted_fact":
            continue
        evidence_strength = str(pr.get("evidence_strength", "") or "").strip().lower()
        if evidence_strength in {"weak", "low"}:
            continue
        rel = str(pr.get("relationship", "") or "").strip()
        status = str(pr.get("status", "") or "").strip()
        evidence = _clip_llm_text(pr.get("evidence", ""), 180)
        non_ml_partner_history.append(pr)
        is_exempt, exempt_reason = _is_spirit_exempt_partner_relation(
            pr,
            analyzed_partner_map=analyzed_partner_map,
            has_non_ml_sex_evidence=has_non_ml_sex_evidence,
        )
        if is_exempt:
            exempt_partner_notes.append(
                {
                    "partner": partner,
                    "reason": exempt_reason,
                    "evidence": _clip_llm_text(evidence, 80),
                }
            )
            spirit_text.append(
                f"[伴侣(非男主对象)] {partner}, 关系:{rel}, 状态:{status}, 豁免候选:{exempt_reason}, 证据:{evidence}"
            )
            continue
        non_exempt_partner_history.append(pr)
        spirit_text.append(f"[伴侣(非男主对象)] {partner}, 关系:{rel}, 状态:{status}, 证据:{evidence}")
    
    # 感情信息：只传【非男主】给模型，避免把“对男主的爱”混进精神洁判定输入
    non_ml_feelings_history = []
    non_ml_positive_feelings_history = []
    for rf in romantic_feelings:
        if rf.get("is_male_lead"):
            continue
        target = str(rf.get("target", "未知") or "未知").strip()
        if _is_placeholder(target):
            continue
        speech_act = str(rf.get("speech_act", "") or "").strip().lower()
        if speech_act and speech_act != "asserted_fact":
            continue
        evidence_strength = str(rf.get("evidence_strength", "") or "").strip().lower()
        if evidence_strength in {"weak", "low"}:
            continue
        evidence = _clip_llm_text(rf.get("evidence", ""), 180)
        non_ml_feelings_history.append(rf)
        positive_flag = _is_positive_non_ml_feeling_record(rf)
        if positive_flag:
            non_ml_positive_feelings_history.append(rf)
        feeling_label = "正向动心证据" if positive_flag else "弱/非正向动心证据"
        spirit_text.append(f"[感情] 对{target}(非男主对象), {feeling_label}, 证据:{evidence}")
    
    if not spirit_text:
        return {
            "is_spirit_clean": True,
            "spirit_status": "✅ 精神洁（无非男主感情记录）",
            "spirit_reason": "无任何非男主对象的感情或伴侣记录",
        }
    
    spirit_str = _compact_llm_lines(spirit_text, label="精神洁证据")
    
    system_prompt = f"""你是严格的小说角色分析专家。判断女性角色【{name}】的精神纯洁度。

【男主】：{male_lead}

【判定规则】：
- ✅ 精神洁：从未真心爱过男主以外的对象（不区分性别）
- ✅ 精神洁（豁免）：
  · 被迫联姻/厌恶对方/被强迫但无感情投入
  · 单方面被追求但从未动心
  · 【成婚/结婚豁免】与非男主成婚/结婚/名义夫妻/政治联姻，但【明确未发生实质性关系】（如未圆房/未同房/有名无实/连手都不让碰/假结婚等），且【未动心/无感情投入】→ 必须判 ✅精神洁
- ❌ 精神非初：真心爱过男主以外的对象（男性/女性均算）
- ❌ 精神非初：有非男主正式伴侣且对其有感情（不限性别）
- ❌ 精神非初：有明确百合/同性恋爱史（对象非男主） 
- ❌ 精神非初：与非男主男性生育亲生孩子

【关键】：
- is_male_lead=true → 不影响精神洁度
- 被迫/无感情 → 可豁免
- 证据不足 → 默认 ✅ 精神洁
- “曾有丈夫/前夫/已完婚”本身不是❌精神非初的充分条件，必须结合“是否自愿 + 是否动心”。
- 若仅有关系事实，但同时出现强烈负面词（如“厌恶/讨厌到想吐/仇恨/畜生/被伤害/家暴”）且无动心证据，应优先按“无感情豁免”处理。
- 只有“真心爱过/主动动心/有感情投入”才可据此判❌精神非初；不得因“名义关系”直接判❌。
- “强奸未遂/企图强奸/没得逞”属于受害或未遂语义，不得当作“已建立恋爱/伴侣感情”证据。
- “无性行为怀孕/试管婴儿/人工授精/供精”等只说明生育方式，不得单独反推“爱过别人/精神非初”。
- 【事实性硬规则（最高优先级）】：
  1) 只采纳“已发生事实”的感情/伴侣记录。
  2) 劝说/假设/未来想象（如“以后会…/就算后来…/你想想那样的场景”）不得作为“爱过别人/有男伴”的依据。
  3) 若某条证据本身注明 `is_fact_statement=false` 或 `speech_act!=asserted_fact`，必须忽略。

【判定提示（提高一致性）】：
- “成婚/结婚/丈夫/妻子/夫妻/婚约/联姻”本身不必然=精神非初；关键看【是否动心】与【是否有实质关系】。
- 若证据同时出现“已结婚”与“未圆房/有名无实/假结婚/未同房”，且未出现“爱上/喜欢/动心/真心/舍不得/放不下”等情感投入词，应倾向判 ✅精神洁。
- 若证据是“被迫关系 + 厌恶/仇恨/创伤叙述”，在无正向感情证据时应判 ✅精神洁（豁免）。

输出 JSON：
{{
  "is_spirit_clean": true/false,
  "spirit_status": "简短状态描述",
  "spirit_reason": "判断理由",
  "loved_others": [  // 爱过的非男主对象列表（如有，含同性）
    {{"name": "对方名", "evidence": "证据摘要"}}
  ]
}}"""

    user_prompt = f"""请判断角色【{name}】的精神纯洁度：

【已知信息】：
- 是否有非男主男伴: {"否" if no_partner else "是"}
- 是否有非男主伴侣(不限性别): {"是" if non_ml_partner_history else "否"}
- 其中“非豁免伴侣关系”数量: {len(non_exempt_partner_history)}
- 其中“豁免候选关系”数量: {len(exempt_partner_notes)}
- 是否有非男主亲生孩子: {"否" if not has_biological_children else "是"}
- 是否有可确认的非男主性关系证据: {"是" if has_non_ml_sex_evidence else "否"}
- 非男主动心证据条数: {len(non_ml_positive_feelings_history)}

【感情/男伴信息】：
{spirit_str}

请判断精神是否纯洁，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        result = {
            "is_spirit_clean": data.get("is_spirit_clean", True),
            "spirit_status": data.get("spirit_status", "❓ 未知"),
            "spirit_reason": data.get("spirit_reason", ""),
            "loved_others": data.get("loved_others", []),
        }
        # 规则兜底1：仅“已发生且明确正向动心”的非男主感情记录（含百合）可强制判精神非初
        if result.get("is_spirit_clean", True) and non_ml_positive_feelings_history:
            targets = []
            for rf in non_ml_positive_feelings_history:
                t = str(rf.get("target", "") or "").strip()
                if t and t not in targets:
                    targets.append(t)
            sample_ev = str(non_ml_positive_feelings_history[0].get("evidence", "") or "")[:80]
            who = ",".join(targets[:3]) if targets else "非男主对象"
            result["is_spirit_clean"] = False
            result["spirit_status"] = f"❌ 精神非初（曾对非男主对象动心：{who}）"
            base_reason = "规则兜底：存在已发生且证据明确的非男主对象正向动心记录（含同性关系）"
            result["spirit_reason"] = f"{base_reason}；证据:{sample_ev}" if sample_ev else base_reason
        # 规则兜底2：仅“非豁免”的非男主伴侣史可强制覆盖为精神非初
        if result.get("is_spirit_clean", True) and non_exempt_partner_history:
            partners = []
            for pr in non_exempt_partner_history:
                p = str(pr.get("partner", "") or "").strip()
                if p and p not in partners:
                    partners.append(p)
            who = ",".join(partners[:3]) if partners else "非男主对象"
            sample_ev = str(non_exempt_partner_history[0].get("evidence", "") or "")[:80]
            result["is_spirit_clean"] = False
            result["spirit_status"] = f"❌ 精神非初（存在非男主伴侣史：{who}）"
            base_reason = "规则兜底：存在已发生且不满足豁免条件的非男主伴侣记录（不限性别）"
            result["spirit_reason"] = f"{base_reason}；证据:{sample_ev}" if sample_ev else base_reason
        # 若仅剩豁免伴侣记录，保留 LLM 的“精神洁”并写明触发依据，便于审计
        if result.get("is_spirit_clean", True) and exempt_partner_notes:
            note = exempt_partner_notes[0]
            base_note = (
                f"规则豁免：伴侣记录“{note.get('partner', '未知')}”命中"
                f"{note.get('reason', '被迫/无感情豁免')}"
            )
            ev = str(note.get("evidence", "") or "")[:80]
            if ev:
                base_note = f"{base_note}；证据:{ev}"
            old_reason = str(result.get("spirit_reason", "") or "").strip()
            if old_reason:
                if base_note not in old_reason:
                    result["spirit_reason"] = f"{old_reason}；{base_note}"
            else:
                result["spirit_reason"] = base_note
        result["partner_exempted_for_clean"] = bool(exempt_partner_notes and not non_exempt_partner_history)
        result["partner_exemption_notes"] = exempt_partner_notes
        result["partner_exemption_reason"] = (
            "；".join(
                f"{note.get('partner', '未知')}：{note.get('reason', '命中豁免')}"
                for note in exempt_partner_notes[:3]
            )
            if exempt_partner_notes
            else ""
        )
        return result
    except Exception as e:
        logger.warning(f"精神洁LLM判断异常: {e}")
        return {
            "is_spirit_clean": True,
            "spirit_status": "❓ 判断失败",
            "spirit_reason": f"LLM调用失败: {e}",
        }


def _llm_judge_virgin(
    name: str,
    no_partner: bool,
    partner_list: List[Dict[str, Any]],
    has_biological_children: bool,
    biological_children: List[Dict[str, Any]],
    sexual_relations: List[Dict[str, Any]],
    virginity_mentions: List[str],
    male_lead_intimacy: List[str],
    male_lead: str,
) -> Dict[str, Any]:
    """
    Step 4: 是否处女（综合判断）
    
    综合考虑：
    1. 有无男伴 - 如果有，查看是否与非男主有性关系
    2. 有无孩子 - 如果有，查看是否亲生
    3. 有无非男主性经历
    4. 与男主发生关系时有无暗示处女（辅助判断）
    
    返回: {
        "is_virgin": bool,        # 是否处女
        "virgin_status": "...",   # 状态描述
        "virgin_reason": "...",   # 判断理由
    }
    """
    # 有亲生孩子直接判定非处
    if has_biological_children and biological_children:
        fathers = [c.get("father", "未知男性") for c in biological_children]
        return {
            "is_virgin": False,
            "virgin_status": f"❌ 非处（与非男主{','.join(fathers)}生育亲生孩子）",
            "virgin_reason": "有非男主亲生孩子，必然发生过性关系",
        }
    
    # 构建性关系信息
    virgin_text = []
    
    # 性关系信息
    for sr in sexual_relations:
        partner = sr.get("partner", "未知")
        is_ml = "男主" if sr.get("is_male_lead") else "非男主"
        detail = _clip_llm_text(sr.get("detail", ""), 180)
        evidence = _clip_llm_text(sr.get("evidence", ""), 180)
        virgin_text.append(f"[性关系] 与{partner}({is_ml}): {detail}, 证据:{evidence}")
    
    # 男伴信息（如果有伴侣，可能有性关系）
    if not no_partner and partner_list:
        for p in partner_list:
            pname = p.get("name", "未知")
            rel = p.get("relationship", "未知")
            virgin_text.append(f"[男伴] {pname}, 关系:{rel}（需判断是否有性关系）")
    
    # 处女暗示信息
    virgin_hints = []
    if virginity_mentions:
        virgin_hints.extend([f"[处女提及] {_clip_llm_text(v, 180)}" for v in virginity_mentions])
    if male_lead_intimacy:
        virgin_hints.extend([f"[与男主亲密] {_clip_llm_text(m, 180)}" for m in male_lead_intimacy])
    
    virgin_str = _compact_llm_lines(virgin_text, label="性关系/男伴信息", empty="（无性关系记录）")
    hints_str = _compact_llm_lines(virgin_hints, label="处女暗示信息", empty="（无处女暗示记录）")
    
    system_prompt = f"""你是严格的小说角色分析专家。判断女性角色【{name}】是否为处女。

【男主】：{male_lead}

【判定规则】：
- ✅ 处女：从未与任何人发生性关系
- ✅ 处女（仅男主）：仅与男主【{male_lead}】发生过性关系 → 必须判为处女！
- ✅ 处女（特殊豁免）：有名无实/未圆房/假结婚的人妻
- ✅ 处女（处女妈妈豁免）：收养/领养/特殊方式诞生的孩子
- ❌ 非处：与非男主男性发生过性关系（包括被强奸/迷奸/轮奸）
- ❌ 非处：有非男主的亲生孩子

【处女暗示辅助判断】：
★ 如果证据显示与男主发生关系时有"初夜/破处/第一次/处女膜/落红"等描写，说明之前是处女
★ 有伴侣但有处女暗示 → 可能伴侣关系未圆房，需综合判断

【严禁错误】：
- 与男主发生关系 → 不能判为非处！
- 证据不足 → 默认处女！
- 【事实性硬规则（最高优先级）】：
  1) 只采纳“已发生事实”的性关系/亲生孩子证据。
  2) 劝说/假设/未来场景（如“以后会恋爱结婚”“就算后来爱上别人”）不得当作已发生性关系。
  3) 伴侣存在本身不等于已发生性关系；若仅有假设性伴侣语句，仍应按证据不足处理。

输出 JSON：
{{
  "is_virgin": true/false,
  "virgin_status": "简短状态描述",
  "virgin_reason": "判断理由",
  "non_ml_sex_partners": [  // 与之发生性关系的非男主列表（如有）
    {{"name": "对方名", "evidence": "证据摘要"}}
  ]
}}"""

    user_prompt = f"""请判断角色【{name}】是否为处女：

【已知信息】：
- 是否有非男主男伴: {"否" if no_partner else "是"}
- 是否有非男主亲生孩子: {"否" if not has_biological_children else "是"}

【性关系/男伴信息】：
{virgin_str}

【处女暗示信息】（辅助判断）：
{hints_str}

请综合判断是否为处女，输出 JSON。"""

    try:
        data = _call_json_chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
        )
        llm_is_virgin = _to_bool(data.get("is_virgin", True), True)
        llm_virgin_status = str(data.get("virgin_status", "❓ 未知") or "❓ 未知")
        llm_virgin_reason = str(data.get("virgin_reason", "") or "")
        llm_non_ml_partners = data.get("non_ml_sex_partners", [])
        if not isinstance(llm_non_ml_partners, list):
            llm_non_ml_partners = []

        result = {
            "is_virgin": llm_is_virgin,
            "virgin_status": llm_virgin_status,
            "virgin_reason": llm_virgin_reason,
            "non_ml_sex_partners": llm_non_ml_partners,
        }

        # ===== 规则核验：不强制覆盖LLM，只做对照 =====
        has_non_ml_sex, non_ml_partners, has_male_lead_sex = _infer_non_male_lead_sex(sexual_relations, male_lead)
        rule_is_virgin = not has_non_ml_sex
        if rule_is_virgin:
            rule_virgin_status = "✅ 处女（仅男主）" if has_male_lead_sex else "✅ 处女"
            rule_virgin_reason = "规则判断：未发现非男主性关系，按“仅男主仍算处女”判定。"
            rule_non_ml_partners: List[Dict[str, str]] = []
        else:
            partner_names = []
            for p in non_ml_partners:
                if isinstance(p, dict):
                    pn = str(p.get("name", "") or "").strip()
                    if pn:
                        partner_names.append(pn)
            rule_virgin_status = f"❌ 非处（与非男主{','.join(partner_names)}有性关系）" if partner_names else "❌ 非处（与非男主有性关系）"
            rule_virgin_reason = "规则判断：发现与非男主存在性关系记录。"
            rule_non_ml_partners = non_ml_partners

        # 规则参考也考虑亲生孩子（仅对照显示，不覆盖 LLM 结论）
        if rule_is_virgin and has_biological_children and biological_children:
            rule_is_virgin = False
            fathers_str = ",".join(c.get("father", "未知男性") for c in biological_children)
            rule_virgin_status = f"❌ 非处（与非男主{fathers_str}有亲生孩子）"
            rule_virgin_reason = "规则参考：存在非男主亲生孩子。"
            rule_non_ml_partners = []

        conflict = (llm_is_virgin != rule_is_virgin)
        result.update({
            "llm_is_virgin": llm_is_virgin,
            "llm_virgin_status": llm_virgin_status,
            "llm_virgin_reason": llm_virgin_reason,
            "llm_non_ml_sex_partners": llm_non_ml_partners,
            "rule_is_virgin": rule_is_virgin,
            "rule_virgin_status": rule_virgin_status,
            "rule_virgin_reason": rule_virgin_reason,
            "rule_non_ml_sex_partners": rule_non_ml_partners,
            "virgin_judgement_conflict": conflict,
        })
        if conflict:
            llm_reason = llm_virgin_reason if llm_virgin_reason else "（无）"
            result["virgin_reason"] = (
                f"LLM与规则判断不一致。"
                f"LLM={llm_virgin_status}；规则={rule_virgin_status}。"
                f"LLM理由：{llm_reason}"
            )

        return result
    except Exception as e:
        logger.warning(f"处女LLM判断异常: {e}")
        return {
            "is_virgin": True,
            "virgin_status": "❓ 判断失败",
            "virgin_reason": f"LLM调用失败: {e}",
        }


def judge_purity_by_llm_stepwise(
    name: str,
    facts: Dict[str, Any],
    detail_evidence: Dict[str, Any],
    male_lead: str,
    raw_data_path: Optional[str] = None,
    heroine_names: Optional[List[str]] = None,
    chunks: Optional[List[str]] = None,
    chunk_manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    分步骤LLM五维纯洁度判断（替代原有混合判断）
    
    五个步骤顺序：
    Step 0: 孩子来源判断（最复杂，含母亲归属校验）
    Step 1: 是否有男伴（最简单）
    Step 2: 是否有非男主肉体接触
    Step 3: 精神纯洁度
    Step 4: 是否处女
    
    输入：
    - name: 女主名称
    - facts: 结构化事实（含 sexual_relations, children_info, physical_contacts, romantic_feelings, partner_relations）
    - detail_evidence: 详细证据（含 virginity_mentions, male_lead_intimacy, non_male_male_interactions 等）
    - male_lead: 男主名称
    - raw_data_path: raw_data.json 路径（用于回溯原文确定伴侣身份）
    - heroine_names: 全体女主名列表（用于母亲归属校验）
    - chunks: 预加载的 chunks（优先使用，避免重复读取）
    
    返回：
    - 五维洁度判定结果（与原有格式兼容，含前世/原故事线与接触等级补充）
    """
    # 先清洗事实
    facts = _sanitize_purity_facts_for_heroine(name, facts)
    # 纠正 is_male_lead 抽取错误（避免把男主当成非男主）
    facts = _normalize_is_male_lead_flags_in_facts(facts, male_lead)
    actual_manifest = chunk_manifest
    if actual_manifest is None and raw_data_path:
        try:
            actual_manifest = _get_novel_chunk_manifest(raw_data_path)
        except Exception as e:
            logger.warning(f"读取 chunk manifest 失败: {e}")

    # 从 children_info 推导隐含伴侣（在回溯之前注入，让回溯步骤验证）
    try:
        children_for_derive = facts.get("children_info", [])
        derived_partners = _derive_partners_from_children_info(
            children_for_derive, male_lead, facts.get("partner_relations", [])
        )
        if derived_partners:
            logger.info(f"  → 从 children_info 推导出 {len(derived_partners)} 条隐含伴侣关系")
            facts["partner_relations"] = facts.get("partner_relations", []) + derived_partners
    except Exception as e:
        logger.warning(f"从 children_info 推导伴侣失败: {e}")

    # 【新增】回溯原文确定伴侣身份（在清洗之前）
    try:
        partner_relations = facts.get("partner_relations", [])
        if partner_relations and raw_data_path:
            if not chunks:
                chunks = _get_novel_chunks(raw_data_path)
            if chunks:
                logger.info(f"  → 回溯原文确定【{name}】的伴侣身份...")
                facts["partner_relations"] = _resolve_vague_partners_by_context(
                    name,
                    partner_relations,
                    male_lead,
                    chunks,
                    raw_data_path=raw_data_path,
                    full_text=(actual_manifest or {}).get("full_text"),
                    chunk_entries=(actual_manifest or {}).get("chunks"),
                )
    except Exception as e:
        logger.warning(f"回溯原文确定伴侣身份失败: {e}")
    
    # 清洗"某个男人/他"等泛指 partner_relations，避免误判为"另一个男人"
    try:
        facts["partner_relations"] = _sanitize_partner_relations_for_purity(facts.get("partner_relations", []), male_lead)
    except Exception:
        pass
    
    # 提取各类信息
    children_info = facts.get("children_info", [])
    sexual_relations = facts.get("sexual_relations", [])
    physical_contacts = facts.get("physical_contacts", [])
    romantic_feelings = facts.get("romantic_feelings", [])
    partner_relations = facts.get("partner_relations", [])
    
    # 从 detail_evidence 提取辅助信息
    virginity_mentions = detail_evidence.get("virginity_mentions", [])
    male_lead_intimacy = detail_evidence.get("male_lead_intimacy", [])
    non_male_male_interactions = detail_evidence.get("non_male_male_interactions", [])
    
    # Step 0: 孩子来源判断（含母亲归属校验）
    logger.info(f"  → Step 0: 判断【{name}】孩子来源...")
    child_result = _llm_judge_child_origin(
        name, children_info, virginity_mentions, male_lead_intimacy, male_lead,
        heroine_names=heroine_names,
        raw_data_path=raw_data_path,
        chunks=chunks,
        chunk_manifest=actual_manifest,
    )
    has_bio_children = child_result.get("has_biological_children", False)
    bio_children = child_result.get("biological_children", [])
    
    # Step 1: 是否有男伴（同时分析每条伴侣关系的 forced 和 has_feelings）
    logger.info(f"  → Step 1: 判断【{name}】是否有男伴（逐条分析伴侣关系）...")
    partner_result = _llm_judge_partner(
        name,
        romantic_feelings,
        partner_relations,
        non_male_male_interactions,
        male_lead,
        female_role_names=heroine_names,
        has_bio_children=has_bio_children,
        bio_children=bio_children,
    )
    no_partner = partner_result.get("no_partner", True)
    partner_list = partner_result.get("partner_list", [])
    analyzed_partners = partner_result.get("analyzed_partners", [])  # 已分析的伴侣关系
    
    # Step 2: 是否有非男主肉体接触
    logger.info(f"  → Step 2: 判断【{name}】是否有非男主接触...")
    contact_result = _llm_judge_contact(
        name, physical_contacts, non_male_male_interactions,
        has_bio_children, bio_children, male_lead,
        female_role_names=heroine_names,
    )
    has_contact = contact_result.get("has_other_contact", False)
    
    # Step 3: 精神纯洁度（使用 Step 1 已分析的伴侣关系，避免重复调用 LLM）
    logger.info(f"  → Step 3: 判断【{name}】精神纯洁度...")
    spirit_result = _llm_judge_spirit(
        name, no_partner, partner_list,
        has_bio_children, bio_children,
        romantic_feelings, male_lead,
        analyzed_partners=analyzed_partners,  # 传入已分析的伴侣关系
        all_partner_relations=partner_relations,  # 传入完整伴侣关系（含同性）供精神维度判断
        sexual_relations=sexual_relations,  # 传入性关系用于豁免判定（避免未遂/无性行为受孕误判）
    )
    is_spirit_clean = spirit_result.get("is_spirit_clean", True)
    partner_exempted_for_clean = _to_bool(spirit_result.get("partner_exempted_for_clean", False), False)
    partner_exemption_notes = spirit_result.get("partner_exemption_notes", [])
    if not isinstance(partner_exemption_notes, list):
        partner_exemption_notes = []
    partner_exemption_reason = str(spirit_result.get("partner_exemption_reason", "") or "")
    
    # Step 4: 是否处女
    logger.info(f"  → Step 4: 判断【{name}】是否处女...")
    virgin_result = _llm_judge_virgin(
        name, no_partner, partner_list,
        has_bio_children, bio_children,
        sexual_relations, virginity_mentions, male_lead_intimacy, male_lead
    )
    is_virgin = virgin_result.get("is_virgin", True)

    # 规则基线判定（用于与 LLM 对照展示）
    rule_baseline = judge_purity_by_facts(name, facts, male_lead, female_role_names=heroine_names)
    
    # LLM vs 规则（接触/男伴/精神）
    llm_has_other_contact = _to_bool(contact_result.get("has_other_contact", has_contact), False)
    llm_contact_status = contact_result.get("contact_status", "❓ 未知")
    llm_contact_reason = str(contact_result.get("contact_reason", "") or "")
    rule_has_other_contact = _to_bool(rule_baseline.get("has_other_contact", False), False)
    rule_contact_status = rule_baseline.get("contact_status", "❓ 未知")
    rule_contact_reason = str(rule_baseline.get("rule_contact_reason", rule_baseline.get("summary", "")) or "")
    contact_judgement_conflict = (llm_has_other_contact != rule_has_other_contact)

    llm_no_partner = _to_bool(partner_result.get("no_partner", no_partner), True)
    llm_partner_status = partner_result.get("partner_status", "❓ 未知")
    llm_partner_reason = str(partner_result.get("partner_reason", "") or "")
    rule_no_partner = _to_bool(rule_baseline.get("no_partner", True), True)
    rule_partner_status = rule_baseline.get("partner_status", "❓ 未知")
    rule_partner_reason = str(rule_baseline.get("rule_partner_reason", rule_baseline.get("summary", "")) or "")
    partner_judgement_conflict = (llm_no_partner != rule_no_partner)

    llm_is_spirit_clean = _to_bool(spirit_result.get("is_spirit_clean", is_spirit_clean), True)
    llm_spirit_status = spirit_result.get("spirit_status", "❓ 未知")
    llm_spirit_reason = str(spirit_result.get("spirit_reason", "") or "")
    rule_is_spirit_clean = _to_bool(rule_baseline.get("is_spirit_clean", True), True)
    rule_spirit_status = rule_baseline.get("spirit_status", "❓ 未知")
    rule_spirit_reason = str(rule_baseline.get("rule_spirit_reason", rule_baseline.get("summary", "")) or "")
    spirit_judgement_conflict = (llm_is_spirit_clean != rule_is_spirit_clean)
    
    # 综合结果：有男伴维度保留事实判定，但被迫/无感情等 partner 豁免可用于最终洁度。
    is_clean = is_virgin and not has_contact and (no_partner or partner_exempted_for_clean) and is_spirit_clean
    past_life = _derive_past_life_cleanliness(
        facts,
        "; ".join([
            virgin_result.get("virgin_reason", ""),
            contact_result.get("contact_reason", ""),
            partner_result.get("partner_reason", ""),
            spirit_result.get("spirit_reason", ""),
        ]),
    )
    contact_level = _derive_contact_level(facts, male_lead, non_male_male_interactions)
    
    result = {
        "name": name,
        "is_virgin": is_virgin,
        "virgin_status": virgin_result.get("virgin_status", "❓ 未知"),
        "llm_is_virgin": virgin_result.get("llm_is_virgin", is_virgin),
        "llm_virgin_status": virgin_result.get("llm_virgin_status", virgin_result.get("virgin_status", "❓ 未知")),
        "llm_virgin_reason": virgin_result.get("llm_virgin_reason", virgin_result.get("virgin_reason", "")),
        "rule_is_virgin": virgin_result.get("rule_is_virgin"),
        "rule_virgin_status": virgin_result.get("rule_virgin_status"),
        "rule_virgin_reason": virgin_result.get("rule_virgin_reason", ""),
        "virgin_judgement_conflict": _to_bool(virgin_result.get("virgin_judgement_conflict", False), False),
        "llm_has_other_contact": llm_has_other_contact,
        "llm_contact_status": llm_contact_status,
        "llm_contact_reason": llm_contact_reason,
        "rule_has_other_contact": rule_has_other_contact,
        "rule_contact_status": rule_contact_status,
        "rule_contact_reason": rule_contact_reason,
        "contact_judgement_conflict": contact_judgement_conflict,
        "has_other_contact": has_contact,
        "contact_status": contact_result.get("contact_status", "❓ 未知"),
        "llm_no_partner": llm_no_partner,
        "llm_partner_status": llm_partner_status,
        "llm_partner_reason": llm_partner_reason,
        "rule_no_partner": rule_no_partner,
        "rule_partner_status": rule_partner_status,
        "rule_partner_reason": rule_partner_reason,
        "partner_judgement_conflict": partner_judgement_conflict,
        "no_partner": no_partner,
        "partner_status": partner_result.get("partner_status", "❓ 未知"),
        "llm_is_spirit_clean": llm_is_spirit_clean,
        "llm_spirit_status": llm_spirit_status,
        "llm_spirit_reason": llm_spirit_reason,
        "rule_is_spirit_clean": rule_is_spirit_clean,
        "rule_spirit_status": rule_spirit_status,
        "rule_spirit_reason": rule_spirit_reason,
        "spirit_judgement_conflict": spirit_judgement_conflict,
        "is_spirit_clean": is_spirit_clean,
        "spirit_status": spirit_result.get("spirit_status", "❓ 未知"),
        "partner_exempted_for_clean": partner_exempted_for_clean,
        "partner_exemption_notes": partner_exemption_notes,
        "partner_exemption_reason": partner_exemption_reason,
        "past_life_clean": past_life.get("past_life_clean"),
        "past_life_severity": past_life.get("past_life_severity"),
        "past_life_severity_label": past_life.get("past_life_severity_label"),
        "past_life_status": past_life.get("past_life_status"),
        "past_life_reason": past_life.get("past_life_reason"),
        "contact_level": contact_level.get("contact_level"),
        "contact_level_label": contact_level.get("contact_level_label"),
        "contact_level_reason": contact_level.get("contact_level_reason"),
        "is_clean": is_clean,
        "summary": "; ".join([
            virgin_result.get("virgin_reason", ""),
            contact_result.get("contact_reason", ""),
            partner_result.get("partner_reason", ""),
            spirit_result.get("spirit_reason", ""),
        ])[:],
        "body_status": f"处女:{virgin_result.get('virgin_status', '?')} | 接触:{contact_result.get('contact_status', '?')} | 男伴:{partner_result.get('partner_status', '?')}",
        "verification": {
            "method": "llm_stepwise_5steps",
            "llm_agreed": True,
            "rounds": 5,
            "step_results": {
                "child_origin": child_result,
                "partner": partner_result,
                "contact": contact_result,
                "spirit": spirit_result,
                "virgin": virgin_result,
            },
            "partner_exempted_for_clean": partner_exempted_for_clean,
            "partner_exemption_notes": partner_exemption_notes,
        },
    }
    
    return _normalize_purity_result_consistency(result)


# --- 4. 保留原 LLM 版本作为备用 ---
def judge_character_purity_llm(name, evidence_list, male_lead):
    """
    【逻辑重构 V17.0】五维洁度判定版
    
    判定维度：
    1. 是否处女（is_virgin）：是否仅与男主/从未与任何人发生过性关系
    2. 有无非男主肉体接触（has_other_contact）：是否被非男主男性接触过（猥亵/爱抚/亲吻等）
    3. 是否从无男伴（no_partner）：是否从未有过非男主的男性伴侣（男朋友/丈夫/恋人等）
    4. 精神洁（is_spirit_clean）：是否从未爱过非男主对象（不限性别）
    
    核心定义：
    - 处女：仅与男主发生关系或从未发生关系 = ✅
    - 非处：与非男主男性发生过性关系 = ❌
    - 无接触：从未被非男主男性亲密接触 = ✅
    - 有接触：被非男主猥亵/接吻/爱抚等 = ❌
    - 无男伴：从未有过非男主的男性伴侣 = ✅
    - 有男伴：有过前男友/前夫/前恋人等 = ❌
    """
    evidence_text = _compact_llm_lines(
        [f"- {_clip_llm_text(e, 240)}" for e in evidence_list],
        label="综合证据",
        empty="（无其他证据）",
    )

    system_prompt = f"""你是一个严格的小说角色鉴赏专家，需结合"规则+证据"判定女性角色【{name}】的肉体与精神洁度。

【最重要原则 - 无罪推定 + 禁止幻觉】：
★ 无罪推定：证据不足/没有明确提到 = 必须判 ✅！绝对不能因为"可能有""应该有"而判❌！
★ 禁止幻觉：你只能根据【下方提供的证据原文】判断，禁止编造/臆测/脑补！
★ 默认规则：
  - 没有提到被非男主接触 → 默认 ✅ 无接触
  - 没有提到有前男友/前夫 → 默认 ✅ 无男伴
  - 没有提到爱过别人 → 默认 ✅ 精神洁
★ 判❌的唯一条件：证据中有【明确的、具体的、直接的】描述！
★ 仔细阅读！"向男主""给男主""属于男主" = 与男主相关 = ✅！不是"非男主"！

【男主角】：{male_lead} (绝对豁免：与男主的任何关系都不视为不洁)

【肉体洁度双维度判定】：
本次判定将肉体洁度拆分为两个独立维度：

★ 维度1：是否处女（is_virgin）
  【核心原则】：此处"处女"指"排他性处女"，即是否只属于男主。与男主的任何关系都视为"洁"！
  
  - ✅ 处女（最重要！）：仅与男主【{male_lead}】发生过性关系 → 必须判定为 ✅ 处女，绝对不能判为非处！
  - ✅ 处女：从未与任何人发生性关系
  - ✅ 处女（特殊豁免）：有名无实/未圆房/假结婚的人妻（未与非男主发生关系）
  - ✅ 处女（处女妈妈豁免）：以下情况有孩子但仍视为处女：
    · 收养/领养的孩子
    · 单性繁殖/无性繁殖/孤雌生殖
    · 特殊功法/法术/能力诞生的孩子（不需要男性）
    · 人工受孕/试管婴儿/魔法造物
    · 神明赐予/天降/异界转生来的孩子
    · 分裂/克隆/复制产生的后代
    · 怀了男主的孩子 → ✅ 处女
  - ❌ 非处：与【非男主】男性发生过性关系（包括被强奸/迷奸/轮奸）
  - ❌ 非处：有【非男主】的孩子，且是通过正常性行为孕育的
  
  【严禁错误】：不得因为"与男主发生关系"而判定为非处！与男主的关系 = ✅ 处女！
  - 证据不足时默认处女

★ 维度2：有无非男主肉体接触（has_other_contact）
  【核心原则】：此维度仅判定是否被【非男主】男性【实际触碰身体】。与男主的任何接触都不算！
  
  - ✅ 无接触：从未被非男主男性实际触碰身体
  - ✅ 无接触：仅被男主【{male_lead}】接触 → 必须判定为 ✅ 无接触！
  - ✅ 无接触：仅被围观/注视/议论/目光凝视 → 不算肉体接触，判定为 ✅ 无接触
  - ❌ 有接触：被【非男主】男性实际触碰身体（猥亵/深吻/爱抚/舌吻/摸胸/强抱等）
  - 注意：被强奸等性行为应同时判定为"非处"和"有接触"
  - 注意：处女妈妈豁免情况下，若孩子来源不涉及肉体接触，则无接触
  
  【严禁错误】：
  - 被男主接触 → 不能判为"有接触"！
  - 被围观/注视/目光 → 不能判为"有接触"！只有实际身体触碰才算！
  - 证据中没有明确描述被非男主触碰 → 必须判 ✅ 无接触！
  - "作为礼物给男主""向男主宣誓" → 这是给男主的，不是非男主！判 ✅ 无接触！
  
  【默认规则】：证据不足/没提到/不确定 → 一律判 ✅ 无接触！绝不能"默认有接触"！

★ 维度3：是否从无男伴（no_partner）
  - ✅ 无男伴：从未在名义上属于过非男主的男性
  - ✅ 无男伴："作为礼物给男主""向男主宣誓""属于男主" → 这是属于男主，不算有非男主男伴！
  - ✅ 无男伴（豁免情况）：
    · 单方面被追求/表白但从未接受
    · 被迫订婚/包办婚姻但最终未完婚（婚约被解除/逃婚/对方死亡等）
  - ❌ 有男伴：曾有过【非男主】的前男友/前夫/前恋人等正式交往关系
  - ❌ 有男伴：与【非男主】已完婚的婚姻，无论有名无实/未圆房/假结婚/政治联姻
  
  【严禁错误】：
  - "给男主""向男主""属于男主" → 这是男主，不是非男主！判 ✅ 无男伴！
  - 证据中没有明确提到有非男主的男友/丈夫 → 必须判 ✅ 无男伴！
  
  【默认规则】：证据不足/没提到/不确定 → 一律判 ✅ 无男伴！

【精神洁度判定】：
- ✅ 精神洁：从未爱过男主以外的对象（不限性别）
- ✅ 精神洁（特殊豁免）：孩子通过以下方式诞生不影响精神洁度：
  · 收养/领养
  · 单性繁殖/无性繁殖/特殊功法
  · 人工受孕/魔法造物/神明赐予
  · 分裂/克隆/异界转生
- ❌ 精神非初：真心爱过男主以外的对象（不限性别，含百合/同性）
- ❌ 精神非初：与非男主男性通过正常性行为生下孩子（生育通常伴随感情）
- 被迫联姻/厌恶对方/被强迫但无感情投入 → 不算精神非初
- 证据不足时默认精神洁

【证据优先级】：
- 【禁止幻觉】：只能引用证据中的原文，绝对不能编造证据中没有的情节（如编造"前夫""被猥亵"等）！
- 无证据=无罪：如果证据中没有明确提到某事，必须判定为✅洁！
- 否定句优先：如"未提及前男友/未发生关系"可证明洁
- 身份≠事实：仅因"妈妈/人妻"身份不得直接判脏，必须有证据中的明确描述
- 处女妈妈优先：有孩子时，优先检查是否为特殊诞生方式（收养/单性/功法/魔法等）
- 历史不可逆：若有明确他男实质关系（通过正常性行为），即便后面怀男主孩子，仍判非处

输出 JSON：
{{
  "is_virgin": true/false,           // 是否处女（仅与男主或从未发生关系=true，与非男主发生过=false）
  "virgin_status": "简短结论",        // 处女状态说明，如"✅ 处女""✅ 处女（仅与男主）""❌ 被非男主强奸致非处"
                                      // 【注意】仅与男主发生关系必须写"✅ 处女（仅与男主）"，不能写"❌ 非处"！
  "has_other_contact": true/false,   // 是否被【非男主】男性【实际触碰身体】（被男主接触=false，被围观=false）
  "contact_status": "简短结论",       // 如"✅ 无接触""✅ 无接触（仅与男主）""❌ 被非男主猥亵"
                                      // 【注意】被男主接触/被围观注视 → 必须写"✅ 无接触"，不能写"❌ 有接触"！
  "no_partner": true/false,          // 是否从未名义上属于过非男主男性
  "partner_status": "简短结论",       // 男伴状态说明，如"✅ 无男伴""❌ 有前夫（政治联姻）"
  "is_spirit_clean": true/false,     // 精神是否洁
  "spirit_status": "简短结论",        // 精神状态说明
  "past_life_clean": true/false/null, // 前世/原故事线洁度；无相关线索用 null
  "past_life_severity": "none/clean/romantic/partner/sexual/forced/severe", // 前世/原故事线风险等级
  "past_life_severity_label": "简短等级说明",
  "past_life_status": "简短结论",      // 如"未见前世线索""前世有前夫风险"
  "past_life_reason": "简短理由",      // 单独说明前世/原故事线/轮回/穿越前婚恋或接触线索
  "contact_level": "L0/L1/L2/L3/L4/L5", // 非男主接触等级，无接触为L0
  "contact_level_label": "简短等级说明",
  "contact_level_reason": "简短证据理由",
  "summary": "50字内理由，必须引用证据原文！禁止编造证据中没有的内容！",
  "is_clean": true/false             // 处女 + 无接触 + 无男伴（或partner豁免） + 精神洁才为 true
}}

【前世洁度补充】：
- 如果证据出现前世、原故事线、原著线、上一世、轮回、重生前、穿越前等线索，必须单独填写 past_life_*。
- 前世/原故事线风险不自动等同当前线非处，但必须在 past_life_status/past_life_reason 单独提示。
- 没有前世/原故事线线索时，past_life_clean=null，past_life_status="未见前世/原故事线洁度线索"。

【再次强调】：禁止无中生有！如果证据中没有提到"前夫/前男友/被猥亵/被强吻"等，就不能判定存在这些情况！"""

    user_prompt = f"""请鉴定角色：【{name}】

【综合证据（含scan与detail补充）】：
{evidence_text}
"""

    last_err = None
    for attempt in range(3):
        try:
            result = _call_json_chat_completion(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.1,
            )
            result["name"] = name
            # 兼容旧字段：生成 body_status 供报告使用
            virgin_status = result.get("virgin_status", "❓ 未知")
            contact_status = result.get("contact_status", "❓ 未知")
            partner_status = result.get("partner_status", "❓ 未知")
            result["body_status"] = f"处女:{virgin_status} | 接触:{contact_status} | 男伴:{partner_status}"
            if "past_life_clean" not in result:
                result.update(_derive_past_life_cleanliness({}, result.get("summary", "")))
            if "contact_level" not in result:
                result.update(_derive_contact_level({}, male_lead))
            return result
        except Exception as e:
            last_err = e
            if attempt < 2:
                logger.warning(f"女主身心鉴定失败（{attempt+1}/3）：{e}，30秒后重试...")
                time.sleep(30)
            else:
                logger.error(f"女主身心鉴定失败（{attempt+1}/3，已放弃）：{e}")
    return {
        "name": name,
        "is_virgin": True,
        "virgin_status": "❓ 未知",
        "has_other_contact": False,
        "contact_status": "❓ 未知",
        "no_partner": True,
        "partner_status": "❓ 未知",
        "is_spirit_clean": True,
        "spirit_status": "❓ 未知",
        "past_life_clean": None,
        "past_life_severity": "none",
        "past_life_severity_label": "无前世/原故事线线索",
        "past_life_status": "未见前世/原故事线洁度线索",
        "past_life_reason": "API调用失败，未能判断前世/原故事线洁度。",
        "contact_level": "L0",
        "contact_level_label": "无非男主接触事实",
        "contact_level_reason": "API调用失败，未能派生接触等级。",
        "body_status": "❓ 未知",
        "summary": f"API调用失败: {last_err}",
        "is_clean": True
    }

def _load_scan_purity_map(heroine_status_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """从 scan 的 heroine_status 构建初/处判定映射"""
    purity_map: Dict[str, Dict[str, Any]] = {}
    for status in heroine_status_list:
        name = (status.get("name") or "").strip()
        if not name:
            continue
        purity_map[name] = {
            "is_chu_body": status.get("is_chu_body"),
            "is_chu_spirit": status.get("is_chu_spirit"),
            "evidence": status.get("evidence"),
            "chunk_index": status.get("chunk_index"),
        }
    return purity_map


def _load_detail_evidence(char_file_path: str) -> Dict[str, Dict[str, Any]]:
    """从 protagonist 产出的 *_detailed_*.json 读取补充证据（含新版结构化事实）"""
    if not char_file_path or not os.path.exists(char_file_path):
        return {}
    try:
        with open(char_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    evid_map: Dict[str, Dict[str, Any]] = {}
    afc = data.get("all_female_characters", {})
    for name, info in afc.items():
        evid_map[name] = {
            # 新版结构化事实
            "purity_facts": _normalize_purity_fact_bucket(info.get("purity_facts", {})),
            # 旧版兼容字段
            "non_male_male_interactions": info.get("non_male_male_interactions", []),
            "male_lead_intimacy": info.get("male_lead_intimacy", []),
            "virginity_mentions": info.get("virginity_mentions", []),
            "children_info": info.get("children_info", []),
        }
    return evid_map


def _load_scan_facts(raw_data_path: str) -> Dict[str, Dict[str, Any]]:
    """
    【已废弃】从 scan 的 raw_data.json 读取结构化事实
    
    由于 novel_scan.py 的 _append_to_detail_file 已做别名归一化写入 *_detailed_*.json，
    不再需要从 raw_data.json 重复读取。此函数保留仅供兼容/调试用途。
    """
    if not raw_data_path or not os.path.exists(raw_data_path):
        return {}
    try:
        with open(raw_data_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    facts_map: Dict[str, Dict[str, Any]] = {}
    heroine_facts = data.get("heroine_facts", [])
    for item in heroine_facts:
        name = item.get("name", "").strip()
        if not name:
            continue
        facts = item.get("facts", {})
        if name not in facts_map:
            facts_map[name] = _empty_purity_fact_bucket()
        # 合并事实
        for key in _FACT_DIMENSIONS:
            facts_map[name][key].extend(facts.get(key, []))
    return facts_map


def _match_heroine_key(name: str, evid_map: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """尽量用模糊包含匹配 heroine 名"""
    if name in evid_map:
        return name
    for k in evid_map.keys():
        if name in k or k in name:
            return k
    return None


def aggregate_and_judge_heroines(heroine_status_list, male_lead, detail_evidences, raw_data_path, executor=None, female_leads=None):
    """
    从 *_detailed_*.json 读取结构化事实，基于规则判定 + LLM 校验身心洁度
    
    【优化说明】：
    - novel_scan.py 已在写入 *_detailed_*.json 时做了别名归一化
    - 因此不再需要从 raw_data.json 重复读取，直接使用 detail_evidences 即可
    - raw_data_path 参数保留但不再使用（保持接口兼容）
    
    executor 参数用于并行 LLM 校验
    """
    female_leads = female_leads or []

    # 目标名单：做“别名归一化”，避免同一角色（短名/全名/别名）被输出成多条
    # 规则：
    # - 若提供 female_leads：以 female_leads 作为规范输出名（canonical）
    # - 否则：对所有候选名做包含式分组，优先用“更长/更完整”的名字做 canonical（如 黑崎十六夜 覆盖 十六夜）

    def _build_canonical_map(candidates: List[str]) -> Dict[str, str]:
        cands = [c.strip() for c in candidates if isinstance(c, str) and c.strip()]
        # 去重但保序
        seen = set()
        cands2 = []
        for c in cands:
            if c not in seen:
                seen.add(c)
                cands2.append(c)
        # 长名优先作为 canonical
        ordered = sorted(cands2, key=lambda x: (len(x), x), reverse=True)
        alias_to_canon: Dict[str, str] = {}
        for name in ordered:
            if name in alias_to_canon:
                continue
            # 默认自身为 canonical
            alias_to_canon[name] = name
            # 吸附所有被包含/包含关系的名字到该 canonical
            for other in ordered:
                if other in alias_to_canon:
                    continue
                if (other in name) or (name in other):
                    alias_to_canon[other] = name
        # 兜底：未覆盖的自己指向自己
        for c in cands2:
            alias_to_canon.setdefault(c, c)
        return alias_to_canon

    if female_leads:
        canonical_names = [x for x in female_leads if isinstance(x, str) and x.strip()]
        alias_to_canon = {c: c for c in canonical_names}  # 先自映射
        # 将 detail 中与 canonical 有包含关系的 key 归并到 canonical
        for c in canonical_names:
            for k in list(detail_evidences.keys()):
                if not isinstance(k, str) or not k.strip():
                    continue
                if (c in k) or (k in c):
                    alias_to_canon[k] = c
        heroine_names: set[str] = set(canonical_names)
    else:
        all_candidates: List[str] = []
        all_candidates.extend([s.get("name", "").strip() for s in heroine_status_list if s.get("name")])
        all_candidates.extend(list(detail_evidences.keys()))
        alias_to_canon = _build_canonical_map(all_candidates)
        heroine_names = set(alias_to_canon.values())

    # 预先合并每个女主的事实 + 辅助证据（从其所有别名/模糊key汇总）
    heroine_merged_facts = {}
    heroine_detail_evidences = {}  # 保存辅助证据（virginity_mentions, male_lead_intimacy 等）
    for name in heroine_names:
        # 收集该 canonical 对应的 detail keys（包括别名、包含匹配）
        detail_keys_to_merge = []
        for k in detail_evidences.keys():
            if alias_to_canon.get(k) == name or (name in k) or (k in name):
                detail_keys_to_merge.append(k)

        # 合并事实（从 detail_facts.purity_facts）
        merged_facts = _empty_purity_fact_bucket()

        # 从 detail_facts.purity_facts 合并（可能多个别名key）
        for dk in detail_keys_to_merge:
            detail_facts = detail_evidences.get(dk, {}) or {}
            purity_facts = detail_facts.get("purity_facts", {}) or {}
            for fact_key in _FACT_DIMENSIONS:
                merged_facts[fact_key].extend(purity_facts.get(fact_key, []))
        
        # 去重
        merged_facts = _dedupe_fact_bucket_by_evidence(merged_facts)
        
        heroine_merged_facts[name] = merged_facts
        
        # 保存辅助证据（用于分步骤LLM判断）
        merged_detail_ev = {
            "virginity_mentions": [],
            "male_lead_intimacy": [],
            "non_male_male_interactions": [],
            "children_info": [],
        }
        for dk in detail_keys_to_merge:
            df = detail_evidences.get(dk, {}) or {}
            for k in merged_detail_ev.keys():
                vv = df.get(k, [])
                if isinstance(vv, list):
                    merged_detail_ev[k].extend(vv)
        # 去重（保序）
        for k in merged_detail_ev.keys():
            seen = set()
            uniq = []
            for item in merged_detail_ev[k]:
                s = str(item)
                if s in seen:
                    continue
                seen.add(s)
                uniq.append(item)
            merged_detail_ev[k] = uniq
        heroine_detail_evidences[name] = merged_detail_ev

    results = {}
    print(f"❤️ 正在对 {len(heroine_names)} 位女性角色进行身心洁度鉴定（分步骤LLM五维判断）...")
    
    # 【新增】预加载 chunk manifest / chunks（避免每个女主重复读取）
    chunk_manifest: Optional[Dict[str, Any]] = None
    chunks: Optional[List[str]] = None
    if raw_data_path:
        try:
            chunk_manifest = _get_novel_chunk_manifest(raw_data_path)
            chunks = _get_novel_chunks(raw_data_path)
            if chunks:
                logger.info(f"  预加载 chunk manifest 成功，共 {len(chunks)} 块")
        except Exception as e:
            logger.warning(f"  预加载 chunk manifest 失败: {e}")
    
    # 【新增】pending_injections：存储从其他女主移入的孩子记录
    # key = 目标女主名，value = 待注入的 children 记录列表
    from collections import defaultdict
    pending_injections: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    
    # 将 heroine_names 转为列表（供传递给子函数）
    heroine_names_list = list(heroine_names)
    
    # 默认无事实结果
    def _default_clean_result(name: str) -> Dict[str, Any]:
        return {
            "name": name,
            "is_virgin": True,
            "virgin_status": "✅ 处女（无相关事实记录）",
            "has_other_contact": False,
            "contact_status": "✅ 无接触（无相关事实记录）",
            "no_partner": True,
            "partner_status": "✅ 无男伴（无相关事实记录）",
            "is_spirit_clean": True,
            "spirit_status": "✅ 精神洁（无相关事实记录）",
            "summary": "无不洁事实记录，默认全初",
            "is_clean": True,
            "body_status": "✅ 全初（无相关事实记录）",
            "verification": {"method": "no_facts_default_clean", "llm_agreed": True, "rounds": 0},
        }
    
    # 【新增】串行处理函数（包含 pending_injections 注入逻辑）
    def _process_heroine_with_injection(
        name: str,
        merged_facts: Dict[str, Any],
        detail_ev: Dict[str, Any],
    ) -> Dict[str, Any]:
        # 注入从其他女主移入的孩子记录
        injected_children = pending_injections.get(name, [])
        if injected_children:
            logger.info(f"  【注入】{name} 接收到 {len(injected_children)} 条从其他女主移入的孩子记录")
            if "children_info" not in merged_facts:
                merged_facts["children_info"] = []
            merged_facts["children_info"] = injected_children + merged_facts["children_info"]
        
        has_facts = any(merged_facts.get(k) for k in merged_facts)
        if not has_facts:
            return _default_clean_result(name)
        
        res = judge_purity_by_llm_stepwise(
            name, merged_facts, detail_ev, male_lead, raw_data_path,
            heroine_names=heroine_names_list,
            chunks=chunks,
            chunk_manifest=chunk_manifest,
        )
        
        # 收集该女主处理后的 moved_in_by_mother（供后续女主使用）
        verification = res.get("verification", {})
        step_results = verification.get("step_results", {})
        child_origin = step_results.get("child_origin", {})
        moved_in = child_origin.get("owner_moved_in_by_mother", {})
        if moved_in:
            for target_mother, records in moved_in.items():
                if target_mother and records:
                    pending_injections[target_mother].extend(records)
                    logger.info(f"  【移出】{name} 的 {len(records)} 条孩子记录移至 {target_mother}")
        
        return res
    
    # 使用线程池并行处理（注意：并行模式下 pending_injections 无法跨女主实时注入）
    if executor:
        futures = {}
        for name in heroine_names:
            merged_facts = heroine_merged_facts[name]
            detail_ev = heroine_detail_evidences.get(name, {})
            
            # 检查是否有结构化事实
            has_facts = any(merged_facts.get(k) for k in merged_facts)
            
            if has_facts:
                # 有事实：提交到线程池进行分步骤LLM判断
                # 【修改】传入 heroine_names 和 chunks
                futures[executor.submit(
                    judge_purity_by_llm_stepwise, 
                    name, merged_facts, detail_ev, male_lead, raw_data_path,
                    heroine_names_list, chunks, chunk_manifest
                )] = name
            else:
                # 无事实：直接默认全初
                results[name] = _default_clean_result(name)
        
        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="鉴定进度"):
            name = futures[future]
            try:
                res = future.result()
                results[name] = res
                
                # 【新增】收集 moved_in_by_mother（并行模式下仅收集，不实时注入）
                verification = res.get("verification", {})
                step_results = verification.get("step_results", {})
                child_origin = step_results.get("child_origin", {})
                moved_in = child_origin.get("owner_moved_in_by_mother", {})
                if moved_in:
                    for target_mother, records in moved_in.items():
                        if target_mother and records:
                            pending_injections[target_mother].extend(records)
            except Exception as e:
                logger.warning(f"角色 {name} 判定失败: {e}")
                results[name] = {
                    "name": name,
                    "is_virgin": True,
                    "virgin_status": "❓ 判定失败",
                    "has_other_contact": False,
                    "contact_status": "❓ 判定失败",
                    "no_partner": True,
                    "partner_status": "❓ 判定失败",
                    "is_spirit_clean": True,
                    "spirit_status": "❓ 判定失败",
                    "summary": f"判定失败: {e}",
                    "is_clean": True,
                    "body_status": "❓ 判定失败",
                }
        
        # 【新增】并行模式下的二次处理：处理接收到移入记录的女主
        if pending_injections:
            logger.info(f"  【二次处理】检查是否有女主需要重新判断（接收到移入的孩子记录）...")
            for target_name, injected_records in pending_injections.items():
                if not injected_records:
                    continue
                if target_name not in heroine_names:
                    logger.warning(f"  【跳过】{target_name} 不在女主列表中")
                    continue
                # 检查该女主是否已经处理过且需要重新判断
                # 注意：这里简化处理，仅在日志中记录移入信息
                # 如需完整实现，可以在此处重新调用 judge_purity_by_llm_stepwise
                logger.info(f"  【待处理】{target_name} 接收到 {len(injected_records)} 条移入记录（可在下次运行时处理）")
    else:
        # 无线程池，串行处理（支持实时注入）
        for name in tqdm(heroine_names, desc="鉴定进度"):
            merged_facts = dict(heroine_merged_facts[name])  # 复制避免修改原数据
            detail_ev = heroine_detail_evidences.get(name, {})
            
            res = _process_heroine_with_injection(name, merged_facts, detail_ev)
            results[name] = res

    return results

def main(novel_path=None, book_name=None, run_id=None, detail_path=None, raw_data_path=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-data", help="指定 raw_data.json 路径")
    parser.add_argument("--results-dir", help="自定义扫描结果根目录（默认 ./results）")
    parse_cli_args = novel_path is None and book_name is None and run_id is None
    args = parser.parse_args() if parse_cli_args else parser.parse_args([])

    if novel_path:
        os.environ["NOVEL_PATH"] = novel_path

    if args.results_dir:
        scan_dir = args.results_dir
    else:
        scan_dir = SCAN_RESULTS_DIR

    print("="*60)
    print("⚖️  小说毒点二审法官 (全AI裁决版)")
    print("="*60)

    raw_data_path = raw_data_path or args.raw_data or find_latest_scan_data(scan_dir)
    if not raw_data_path:
        print(f"❌ 未找到 raw_data.json，请确认目录: {scan_dir}")
        return
    if not os.path.exists(raw_data_path):
        print(f"❌ 指定的 raw_data 不存在: {raw_data_path}")
        return

    resolved_book_name = (book_name or "").strip()
    if not resolved_book_name:
        novel_path_for_book = novel_path or os.environ.get("NOVEL_PATH", "")
        if novel_path_for_book:
            resolved_book_name = os.path.splitext(os.path.basename(novel_path_for_book))[0].strip()
    if not resolved_book_name:
        results_dir_name = os.path.basename(os.path.dirname(raw_data_path))
        resolved_book_name = results_dir_name.split("_scan_")[0].strip() or "unknown_book"
    init_token_tracker(
        resolved_book_name,
        run_id=run_id,
        out_path=os.path.join(BASE_DIR, "results", "token_usage.json"),
    )

    # 初始化日志到当前扫描目录
    log_path = os.path.join(os.path.dirname(raw_data_path), "reviewer.log")
    configure_rotating_file_logger(logger, log_path)
    logger.info(f"日志文件: {log_path}")

    with open(raw_data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    _validate_raw_data_matches_novel(data, raw_data_path, novel_path, book_name)

    char_file_path = find_character_data(raw_data_path, detail_path=detail_path)
    male_lead, female_leads = extract_roles(char_file_path)
    print(f"👨‍🦰 男主锁定: 【{male_lead}】")

    issues = data.get("issues", [])
    heroine_status = data.get("heroine_status", [])

    # 矛盾检测与置信度标注
    contradiction_report = ""
    if CONTRADICTION_DETECTION_ENABLED:
        heroine_facts_raw = data.get("heroine_facts", [])
        contradictions = detect_all_contradictions(heroine_facts_raw)
        if contradictions:
            contradiction_report = generate_contradiction_report(contradictions)
            print(f"⚠️ 检测到 {len(contradictions)} 处潜在事实矛盾")
            logger.info(f"矛盾检测: {len(contradictions)} 处矛盾\n{contradiction_report}")
        # 置信度标注（in-place）
        annotate_confidence(heroine_facts_raw)
        # 保存矛盾报告
        contradiction_path = os.path.join(os.path.dirname(raw_data_path), "contradiction_report.txt")
        with open(contradiction_path, "w", encoding="utf-8") as f:
            f.write(contradiction_report or "未检测到事实矛盾。")
        # 保存标注后的数据
        if heroine_facts_raw:
            data["heroine_facts"] = heroine_facts_raw
            with open(raw_data_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # 断点续传文件放在扫描结果目录
    checkpoint_file = os.path.join(os.path.dirname(raw_data_path), "reviewer3_checkpoint.json")
    (
        verified_issues,
        rejected_count,
        rejected_issues,
        processed_issue_indices,
        cached_heroine_report,
        cached_pushed_map,
        cached_finished,
        cached_finished_reason,
        purity_done,
        finish_done,
    ) = load_checkpoint(raw_data_path, checkpoint_file)
    finished = cached_finished
    finished_reason = cached_finished_reason
    completed_checkpoint_has_gap = False
    # `finished` 是小说是否完结的判定结果，不是审查流程是否已经跑完。
    has_complete_checkpoint = purity_done and finish_done and cached_pushed_map is not None
    if has_complete_checkpoint:
        novel_path = _infer_novel_path_from_scan(raw_data_path)
        novel_tail = _read_tail(novel_path) if novel_path else None
        expected_leak_issues, _ = _rebuild_leak_state_from_pushed_map(
            female_leads=female_leads,
            char_file_path=char_file_path,
            novel_tail=novel_tail,
            finished=finished,
            pushed_map=cached_pushed_map,
        )
        _, missing_leak_count = _merge_unique_review_issues(verified_issues, expected_leak_issues)
        completed_checkpoint_has_gap = missing_leak_count > 0
        if completed_checkpoint_has_gap:
            logger.info(f"? ???????? {missing_leak_count} ????????????")
    if has_complete_checkpoint and not completed_checkpoint_has_gap:
        logger.info("==================================================")
        logger.info(f"★ 发现完整断点，跳过重新审查：{checkpoint_file}")
        logger.info(f"结束原因：{finished_reason}")
        logger.info("如需重新生成，请删除该 checkpoint 文件或将 finished/purity_done/finish_done 设为 false。")
        logger.info("==================================================")
        return

    if processed_issue_indices:
        print(f"⏸️ 检测到断点，将跳过已完成的 {len(processed_issue_indices)} 条指控")

    # 共用一个线程池
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        
        # --- 1. 毒点二审 (并发) ---
        print(f"⚡ 审查 {len(issues)} 条毒点指控...")
        rules_map = load_rules_dict()
        # 若无断点则初始化
        if not processed_issue_indices:
            verified_issues = []
            rejected_issues = []
            rejected_count = 0

        def current_checkpoint_saver(*, verified_issues, rejected_count, rejected_issues, processed_issue_indices):
            save_checkpoint(
                raw_data_path,
                verified_issues,
                rejected_count,
                rejected_issues,
                processed_issue_indices,
                checkpoint_file,
                heroine_report=cached_heroine_report,
                pushed_map=cached_pushed_map,
                finished=cached_finished,
                finished_reason=cached_finished_reason,
                purity_done=purity_done,
                finish_done=finish_done,
            )

        toxic_result = batch_review_toxic_points(
            executor=executor,
            issues=issues,
            rules_map=rules_map,
            male_lead=male_lead,
            female_leads=female_leads,
            processed_issue_indices=processed_issue_indices,
            verified_issues=verified_issues,
            rejected_issues=rejected_issues,
            rejected_count=rejected_count,
            save_checkpoint_fn=current_checkpoint_saver,
        )
        verified_issues = toxic_result.verified_issues
        rejected_issues = toxic_result.rejected_issues
        rejected_count = toxic_result.rejected_count
        pending_by_idx = toxic_result.pending_by_idx
        processed_issue_indices = toxic_result.processed_issue_indices

        # --- 2. 女主身心洁度鉴定 (规则版 + LLM，使用 scan 与 detail 证据) ---
        if purity_done and cached_heroine_report is not None:
            print("★ 检测到断点：已完成女主身心鉴定，跳过此步骤。")
            final_heroine_report = cached_heroine_report
        else:
            detail_evidences = _load_detail_evidence(char_file_path)
            final_heroine_report = aggregate_and_judge_heroines(heroine_status, male_lead, detail_evidences, raw_data_path, executor, female_leads)
            purity_done = True
        final_heroine_report = _normalize_heroine_report_consistency(final_heroine_report)
        if not purity_done:
            purity_done = True
        save_checkpoint(
            raw_data_path,
            verified_issues,
            rejected_count,
            rejected_issues,
            processed_issue_indices,
            checkpoint_file,
            heroine_report=final_heroine_report,
            pushed_map=cached_pushed_map,
            finished=cached_finished,
            finished_reason=cached_finished_reason,
            purity_done=purity_done,
            finish_done=finish_done,
        )

    # --- 2.5 漏女判定 ---
    novel_path = _infer_novel_path_from_scan(raw_data_path)
    novel_tail = _read_tail(novel_path) if novel_path else None
    leak_status_map: Dict[str, Dict[str, Any]] = {}

    if finish_done and cached_finished is not None and cached_pushed_map is not None:
        print("☑ 检测到断点：已完成完结/漏女判定，正在重建漏女状态并补齐缺失结果。")
        finished = cached_finished
        finished_reason = cached_finished_reason
        pushed_map = cached_pushed_map
        leak_issues, leak_status_map = _rebuild_leak_state_from_pushed_map(
            female_leads=female_leads,
            char_file_path=char_file_path,
            novel_tail=novel_tail,
            finished=finished,
            pushed_map=pushed_map,
        )
        verified_issues, added_leak_count = _merge_unique_review_issues(verified_issues, leak_issues)
        if added_leak_count:
            print(f"😢 检测到旧 checkpoint 缺少漏女 {added_leak_count} 条，已自动回写。")
            save_checkpoint(
                raw_data_path,
                verified_issues,
                rejected_count,
                rejected_issues,
                processed_issue_indices,
                checkpoint_file,
                heroine_report=final_heroine_report,
                pushed_map=pushed_map,
                finished=finished,
                finished_reason=finished_reason,
                purity_done=purity_done,
                finish_done=finish_done,
            )
    else:
        finished = None
        finished_reason = ""
        if novel_tail:
            finished, finished_reason = judge_novel_finished(novel_tail)
            if finished is not None:
                print(f"📘 完结判定: {finished} ({finished_reason})")
        leak_issues, pushed_map = detect_leak_heroines(female_leads, male_lead, char_file_path, novel_tail, finished)
        _, leak_status_map = _rebuild_leak_state_from_pushed_map(
            female_leads=female_leads,
            char_file_path=char_file_path,
            novel_tail=novel_tail,
            finished=finished,
            pushed_map=pushed_map,
        )
        verified_issues, added_leak_count = _merge_unique_review_issues(verified_issues, leak_issues)
        if added_leak_count:
            print(f"😢 检测到漏女 {added_leak_count} 条，已并入郁闷点。")
        finish_done = True
        save_checkpoint(
            raw_data_path,
            verified_issues,
            rejected_count,
            rejected_issues,
            processed_issue_indices,
            checkpoint_file,
            heroine_report=final_heroine_report,
            pushed_map=pushed_map,
            finished=finished,
            finished_reason=finished_reason,
            purity_done=purity_done,
            finish_done=finish_done,
        )

    # --- 3. 生成报告 ---
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_dir = os.path.dirname(raw_data_path)
    report_file = os.path.join(output_dir, f"VERIFIED_REPORT_{timestamp}.txt")
    summary_file = os.path.join(output_dir, f"VERIFIED_SUMMARY_{timestamp}.json")

    # 预先分类雷点/郁闷点，便于文本和 JSON 复用
    lei_points = [x for x in verified_issues if '雷' in x.get('category', '')]
    yumen_points = [x for x in verified_issues if '郁闷' in x.get('category', '')]
    pending_issues = list(pending_by_idx.values())
    
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(f"⚖️ 最终排毒报告\n审核时间: {timestamp} | 男主: {male_lead}\n")
        f.write("="*60 + "\n\n")

        # 写入女主报告（程序判定 + LLM 校验）
        f.write("❤️ 【女主身心全初鉴别 (程序判定 + LLM校验)】\n")
        f.write("标准：[全初] = 处女 + 无非男主接触 + 无非男主男伴 + 精神洁\n")
        f.write("验证：程序先判定 → LLM校验 → 不一致则二次校验\n\n")
        
        # 排序：优先显示不洁的，然后是女主名单里的，最后是全初的
        sorted_names = sorted(final_heroine_report.keys(), key=lambda x: (final_heroine_report[x]['is_clean'], x not in female_leads))
        
        for name in sorted_names:
            info = final_heroine_report[name]
            is_lead = any(lead in name for lead in female_leads)
            pushed, pushed_reason = pushed_map.get(name, (None, "???"))
            leak_info = leak_status_map.get(name, {})
            is_clean = info['is_clean']
            
            # 过滤逻辑：只显示重要的（是女主，或者不洁的）
            if is_lead or not is_clean:
                clean_tag = "[🌟 全初]" if is_clean else "[💔 有瑕]"
                f.write(f"角色：{name} {clean_tag}\n")
                # 显示五维洁度
                virgin_status = info.get('virgin_status', info.get('body_status', '❓ 未知'))
                contact_status = info.get('contact_status', '❓ 未知')
                partner_status = info.get('partner_status', '❓ 未知')
                virgin_conflict = _to_bool(info.get("virgin_judgement_conflict", False), False)
                llm_virgin_status = info.get("llm_virgin_status", virgin_status)
                rule_virgin_status = info.get("rule_virgin_status", "❓ 未知")
                llm_virgin_reason = str(info.get("llm_virgin_reason", "") or "")
                rule_virgin_reason = str(info.get("rule_virgin_reason", "") or "")
                contact_conflict = _to_bool(info.get("contact_judgement_conflict", False), False)
                llm_contact_status = info.get("llm_contact_status", contact_status)
                rule_contact_status = info.get("rule_contact_status", "❓ 未知")
                llm_contact_reason = str(info.get("llm_contact_reason", "") or "")
                rule_contact_reason = str(info.get("rule_contact_reason", "") or "")
                partner_conflict = _to_bool(info.get("partner_judgement_conflict", False), False)
                llm_partner_status = info.get("llm_partner_status", partner_status)
                rule_partner_status = info.get("rule_partner_status", "❓ 未知")
                llm_partner_reason = str(info.get("llm_partner_reason", "") or "")
                rule_partner_reason = str(info.get("rule_partner_reason", "") or "")
                spirit_conflict = _to_bool(info.get("spirit_judgement_conflict", False), False)
                llm_spirit_status = info.get("llm_spirit_status", info.get('spirit_status', '❓ 未知'))
                rule_spirit_status = info.get("rule_spirit_status", "❓ 未知")
                llm_spirit_reason = str(info.get("llm_spirit_reason", "") or "")
                rule_spirit_reason = str(info.get("rule_spirit_reason", "") or "")
                f.write(f"  - 是否处女: {virgin_status}\n")
                if virgin_conflict:
                    f.write("  - 处女判定冲突: ⚠️ LLM与规则不一致\n")
                    f.write(f"    LLM判断: {llm_virgin_status}\n")
                    f.write(f"    规则判断: {rule_virgin_status}\n")
                    if llm_virgin_reason:
                        f.write(f"    LLM理由: {llm_virgin_reason[:120]}\n")
                    if rule_virgin_reason:
                        f.write(f"    规则理由: {rule_virgin_reason[:120]}\n")
                f.write(f"  - 非男主接触: {contact_status}\n")
                if contact_conflict:
                    f.write("  - 接触判定冲突: ⚠️ LLM与规则不一致\n")
                    f.write(f"    LLM判断: {llm_contact_status}\n")
                    f.write(f"    规则判断: {rule_contact_status}\n")
                    if llm_contact_reason:
                        f.write(f"    LLM理由: {llm_contact_reason[:120]}\n")
                    if rule_contact_reason:
                        f.write(f"    规则理由: {rule_contact_reason[:120]}\n")
                f.write(f"  - 有无男伴: {partner_status}\n")
                if partner_conflict:
                    f.write("  - 男伴判定冲突: ⚠️ LLM与规则不一致\n")
                    f.write(f"    LLM判断: {llm_partner_status}\n")
                    f.write(f"    规则判断: {rule_partner_status}\n")
                    if llm_partner_reason:
                        f.write(f"    LLM理由: {llm_partner_reason[:120]}\n")
                    if rule_partner_reason:
                        f.write(f"    规则理由: {rule_partner_reason[:120]}\n")
                f.write(f"  - 精神: {info.get('spirit_status')}\n")
                for line in _format_heroine_purity_supplement(info):
                    f.write(line + "\n")
                is_leak_heroine = leak_info.get("is_leak_heroine")
                leak_reason = str(leak_info.get("leak_reason", "未判定") or "未判定")
                f.write(f"  - 是否被推倒: {_bool_mark(pushed)}\n")
                f.write(f"  - 推倒说明: {pushed_reason}\n")
                f.write(f"  - 是否漏女: {_bool_mark(is_leak_heroine)}\n")
                f.write(f"  - 漏女说明: {leak_reason}\n")
                if spirit_conflict:
                    f.write("  - 精神判定冲突: ⚠️ LLM与规则不一致\n")
                    f.write(f"    LLM判断: {llm_spirit_status}\n")
                    f.write(f"    规则判断: {rule_spirit_status}\n")
                    if llm_spirit_reason:
                        f.write(f"    LLM理由: {llm_spirit_reason[:120]}\n")
                    if rule_spirit_reason:
                        f.write(f"    规则理由: {rule_spirit_reason[:120]}\n")
                f.write(f"  - 鉴定结论: {info.get('summary')}\n")
                
                # 显示验证信息
                verification = info.get('verification', {})
                method = verification.get('method', 'unknown')
                rounds = verification.get('rounds', 0)
                llm_agreed = verification.get('llm_agreed', True)
                
                if method == 'program_with_llm_agree':
                    f.write(f"  - 验证: ✅ LLM 同意程序判定\n")
                elif method == 'program_confirmed_after_second_round':
                    f.write(f"  - 验证: ⚠️ LLM 初次不同意，二次校验后确认\n")
                    f.write(f"    · 初次分歧: {verification.get('first_disagreement', '')[:50]}\n")
                    f.write(f"    · 二次理由: {verification.get('second_reason', '')[:50]}\n")
                elif method == 'llm_override_after_disagreement':
                    f.write(f"  - 验证: ⚠️ 程序与LLM存在分歧，采用LLM二次校验结果\n")
                    f.write(f"    · 初次分歧: {verification.get('first_disagreement', '')[:50]}\n")
                    f.write(f"    · 二次理由: {verification.get('second_reason', '')[:50]}\n")
                    # 显示程序原判定
                    prog_res = verification.get('program_result', {})
                    if prog_res:
                        f.write(f"    · 程序原判: 处女={prog_res.get('is_virgin')}, 接触={prog_res.get('has_other_contact')}, 男伴={not prog_res.get('no_partner', True)}, 精神洁={prog_res.get('is_spirit_clean')}\n")
                elif method == 'program_final_hard_rules':
                    f.write(f"  - 验证: ⚠️ LLM 与程序存在分歧，已忽略 LLM 覆盖，采用程序硬规则结果\n")
                    f.write(f"    · 初次分歧: {verification.get('first_disagreement', '')[:50]}\n")
                    f.write(f"    · 二次理由: {verification.get('second_reason', '')[:50]}\n")
                elif method == 'no_facts_default_clean':
                    f.write(f"  - 验证: ℹ️ 无事实记录，默认全初\n")
                
                f.write("\n")

        f.write("="*60 + "\n\n")
        
        f.write(f"💀 【毒点二审结果】 (驳回误判: {rejected_count}条)\n\n")
        if lei_points:
            f.write(">>> 严重雷点 <<<\n")
            for i, item in enumerate(lei_points, 1):
                f.write(f"{i}. [{item.get('type')}] (第{item['chunk_index']}块)\n")
                f.write(f"   原文: {item.get('content', '')[:100]}...\n")
                f.write(f"   裁决: {item.get('review_comment')}\n\n")
        if yumen_points:
            f.write(">>> 郁闷点 <<<\n")
            for i, item in enumerate(yumen_points, 1):
                f.write(f"{i}. [{item.get('type')}] (第{item['chunk_index']}块)\n")
                f.write(f"   原文: {item.get('content', '')[:100]}...\n")
                f.write(f"   裁决: {item.get('review_comment')}\n\n")
        if pending_issues:
            f.write(">>> ⚠️ 未判定（API失败，建议补充可用KEY后重跑） <<<\n")
            for i, item in enumerate(pending_issues, 1):
                f.write(f"{i}. [{item.get('type')}] (第{item.get('chunk_index', '?')}块)\n")
                f.write(f"   原文: {item.get('content', '')[:100]}...\n")
                f.write(f"   状态: {item.get('review_comment', 'API错误')}\n\n")
        if not lei_points and not yumen_points and not pending_issues:
            f.write("✅ 全书无明显雷点。")

    # 生成 JSON 摘要：女主最终初/处 + 雷点/郁闷点
    summary_json = {
        "generated_at": timestamp,
        "male_lead": male_lead,
        "female_leads": female_leads,
        "heroines_purity": [],
        "lei_points": [],
        "yumen_points": [],
        "pending_points": [],
        "rejected_points": [],
    }

    def _issue_evidence_card(item, confidence):
        evidence_text = (
            str(item.get("evidence") or "").strip()
            or str(item.get("content") or "").strip()
            or str(item.get("reason") or "").strip()
        )
        return {
            "fact_type": item.get("type"),
            "category": item.get("category"),
            "source_chunk": item.get("chunk_index"),
            "evidence_text": evidence_text,
            "review_comment": item.get("review_comment"),
            "confidence": confidence,
        }

    for name, info in final_heroine_report.items():
        pushed, pushed_reason = pushed_map.get(name, (None, "未判定"))
        leak_info = leak_status_map.get(name, {})
        verification = info.get("verification", {})
        
        heroine_entry = {
            "name": name,
            # 五维洁度判定
            "is_virgin": info.get("is_virgin", True),
            "virgin_status": info.get("virgin_status", info.get("body_status", "❓ 未知")),
            "virgin_judgement_conflict": _to_bool(info.get("virgin_judgement_conflict", False), False),
            "llm_is_virgin": info.get("llm_is_virgin", info.get("is_virgin", True)),
            "llm_virgin_status": info.get("llm_virgin_status", info.get("virgin_status", info.get("body_status", "❓ 未知"))),
            "llm_virgin_reason": info.get("llm_virgin_reason"),
            "rule_is_virgin": info.get("rule_is_virgin"),
            "rule_virgin_status": info.get("rule_virgin_status"),
            "rule_virgin_reason": info.get("rule_virgin_reason"),
            "llm_has_other_contact": info.get("llm_has_other_contact", info.get("has_other_contact", False)),
            "llm_contact_status": info.get("llm_contact_status", info.get("contact_status", "❓ 未知")),
            "llm_contact_reason": info.get("llm_contact_reason"),
            "rule_has_other_contact": info.get("rule_has_other_contact"),
            "rule_contact_status": info.get("rule_contact_status"),
            "rule_contact_reason": info.get("rule_contact_reason"),
            "contact_judgement_conflict": _to_bool(info.get("contact_judgement_conflict", False), False),
            "has_other_contact": info.get("has_other_contact", False),
            "contact_status": info.get("contact_status", "❓ 未知"),
            "llm_no_partner": info.get("llm_no_partner", info.get("no_partner", True)),
            "llm_partner_status": info.get("llm_partner_status", info.get("partner_status", "❓ 未知")),
            "llm_partner_reason": info.get("llm_partner_reason"),
            "rule_no_partner": info.get("rule_no_partner"),
            "rule_partner_status": info.get("rule_partner_status"),
            "rule_partner_reason": info.get("rule_partner_reason"),
            "partner_judgement_conflict": _to_bool(info.get("partner_judgement_conflict", False), False),
            "no_partner": info.get("no_partner", True),
            "partner_status": info.get("partner_status", "❓ 未知"),
            # 精神洁度
            "llm_is_spirit_clean": info.get("llm_is_spirit_clean", info.get("is_spirit_clean", True)),
            "llm_spirit_status": info.get("llm_spirit_status", info.get("spirit_status")),
            "llm_spirit_reason": info.get("llm_spirit_reason"),
            "rule_is_spirit_clean": info.get("rule_is_spirit_clean"),
            "rule_spirit_status": info.get("rule_spirit_status"),
            "rule_spirit_reason": info.get("rule_spirit_reason"),
            "spirit_judgement_conflict": _to_bool(info.get("spirit_judgement_conflict", False), False),
            "is_spirit_clean": info.get("is_spirit_clean", True),
            "spirit_status": info.get("spirit_status"),
            "partner_exempted_for_clean": _to_bool(info.get("partner_exempted_for_clean", False), False),
            "partner_exemption_notes": info.get("partner_exemption_notes", []),
            "partner_exemption_reason": info.get("partner_exemption_reason", ""),
            "past_life_clean": info.get("past_life_clean"),
            "past_life_severity": info.get("past_life_severity"),
            "past_life_severity_label": info.get("past_life_severity_label"),
            "past_life_status": info.get("past_life_status", "未见前世/原故事线洁度线索"),
            "past_life_reason": info.get("past_life_reason", ""),
            "contact_level": info.get("contact_level", "L0"),
            "contact_level_label": info.get("contact_level_label", "无非男主接触事实"),
            "contact_level_reason": info.get("contact_level_reason", ""),
            # 综合判定
            "is_clean": info.get("is_clean"),
            "summary": info.get("summary"),
            # 推倒状态
            "pushed_by_male_lead": pushed,
            "pushed_reason": pushed_reason,
            "is_leak_heroine": leak_info.get("is_leak_heroine"),
            "leak_reason": leak_info.get("leak_reason"),
            "leak_emotional_depth": leak_info.get("leak_emotional_depth"),
            "leak_emotional_depth_reason": leak_info.get("leak_emotional_depth_reason"),
            "leak_relationship_confirmed": leak_info.get("leak_relationship_confirmed"),
            "leak_relationship_reason": leak_info.get("leak_relationship_reason"),
            "leak_ending_accounted": leak_info.get("leak_ending_accounted"),
            "leak_ending_reason": leak_info.get("leak_ending_reason"),
            # 兼容旧字段
            "body_status": info.get("body_status"),
            # 验证信息
            "verification": {
                "method": verification.get("method", "unknown"),
                "llm_agreed": verification.get("llm_agreed", True),
                "rounds": verification.get("rounds", 0),
            },
        }
        
        # 如果存在分歧，添加详细信息
        if verification.get("method") == "llm_override_after_disagreement":
            heroine_entry["verification"]["first_disagreement"] = verification.get("first_disagreement", "")
            heroine_entry["verification"]["second_reason"] = verification.get("second_reason", "")
            heroine_entry["verification"]["program_result"] = verification.get("program_result", {})
            heroine_entry["verification"]["llm_first_result"] = verification.get("llm_first_result", {})
        elif verification.get("method") == "program_final_hard_rules":
            heroine_entry["verification"]["first_disagreement"] = verification.get("first_disagreement", "")
            heroine_entry["verification"]["second_reason"] = verification.get("second_reason", "")
            heroine_entry["verification"]["llm_first_result"] = verification.get("llm_first_result", {})
            heroine_entry["verification"]["llm_second_result"] = verification.get("llm_second_result", {})
        elif verification.get("method") == "program_confirmed_after_second_round":
            heroine_entry["verification"]["first_disagreement"] = verification.get("first_disagreement", "")
            heroine_entry["verification"]["second_reason"] = verification.get("second_reason", "")
        
        summary_json["heroines_purity"].append(heroine_entry)

    for item in lei_points:
        summary_json["lei_points"].append({
            "category": item.get("category"),
            "type": item.get("type"),
            "content": item.get("content"),
            "reason": item.get("reason"),
            "review_comment": item.get("review_comment"),
            "chunk_index": item.get("chunk_index"),
            "evidence_card": _issue_evidence_card(item, "confirmed"),
        })

    for item in yumen_points:
        summary_json["yumen_points"].append({
            "category": item.get("category"),
            "type": item.get("type"),
            "content": item.get("content"),
            "reason": item.get("reason"),
            "review_comment": item.get("review_comment"),
            "chunk_index": item.get("chunk_index"),
            "evidence_card": _issue_evidence_card(item, "confirmed"),
        })

    for item in pending_issues:
        summary_json["pending_points"].append({
            "category": item.get("category"),
            "type": item.get("type"),
            "content": item.get("content"),
            "reason": item.get("reason"),
            "review_comment": item.get("review_comment"),
            "chunk_index": item.get("chunk_index"),
            "api_error": True,
            "evidence_card": _issue_evidence_card(item, "pending"),
        })

    for item in rejected_issues:
        summary_json["rejected_points"].append({
            "category": item.get("category"),
            "type": item.get("type"),
            "content": item.get("content"),
            "reason": item.get("reason"),
            "review_comment": item.get("review_comment"),
            "chunk_index": item.get("chunk_index"),
            "evidence_card": _issue_evidence_card(item, "rejected"),
        })

    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary_json, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 报告已生成: {report_file}")
    print(f"📦 JSON 摘要: {summary_file}")
    tracker = get_token_tracker()
    if tracker is not None:
        snap = tracker.snapshot()
        print(f"🔢 Token 统计: 输入 {snap.get('input', 0)} ，输出 {snap.get('output', 0)} ，总计 {snap.get('total', 0)}")
        tracker.flush(status="finished")
    print("="*60)

if __name__ == "__main__":
    main()
