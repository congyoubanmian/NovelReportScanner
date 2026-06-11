"""
sentiment_arcs.py — 情感曲线分析引擎（纯规则统计，零 LLM 成本）

从原文直接按章节计算情感极性，生成全书情感轨迹，归类为经典叙事弧线。
每个指标附采样量和可信度标注。

设计原则：
- 只用中文情感词典 + 简单计数，不调 LLM
- 每个指标标注可信度（基于章节覆盖率和词典匹配率）
- 输出可直接插入报告的文本块
"""

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

SENTIMENT_ARCS_ENABLED = os.environ.get("SENTIMENT_ARCS_ENABLED", "1").strip() == "1"
SENTIMENT_ARCS_MIN_CHAPTERS = int(os.environ.get("SENTIMENT_ARCS_MIN_CHAPTERS", "5") or "5")

# ===================== 中文情感词典 =====================

POSITIVE_WORDS = {
    "快乐", "高兴", "欢喜", "开心", "幸福", "满足", "喜悦", "欣慰", "兴奋",
    "激动", "欣喜", "振奋", "畅快", "愉悦", "陶醉", "感动", "温暖", "温馨",
    "甜蜜", "浪漫", "希望", "光明", "美好", "善良", "勇敢", "坚强", "自信",
    "骄傲", "自豪", "崇敬", "敬佩", "尊重", "珍惜", "感恩", "热爱", "热情",
    "友好", "和睦", "和谐", "安宁", "平静", "安心", "放心", "信任", "忠诚",
    "真诚", "坦率", "宽容", "包容", "大方", "慷慨", "无私", "奉献", "助人",
    "团结", "合作", "友谊", "友爱", "深情", "眷恋", "思念", "牵挂", "祝福",
    "成功", "胜利", "凯旋", "突破", "进步", "成长", "收获", "丰收", "荣耀",
    "辉煌", "灿烂", "绚丽", "壮观", "壮丽", "威武", "雄伟", "磅礴", "浩荡",
    "欢笑", "微笑", "笑容", "笑意", "喜悦", "得意", "称心", "如意", "顺心",
    "舒适", "惬意", "悠闲", "轻松", "自在", "洒脱", "逍遥", "飘逸", "超然",
    "聪慧", "智慧", "才华", "精妙", "巧妙", "高明", "出色", "优秀", "卓越",
    "精致", "精美", "瑰丽", "奇妙", "神奇", "非凡", "绝妙", "完美", "圆满",
    "和解", "团圆", "重逢", "相认", "归来", "回归", "复苏", "新生", "涅槃",
    "逆袭", "翻盘", "扭转", "拯救", "救赎", "守护", "保护", "庇护", "扶持",
    "敬佩", "仰慕", "崇拜", "钦佩", "赞叹", "喝彩", "欢呼", "鼓舞", "激励",
    "深情", "柔情", "温存", "体贴", "关怀", "呵护", "怜惜", "疼爱", "宠爱",
}

NEGATIVE_WORDS = {
    "悲伤", "痛苦", "绝望", "恐惧", "愤怒", "仇恨", "厌恶", "鄙视", "嫉妒",
    "焦虑", "不安", "紧张", "恐惧", "惊恐", "惶恐", "恐慌", "忧虑", "担忧",
    "烦恼", "苦闷", "郁闷", "沮丧", "颓废", "消沉", "低落", "失落", "失望",
    "遗憾", "后悔", "懊悔", "愧疚", "内疚", "羞愧", "惭愧", "尴尬", "难堪",
    "屈辱", "耻辱", "侮辱", "欺凌", "压迫", "剥削", "虐待", "折磨", "煎熬",
    "孤独", "寂寞", "凄凉", "冷清", "萧条", "荒凉", "惨淡", "黯淡", "阴暗",
    "黑暗", "邪恶", "残忍", "残暴", "凶狠", "狠毒", "阴险", "狡诈", "虚伪",
    "背叛", "欺骗", "出卖", "陷害", "诬陷", "栽赃", "诽谤", "中伤", "污蔑",
    "失败", "挫折", "困难", "艰难", "困境", "危机", "灾难", "浩劫", "劫难",
    "死亡", "牺牲", "丧命", "遇害", "被杀", "阵亡", "殉职", "殉难", "殉情",
    "离别", "分离", "诀别", "永别", "失散", "流离", "颠沛", "漂泊", "流浪",
    "哭泣", "流泪", "泪水", "泪痕", "泪目", "痛哭", "哀嚎", "悲鸣", "呜咽",
    "毁灭", "破坏", "摧毁", "粉碎", "破碎", "崩塌", "坍塌", "瓦解", "溃败",
    "困苦", "贫苦", "贫困", "穷困", "饥寒", "饥荒", "饥饿", "挨饿", "受冻",
    "疾病", "病痛", "伤痛", "创伤", "伤口", "流血", "鲜血", "血腥", "惨烈",
    "阴谋", "诡计", "陷阱", "圈套", "算计", "暗算", "偷袭", "伏击", "埋伏",
    "冤枉", "委屈", "冤屈", "不公", "不平", "偏见", "歧视", "排斥", "孤立",
    "压抑", "窒息", "沉重", "凝重", "肃杀", "肃穆", "森严", "严酷", "冷酷",
}

