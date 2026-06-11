"""
readability_scorer.py — 中文可读性评分引擎（纯规则统计，零 LLM 成本）

结合中文文本特征的可读性评估，输出 0-100 综合可读性指数。
设计原则同 sentiment_arcs.py：
- 只用正则和计数，不调 LLM
- 每个指标附采样量和可信度
"""

import math
import os
import re
from typing import Any, Dict, List, Tuple

READABILITY_SCORER_ENABLED = os.environ.get("READABILITY_SCORER_ENABLED", "1").strip() == "1"
READABILITY_SAMPLE_CHARS = int(os.environ.get("READABILITY_SAMPLE_CHARS", "300000") or "300000")

# ===================== 连接词库 =====================

CONNECTIVE_WORDS = {
    "因果": ["因此", "所以", "因为", "由于", "既然", "以致", "于是", "从而导致"],
    "转折": ["但是", "然而", "可是", "不过", "却", "虽然", "尽管", "固然", "只是"],
    "递进": ["而且", "并且", "此外", "另外", "同时", "甚至", "更", "不但", "不仅"],
    "条件": ["如果", "假如", "倘若", "只要", "只有", "除非", "万一"],
    "并列": ["同时", "一边", "一方面", "另一方面", "既", "又"],
    "时间": ["然后", "接着", "随后", "之后", "最后", "终于", "此时", "这时"],
    "总结": ["总之", "综上所述", "由此可见", "也就是说", "换言之"],
}

# 中文标点句末
_SENTENCE_END_RE = re.compile(r"[。！？!?…]+")
_DIALOGUE_RE = re.compile(r"\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|\u300c[^\u300d]*\u300d", re.DOTALL)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n|\r\n\s*\r\n")


# ===================== 核心计算 =====================

def compute_readability(text: str) -> Dict[str, Any]:
    """
    纯规则计算中文可读性综合指数。

    返回：
    {
        "sample_chars": int,
        "avg_sentence_len": float,         # 平均句长（字）
        "sentence_len_std": float,         # 句长标准差
        "long_sentence_ratio": float,      # 长句(>60字)占比
        "avg_paragraph_len": float,        # 平均段落长度
        "dialogue_ratio": float,           # 对话占比
        "dialogue_balance": float,         # 对话平衡度（0-1，1=最平衡）
        "connective_density": float,       # 连接词/万字
        "connective_detail": dict,         # 各类连接词数量
        "info_density": float,             # 信息密度（去重字符/总字符）
        "readability_index": float,        # 综合可读性指数 0-100
        "readability_grade": str,          # 等级标签
        "confidence": str,                 # "high"/"medium"/"low"
    }
    """
    if not text:
        return _empty_readability()

    sample = text[:READABILITY_SAMPLE_CHARS] if len(text) > READABILITY_SAMPLE_CHARS else text
    total_chars = max(1, len(sample))

    # 1. 句子统计
    sentences = _SENTENCE_END_RE.split(sample)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 2]
    sent_lengths = [len(s) for s in sentences] if sentences else [0]
    avg_sent_len = round(_mean(sent_lengths), 1)
    sent_len_std = round(_std(sent_lengths), 1)
    long_sents = sum(1 for sl in sent_lengths if sl > 60)
    long_sent_ratio = round(long_sents / max(1, len(sent_lengths)), 4)

    # 2. 段落统计
    paragraphs = _PARAGRAPH_SPLIT_RE.split(sample)
    paragraphs = [p.strip() for p in paragraphs if len(p.strip()) > 10]
    para_lengths = [len(p) for p in paragraphs] if paragraphs else [0]
    avg_para_len = round(_mean(para_lengths), 1)

    # 3. 对话统计
    dialogue_chars = sum(len(m.group(0)) for m in _DIALOGUE_RE.finditer(sample))
    dialogue_ratio = round(dialogue_chars / total_chars, 4)
    # 对话平衡度：理想区间 0.15-0.35
    if 0.15 <= dialogue_ratio <= 0.35:
        dialogue_balance = 1.0
    elif dialogue_ratio < 0.15:
        dialogue_balance = round(dialogue_ratio / 0.15, 2)
    else:
        dialogue_balance = round(max(0, 1.0 - (dialogue_ratio - 0.35) / 0.3), 2)

    # 4. 连接词密度
    conn_detail = {}
    conn_total = 0
    for cat, words in CONNECTIVE_WORDS.items():
        cat_count = sum(sample.count(w) for w in words)
        if cat_count > 0:
            conn_detail[cat] = cat_count
            conn_total += cat_count
    connective_density = round(conn_total * 10000 / total_chars, 2)

    # 5. 信息密度（去重字符/总字符）
    unique_chars = len(set(sample))
    info_density = round(unique_chars / total_chars, 4)

    # 6. 综合可读性指数（加权）
    # 句长适中（20-35字）得分高
    sent_score = _clamp_score((35 - abs(avg_sent_len - 28)) / 35 * 25, 0, 25)
    # 句长方差低得分高（节奏稳定）
    var_score = _clamp_score((20 - min(sent_len_std, 20)) / 20 * 15, 0, 15)
    # 对话平衡
    dial_score = dialogue_balance * 20
    # 段落长度适中（100-300字）
    para_score = _clamp_score((200 - abs(avg_para_len - 200)) / 200 * 15, 0, 15)
    # 连接词密度适中（5-20/万字）
    if 5 <= connective_density <= 20:
        conn_score = 15
    elif connective_density < 5:
        conn_score = connective_density / 5 * 15
    else:
        conn_score = max(0, 15 - (connective_density - 20) / 10 * 15)
    conn_score = round(conn_score, 1)
    # 信息密度（0.08-0.20适中）
    if 0.08 <= info_density <= 0.20:
        info_score = 10
    elif info_density < 0.08:
        info_score = info_density / 0.08 * 10
    else:
        info_score = max(0, 10 - (info_density - 0.20) / 0.10 * 10)
    info_score = round(info_score, 1)

    readability_index = round(sent_score + var_score + dial_score + para_score + conn_score + info_score, 1)
    readability_index = min(100, max(0, readability_index))

    if readability_index >= 80:
        grade = "通俗易懂（爽文节奏）"
    elif readability_index >= 60:
        grade = "正常阅读体验"
    elif readability_index >= 40:
        grade = "需要一定耐心"
    else:
        grade = "晦涩难懂/信息过载"

    conf = "high" if total_chars >= 100000 else "medium" if total_chars >= 30000 else "low"

    return {
        "sample_chars": total_chars,
        "avg_sentence_len": avg_sent_len,
        "sentence_len_std": sent_len_std,
        "long_sentence_ratio": long_sent_ratio,
        "avg_paragraph_len": avg_para_len,
        "dialogue_ratio": dialogue_ratio,
        "dialogue_balance": dialogue_balance,
        "connective_density": connective_density,
        "connective_detail": conn_detail,
        "info_density": info_density,
        "readability_index": readability_index,
        "readability_grade": grade,
        "confidence": conf,
    }


