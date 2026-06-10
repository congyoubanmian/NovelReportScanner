"""
literary_metrics.py — 文学质感量化引擎（纯规则统计，零 LLM 成本）

从原文直接计算文本特征指标，与 LLM 评分交叉验证，产生可信度标注。
设计原则：
- 只用正则和计数，不调 LLM
- 输出与 radar_scores 同维度的程序化评分，用于交叉验证
- 每个指标附采样量和可信度
"""

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

LITERARY_METRICS_ENABLED = os.environ.get("LITERARY_METRICS_ENABLED", "1").strip() == "1"
LITERARY_METRICS_SAMPLE_CHARS = int(os.environ.get("LITERARY_METRICS_SAMPLE_CHARS", "300000") or "300000")

# ===================== 模板词库 =====================

WORD_POVERTY_MARKERS = {
    "强者描写": ["如斯恐怖", "恐怖如斯", "深不可测", "不可一世", "威压", "气息暴涨", "威势"],
    "吃瓜群众": ["倒吸了一口凉气", "倒吸一口凉气", "全场哗然", "目瞪口呆", "惊掉下巴", "倒吸凉气"],
    "女子描写": ["高贵冷艳", "温婉可人", "风情万种", "小家碧玉", "冰清玉洁", "倾国倾城", "绝世容颜"],
    "表情描写": ["一脸惊讶", "震惊", "冷笑", "淡淡的", "冷冷的", "不禁", "下意识", "微微一愣"],
    "战斗描写": ["身形一闪", "倒退几步", "口中鲜血狂喷", "脸色大变", "瞳孔一缩", "轰然炸开"],
    "情绪副词": ["不禁", "不由", "下意识", "猛地", "突然", "竟然", "居然", "赫然"],
}

# 省略号/感叹号等风格标记
_EXCLAMATION_RE = re.compile(r"[！!]")
_ELLIPSIS_RE = re.compile(r"[…]{2,}|[。]{3,}|\.{3,}")
_DIALOGUE_RE = re.compile(r"[\"\"「「『『].*?[\"」」』』]", re.DOTALL)
_SENTENCE_END_RE = re.compile(r"[。！？!?…]+")
_CHAPTER_RE = re.compile(
    r"^\s*(?:第[\u4e00-\u9fff零〇一二三四五六七八九十百千万0-9]+[章节回卷部集]"
    r"|序章|楔子|终章|尾声|番外)\s*",
    re.MULTILINE,
)


# ===================== 核心计算 =====================

