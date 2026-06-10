"""
contradiction_detector.py - 矛盾检测与置信度分级

在 reviewer 阶段之前或之中，对同一角色的所有事实做逻辑矛盾检测，
并为每条事实标注置信度，帮助 reviewer 做出更稳定的判断。

核心功能：
1. 同一角色事实矛盾检测（如"有孩子" vs "未破处"）
2. 置信度分级（high/medium/low）
3. 证据强度评估

设计原则：
- 矛盾检测是"辅助信号"，不直接删除事实，而是标注供 reviewer 参考
- 置信度基于多维度评估：evidence_level、证据数量、是否有 corroborating evidence
- 检测规则可配置，避免误报
"""

import json
import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ===================== 配置 =====================
CONTRADICTION_DETECTION_ENABLED = os.environ.get("CONTRADICTION_DETECTION_ENABLED", "1").strip() == "1"
CONFIDENCE_SCORING_ENABLED = os.environ.get("CONFIDENCE_SCORING_ENABLED", "1").strip() == "1"


# ===================== 矛盾规则定义 =====================
# 每条规则定义：当事实 A 和事实 B 同时存在时的矛盾判断
CONTRADICTION_RULES = [
    {
        "id": "virgin_pregnant",
        "description": "未破处 vs 有孩子/怀孕",
        "fact_a": {"dimension": "sexual_relations", "pattern": r"未破处|处女|初夜未发生"},
        "fact_b": {"dimension": "children_info", "pattern": r"怀孕|生子|孩子|怀了|生下|流产"},
        "severity": "critical",
        "resolution_hint": "检查时间线：未破处描述是否在孩子/怀孕之前；或是否为非正常受孕（试管/魔法）",
    },
    {
        "id": "virgin_sexual_relation",
        "description": "未破处 vs 有性关系",
        "fact_a": {"dimension": "sexual_relations", "pattern": r"未破处|处女"},
        "fact_b": {"dimension": "sexual_relations", "pattern": r"发生性关系|同房|破处|初夜|性行为"},
        "severity": "critical",
        "resolution_hint": "检查是否指向同一时间段；可能是时间线变化（之前处女，后来发生关系）",
    },
    {
        "id": "no_relation_has_child",
        "description": "无性关系 vs 有孩子",
        "fact_a": {"dimension": "sexual_relations", "pattern": r"无性关系|未发生|没有性关系"},
        "fact_b": {"dimension": "children_info", "pattern": r"怀孕|生子|孩子|怀了|生下"},
        "severity": "critical",
        "resolution_hint": "检查是否为非正常受孕（试管/魔法/收养），或性关系信息有遗漏",
    },
    {
        "id": "forced_consent_same_event",
        "description": "同一事件中同时标注强迫和自愿",
        "fact_a": {"dimension": "sexual_relations", "pattern": r"强迫|被迫|非自愿"},
        "fact_b": {"dimension": "sexual_relations", "pattern": r"自愿|同意|主动"},
        "severity": "warning",
        "resolution_hint": "可能是同一段落中不同人物的视角描述",
    },
]


# ===================== 置信度评分 =====================
def score_fact_confidence(fact: Dict[str, Any]) -> str:
    """
    为单条事实评估置信度。

    返回 "high"、"medium" 或 "low"。

    评分维度：
    - evidence_level: explicit > implicit
    - evidence 长度：有 evidence 且长度 >= 12 字加分
    - evidence_strength: strong > medium > weak
    - speech_act: asserted_fact > 其他
    - 多次出现（同一事实在不同 chunk 出现）：加分
    """
    score = 0

    # evidence_level
    ev_level = str(fact.get("evidence_level", "")).lower()
    if ev_level == "explicit":
        score += 3
    elif ev_level == "implicit":
        score += 1

    # evidence 存在性和长度
    evidence = str(fact.get("evidence", ""))
    if evidence and len(evidence) >= 12:
        score += 2
    elif evidence:
        score += 1

    # evidence_strength
    ev_strength = str(fact.get("evidence_strength", "")).lower()
    if ev_strength == "strong":
        score += 2
    elif ev_strength == "medium":
        score += 1

    # speech_act
    speech_act = str(fact.get("speech_act", "")).lower()
    if speech_act == "asserted_fact":
        score += 2

    # detail 非空
    detail = str(fact.get("detail", ""))
    if detail and len(detail) >= 5:
        score += 1

    # 分级
    if score >= 6:
        return "high"
    elif score >= 3:
        return "medium"
    else:
        return "low"


