"""
reading_metrics.py — 阅读体验量化聚合与可视化

从 chunk 扫描结果中提取 LLM 已经输出的评分维度（tension, emotion, pacing 等），
做全书的统计聚合、波动分析、趋势曲线，并渲染为 ASCII 图表。

设计原则：
- 不发明新算法，只聚合 LLM 已有的判断
- 输出置信区间（基于样本量+标准差）
- ASCII 可视化无需前端即可阅读
"""

import math
import os
from typing import Any, Dict, List, Optional, Tuple
from shared_utils import _mean, _std

READING_METRICS_ENABLED = os.environ.get("READING_METRICS_ENABLED", "1").strip() == "1"
READING_METRICS_MIN_CHUNKS = int(os.environ.get("READING_METRICS_MIN_CHUNKS", "5") or "5")


# ===================== 数据提取 =====================

def extract_chunk_scores(chunk_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    从 chunk_results 提取每个 chunk 的评分数据。

    返回列表，每项：
    {
        "chunk_index": int,
        "tension": float,        # 0-10 张力
        "emotion_intensity": float, # 0-10 情绪强度
        "emotion_tone": str,      # 爽/虐/燃/悲/悬/平...
        "pacing_type": str,       # fast/slow/climax/transition...
        "engagement": str,        # high/medium/low
        "has_payoff": bool,
        "has_suffering": bool,
        "cliffhanger": str,       # strong/medium/weak/none
    }
    """
    scores = []
    for chunk in chunk_results:
        if not isinstance(chunk, dict):
            continue
        pacing = chunk.get("pacing_analysis") or {}
        experience = chunk.get("reader_experience") or {}

        # engagement 可能在 pacing 或 experience 里
        engagement = (pacing.get("reader_engagement_prediction")
                      or experience.get("engagement_level") or "")

        scores.append({
            "chunk_index": chunk.get("chunk_index", 0),
            "tension": _safe_float(pacing.get("tension_level")),
            "emotion_intensity": _safe_float(pacing.get("emotion_intensity")),
            "emotion_tone": str(pacing.get("emotion_tone") or "").strip(),
            "pacing_type": str(pacing.get("pacing_type") or "").strip().lower(),
            "engagement": str(engagement).strip().lower(),
            "has_payoff": bool(pacing.get("payoff_moment", "").strip()),
            "has_suffering": bool(pacing.get("suffering_moment", "").strip()),
            "cliffhanger": _normalize_cliffhanger(pacing.get("cliffhanger_quality")),
        })
    return scores


def _safe_float(value) -> float:
    try:
        return max(0.0, min(10.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_cliffhanger(value) -> str:
    v = str(value or "").strip().lower()
    if v in ("strong", "medium", "weak", "none"):
        return v
    return "none"


# ===================== 统计聚合 =====================

def aggregate_metrics(chunk_scores: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    聚合全书的阅读体验指标。

    返回：
    {
        "total_chunks": int,
        "avg_tension": float,
        "avg_emotion": float,
        "tension_std": float,         # 波动（标准差）
        "emotion_std": float,
        "trend_tension": str,         # "上升"/"下降"/"平稳"/"先升后降"...
        "trend_emotion": str,
        "emotion_distribution": {"爽": 12, "虐": 5, ...},
        "pacing_distribution": {"fast": 10, "slow": 3, ...},
        "payoff_rate": float,         # 有爽点的 chunk 占比
        "suffering_rate": float,
        "cliffhanger_rate": float,
        "engagement_distribution": {"high": 20, "medium": 10, "low": 2},
        "high_tension_chunks": [int],  # tension >= 7 的 chunk 索引
        "low_engagement_zones": [int], # engagement=low 的连续段
        "confidence": float,          # 0-1 置信度（基于样本量）
    }
    """
    n = len(chunk_scores)
    if n < READING_METRICS_MIN_CHUNKS:
        return _low_confidence_result(n)

    tensions = [s["tension"] for s in chunk_scores if s["tension"] > 0]
    emotions = [s["emotion_intensity"] for s in chunk_scores if s["emotion_intensity"] > 0]

    avg_tension = _mean(tensions) if tensions else 0.0
    avg_emotion = _mean(emotions) if emotions else 0.0
    tension_std = _std(tensions) if len(tensions) >= 2 else 0.0
    emotion_std = _std(emotions) if len(emotions) >= 2 else 0.0

    # 趋势：比较前半/后半均值
    trend_tension = _compute_trend(tensions)
    trend_emotion = _compute_trend(emotions)

    # 分布统计
    emotion_dist = _count_values([s["emotion_tone"] for s in chunk_scores if s["emotion_tone"]])
    pacing_dist = _count_values([s["pacing_type"] for s in chunk_scores if s["pacing_type"]])
    engagement_dist = _count_values([s["engagement"] for s in chunk_scores if s["engagement"]])

    # 爽/虐/悬念比率
    payoff_count = sum(1 for s in chunk_scores if s["has_payoff"])
    suffering_count = sum(1 for s in chunk_scores if s["has_suffering"])
    cliffhanger_count = sum(1 for s in chunk_scores if s["cliffhanger"] in ("strong", "medium"))

    # 高张力区域
    high_tension = [s["chunk_index"] for s in chunk_scores if s["tension"] >= 7.0]

    # 低投入连续段
    low_engagement_zones = _find_low_zones(chunk_scores)

    # 置信度：基于有效样本量
    confidence = min(1.0, len(tensions) / 30.0)  # 30个以上满分

    return {
        "total_chunks": n,
        "valid_tension_samples": len(tensions),
        "valid_emotion_samples": len(emotions),
        "avg_tension": round(avg_tension, 2),
        "avg_emotion": round(avg_emotion, 2),
        "tension_std": round(tension_std, 2),
        "emotion_std": round(emotion_std, 2),
        "trend_tension": trend_tension,
        "trend_emotion": trend_emotion,
        "emotion_distribution": emotion_dist,
        "pacing_distribution": pacing_dist,
        "payoff_rate": round(payoff_count / n, 3),
        "suffering_rate": round(suffering_count / n, 3),
        "cliffhanger_rate": round(cliffhanger_count / n, 3),
        "engagement_distribution": engagement_dist,
        "high_tension_chunks": high_tension[:20],
        "low_engagement_zones": low_engagement_zones[:10],
        "confidence": round(confidence, 2),
    }


def _low_confidence_result(n: int) -> Dict[str, Any]:
    return {
        "total_chunks": n,
        "valid_tension_samples": 0,
        "valid_emotion_samples": 0,
        "avg_tension": 0.0,
        "avg_emotion": 0.0,
        "tension_std": 0.0,
        "emotion_std": 0.0,
        "trend_tension": "数据不足",
        "trend_emotion": "数据不足",
        "emotion_distribution": {},
        "pacing_distribution": {},
        "payoff_rate": 0.0,
        "suffering_rate": 0.0,
        "cliffhanger_rate": 0.0,
        "engagement_distribution": {},
        "high_tension_chunks": [],
        "low_engagement_zones": [],
        "confidence": 0.0,
    }


# ===================== 数学工具 =====================
# _mean / _std 已合并到 shared_utils.py

def _compute_trend(values: List[float]) -> str:
    """比较前半/后半均值判断趋势。"""
    if len(values) < 6:
        return "数据不足"
    mid = len(values) // 2
    first_half = _mean(values[:mid])
    second_half = _mean(values[mid:])

    diff = second_half - first_half
    threshold = _std(values) * 0.3 if _std(values) > 0 else 0.3

    if diff > threshold:
        return "上升"
    elif diff < -threshold:
        return "下降"
    else:
        return "平稳"


def _count_values(items: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        if not item:
            continue
        counts[item] = counts.get(item, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _find_low_zones(chunk_scores: List[Dict[str, Any]], min_run: int = 3) -> List[int]:
    """找连续 engagement=low 的区域起始 chunk_index。"""
    zones = []
    run_start = None
    for i, s in enumerate(chunk_scores):
        if s["engagement"] == "low":
            if run_start is None:
                run_start = s["chunk_index"]
        else:
            if run_start is not None and (i - (i - 1)) >= min_run - 1:
                # 简化：记录连续low的起始
                zones.append(run_start)
            run_start = None
    return zones


# ===================== ASCII 可视化 =====================

def render_reading_experience_report(metrics: Dict[str, Any], chunk_scores: List[Dict[str, Any]] = None) -> str:
    """
    渲染阅读体验量化报告文本，含 ASCII 曲线图。

    返回可直接插入报告的文本块。
    """
    if not READING_METRICS_ENABLED:
        return ""
    if not metrics or metrics.get("total_chunks", 0) < READING_METRICS_MIN_CHUNKS:
        return ""

    lines = []
    n = metrics["total_chunks"]
    conf = metrics["confidence"]

    # 置信度标注
    conf_label = "高" if conf >= 0.8 else "中" if conf >= 0.5 else "低"
    lines.append(f"（基于 {metrics['valid_tension_samples']} 个有效片段，置信度 {conf_label}）")

    # 核心指标
    lines.append("")
    lines.append(f"  平均张力：{metrics['avg_tension']:.1f}/10  波动：±{metrics['tension_std']:.1f}  趋势：{metrics['trend_tension']}")
    lines.append(f"  情绪强度：{metrics['avg_emotion']:.1f}/10  波动：±{metrics['emotion_std']:.1f}  趋势：{metrics['trend_emotion']}")
    lines.append(f"  爽点密度：{metrics['payoff_rate']:.0%}  虐点密度：{metrics['suffering_rate']:.0%}  悬念钩子：{metrics['cliffhanger_rate']:.0%}")

    # 情绪分布
    emo_dist = metrics.get("emotion_distribution", {})
    if emo_dist:
        top_emo = list(emo_dist.items())[:6]
        emo_str = "  ".join(f"{k}×{v}" for k, v in top_emo)
        lines.append(f"  情绪色调：{emo_str}")

    # 节奏分布
    pac_dist = metrics.get("pacing_distribution", {})
    if pac_dist:
        top_pac = list(pac_dist.items())[:6]
        pac_str = "  ".join(f"{k}×{v}" for k, v in top_pac)
        lines.append(f"  节奏分布：{pac_str}")

    # 投入度分布
    eng_dist = metrics.get("engagement_distribution", {})
    if eng_dist:
        high_eng = eng_dist.get("high", 0)
        med_eng = eng_dist.get("medium", 0)
        low_eng = eng_dist.get("low", 0)
        total_eng = high_eng + med_eng + low_eng
        if total_eng > 0:
            lines.append(f"  读者投入度：高 {high_eng/total_eng:.0%}  中 {med_eng/total_eng:.0%}  低 {low_eng/total_eng:.0%}")

    # 张力曲线
    if chunk_scores and len(chunk_scores) >= 8:
        lines.append("")
        lines.append("  张力曲线（每点=一个片段，高度=张力值）：")
        curve = _render_ascii_curve(
            [(s["chunk_index"], s["tension"]) for s in chunk_scores],
            title="张力",
            max_val=10.0,
            width=60,
            height=8,
        )
        for cl in curve:
            lines.append(f"  {cl}")

    # 高潮/低谷标记
    high_t = metrics.get("high_tension_chunks", [])
    if high_t:
        samples = high_t[:8]
        lines.append(f"  🔥 高张力片段：{', '.join(str(x) for x in samples)}{'…' if len(high_t) > 8 else ''}")

    low_zones = metrics.get("low_engagement_zones", [])
    if low_zones:
        samples = low_zones[:5]
        lines.append(f"  😐 低投入区域：片段 {', '.join(str(x) for x in samples)}{'…' if len(low_zones) > 5 else ''}")

    return "\n".join(lines)


def _render_ascii_curve(
    data: List[Tuple[int, float]],
    title: str = "",
    max_val: float = 10.0,
    width: int = 60,
    height: int = 6,
) -> List[str]:
    """
    渲染 ASCII 折线图。

    data: [(chunk_index, value), ...]
    返回每行一个字符串的列表。
    """
    if not data:
        return []

    # 采样到 width 个点
    n = len(data)
    if n <= width:
        sampled = data
    else:
        step = n / width
        sampled = [data[int(i * step)] for i in range(width)]

    values = [v for _, v in sampled]
    if not values:
        return []

    rows = []
    for row in range(height, 0, -1):
        threshold = (row / height) * max_val
        chars = []
        for val in values:
            if val >= threshold:
                chars.append("█")
            elif val >= threshold - (max_val / height):
                chars.append("▄")
            else:
                chars.append(" ")
        label = f"{threshold:.0f}"
        rows.append(f"{label:>2}│{''.join(chars)}")

    # 底部轴线
    rows.append(f"  └{'─' * len(sampled)}")
    rows.append(f"   片段 1→{sampled[-1][0]}")

    return rows
