import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from shared_utils import get_base_dir, read_file_safely


DEFAULT_PROFILE = "harem"
AUTO_PROFILE = "auto"
AUTO_PROFILE_MIN_SCORE = 6
AUTO_PROFILE_MAX_PROFILES = 3
PROFILE_INFERENCE_TEXT_LIMIT = 60000


@dataclass(frozen=True)
class AnalysisProfile:
    name: str
    display_name: str
    description: str
    enabled_stages: List[str]
    rules_file: str
    report_mode: str
    scan_focus: List[str]
    summary_fields: List[str]
    harem_plus: Dict[str, Any]
    cross_profile_rules: Dict[str, Any]
    sort_order: int = 1000
    version: str = "2.1.0"
    version_history: List[Dict[str, Any]] = None
    min_supported_scanner_version: str = "1.5.0"
    breaking_changes: bool = False

    @property
    def uses_harem_reviewer(self) -> bool:
        return "harem_reviewer" in self.enabled_stages

    @property
    def uses_general_scan(self) -> bool:
        return "general_scan" in self.enabled_stages

    @property
    def supports_harem_plus(self) -> bool:
        return self.name == "harem" and bool(self.harem_plus.get("enabled"))


@dataclass(frozen=True)
class ProfileInference:
    name: str
    display_name: str
    score: int
    confidence: float
    matched_keywords: List[str]
    confidence_level: str = "medium"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "score": self.score,
            "confidence": round(self.confidence, 3),
            "confidence_level": self.confidence_level,
            "matched_keywords": self.matched_keywords,
        }