def compute_literary_metrics(text: str) -> Dict[str, Any]:
    """
    纯规则计算全文文本质感指标。

    返回：
    {
        "sample_chars": int,          # 采样字符数
        "total_chars": int,           # 全文字符数
        "template_word_density": float,  # 模板词频次/万字
        "template_word_hits": dict,       # 每类模板词命中详情
        "dialogue_ratio": float,          # 对话占比
        "exclamation_density": float,     # 感叹号/万字
        "ellipsis_density": float,        # 省略号/万字
        "avg_sentence_len": float,        # 平均句长
        "long_sentence_ratio": float,     # 长句(>60字)占比
        "char_diversity": float,          # 字符多样性（去重/总数）
        "chapter_count": int,             # 章节数
        "chapter_length_cv": float,       # 章节长度变异系数
        "long_chapter_ratio": float,      # 长章占比
        "short_chapter_ratio": float,     # 短章占比

        # 程序化评分（0-10，可与 radar_scores 交叉验证）
        "rule_writing_score": float,      # 文笔水准（基于模板词+多样性+句长）
        "rule_pacing_score": float,       # 节奏把控（基于章节长度均匀度）
        "rule_emotion_score": float,      # 情绪调动（基于感叹号+情绪词）

        "confidence": str,               # "high"/"medium"/"low"
    }
    """
    if not text:
        return _empty_metrics()

    sample = text[:LITERARY_METRICS_SAMPLE_CHARS] if len(text) > LITERARY_METRICS_SAMPLE_CHARS else text
    total_chars = max(1, len(sample))
    total_chars_full = len(text)

    # 1. 模板词密度
    template_hits = {}
    template_total = 0
    for category, words in WORD_POVERTY_MARKERS.items():
        hits = []
        for word in words:
            count = sample.count(word)
            if count > 0:
                hits.append({"word": word, "count": count})
                template_total += count
        if hits:
            template_hits[category] = hits
    template_density = round(template_total * 10000 / total_chars, 2)

    # 2. 对话占比
    dialogue_chars = sum(len(m.group(0)) for m in _DIALOGUE_RE.finditer(sample))
    dialogue_ratio = round(dialogue_chars / total_chars, 4) if total_chars > 0 else 0.0

    # 3. 感叹号密度
    excl_count = len(_EXCLAMATION_RE.findall(sample))
    excl_density = round(excl_count * 10000 / total_chars, 2)

    # 4. 省略号密度
    ellipsis_count = len(_ELLIPSIS_RE.findall(sample))
    ellipsis_density = round(ellipsis_count * 10000 / total_chars, 2)

    # 5. 句子统计
    sentences = _SENTENCE_END_RE.split(sample)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 2]
    if sentences:
        sent_lengths = [len(s) for s in sentences]
        avg_sent_len = round(sum(sent_lengths) / len(sent_lengths), 1)
        long_sents = sum(1 for sl in sent_lengths if sl > 60)
        long_sent_ratio = round(long_sents / len(sentences), 3)
    else:
        avg_sent_len = 0.0
        long_sent_ratio = 0.0

    # 6. 字符多样性
    if total_chars > 100:
        unique_chars = len(set(sample))
        char_diversity = round(unique_chars / total_chars, 4)
    else:
        char_diversity = 0.0

    # 7. 章节分析
    chapter_breaks = [m.start() for m in _CHAPTER_RE.finditer(text)]
    chapter_count = len(chapter_breaks)
    chapter_length_cv = 0.0
    long_chapter_ratio = 0.0
    short_chapter_ratio = 0.0

    if chapter_count >= 3:
        breaks = chapter_breaks + [len(text)]
        chapter_lengths = [breaks[i + 1] - breaks[i] for i in range(len(breaks) - 1)]
        mean_len = sum(chapter_lengths) / len(chapter_lengths)
        if mean_len > 0:
            variance = sum((cl - mean_len) ** 2 for cl in chapter_lengths) / len(chapter_lengths)
            std_len = math.sqrt(variance)
            chapter_length_cv = round(std_len / mean_len, 3)
            long_chapter_ratio = round(sum(1 for cl in chapter_lengths if cl > mean_len * 1.5) / len(chapter_lengths), 3)
            short_chapter_ratio = round(sum(1 for cl in chapter_lengths if cl < mean_len * 0.5) / len(chapter_lengths), 3)

    # 8. 程序化评分（映射到 0-10）
    rule_writing_score = _compute_writing_score(template_density, char_diversity, avg_sent_len)
    rule_pacing_score = _compute_pacing_score(chapter_length_cv, long_chapter_ratio, short_chapter_ratio)
    rule_emotion_score = _compute_emotion_score(excl_density, template_density)

    # 9. 置信度
    conf = "high" if total_chars >= 100000 else "medium" if total_chars >= 30000 else "low"

    return {
        "sample_chars": total_chars,
        "total_chars": total_chars_full,
        "template_word_density": template_density,
        "template_word_hits": template_hits,
        "dialogue_ratio": dialogue_ratio,
        "exclamation_density": excl_density,
        "ellipsis_density": ellipsis_density,
        "avg_sentence_len": avg_sent_len,
        "long_sentence_ratio": long_sent_ratio,
        "char_diversity": char_diversity,
        "chapter_count": chapter_count,
        "chapter_length_cv": chapter_length_cv,
        "long_chapter_ratio": long_chapter_ratio,
        "short_chapter_ratio": short_chapter_ratio,
        "rule_writing_score": rule_writing_score,
        "rule_pacing_score": rule_pacing_score,
        "rule_emotion_score": rule_emotion_score,
        "confidence": conf,
    }


def _empty_metrics() -> Dict[str, Any]:
    return {
        "sample_chars": 0, "total_chars": 0,
        "template_word_density": 0.0, "template_word_hits": {},
        "dialogue_ratio": 0.0, "exclamation_density": 0.0,
        "ellipsis_density": 0.0, "avg_sentence_len": 0.0,
        "long_sentence_ratio": 0.0, "char_diversity": 0.0,
        "chapter_count": 0, "chapter_length_cv": 0.0,
        "long_chapter_ratio": 0.0, "short_chapter_ratio": 0.0,
        "rule_writing_score": 0.0, "rule_pacing_score": 0.0,
        "rule_emotion_score": 0.0,
        "confidence": "low",
    }


