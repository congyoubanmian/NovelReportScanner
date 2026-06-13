"""
名字归一化与拆分工具 — 从 protagonist.py 提取的纯函数模块。

提供人名规范化、多名字拆分、枚举检测、近似名比较等零依赖功能。
"""

import re

from name_authority import is_generic_person_name


def normalize_person_name(name: str) -> str:
    """
    规范化人名字符串：
    - 去首尾空白/引号/全角空格
    - 统一部分标点（全角逗号等）
    - 压缩多余空白
    注意：不做大小写/空格强行移除（避免影响外国人名，如 'Jean Pierre'）。
    """
    if name is None:
        return ""
    s = str(name).strip().strip("\u3000")
    # 去掉常见引号
    s = s.strip("""\"'`""")
    # 统一中文逗号/分号为英文逗号，便于后续拆分判断
    s = s.replace("，", ",").replace("；", ";").replace("：", ":")
    # 压缩多空白
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_multi_names(name: str):
    """
    将可能被模型/作者写在同一个字段里的"多个角色名"拆分出来。

    重点处理：'、' / ',' / '，' 等枚举分隔。
    安全策略：如果两侧都是纯英文(含空格/点/连字符)，不拆分（避免 'Smith, John' 之类外文格式）。
    """
    s = normalize_person_name(name)
    if not s:
        return []
    if is_group_or_title_enumeration_name(s):
        return []

    # 最强拆分：顿号（中文枚举）
    if "、" in s:
        parts = split_top_level_name_parts(s, {"、"})
        # 过滤明显无效段
        parts = [normalize_person_name(p) for p in parts if normalize_person_name(p)]
        return parts if len(parts) >= 2 else [s]

    # 次强拆分：逗号（仅当包含中日韩字符时才拆）
    if "," in s:
        parts = split_top_level_name_parts(s, {","})
        if len(parts) < 2:
            return [s]

        ascii_wordish = re.compile(r"^[A-Za-z][A-Za-z .'\-]*$")
        # 两侧都像英文名 → 不拆
        if all(ascii_wordish.match(p) for p in parts):
            return [s]
        # 有明显中日韩字符 → 拆
        has_cjk = any(re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", p) for p in parts)
        if has_cjk:
            parts = [normalize_person_name(p) for p in parts if normalize_person_name(p)]
            return parts if len(parts) >= 2 else [s]

    return [s]


def split_top_level_name_parts(name: str, separators: set[str]) -> list[str]:
    parts = []
    current = []
    depth = 0
    open_marks = {"（", "("}
    close_marks = {"）", ")"}
    for ch in name:
        if ch in open_marks:
            depth += 1
            current.append(ch)
            continue
        if ch in close_marks:
            depth = max(0, depth - 1)
            current.append(ch)
            continue
        if depth == 0 and ch in separators:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(ch)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def is_group_or_title_enumeration_name(name: str) -> bool:
    s = normalize_person_name(name)
    if not s:
        return True
    core = re.sub(r"[（(][^）)]*[）)]", "", s).strip()
    if is_generic_person_name(core):
        return True
    group_terms = (
        "各州", "诸州", "众", "群", "等人", "一行人", "刺史", "官员", "群臣",
        "将领", "士兵", "侍卫", "护卫", "家丁", "丫鬟", "仆役", "百姓", "商人",
        "使者", "官吏", "文官", "武将", "勋贵", "门客", "弟子", "族人",
    )
    if any(term in s for term in group_terms) and any(mark in s for mark in ("、", ",", "，", "等", "诸", "各")):
        return True
    return False


def levenshtein_distance(a: str, b: str, max_dist: int = 2) -> int:
    """小字符串编辑距离（带上限早停），用于错别字/近似名候选判断。"""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    # 早停：长度差过大不算近似
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    # DP（带上限剪枝）
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            dele = prev[j] + 1
            rep = prev[j - 1] + (0 if ca == cb else 1)
            v = min(ins, dele, rep)
            cur.append(v)
            if v < row_min:
                row_min = v
        prev = cur
        if row_min > max_dist:
            return max_dist + 1
    return prev[-1]


def quick_text_similarity(a_texts, b_texts) -> float:
    """
    基于摘要/互动的快速相似度：把多条文本拼接后做 SequenceMatcher。
    仅用于"是否有足够证据合并"的兜底判定。
    """
    try:
        from difflib import SequenceMatcher
        a = " ".join([str(x) for x in (a_texts or []) if x])[:8000]
        b = " ".join([str(x) for x in (b_texts or []) if x])[:8000]
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a, b).ratio()
    except Exception:
        return 0.0