COMBO_BONUSES = {
    "game_system": [
        ({"系统", "面板", "副本"}, 8),
        ({"系统", "面板"}, 5),
        ({"无限流", "副本", "主神"}, 10),
        ({"主神空间", "轮回空间"}, 8),
        ({"诸天", "万界", "穿梭"}, 8),
        ({"签到", "加点", "面板"}, 7),
    ],
    "simulator": [
        ({"模拟器", "人生模拟"}, 8),
        ({"模拟器", "未来推演"}, 8),
        ({"模拟结果", "结算奖励"}, 8),
        ({"天赋词条", "重开", "推演"}, 8),
    ],
    "nation_fate": [
        ({"国运", "文明试炼"}, 8),
        ({"国运擂台", "历史人物代战"}, 8),
        ({"神话擂台", "神明召唤"}, 8),
        ({"华夏", "全球对抗", "国运奖励"}, 7),
    ],
    "mastermind_hidden": [
        ({"幕后流", "马甲"}, 8),
        ({"马甲流", "掉马"}, 8),
        ({"幕后黑手", "信息差"}, 8),
        ({"隐藏身份", "暗中操控"}, 8),
        ({"多马甲", "幕后操控", "布局"}, 8),
        ({"操盘手", "多方博弈"}, 7),
    ],
    "apocalypse_survival": [
        ({"末世", "丧尸", "基地"}, 8),
        ({"末日", "幸存者", "物资"}, 8),
        ({"异能", "晶核", "进化"}, 8),
        ({"废土", "辐射", "安全区"}, 8),
        ({"极寒", "天灾", "避难所"}, 8),
    ],
    "cosmic_horror": [
        ({"克苏鲁", "旧日", "外神"}, 10),
        ({"序列", "魔药", "扮演法"}, 10),
        ({"规则怪谈", "规则", "污染"}, 8),
        ({"收容物", "模因", "认知崩溃"}, 8),
        ({"SAN值", "理智", "精神污染"}, 8),
    ],
    "chinese_weird": [
        ({"规则怪谈", "隐藏规则"}, 8),
        ({"规则怪谈", "违反规则", "逃生"}, 8),
        ({"中式诡异", "民俗", "禁忌"}, 8),
        ({"祠堂", "纸人", "红白喜事"}, 8),
        ({"怪谈副本", "通关规则"}, 8),
    ],
    "xianxia_fantasy": [
        ({"修仙", "灵根", "筑基"}, 8),
        ({"金丹", "元婴", "化神"}, 10),
        ({"宗门", "秘境", "法宝"}, 7),
        ({"洪荒", "封神", "天庭"}, 8),
        ({"圣体", "武魂", "血脉"}, 8),
    ],
    "steampunk_fantasy": [
        ({"蒸汽", "炼金"}, 6),
        ({"蒸汽朋克", "西幻"}, 8),
        ({"差分机", "蒸汽", "炼金矩阵"}, 8),
    ],
    "history": [
        ({"三国", "诸侯", "大汉"}, 8),
        ({"大明", "锦衣卫", "宦官"}, 8),
        ({"朝廷", "科举", "士族"}, 7),
        ({"皇帝", "宰相", "边军"}, 7),
        ({"穿越", "古代", "变法"}, 7),
    ],
    "farming_management": [
        ({"种田", "经营", "基建"}, 8),
        ({"领地", "农田", "作坊"}, 8),
        ({"产业链", "供应链", "产量"}, 7),
        ({"灵田", "药园", "宗门建设"}, 7),
        ({"基地", "生产链", "资源"}, 6),
    ],
    "entertainment_industry": [
        ({"娱乐圈", "影帝", "影后"}, 8),
        ({"选秀", "练习生", "出道"}, 8),
        ({"剧组", "导演", "热搜"}, 7),
        ({"网红", "MCN", "饭圈"}, 7),
    ],
    "sports_competition": [
        ({"篮球", "联赛", "教练"}, 8),
        ({"足球", "俱乐部", "战术"}, 8),
        ({"电竞", "战队", "赛季"}, 8),
        ({"围棋", "世锦赛", "棋手"}, 8),
        ({"拳击", "格斗", "回合"}, 8),
    ],
    "crime_forensics": [
        ({"刑警", "法医", "尸检"}, 8),
        ({"案发现场", "证据链", "嫌疑人"}, 8),
        ({"毒理", "DNA", "指纹"}, 8),
        ({"专案组", "禁毒", "扫黑"}, 8),
    ],
    "mystery_detective": [
        ({"密室", "诡计", "侦探"}, 8),
        ({"暴风雪山庄", "红鲱鱼", "时刻表"}, 8),
        ({"线索", "动机", "推理"}, 6),
    ],
    "isekai_lightnovel": [
        ({"异世界", "转生", "冒险者"}, 8),
        ({"勇者", "魔王", "地下城"}, 8),
        ({"贵族", "王国", "精灵"}, 7),
    ],
    "military_war": [
        ({"军团", "火炮", "后勤"}, 8),
        ({"兵王", "军营", "演习"}, 8),
        ({"军阀", "兵工厂", "步兵"}, 8),
        ({"战区", "指挥", "补给"}, 7),
    ],
    "urban_power": [
        ({"都市", "神豪", "打脸"}, 8),
        ({"赘婿", "神医", "豪门"}, 8),
        ({"下山", "龙王", "战神"}, 8),
        ({"异能", "校花", "扮猪吃虎"}, 6),
    ],
}