def _compute_writing_score(template_density: float, char_diversity: float, avg_sent_len: float) -> float:
    """
    文笔水准评分（规则估算）。
    - 模板词越少越好（高密度扣分）
    - 字符多样性越高越好
    - 句长适中（15-40字）最好
    """
    score = 7.0  # 基准分

    # 模板词扣分
    if template_density > 30:
        score -= 2.5
    elif template_density > 15:
        score -= 1.5
    elif template_density > 8:
        score -= 0.8
    elif template_density < 3:
        score += 0.5

    # 多样性加分/扣分
    if char_diversity > 0.25:
        score += 0.5
    elif char_diversity < 0.12:
        score -= 1.0

    # 句长
    if avg_sent_len > 50:
        score -= 0.8
    elif 15 <= avg_sent_len <= 40:
        score += 0.3

    return round(max(1.0, min(10.0, score)), 1)


def _compute_pacing_score(chapter_cv: float, long_ratio: float, short_ratio: float) -> float:
    """
    节奏把控评分（规则估算）。
    - 章节长度越均匀越好（CV 低）
    - 长章/短章过多扣分
    """
    score = 7.0

    if chapter_cv > 1.0:
        score -= 2.0
    elif chapter_cv > 0.7:
        score -= 1.0
    elif chapter_cv < 0.3:
        score += 0.5

    if long_ratio > 0.3:
        score -= 0.8
    if short_ratio > 0.3:
        score -= 0.8

    return round(max(1.0, min(10.0, score)), 1)


def _compute_emotion_score(excl_density: float, template_density: float) -> float:
    """
    情绪调动评分（规则估算）。
    - 适度感叹号说明有情绪波动
    - 太多可能是廉价情绪
    - 模板词多意味着套路化情绪
    """
    score = 6.0

    if 5 <= excl_density <= 25:
        score += 1.5
    elif excl_density > 40:
        score -= 1.0  # 廉价情绪
    elif excl_density < 2:
        score -= 0.5  # 太平

    if template_density > 20:
        score -= 1.0  # 套路情绪

    return round(max(1.0, min(10.0, score)), 1)


# ===================== 融合与可信度 =====================

