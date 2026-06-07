import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from shared_utils import get_base_dir, read_file_safely


DEFAULT_PROFILE = "harem"
AUTO_PROFILE = "auto"


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

    @property
    def uses_harem_reviewer(self) -> bool:
        return "harem_reviewer" in self.enabled_stages

    @property
    def uses_general_scan(self) -> bool:
        return "general_scan" in self.enabled_stages


@dataclass(frozen=True)
class ProfileInference:
    name: str
    display_name: str
    score: int
    confidence: float
    matched_keywords: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "score": self.score,
            "confidence": round(self.confidence, 3),
            "matched_keywords": self.matched_keywords,
        }


_PROFILE_ORDER = {
    "harem": 10,
    "general": 20,
    "history": 30,
    "hard_sci_fi": 40,
    "xianxia_fantasy": 50,
}


_BUILTIN_INFERENCE_KEYWORDS = {
    "history": [
        ("皇帝", 3), ("朝廷", 3), ("宰相", 3), ("官职", 2), ("科举", 3),
        ("藩镇", 3), ("诸侯", 3), ("边军", 2), ("骑兵", 2), ("史书", 2),
        ("大明", 4), ("大唐", 4), ("大宋", 4), ("三国", 4), ("汉末", 4),
        ("王朝", 2), ("郡县", 2), ("爵位", 2), ("礼法", 2), ("庙堂", 2),
    ],
    "hard_sci_fi": [
        ("飞船", 4), ("星舰", 4), ("星际", 4), ("光速", 3), ("曲率", 4),
        ("量子", 3), ("人工智能", 3), ("ai", 2), ("机器人", 2), ("轨道", 2),
        ("殖民星", 4), ("太空", 3), ("宇宙", 2), ("引擎", 2), ("基因编辑", 3),
        ("纳米", 3), ("黑洞", 3), ("虫洞", 4), ("戴森", 4), ("文明等级", 3),
    ],
    "harem": [
        ("后宫", 5), ("女主", 2), ("男主", 2), ("双修", 3), ("炉鼎", 3),
        ("侍妾", 3), ("纳妾", 4), ("道侣", 2), ("红颜", 2), ("暧昧", 2),
        ("推倒", 4), ("处女", 3), ("初夜", 3), ("未婚妻", 2), ("师姐", 1),
    ],
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
            enabled_stages = ["character_analysis", "general_report"]

    rules_file = manifest.get("rules_file") or default_rules
    if not os.path.isabs(rules_file):
        rules_file = os.path.join(profile_root, rules_file)
    if not os.path.exists(rules_file) and name == "harem":
        rules_file = os.path.join(get_base_dir(), "rules2.json")

    return AnalysisProfile(
        name=name,
        display_name=str(manifest.get("display_name") or name),
        description=str(manifest.get("description") or ""),
        enabled_stages=[str(x) for x in enabled_stages],
        rules_file=rules_file,
        report_mode=str(manifest.get("report_mode") or name),
        scan_focus=[str(x) for x in manifest.get("scan_focus", []) if str(x).strip()],
        summary_fields=[str(x) for x in manifest.get("summary_fields", []) if str(x).strip()],
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

    profiles.sort(key=lambda p: (_PROFILE_ORDER.get(p.name, 1000), p.name))
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
    return keywords or list(_BUILTIN_INFERENCE_KEYWORDS.get(profile_name, []))


def _score_keyword_matches(text: str, keywords: List[Tuple[str, int]]) -> Tuple[int, List[str]]:
    score = 0
    matches = []
    for word, weight in keywords:
        needle = str(word or "").lower()
        if needle and needle in text:
            score += weight
            matches.append(word)
    return score, matches


def infer_profile_candidates_for_text(title: str, text: str, min_score: int = 1) -> List[Dict[str, Any]]:
    blob = f"{title}\n{text[:20000]}".lower()
    raw = []
    for profile in list_available_profiles():
        if profile.name == "general":
            continue
        score, matches = _score_keyword_matches(blob, _keywords_from_manifest(profile.name))
        if score >= min_score:
            raw.append((profile, score, matches))

    total_score = sum(score for _profile, score, _matches in raw)
    candidates = [
        ProfileInference(
            name=profile.name,
            display_name=profile.display_name,
            score=score,
            confidence=(score / total_score) if total_score else 0.0,
            matched_keywords=matches[:12],
        ).to_dict()
        for profile, score, matches in raw
    ]
    candidates.sort(key=lambda item: (-item["score"], _PROFILE_ORDER.get(item["name"], 1000), item["name"]))

    if not candidates:
        general = load_analysis_profile("general")
        return [ProfileInference(general.name, general.display_name, 0, 1.0, []).to_dict()]
    return candidates


def infer_profile_candidates_for_novel(novel_path: str, book_name: str = "", min_score: int = 1) -> List[Dict[str, Any]]:
    try:
        text = _read_text_prefix_safely(novel_path, char_limit=20000)
    except Exception:
        text = ""
    title = book_name or os.path.splitext(os.path.basename(novel_path))[0]
    return infer_profile_candidates_for_text(title, text, min_score=min_score)


def _read_text_prefix_safely(path: str, char_limit: int = 20000) -> str:
    byte_limit = max(char_limit * 4, 4096)
    with open(path, "rb") as f:
        raw = f.read(byte_limit)
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)[:char_limit]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")[:char_limit]


def infer_profile_for_text(title: str, text: str) -> str:
    candidates = infer_profile_candidates_for_text(title, text, min_score=1)
    best = candidates[0] if candidates else {"name": "general", "score": 0}
    if best["name"] != "general" and best["score"] >= 6:
        return best["name"]
    return "general"


def infer_profile_for_novel(novel_path: str, book_name: str = "") -> str:
    candidates = infer_profile_candidates_for_novel(novel_path, book_name, min_score=1)
    best = candidates[0] if candidates else {"name": "general", "score": 0}
    if best["name"] != "general" and best["score"] >= 6:
        return best["name"]
    return "general"