NEGATIVE_KEYWORDS = {
    "game_system": [("篮球", -4), ("足球", -4), ("联赛", -3), ("教练", -3), ("电竞", -2), ("高考", -3)],
    "sports_competition": [("系统", -3), ("面板", -4), ("修仙", -3), ("魔王", -3), ("副本", -3)],
    "urban_power": [
        ("副本", -4), ("主神", -4), ("无限流", -4),
        ("宗门", -3), ("金丹", -3), ("灵根", -3),
        ("融资", -3), ("股权", -3), ("董事会", -3), ("上市", -3), ("并购", -3),
        ("供应链", -3), ("现金流", -3), ("投资人", -3),
        ("军营", -4), ("演习", -4), ("战区", -4), ("后勤", -3), ("补给", -3), ("火炮", -3),
    ],
    "farming_management": [("娱乐圈", -4), ("影帝", -3), ("影后", -3), ("明星", -3), ("球员", -3), ("联赛", -3)],
    "business_career": [("宗门", -3), ("灵田", -3), ("末世", -3), ("丧尸", -3), ("娱乐圈", -2)],
    "isekai_lightnovel": [("三国", -3), ("大明", -3), ("大唐", -3), ("朝廷", -3), ("现代都市", -3)],
    "history": [("异世界", -4), ("魔王", -3), ("冒险者", -3), ("星舰", -3), ("赛博", -3)],
    "campus_youth": [("军团", -3), ("丧尸", -3), ("董事会", -3), ("娱乐圈", -3), ("法医", -3)],
    "military_war": [("球员", -3), ("联赛", -3), ("法医", -3), ("尸检", -3), ("剧组", -3)],
    "mystery_detective": [("法医", -3), ("尸检", -3), ("刑警", -2), ("系统", -3), ("超能力", -3)],
    "crime_forensics": [("侦探", -2), ("暴风雪山庄", -3), ("红鲱鱼", -3), ("魔法", -3), ("系统", -3)],
    "entertainment_industry": [("经营农田", -3), ("领地", -3), ("宗门", -3), ("丧尸", -3)],
    "simulator": [("副本", -4), ("主神空间", -4), ("轮回空间", -4), ("玩家", -3), ("NBA", -3)],
    "nation_fate": [("公司", -3), ("校园", -3), ("娱乐圈", -3), ("操作系统", -4)],
    "mastermind_hidden": [
        ("幕后花絮", -5), ("幕后制作", -5), ("幕后团队", -4),
        ("剧组", -3), ("导演", -3), ("选秀", -3),
        ("融资", -3), ("董事会", -3), ("供应链", -3),
        ("刑警", -3), ("法医", -3), ("案发现场", -3),
        ("篮球", -3), ("足球", -3),
    ],
    "chinese_weird": [("克苏鲁", -5), ("旧日", -5), ("外神", -5), ("魔药", -4), ("序列", -4), ("扮演法", -4), ("篮球", -3)],
}


KEYWORD_CONTEXT_BLOCKS = {
    "game_system": {
        "系统": ("操作系统", "文件系统", "生态系统", "系统化", "系统性", "系统工程"),
        "等级": ("职业等级", "等级制度", "等级森严"),
        "天灾": ("第四天灾",),
    },
    "urban_power": {
        "系统": ("操作系统", "文件系统", "生态系统", "系统化", "系统性", "系统工程"),
        "修仙": ("修仙种田", "修仙聊天群"),
    },
    "apocalypse_survival": {
        "天灾": ("第四天灾",),
        "异能": ("异能者", "都市异能"),
    },
    "mystery_detective": {
        "证据": ("证据链", "物证",),
    },
    "farming_management": {
        "工厂": ("兵工厂", "军工厂"),
    },
    "xianxia_fantasy": {
        "修仙": ("修仙种田",),
    },
    "mastermind_hidden": {
        "隐藏身份": ("没有隐藏身份", "无隐藏身份", "并无隐藏身份", "并非隐藏身份"),
        "马甲": ("没有隐藏身份、马甲", "没有马甲", "无马甲", "并无马甲", "并非马甲"),
        "幕后操控": ("没有隐藏身份、马甲或幕后操控", "没有幕后操控", "无幕后操控", "并无幕后操控"),
        "幕后": ("没有隐藏身份、马甲或幕后操控", "幕后花絮", "幕后制作", "幕后团队"),
        "布局": ("工作布局", "页面布局", "布局和团队协作"),
    },
}


PROFILE_MIN_SCORE_OVERRIDES = {
    "cosmic_horror": 5,
    "hard_sci_fi": 5,
    "entertainment_industry": 5,
    "campus_youth": 5,
    "harem": 5,
    "steampunk_fantasy": 5,
    "nation_fate": 5,
    "mastermind_hidden": 5,
    "simulator": 5,
    "chinese_weird": 5,
    "urban_power": 7,
    "game_system": 7,
}

KEYWORD_NEGATION_HINTS = (
    "没有", "没", "无", "未见", "并无", "并未", "不含", "不是", "并非", "缺少", "缺乏",
)