def fuse_scores_with_confidence(
    radar_scores: Dict[str, Any],
    literary_metrics: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """
    将 LLM radar_scores 与 rule-based literary_metrics 交叉验证，
    返回每个维度的融合评分 + 可信度。

    返回：
    {
        "plot":      {"score": float, "confidence": "high"/"medium"/"low", "note": "..."},
        "characters": {"score": float, "confidence": "high"/"medium"/"low", "note": "..."},
        "worldbuilding": {...},
        "pacing":    {"score": float, "confidence": "high"/"medium"/"low", "note": "..."},
        "writing":   {"score": float, "confidence": "high"/"medium"/"low", "note": "..."},
        "emotion":   {"score": float, "confidence": "high"/"medium"/"low", "note": "..."},
    }
    """
    metrics_conf = literary_metrics.get("confidence", "low")
    result = {}

    dim_rules = {
        "writing": ("rule_writing_score", "文笔"),
        "pacing": ("rule_pacing_score", "节奏"),
        "emotion": ("rule_emotion_score", "情绪"),
    }

    for dim_key in ("plot", "characters", "worldbuilding", "pacing", "writing", "emotion"):
        llm_entry = radar_scores.get(dim_key) or {}
        llm_score = llm_entry.get("score") if isinstance(llm_entry, dict) else None
        llm_reason = llm_entry.get("reason", "") if isinstance(llm_entry, dict) else ""

        rule_key, rule_label = dim_rules.get(dim_key, (None, ""))
        rule_score = literary_metrics.get(rule_key) if rule_key else None

        fused, conf, note = _fuse_single_dimension(
            dim_key, llm_score, llm_reason, rule_score, metrics_conf
        )
        result[dim_key] = {
            "score": fused,
            "confidence": conf,
            "note": note,
        }

    return result


def _fuse_single_dimension(
    dim_key: str,
    llm_score: Optional[float],
    llm_reason: str,
    rule_score: Optional[float],
    metrics_conf: str,
) -> Tuple[float, str, str]:
    """融合单个维度的 LLM 和规则评分。"""
    has_llm = llm_score is not None and llm_score > 0
    has_rule = rule_score is not None and rule_score > 0

    if has_llm and has_rule:
        diff = abs(llm_score - rule_score)
        if diff <= 1.5:
            # 一致 → 高可信，取均值
            fused = round((llm_score + rule_score) / 2, 1)
            return fused, "high", f"LLM {llm_score} 与规则 {rule_score} 一致"
        elif diff <= 3.0:
            # 中等偏差 → 中可信，偏向 LLM
            fused = round(llm_score * 0.6 + rule_score * 0.4, 1)
            return fused, "medium", f"LLM {llm_score} 与规则 {rule_score} 有偏差，偏向LLM"
        else:
            # 大偏差 → 中可信，标注矛盾
            fused = round(llm_score * 0.7 + rule_score * 0.3, 1)
            return fused, "medium", f"LLM {llm_score} 与规则 {rule_score} 矛盾较大"

    if has_llm:
        conf = "medium" if metrics_conf == "high" else "low"
        return round(llm_score, 1), conf, f"仅LLM评分 {llm_score}，无规则交叉验证"

    if has_rule:
        return round(rule_score, 1), "low", f"仅规则评分 {rule_score}，无LLM评分"

    return 5.0, "low", "无评分数据"


# ===================== 报告渲染 =====================

def render_literary_metrics_report(metrics: Dict[str, Any]) -> str:
    """渲染文本质感量化报告文本，可直接插入报告。"""
    if not metrics or metrics.get("sample_chars", 0) == 0:
        return ""

    lines = []
    conf_label = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低"}.get(metrics.get("confidence", "low"), "🔴 低")
    lines.append(f"（采样 {metrics['sample_chars']:,} 字 / 全文 {metrics['total_chars']:,} 字，可信度 {conf_label}）")
    lines.append("")

    # 模板词
    td = metrics["template_word_density"]
    if td > 0:
        lines.append(f"  模板词密度：{td}/万字  {'⚠️ 偏高' if td > 15 else '✅ 正常' if td < 8 else '⚡ 中等'}")
        hits = metrics.get("template_word_hits", {})
        for cat, items in hits.items():
            top = items[:3]
            detail = "、".join(f"{it['word']}×{it['count']}" for it in top)
            lines.append(f"    {cat}：{detail}")

    # 对话
    dr = metrics["dialogue_ratio"]
    lines.append(f"  对话占比：{dr:.1%}  {'📝 对话驱动' if dr > 0.25 else '📖 叙述为主' if dr < 0.1 else ''}")

    # 句长
    avg_sl = metrics["avg_sentence_len"]
    lines.append(f"  平均句长：{avg_sl:.0f} 字  {'较长' if avg_sl > 40 else '适中' if avg_sl >= 15 else '偏短'}")

    # 感叹号/省略号
    lines.append(f"  感叹号密度：{metrics['exclamation_density']}/万字  省略号密度：{metrics['ellipsis_density']}/万字")

    # 字符多样性
    cd = metrics["char_diversity"]
    lines.append(f"  字符多样性：{cd:.3f}  {'丰富' if cd > 0.2 else '一般' if cd > 0.12 else '单调'}")

    # 章节
    cc = metrics["chapter_count"]
    if cc > 0:
        cv = metrics["chapter_length_cv"]
        lines.append(f"  章节数：{cc}  长度变异系数：{cv:.2f}  {'均匀' if cv < 0.3 else '不均匀' if cv > 0.7 else '中等'}")
        if metrics["long_chapter_ratio"] > 0:
            lines.append(f"  长章占比：{metrics['long_chapter_ratio']:.0%}  短章占比：{metrics['short_chapter_ratio']:.0%}")

    # 程序化评分
    lines.append("")
    lines.append("  程序化评分（仅基于文本统计，非 AI 判断）：")
    lines.append(f"    文笔水准：{metrics['rule_writing_score']}/10")
    lines.append(f"    节奏把控：{metrics['rule_pacing_score']}/10")
    lines.append(f"    情绪调动：{metrics['rule_emotion_score']}/10")

    return "\n".join(lines)


def render_fused_confidence_report(fused_scores: Dict[str, Dict[str, Any]]) -> str:
    """渲染融合可信度报告，可直接插入报告。"""
    if not fused_scores:
        return ""

    lines = ["", "【评分可信度报告】"]
    conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}
    dim_labels = {
        "plot": "剧情质量", "characters": "人物塑造", "worldbuilding": "世界观",
        "pacing": "节奏把控", "writing": "文笔水准", "emotion": "情绪调动",
    }

    for dim_key, label in dim_labels.items():
        entry = fused_scores.get(dim_key, {})
        if not entry:
            continue
        score = entry.get("score", 0)
        conf = entry.get("confidence", "low")
        note = entry.get("note", "")
        emoji = conf_emoji.get(conf, "🔴")
        lines.append(f"  {label}：{score}/10  {emoji} {note}")

    return "\n".join(lines)