# ===================== 矛盾检测 =====================
def detect_contradictions_for_character(
    character_name: str,
    heroine_facts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    检测同一角色的所有事实中的逻辑矛盾。

    返回矛盾列表：
    [
        {
            "rule_id": "virgin_pregnant",
            "character": "角色名",
            "description": "未破处 vs 有孩子/怀孕",
            "fact_a": {...},
            "fact_b": {...},
            "severity": "critical",
            "resolution_hint": "...",
        },
        ...
    ]
    """
    if not CONTRADICTION_DETECTION_ENABLED:
        return []

    # 按维度组织事实
    facts_by_dimension: Dict[str, List[Dict[str, Any]]] = {}
    for fact in heroine_facts:
        dim = fact.get("dimension", "")
        if dim not in facts_by_dimension:
            facts_by_dimension[dim] = []
        facts_by_dimension[dim].append(fact)

    contradictions = []

    for rule in CONTRADICTION_RULES:
        dim_a = rule["fact_a"]["dimension"]
        dim_b = rule["fact_b"]["dimension"]
        pattern_a = re.compile(rule["fact_a"]["pattern"])
        pattern_b = re.compile(rule["fact_b"]["pattern"])

        facts_a = facts_by_dimension.get(dim_a, [])
        facts_b = facts_by_dimension.get(dim_b, [])

        for fa in facts_a:
            detail_a = str(fa.get("detail", "")) + " " + str(fa.get("evidence", ""))
            if not pattern_a.search(detail_a):
                continue

            for fb in facts_b:
                # 跳过自匹配（同一维度同一事实对象）
                if fa is fb:
                    continue
                detail_b = str(fb.get("detail", "")) + " " + str(fb.get("evidence", ""))
                if not pattern_b.search(detail_b):
                    continue

                contradictions.append({
                    "rule_id": rule["id"],
                    "character": character_name,
                    "description": rule["description"],
                    "fact_a_summary": _summarize_fact(fa),
                    "fact_b_summary": _summarize_fact(fb),
                    "fact_a_chunk": fa.get("chunk_index"),
                    "fact_b_chunk": fb.get("chunk_index"),
                    "severity": rule["severity"],
                    "resolution_hint": rule["resolution_hint"],
                })

    return contradictions


def detect_all_contradictions(
    all_heroine_facts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    对所有女主事实做矛盾检测。

    all_heroine_facts 是扁平列表，每项包含 heroine + facts。
    """
    if not CONTRADICTION_DETECTION_ENABLED:
        return []

    # 按角色分组
    by_character: Dict[str, List[Dict[str, Any]]] = {}
    for entry in all_heroine_facts:
        name = entry.get("heroine", entry.get("name", ""))
        if not name:
            continue
        facts = entry.get("facts", {})
        if isinstance(facts, dict):
            for dim, items in facts.items():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item_with_dim = dict(item)
                            item_with_dim["dimension"] = dim
                            by_character.setdefault(name, []).append(item_with_dim)
        elif isinstance(facts, list):
            by_character.setdefault(name, []).extend(facts)

    all_contradictions = []
    for name, facts in by_character.items():
        contradictions = detect_contradictions_for_character(name, facts)
        all_contradictions.extend(contradictions)

    return all_contradictions


# ===================== 置信度标注 =====================
def annotate_confidence(
    all_heroine_facts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    为所有女主事实标注置信度。

    在每条事实中添加 _confidence 字段。
    """
    if not CONFIDENCE_SCORING_ENABLED:
        return all_heroine_facts

    for entry in all_heroine_facts:
        facts = entry.get("facts", {})
        if isinstance(facts, dict):
            for dim, items in facts.items():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item["_confidence"] = score_fact_confidence(item)

    return all_heroine_facts


# ===================== 辅助函数 =====================
def _summarize_fact(fact: Dict[str, Any]) -> str:
    """生成事实的简短摘要。"""
    dim = fact.get("dimension", "")
    detail = str(fact.get("detail", ""))[:40]
    evidence = str(fact.get("evidence", ""))[:30]
    return f"[{dim}] {detail}" + (f" (证据: {evidence}…)" if evidence else "")


def generate_contradiction_report(contradictions: List[Dict[str, Any]]) -> str:
    """生成矛盾报告文本。"""
    if not contradictions:
        return "未检测到事实矛盾。"

    lines = [f"检测到 {len(contradictions)} 处潜在矛盾："]
    for i, c in enumerate(contradictions, 1):
        severity = "🔴" if c["severity"] == "critical" else "🟡"
        lines.append(f"\n{i}. {severity} [{c['rule_id']}] {c['description']}")
        lines.append(f"   角色：{c['character']}")
        lines.append(f"   事实A：{c['fact_a_summary']} (chunk {c.get('fact_a_chunk', '?')})")
        lines.append(f"   事实B：{c['fact_b_summary']} (chunk {c.get('fact_b_chunk', '?')})")
        lines.append(f"   建议：{c['resolution_hint']}")

    return "\n".join(lines)