PROFILE_GENERIC_ONLY_KEYWORDS = {
    "game_system": {"系统"},
    "urban_power": {"系统"},
    "mastermind_hidden": {"幕后", "布局", "操纵", "暗中", "操控", "伪装", "面具"},
}


def normalize_profile_name(value: str) -> str:
    name = (value or DEFAULT_PROFILE).strip().lower()
    aliases = {
        "后宫": "harem",
        "后宫类": "harem",
        "男性向": "harem",
        "通用": "general",
        "普通": "general",
        "常规": "general",
        "自动": "auto",
        "自动识别": "auto",
        "历史": "history",
        "历史小说": "history",
        "硬核": "hard_sci_fi",
        "硬科幻": "hard_sci_fi",
        "科幻": "hard_sci_fi",
        "仙侠": "xianxia_fantasy",
        "玄幻": "xianxia_fantasy",
        "修仙": "xianxia_fantasy",
        "仙侠玄幻": "xianxia_fantasy",
        "悬疑": "mystery_detective",
        "推理": "mystery_detective",
        "侦探": "mystery_detective",
        "悬疑推理": "mystery_detective",
        "游戏": "game_system",
        "系统": "game_system",
        "无限流": "game_system",
        "游戏系统": "game_system",
        "模拟器": "simulator",
        "人生模拟": "simulator",
        "模拟器流": "simulator",
        "未来推演": "simulator",
        "国运": "nation_fate",
        "文明": "nation_fate",
        "国运文": "nation_fate",
        "文明对抗": "nation_fate",
        "国运流": "nation_fate",
        "幕后": "mastermind_hidden",
        "马甲": "mastermind_hidden",
        "幕后流": "mastermind_hidden",
        "马甲流": "mastermind_hidden",
        "幕后黑手": "mastermind_hidden",
        "多马甲": "mastermind_hidden",
        "隐藏身份": "mastermind_hidden",
        "幕后操控": "mastermind_hidden",
        "behind_scenes": "mastermind_hidden",
        "mastermind": "mastermind_hidden",
        "都市": "urban_power",
        "都市异能": "urban_power",
        "都市爽文": "urban_power",
        "爽文": "urban_power",
        "军事": "military_war",
        "战争": "military_war",
        "军事战争": "military_war",
        "末世": "apocalypse_survival",
        "末日": "apocalypse_survival",
        "灾变": "apocalypse_survival",
        "生存": "apocalypse_survival",
        "末世生存": "apocalypse_survival",
        "克苏鲁": "cosmic_horror",
        "诡秘": "cosmic_horror",
        "克系": "cosmic_horror",
        "中式诡异": "chinese_weird",
        "规则怪谈": "chinese_weird",
        "民俗怪谈": "chinese_weird",
        "怪谈": "chinese_weird",
        "诡异": "chinese_weird",
        "诡异复苏": "chinese_weird",
        "体育": "sports_competition",
        "竞技": "sports_competition",
        "体育竞技": "sports_competition",
        "电竞": "sports_competition",
        "娱乐圈": "entertainment_industry",
        "文娱": "entertainment_industry",
        "明星": "entertainment_industry",
        "娱乐文": "entertainment_industry",
        "职场": "business_career",
        "商战": "business_career",
        "创业": "business_career",
        "商业": "business_career",
        "职场商战": "business_career",
        "刑侦": "crime_forensics",
        "法医": "crime_forensics",
        "案件": "crime_forensics",
        "刑侦法医": "crime_forensics",
        "破案": "crime_forensics",
        "校园": "campus_youth",
        "青春": "campus_youth",
        "校园青春": "campus_youth",
        "青春成长": "campus_youth",
        "种田": "farming_management",
        "经营": "farming_management",
        "基建": "farming_management",
        "种田经营": "farming_management",
        "异世界": "isekai_lightnovel",
        "轻小说": "isekai_lightnovel",
        "转生": "isekai_lightnovel",
        "穿越异世界": "isekai_lightnovel",
        "日轻": "isekai_lightnovel",
        "西幻": "steampunk_fantasy",
        "蒸汽": "steampunk_fantasy",
        "蒸汽朋克": "steampunk_fantasy",
        "炼金": "steampunk_fantasy",
        "炼金工业": "steampunk_fantasy",
        "蒸汽西幻": "steampunk_fantasy",
    }
    return aliases.get(name, name)


