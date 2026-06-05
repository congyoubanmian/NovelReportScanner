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


def _score_keywords(text: str, keywords: List[Tuple[str, int]]) -> int:
    score = 0
    for word, weight in keywords:
        if word and word in text:
            score += weight
    return score


def infer_profile_for_text(title: str, text: str) -> str:
    blob = f"{title}\n{text[:20000]}".lower()
    history_score = _score_keywords(blob, [
        ("皇帝", 3), ("朝廷", 3), ("宰相", 3), ("官职", 2), ("科举", 3),
        ("藩镇", 3), ("诸侯", 3), ("边军", 2), ("骑兵", 2), ("史书", 2),
        ("大明", 4), ("大唐", 4), ("大宋", 4), ("三国", 4), ("汉末", 4),
        ("王朝", 2), ("郡县", 2), ("爵位", 2), ("礼法", 2), ("庙堂", 2),
    ])
    sci_fi_score = _score_keywords(blob, [
        ("飞船", 4), ("星舰", 4), ("星际", 4), ("光速", 3), ("曲率", 4),
        ("量子", 3), ("人工智能", 3), ("ai", 2), ("机器人", 2), ("轨道", 2),
        ("殖民星", 4), ("太空", 3), ("宇宙", 2), ("引擎", 2), ("基因编辑", 3),
        ("纳米", 3), ("黑洞", 3), ("虫洞", 4), ("戴森", 4), ("文明等级", 3),
    ])
    harem_score = _score_keywords(blob, [
        ("后宫", 5), ("女主", 2), ("男主", 2), ("双修", 3), ("炉鼎", 3),
        ("侍妾", 3), ("纳妾", 4), ("道侣", 2), ("红颜", 2), ("暧昧", 2),
        ("推倒", 4), ("处女", 3), ("初夜", 3), ("未婚妻", 2), ("师姐", 1),
    ])

    scores = {
        "history": history_score,
        "hard_sci_fi": sci_fi_score,
        "harem": harem_score,
    }
    best_name, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score >= 6:
        return best_name
    return "general"


def infer_profile_for_novel(novel_path: str, book_name: str = "") -> str:
    try:
        text = read_file_safely(novel_path)
    except Exception:
        text = ""
    title = book_name or os.path.splitext(os.path.basename(novel_path))[0]
    return infer_profile_for_text(title, text)