# 章节分割正则（复用 literary_metrics 的模式）
_CHAPTER_RE = re.compile(
    r"^\s*(?:第[\u4e00-\u9fff零〇一二三四五六七八九十百千万0-9]+[章节回卷部集]"
    r"|序章|楔子|终章|尾声|番外)\s*",
    re.MULTILINE,
)


# ===================== 核心计算 =====================

def compute_sentiment_arcs(text: str) -> Dict[str, Any]:
    """
    纯规则计算全书情感曲线。

    返回：
    {
        "total_chapters": int,
        "analyzed_chapters": int,
        "chapter_polarities": [{"index": int, "polarity": float, "chars": int}, ...],
        "overall_polarity": float,          # 全书情绪极性 [-1, +1]
        "polarity_std": float,              # 波动量（标准差）
        "arc_type": str,                    # 叙事弧线类型
        "arc_type_cn": str,                 # 中文名
        "most_positive_chapter": int,       # 最振奋章节
        "most_negative_chapter": int,       # 最压抑章节
        "positive_word_count": int,
        "negative_word_count": int,
        "match_rate": float,                # 词典匹配率
        "confidence": str,                  # "high"/"medium"/"low"
        "confidence_note": str,
    }
    """
    if not text:
        return _empty_arcs()

    chapters = _split_chapters(text)
    if len(chapters) < SENTIMENT_ARCS_MIN_CHAPTERS:
        return _empty_arcs(len(chapters))

    chapter_pols = []
    total_pos = 0
    total_neg = 0
    total_chars = 0

    for idx, ch_text in enumerate(chapters):
        if not ch_text or len(ch_text.strip()) < 50:
            continue
        pos_count = sum(ch_text.count(w) for w in POSITIVE_WORDS)
        neg_count = sum(ch_text.count(w) for w in NEGATIVE_WORDS)
        total = pos_count + neg_count
        ch_chars = len(ch_text)
        total_pos += pos_count
        total_neg += neg_count
        total_chars += ch_chars

        polarity = 0.0
        if total > 0:
            polarity = (pos_count - neg_count) / total
        chapter_pols.append({
            "index": idx,
            "polarity": round(polarity, 4),
            "chars": ch_chars,
        })

    if not chapter_pols:
        return _empty_arcs(len(chapters))

    analyzed = len(chapter_pols)
    polarities = [cp["polarity"] for cp in chapter_pols]

    overall = 0.0
    if total_pos + total_neg > 0:
        overall = round((total_pos - total_neg) / (total_pos + total_neg), 4)

    pol_std = _std(polarities) if len(polarities) >= 2 else 0.0

    match_rate = 0.0
    if total_chars > 0:
        match_rate = round((total_pos + total_neg) * 10000 / total_chars, 2)

    arc_type, arc_cn = _classify_arc(polarities)
    most_pos = max(chapter_pols, key=lambda x: x["polarity"])["index"]
    most_neg = min(chapter_pols, key=lambda x: x["polarity"])["index"]

    coverage = analyzed / max(1, len(chapters))
    conf = "high" if coverage >= 0.8 and match_rate >= 5 else "medium" if coverage >= 0.5 else "low"

    return {
        "total_chapters": len(chapters),
        "analyzed_chapters": analyzed,
        "chapter_polarities": chapter_pols,
        "overall_polarity": overall,
        "polarity_std": round(pol_std, 4),
        "arc_type": arc_type,
        "arc_type_cn": arc_cn,
        "most_positive_chapter": most_pos,
        "most_negative_chapter": most_neg,
        "positive_word_count": total_pos,
        "negative_word_count": total_neg,
        "match_rate": match_rate,
        "confidence": conf,
        "confidence_note": f"覆盖{analyzed}/{len(chapters)}章，词典匹配{match_rate}/万字",
    }


def _split_chapters(text: str) -> List[str]:
    """按章节标题分割文本。"""
    splits = _CHAPTER_RE.split(text)
    if len(splits) <= 1:
        chunk_size = max(5000, len(text) // 30)
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] if text else []
    return [s.strip() for s in splits if s.strip()]