def _profile_exists(profile_name: str) -> bool:
    return os.path.exists(os.path.join(_profile_dir(profile_name), "profile.json"))


def resolve_profile_name(value: str) -> str:
    name = normalize_profile_name(value)
    if name == AUTO_PROFILE:
        return name
    if not _profile_exists(name):
        print(f"[WARN] 未知 ANALYSIS_PROFILE={value!r}，已回退到 {DEFAULT_PROFILE}")
        return DEFAULT_PROFILE
    return name


def get_active_profile_name() -> str:
    return resolve_profile_name(os.environ.get("ANALYSIS_PROFILE", DEFAULT_PROFILE))


def _profile_dir(profile_name: str) -> str:
    return os.path.join(get_base_dir(), "profiles", profile_name)


def _load_profile_manifest(profile_name: str) -> Dict[str, Any]:
    manifest_path = os.path.join(_profile_dir(profile_name), "profile.json")
    if not os.path.exists(manifest_path):
        return {}
    try:
        data = json.loads(read_file_safely(manifest_path))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[WARN] profile manifest 读取失败: {manifest_path} ({exc})")
        return {}


def load_analysis_profile(profile_name: str = None) -> AnalysisProfile:
    name = resolve_profile_name(profile_name or get_active_profile_name())
    if name == AUTO_PROFILE:
        name = DEFAULT_PROFILE
    manifest = _load_profile_manifest(name)
    profile_root = _profile_dir(name)
    default_rules = os.path.join(profile_root, "rules.json")

    enabled_stages = manifest.get("enabled_stages")
    if not isinstance(enabled_stages, list):
        enabled_stages = ["character_analysis", "harem_scan", "harem_reviewer", "harem_report"]
        if name == "general":
            enabled_stages = ["character_analysis", "general_scan", "general_report"]

    rules_file = manifest.get("rules_file") or default_rules
    if not os.path.isabs(rules_file):
        rules_file = os.path.join(profile_root, rules_file)
    if not os.path.exists(rules_file) and name == "harem":
        rules_file = os.path.join(get_base_dir(), "rules2.json")
    try:
        sort_order = int(manifest.get("sort_order", 1000))
    except Exception:
        sort_order = 1000

    return AnalysisProfile(
        name=name,
        display_name=str(manifest.get("display_name") or name),
        description=str(manifest.get("description") or ""),
        enabled_stages=[str(x) for x in enabled_stages],
        rules_file=rules_file,
        report_mode=str(manifest.get("report_mode") or name),
        scan_focus=[str(x) for x in manifest.get("scan_focus", []) if str(x).strip()],
        summary_fields=[str(x) for x in manifest.get("summary_fields", []) if str(x).strip()],
        harem_plus=manifest.get("harem_plus") if isinstance(manifest.get("harem_plus"), dict) else {},
        cross_profile_rules=manifest.get("cross_profile_rules") if isinstance(manifest.get("cross_profile_rules"), dict) else {},
        sort_order=sort_order,
        version=str(manifest.get("version") or "2.1.0"),
        version_history=manifest.get("version_history") if isinstance(manifest.get("version_history"), list) else [],
        min_supported_scanner_version=str(manifest.get("min_supported_scanner_version") or "1.5.0"),
        breaking_changes=bool(manifest.get("breaking_changes", False)),
    )


def list_available_profiles() -> List[AnalysisProfile]:
    profiles_root = os.path.join(get_base_dir(), "profiles")
    if not os.path.isdir(profiles_root):
        return [load_analysis_profile(DEFAULT_PROFILE)]

    profiles = []
    for name in os.listdir(profiles_root):
        profile_name = normalize_profile_name(name)
        if profile_name == AUTO_PROFILE or not _profile_exists(profile_name):
            continue
        try:
            profiles.append(load_analysis_profile(profile_name))
        except Exception as exc:
            print(f"[WARN] 跳过无效 profile={profile_name!r}: {exc}")

    profiles.sort(key=lambda p: (p.sort_order, p.name))
    return profiles