def _clamp_score(value: float, min_v: float, max_v: float) -> float:
    return round(max(min_v, min(max_v, value)), 1)


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _empty_readability() -> Dict[str, Any]:
    return {
        "sample_chars": 0,
        "avg_sentence_len": 0,
        "sentence_len_std": 0,
        "long_sentence_ratio": 0,
        "avg_paragraph_len": 0,
        "dialogue_ratio": 0,
        "dialogue_balance": 0,
        "connective_density": 0,
        "connective_detail": {},
        "info_density": 0,
        "readability_index": 0,
        "readability_grade": "数据不足",
        "confidence": "low",
    }


# ===================== 报告渲染 =====================

def render_readability_report(readability: Dict[str, Any]) -> str:
    """渲染可读性评分报告，可直接插入报告。"""
    if not readability or readability.get("sample_chars", 0) == 0:
        return ""

    lines = []
    conf = readability.get("confidence", "low")
    conf_label = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低"}.get(conf, "🔴 低")
    lines.append(f"（采样 {readability['sample_chars']:,} 字，可信度 {conf_label}）")
    lines.append("")

    idx = readability["readability_index"]
    grade = readability["readability_grade"]
    lines.append(f"  综合可读性：{idx}/100  「{grade}」")

    # 条形图
    bar_len = int(idx / 100 * 40)
    bar = "█" * bar_len + "░" * (40 - bar_len)
    lines.append(f"  [{bar}] {idx:.0f}")
    lines.append("")

    lines.append(f"  平均句长：{readability['avg_sentence_len']:.0f}字  标准差：{readability['sentence_len_std']:.1f}  长句占比：{readability['long_sentence_ratio']:.0%}")
    lines.append(f"  平均段落长度：{readability['avg_paragraph_len']:.0f}字")

    dr = readability["dialogue_ratio"]
    db = readability["dialogue_balance"]
    lines.append(f"  对话占比：{dr:.1%}  平衡度：{db:.0%}{'（对话驱动）' if dr > 0.25 else '（叙述为主）' if dr < 0.1 else ''}")

    cd = readability["connective_density"]
    conn_detail = readability.get("connective_detail", {})
    lines.append(f"  连接词密度：{cd}/万字{' ✅' if 5 <= cd <= 20 else ' ⚠️' if cd < 5 else ' ⚡'}")
    if conn_detail:
        detail_str = "  ".join(f"{k}×{v}" for k, v in sorted(conn_detail.items(), key=lambda x: -x[1]))
        lines.append(f"    {detail_str}")

    info_d = readability["info_density"]
    lines.append(f"  信息密度：{info_d:.3f}（去重字符/总字符）")

    return "\n".join(lines)