def _classify_arc(polarities: List[float]) -> Tuple[str, str]:
    """
    将情感极性序列归类为 6 种经典叙事弧线。

    基于 Reagan et al. (2016) 的方法简化：
    - 比较前1/3、中1/3、后1/3的均值
    """
    n = len(polarities)
    if n < 3:
        return "flat", "平稳型"

    third = max(1, n // 3)
    first = _mean(polarities[:third])
    mid = _mean(polarities[third:2 * third])
    last = _mean(polarities[2 * third:])

    up = first < mid < last
    down = first > mid > last
    v_shape = first > mid < last
    inv_v = first < mid > last
    rise_fall_rise = first < mid > last and first < last
    fall_rise_fall = first > mid < last and first > last

    if up:
        return "rags_to_riches", "持续上升型（白手起家）"
    if down:
        return "riches_to_rags", "持续下降型（由盛转衰）"
    if fall_rise_fall:
        return "oedipus", "降→升→降型（俄狄浦斯）"
    if rise_fall_rise:
        return "cinderella", "升→降→升型（灰姑娘）"
    if v_shape:
        return "man_in_hole", "先苦后甜型（绝处逢生）"
    if inv_v:
        return "icarus", "先甜后苦型（盛极而衰）"

    return "flat", "平稳型"


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _empty_arcs(chapter_count: int = 0) -> Dict[str, Any]:
    return {
        "total_chapters": chapter_count,
        "analyzed_chapters": 0,
        "chapter_polarities": [],
        "overall_polarity": 0.0,
        "polarity_std": 0.0,
        "arc_type": "unknown",
        "arc_type_cn": "数据不足",
        "most_positive_chapter": -1,
        "most_negative_chapter": -1,
        "positive_word_count": 0,
        "negative_word_count": 0,
        "match_rate": 0.0,
        "confidence": "low",
        "confidence_note": f"仅{chapter_count}章，不足分析最低要求",
    }


# ===================== 报告渲染 =====================

def render_sentiment_arcs_report(arcs: Dict[str, Any]) -> str:
    """渲染情感曲线报告，可直接插入报告。"""
    if not arcs or arcs.get("analyzed_chapters", 0) < SENTIMENT_ARCS_MIN_CHAPTERS:
        return ""

    lines = []
    conf = arcs.get("confidence", "low")
    conf_label = {"high": "🟢 高", "medium": "🟡 中", "low": "🔴 低"}.get(conf, "🔴 低")
    lines.append(f"（分析 {arcs['analyzed_chapters']}/{arcs['total_chapters']} 章，可信度 {conf_label}）")
    lines.append("")

    overall = arcs["overall_polarity"]
    overall_label = "偏正面" if overall > 0.15 else "偏负面" if overall < -0.15 else "中性"
    lines.append(f"  情绪轨迹类型：{arcs['arc_type_cn']}")
    lines.append(f"  全书情绪极性：{overall:+.2f}（{overall_label}）")
    lines.append(f"  情绪波动量：±{arcs['polarity_std']:.2f}{'（高波动）' if arcs['polarity_std'] > 0.3 else '（中等波动）' if arcs['polarity_std'] > 0.15 else '（平稳）'}")
    lines.append(f"  最振奋段落：第{arcs['most_positive_chapter'] + 1}章附近")
    lines.append(f"  最压抑段落：第{arcs['most_negative_chapter'] + 1}章附近")

    pos = arcs["positive_word_count"]
    neg = arcs["negative_word_count"]
    total = pos + neg
    if total > 0:
        lines.append(f"  正面情感词：{pos}次（{pos / total:.0%}）  负面情感词：{neg}次（{neg / total:.0%}）")
    lines.append(f"  词典匹配率：{arcs['match_rate']}/万字")

    # ASCII 情感曲线
    chapter_pols = arcs.get("chapter_polarities", [])
    if len(chapter_pols) >= 8:
        lines.append("")
        lines.append("  情感曲线（高度=情绪极性，正值=正面，负值=负面）：")
        curve_lines = _render_polarity_curve([(cp["index"], cp["polarity"]) for cp in chapter_pols])
        for cl in curve_lines:
            lines.append(f"  {cl}")

    return "\n".join(lines)


def _render_polarity_curve(
    data: List[Tuple[int, float]],
    width: int = 60,
    height: int = 8,
) -> List[str]:
    """渲染 [-1, +1] 范围的 ASCII 情感曲线。"""
    if not data:
        return []

    n = len(data)
    if n <= width:
        sampled = data
    else:
        step = n / width
        sampled = [data[int(i * step)] for i in range(width)]

    values = [v for _, v in sampled]
    rows = []
    for row in range(height, 0, -1):
        threshold = (row / height) * 2 - 1  # [-1, +1]
        chars = []
        for val in values:
            if val >= threshold - (2.0 / height) and val < threshold + (2.0 / height) / 2:
                chars.append("█")
            elif val >= threshold - (2.0 / height):
                chars.append("▄" if threshold > 0 else "▀")
            else:
                chars.append(" ")
        label = f"{threshold:+.1f}"
        rows.append(f"{label:>4}│{''.join(chars)}")

    # 零线
    rows.append(f"   0│{'─' * len(sampled)}")
    rows.append(f"     └{'─' * len(sampled)}")
    rows.append(f"      章节 1→{sampled[-1][0] + 1}")

    return rows