def profile_options(include_auto: bool = True) -> List[Dict[str, Any]]:
    options = []
    if include_auto:
        options.append({
            "name": AUTO_PROFILE,
            "display_name": "自动识别",
            "description": "按书名和正文前段自动建议分析类型；扫描前仍可手动调整。",
            "report_mode": "auto",
        })
    for profile in list_available_profiles():
        options.append({
            "name": profile.name,
            "display_name": profile.display_name,
            "description": profile.description,
            "report_mode": profile.report_mode,
            "version": profile.version,
            "min_supported_scanner_version": profile.min_supported_scanner_version,
            "breaking_changes": profile.breaking_changes,
        })
    return options


def _keywords_from_manifest(profile_name: str) -> List[Tuple[str, int]]:
    manifest = _load_profile_manifest(profile_name)
    raw_keywords = manifest.get("inference_keywords")
    keywords = []
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            if isinstance(item, str):
                keywords.append((item, 1))
            elif isinstance(item, dict):
                word = str(item.get("word") or item.get("keyword") or "").strip()
                if not word:
                    continue
                try:
                    weight = int(item.get("weight", 1))
                except (TypeError, ValueError):
                    weight = 1
                keywords.append((word, max(1, weight)))
            elif isinstance(item, (list, tuple)) and item:
                word = str(item[0]).strip()
                if not word:
                    continue
                try:
                    weight = int(item[1]) if len(item) > 1 else 1
                except (TypeError, ValueError):
                    weight = 1
                keywords.append((word, max(1, weight)))
    return keywords


def _keyword_negated_at(text: str, index: int) -> bool:
    window = text[max(0, index - 8):index]
    return any(hint in window for hint in KEYWORD_NEGATION_HINTS)


def _keyword_occurrences(text: str, needle: str) -> int:
    if not needle:
        return 0
    count = 0
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        if not _keyword_negated_at(text, index):
            count += 1
        start = index + len(needle)
    return count


def _keyword_effectively_present(text: str, needle: str, profile_name: str = "") -> bool:
    needle = str(needle or "").lower()
    return bool(
        needle
        and not _keyword_context_blocked(text, profile_name, needle)
        and _keyword_occurrences(text, needle) > 0
    )


def _keyword_context_blocked(text: str, profile_name: str, needle: str) -> bool:
    blocked_phrases = (KEYWORD_CONTEXT_BLOCKS.get(profile_name, {}) or {}).get(needle, ())
    return any(phrase.lower() in text for phrase in blocked_phrases)


def _score_keyword_matches(text: str, keywords: List[Tuple[str, int]], profile_name: str = "") -> Tuple[int, List[str]]:
    score = 0
    matches = []
    for word, weight in keywords:
        needle = str(word or "").lower()
        if not needle or _keyword_context_blocked(text, profile_name, needle):
            continue
        count = _keyword_occurrences(text, needle)
        if count:
            score += weight
            if count >= 3 and weight >= 3:
                score += min(3, count - 1)
            matches.append(word)
    return score, matches


def _score_combo_bonuses(text: str, profile_name: str) -> Tuple[int, List[str]]:
    score = 0
    matches = []
    for words, bonus in COMBO_BONUSES.get(profile_name, []):
        normalized_words = [str(word or "").lower() for word in words if str(word or "").strip()]
        if normalized_words and all(_keyword_effectively_present(text, word, profile_name) for word in normalized_words):
            score += int(bonus)
            matches.append("组合:" + "+".join(sorted(words)))
    return score, matches


def _score_negative_keywords(text: str, profile_name: str) -> Tuple[int, List[str]]:
    score = 0
    matches = []
    for word, weight in NEGATIVE_KEYWORDS.get(profile_name, []):
        needle = str(word or "").lower()
        if needle and needle in text:
            score += int(weight)
            matches.append(f"负向:{word}")
    return score, matches


