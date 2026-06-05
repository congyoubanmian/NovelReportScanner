import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from shared_utils import get_base_dir, read_file_safely


DEFAULT_PROFILE = "harem"
SUPPORTED_PROFILES = {"harem", "general"}


@dataclass(frozen=True)
class AnalysisProfile:
    name: str
    display_name: str
    description: str
    enabled_stages: List[str]
    rules_file: str
    report_mode: str

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
    }
    name = aliases.get(name, name)
    if name not in SUPPORTED_PROFILES:
        print(f"[WARN] 未知 ANALYSIS_PROFILE={value!r}，已回退到 {DEFAULT_PROFILE}")
        return DEFAULT_PROFILE
    return name


def get_active_profile_name() -> str:
    return normalize_profile_name(os.environ.get("ANALYSIS_PROFILE", DEFAULT_PROFILE))


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
    name = normalize_profile_name(profile_name or get_active_profile_name())
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
    )