def _profile_match_guard(profile_name: str, matches: List[str]) -> bool:
    generic_only = PROFILE_GENERIC_ONLY_KEYWORDS.get(profile_name)
    if not generic_only:
        return True
    positive_matches = [
        str(match)
        for match in matches
        if match and not str(match).startswith("负向:")
    ]
    if not positive_matches:
        return True
    if all(match in generic_only for match in positive_matches):
        return False
    return True


def _calibrated_confidence(
    profile_name: str,
    score: int,
    nearest_competitor_score: int = 0,
    evidence_count: int = 0,
) -> Tuple[float, str]:
    threshold = max(1, _min_score_for_profile(profile_name, AUTO_PROFILE_MIN_SCORE))
    strength = min(1.0, max(0.0, (score - threshold) / max(threshold * 2.0, 1)))
    margin = (score - nearest_competitor_score) / max(score, 1)
    margin = max(-1.0, min(1.0, margin))
    confidence = 0.35 + 0.45 * strength + 0.20 * max(0.0, margin)
    if score < threshold:
        confidence = min(confidence, 0.45)
    if evidence_count <= 1:
        confidence = min(confidence, 0.6)
    elif evidence_count == 2:
        confidence = min(confidence, 0.72)
    confidence = max(0.05, min(0.95, confidence))
    if confidence >= 0.75:
        level = "high"
    elif confidence >= 0.55:
        level = "medium"
    else:
        level = "low"
    return confidence, level


def infer_profile_candidates_for_text(title: str, text: str, min_score: int = 1) -> List[Dict[str, Any]]:
    title_text = str(title or "")
    body_text = str(text or "")[:PROFILE_INFERENCE_TEXT_LIMIT]
    blob = f"{title_text}\n{title_text}\n{title_text}\n{body_text}".lower()
    raw = []
    for profile in list_available_profiles():
        if profile.name == "general":
            continue
        keyword_score, matches = _score_keyword_matches(blob, _keywords_from_manifest(profile.name), profile.name)
        combo_score, combo_matches = _score_combo_bonuses(blob, profile.name)
        negative_score, negative_matches = _score_negative_keywords(blob, profile.name)
        score = max(0, keyword_score + combo_score + negative_score)
        all_matches = [*matches, *combo_matches, *negative_matches]
        if score >= min_score and _profile_match_guard(profile.name, all_matches):
            raw.append((profile, score, all_matches))

    raw.sort(key=lambda item: (-item[1], item[0].sort_order, item[0].name))
    selected_names = [
        profile.name
        for profile, score, _matches in raw
        if score >= _min_score_for_profile(profile.name, AUTO_PROFILE_MIN_SCORE)
    ][:AUTO_PROFILE_MAX_PROFILES]
    candidates = []
    for index, (profile, score, matches) in enumerate(raw):
        if index == 0:
            nearest_competitor_score = raw[1][1] if len(raw) > 1 else 0
        else:
            nearest_competitor_score = raw[0][1]
        evidence_count = len([
            match
            for match in matches
            if match and not str(match).startswith("负向:")
        ])
        confidence, confidence_level = _calibrated_confidence(
            profile.name,
            score,
            nearest_competitor_score,
            evidence_count,
        )
        item = ProfileInference(
            name=profile.name,
            display_name=profile.display_name,
            score=score,
            confidence=confidence,
            matched_keywords=matches[:12],
            confidence_level=confidence_level,
        ).to_dict()
        item["rank"] = index + 1
        item["auto_selected"] = profile.name in selected_names
        candidates.append(item)

    if not candidates:
        general = load_analysis_profile("general")
        item = ProfileInference(general.name, general.display_name, 0, 1.0, [], "high").to_dict()
        item["rank"] = 1
        item["auto_selected"] = True
        return [item]
    return candidates


def infer_profile_candidates_for_novel(novel_path: str, book_name: str = "", min_score: int = 1) -> List[Dict[str, Any]]:
    try:
        text = _read_text_timeline_samples_safely(novel_path, char_limit=30000)
    except Exception:
        text = ""
    title = book_name or os.path.splitext(os.path.basename(novel_path))[0]
    return infer_profile_candidates_for_text(title, text, min_score=min_score)


def _read_text_timeline_samples_safely(path: str, char_limit: int = 30000) -> str:
    """Read head/middle/tail samples so auto profile inference can see late genre turns."""
    per_sample_chars = max(2000, char_limit // 3)
    per_sample_bytes = max(per_sample_chars * 4, 4096)
    size = os.path.getsize(path)
    offsets = [0]
    if size > per_sample_bytes * 2:
        offsets.append(max(0, size // 2 - per_sample_bytes // 2))
    if size > per_sample_bytes:
        offsets.append(max(0, size - per_sample_bytes))

    chunks = []
    seen_offsets = set()
    with open(path, "rb") as f:
        for label, offset in zip(("head", "middle", "tail"), offsets):
            if offset in seen_offsets:
                continue
            seen_offsets.add(offset)
            f.seek(offset)
            raw = f.read(per_sample_bytes)
            text = _decode_text_sample(raw, per_sample_chars, position=label)
            if text:
                chunks.append(f"__sample_{label}__\n{text}")
    return "\n".join(chunks)[:char_limit]


def _decode_text_sample(raw: bytes, char_limit: int, position: str = "head") -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return _slice_decoded_sample(raw.decode(encoding), char_limit, position)
        except UnicodeDecodeError:
            continue
    return _slice_decoded_sample(raw.decode("utf-8", errors="ignore"), char_limit, position)


def _slice_decoded_sample(text: str, char_limit: int, position: str = "head") -> str:
    if len(text) <= char_limit:
        return text
    if position == "tail":
        return text[-char_limit:]
    if position == "middle":
        start = max(0, len(text) // 2 - char_limit // 2)
        return text[start : start + char_limit]
    return text[:char_limit]


def _read_text_prefix_safely(path: str, char_limit: int = 20000) -> str:
    byte_limit = max(char_limit * 4, 4096)
    with open(path, "rb") as f:
        raw = f.read(byte_limit)
    return _decode_text_sample(raw, char_limit)


def infer_profile_for_text(title: str, text: str) -> str:
    candidates = infer_profile_candidates_for_text(title, text, min_score=1)
    best = candidates[0] if candidates else {"name": "general", "score": 0}
    if best["name"] != "general" and best["score"] >= AUTO_PROFILE_MIN_SCORE:
        return best["name"]
    return "general"


def infer_profile_for_novel(novel_path: str, book_name: str = "") -> str:
    candidates = infer_profile_candidates_for_novel(novel_path, book_name, min_score=1)
    best = candidates[0] if candidates else {"name": "general", "score": 0}
    if best["name"] != "general" and best["score"] >= _min_score_for_profile(best.get("name"), AUTO_PROFILE_MIN_SCORE):
        return best["name"]
    return "general"


def _min_score_for_profile(profile_name: str, requested_min_score: int) -> int:
    requested = int(requested_min_score or AUTO_PROFILE_MIN_SCORE)
    if requested != AUTO_PROFILE_MIN_SCORE:
        return requested
    return int(PROFILE_MIN_SCORE_OVERRIDES.get(profile_name, requested))


def infer_profiles_for_text(title: str, text: str, min_score: int = AUTO_PROFILE_MIN_SCORE) -> List[str]:
    names = [
        item["name"]
        for item in infer_profile_candidates_for_text(title, text, min_score=1)
        if item.get("name") != "general"
        and int(item.get("score") or 0) >= _min_score_for_profile(item.get("name"), min_score)
    ]
    return names[:AUTO_PROFILE_MAX_PROFILES] or ["general"]


def infer_profiles_for_novel(novel_path: str, book_name: str = "", min_score: int = AUTO_PROFILE_MIN_SCORE) -> List[str]:
    try:
        text = _read_text_timeline_samples_safely(novel_path, char_limit=30000)
    except Exception:
        text = ""
    title = book_name or os.path.splitext(os.path.basename(novel_path))[0]
    return infer_profiles_for_text(title, text, min_score=min_score)
