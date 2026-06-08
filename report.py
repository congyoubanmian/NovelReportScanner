import os
import glob
import json
import argparse
import hashlib
from datetime import datetime
import concurrent.futures
import time
from tqdm import tqdm
import threading
import re
import logging
from shared_utils import (
    DEFAULT_MAX_403_RETRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TIMEOUT_RETRIES,
    DEFAULT_REQUEST_TIMEOUT,
    call_json_chat_completion_with_fallback,
    configure_rotating_file_logger,
    create_chat_completion,
    get_base_dir,
    read_file_safely,
)
from token_tracker import create_default_tracker
from analysis_profiles import load_analysis_profile
from toxic_reviewer import is_strict_harem_issue_type

try:
    from openai import OpenAI
    from openai import APIStatusError
except Exception:
    OpenAI = None  # 若未安装 openai，润色功能将不可用
    APIStatusError = Exception

# token 统计
token_tracker = None

SUMMARY_FIELD_TITLES = {
    "historical_logic": "历史制度与时代逻辑",
    "historical_atmosphere": "历史氛围",
    "power_structure": "权力结构与派系",
    "warfare_and_intrigue": "战争与权谋",
    "foreshadowing_and_payoff": "伏笔与回收",
    "foreshadowing_engineering_analysis": "伏笔工程分析",
    "semantic_layers_analysis": "深层语义分析",
    "reader_experience_analysis": "读者体验分析",
    "scientific_assumptions": "科学假设",
    "technology_chain": "技术链与工程约束",
    "science_consistency": "科学设定自洽性",
    "scale_and_wonder": "尺度感与科幻奇观",
    "social_ethical_impact": "社会与伦理影响",
    "character_highlights": "角色亮点",
    "pacing_and_emotion": "节奏与情绪曲线",
    "writing_quality_overall": "写作质量总评",
    "pacing_analysis_overall": "节奏曲线分析",
    "information_density_audit": "信息密度审计",
    "water_chapter_analysis": "水文与冗余分析",
    "narrative_structure_analysis": "叙事结构分析",
    "outline_architecture_overall": "大纲架构分析",
    "prose_quality": "文笔质量",
    "character_depth": "人物塑造",
    "narrative_technique": "叙事技巧",
    "dialogue_quality": "对话质量",
    "scene_description": "场景描写",
    "emotional_impact": "情感渲染",
    "info_density": "信息密度",
    "worldbuilding_integration": "世界观融入",
    "cultivation_system": "修炼体系",
    "bloodline_physique": "血脉/体质/天赋",
    "power_scaling": "战力层级",
    "faction_structure": "势力结构",
    "mythology_elements": "东方神话元素",
    "upgrade_pacing": "升级节奏",
    "dao_theme": "求道/长生主题",
    "mystery_setup": "谜题设置",
    "puzzle_fairness": "谜题公平性",
    "clue_fairness": "线索公平性",
    "narrative_trick": "叙述性诡计",
    "trick_logic": "诡计与逻辑",
    "detective_method": "侦探方法论",
    "logic_chain_integrity": "逻辑链完整性",
    "reveal_and_payoff": "真相揭示与回收",
    "detective_character": "侦探角色",
    "narrative_structure": "叙事结构",
    "system_rules": "系统规则",
    "progression_balance": "成长与数值平衡",
    "instance_design": "副本/关卡设计",
    "instance_variety": "副本/世界多样性",
    "player_interaction": "玩家互动",
    "reward_and_cost": "奖励与代价",
    "novelty_mechanics": "系统机制创新",
    "real_world_impact": "现实世界影响",
    "urban_setting": "都市现实背景",
    "golden_finger_system": "异能/金手指体系",
    "power_system": "异能/金手指体系",
    "face_slapping_pacing": "装逼打脸节奏",
    "relationships": "关系线",
    "villain_quality": "反派质量",
    "realism_risks": "现实逻辑风险",
    "war_type_and_scale": "战争类型与规模",
    "strategy_logic": "战略逻辑",
    "tactics_and_operations": "战术与行动",
    "logistics_and_cost": "后勤与战争代价",
    "command_structure": "指挥链与组织",
    "force_buildup": "部队建设",
    "equipment_and_tech": "装备与军工科技",
    "combat_writing": "战斗描写",
    "political_diplomacy": "政治与外交",
    "apocalypse_cause": "灾变成因与机制",
    "survival_resources": "生存资源",
    "threat_escalation": "威胁升级",
    "shelter_and_order": "据点与秩序",
    "social_collapse_and_rebuild": "秩序崩塌与重建",
    "humanity_moral_dilemmas": "人性与道德困境",
    "power_evolution_system": "能力/进化体系",
    "exploration_adventure": "探索冒险",
    "anomaly_rules": "异常规则",
    "sequence_system": "序列/魔药体系",
    "san_mechanics": "SAN值/理智机制",
    "rule_based_horror": "规则怪谈",
    "contamination_levels": "污染等级",
    "investigation_clues": "调查线索",
    "sanity_and_corruption": "理智与污染代价",
    "horror_atmosphere": "恐怖氛围",
    "alias_system": "马甲体系",
    "identity_system": "身份体系",
    "information_asymmetry": "信息差操纵",
    "information_advantage": "信息优势",
    "mastermind_schemes": "幕后排局",
    "scheme_design": "布局设计",
    "faction_balance": "势力平衡",
    "exposure_risk": "暴露风险",
    "alias_network": "马甲关系网络",
    "reveal_payoff": "掉马与揭秘爽点",
    "reveal_impact": "揭秘冲击力",
    "payoff_design": "爽点设计",
    "weird_rules": "规则机制",
    "folk_taboo_system": "民俗禁忌体系",
    "instance_escape_loop": "副本逃生闭环",
    "rule_discovery_strategy": "规则发现策略",
    "reality_intrusion": "现实侵蚀",
    "folk_horror_atmosphere": "民俗恐怖氛围",
    "organization_information": "组织与信息渠道",
    "competition_rules": "竞技规则",
    "technique_tactics": "专业技战术",
    "season_structure": "赛事/赛季结构",
    "rivalry_and_opponents": "对手群像",
    "training_progression": "训练成长",
    "tactical_matchups": "战术对局",
    "career_and_team": "职业线与团队",
    "creative_works": "作品创作",
    "industry_resources": "行业资源",
    "public_opinion": "舆论经营",
    "career_growth": "事业成长",
    "creative_process": "创作过程",
    "fan_economy": "粉丝经济",
    "business_model": "商业模式",
    "market_competition": "市场竞争",
    "organization_management": "组织管理",
    "career_progression": "职场成长",
    "corporate_politics": "职场政治",
    "supply_chain": "供应链/产业链",
    "case_structure": "案件结构",
    "case_complexity": "案件复杂度",
    "evidence_chain": "证据链",
    "forensic_procedure": "法医与侦查程序",
    "criminal_psychology": "犯罪心理",
    "team_dynamics": "团队协作",
    "social_reflection": "社会映射",
    "legal_realism": "法律现实性",
    "campus_setting": "校园环境",
    "youth_relationships": "青春关系",
    "academic_growth": "学习与竞赛成长",
    "coming_of_age": "成长弧线",
    "era_atmosphere": "时代氛围",
    "family_dynamics": "原生家庭/家庭关系",
    "production_chain": "生产链条",
    "resource_management": "资源管理",
    "technology_progression": "技术升级路径",
    "civilization_level": "文明/产业层级",
    "population_management": "人口管理",
    "trade_expansion": "贸易与扩张",
    "community_building": "组织与社区建设",
    "isekai_premise": "异世界前提",
    "adventure_system": "冒险体系",
    "party_dynamics": "队伍互动",
    "races_culture": "种族与文化生态",
    "politics_society": "贵族/国家政治",
    "romance_comedy_balance": "恋爱喜剧平衡",
    "slice_of_life": "日常/慢生活",
    "lightnovel_pacing": "轻小说节奏",
    "steampunk_setting": "蒸汽西幻底盘",
    "alchemy_industry": "炼金工业",
    "tech_feasibility": "技术可行性",
    "church_empire_politics": "教会/帝国/王室政治",
    "mysticism_integration": "神秘学与工业结合",
    "episodic_mainline_integration": "单元剧情与主线连接度",
    "nation_fate_mechanics": "国运绑定机制",
    "civilization_selection": "文明选拔规则",
    "historical_figures": "历史人物/神话人物",
    "nation_individual_link": "国家兴亡与个体命运",
    "civilization_diversity": "文明差异与特色",
    "confrontation_pacing": "文明对抗节奏",
    "audience_and_state_reaction": "直播舆论与国家反馈",
    "simulation_rules": "模拟器规则",
    "simulation_reality_loop": "推演与现实闭环",
    "branching_causality": "分支选择与因果链",
    "future_information_boundary": "未来信息边界",
    "relationship_carryover": "模拟关系反馈",
    "simulation_readability": "模拟文本可读性",
    "shortcut_detection_dependency": "外挂破案依赖度",
    "case_mainline_link": "案件与主线连接度",
    "system_cost_validity": "系统代价有效性",
    "power_creep": "能力膨胀风险",
    "technical_leap_risk": "技术跃迁风险",
    "romance_density": "感情戏密度",
    "female_presence": "女角色存在感",
    "romance_progression": "恋爱推进",
    "female_tooling_risk": "女角色工具人风险",
    "romance_expectation_gap": "感情预期落差",
    "male_past_romance_risk": "男主前史情感雷点",
    "main_plot": "主线剧情",
    "core_conflicts": "核心冲突",
    "worldbuilding": "世界观",
    "themes": "主题",
    "strengths": "优点与亮点",
    "risks_or_issues": "风险与问题",
    "reader_fit": "适合读者",
    "overall_assessment": "总体评价",
    "heroines": "女主群像",
    "candidate_heroines": "候选女主",
    "missed_heroines": "漏女",
    "purity_assessment": "洁度评估",
    "depressing_points": "郁闷点",
    "poison_points": "毒点",
    "male_protagonist": "男主定位",
    "relationship_progression": "感情线推进",
}

SUMMARY_FIELD_ALIASES = {
    "overview": "story_overview",
    "book_overview": "story_overview",
    "story_summary": "story_overview",
    "summary_overview": "story_overview",
    "plot": "main_plot",
    "main_story": "main_plot",
    "plot_summary": "main_plot",
    "storyline": "main_plot",
    "conflict": "core_conflicts",
    "conflicts": "core_conflicts",
    "main_conflicts": "core_conflicts",
    "world_building": "worldbuilding",
    "setting": "worldbuilding",
    "settings": "worldbuilding",
    "world_setting": "worldbuilding",
    "theme": "themes",
    "motifs": "themes",
    "characters": "character_highlights",
    "characterization": "character_highlights",
    "character_arcs": "character_highlights",
    "character_moments": "character_highlights",
    "pacing": "pacing_and_emotion",
    "pacing_emotion": "pacing_and_emotion",
    "emotion_curve": "pacing_and_emotion",
    "writing_quality": "writing_quality_overall",
    "writing_quality_summary": "writing_quality_overall",
    "craft_quality": "writing_quality_overall",
    "pacing_analysis": "pacing_analysis_overall",
    "rhythm_curve": "pacing_analysis_overall",
    "density_audit": "information_density_audit",
    "information_density": "information_density_audit",
    "structure_analysis": "narrative_structure_analysis",
    "narrative_architecture": "outline_architecture_overall",
    "outline_architecture": "outline_architecture_overall",
    "foreshadowing_engineering": "foreshadowing_engineering_analysis",
    "foreshadowing_analysis": "foreshadowing_engineering_analysis",
    "thread_engineering": "foreshadowing_engineering_analysis",
    "payoff_engineering": "foreshadowing_engineering_analysis",
    "semantic_layers": "semantic_layers_analysis",
    "deep_semantic": "semantic_layers_analysis",
    "subtext_analysis": "semantic_layers_analysis",
    "reader_effect_analysis": "semantic_layers_analysis",
    "semantic_analysis": "semantic_layers_analysis",
    "reader_experience": "reader_experience_analysis",
    "reader_effects": "reader_experience_analysis",
    "engagement_analysis": "reader_experience_analysis",
    "anticipation_analysis": "reader_experience_analysis",
    "satisfaction_analysis": "reader_experience_analysis",
    "foreshadowing": "foreshadowing_and_payoff",
    "plot_threads": "foreshadowing_and_payoff",
    "payoff": "foreshadowing_and_payoff",
    "historical_accuracy": "historical_logic",
    "historical_authenticity": "historical_logic",
    "political_structure": "power_structure",
    "war_intrigue": "warfare_and_intrigue",
    "war_and_intrigue": "warfare_and_intrigue",
    "tech_chain": "technology_chain",
    "tech_constraints": "technology_chain",
    "technology_constraints": "technology_chain",
    "science_logic": "science_consistency",
    "scientific_logic": "science_consistency",
    "science_plausibility": "science_consistency",
    "scientific_basis": "scientific_assumptions",
    "sense_of_wonder": "scale_and_wonder",
    "advantages": "strengths",
    "merits": "strengths",
    "highlights": "strengths",
    "risks": "risks_or_issues",
    "issues": "risks_or_issues",
    "problems": "risks_or_issues",
    "weaknesses": "risks_or_issues",
    "humanity_and_morality": "humanity_moral_dilemmas",
    "power_system": "power_evolution_system",
    "exploration_and_adventure": "exploration_adventure",
    "case_design": "case_structure",
    "case_logic": "logic_chain_integrity",
    "clue_logic": "clue_fairness",
    "investigation_method": "detective_method",
    "reveal_payoff": "reveal_and_payoff",
    "case_link_to_mainline": "case_mainline_link",
    "case_mainline_connection": "case_mainline_link",
    "procedure_realism": "legal_realism",
    "legal_procedure": "legal_realism",
    "social_relevance": "social_reflection",
    "tech_plausibility": "tech_feasibility",
    "technology_feasibility": "tech_feasibility",
    "alchemy_technology": "alchemy_industry",
    "church_politics": "church_empire_politics",
    "mysticism_industry": "mysticism_integration",
    "unit_plot_mainline_link": "episodic_mainline_integration",
    "case_unit_mainline_link": "episodic_mainline_integration",
    "unit_case_mainline_connection": "episodic_mainline_integration",
    "cheat_detection_dependency": "shortcut_detection_dependency",
    "shortcut_detection_reliance": "shortcut_detection_dependency",
    "external_power_detection_dependency": "shortcut_detection_dependency",
    "adventure_structure": "adventure_system",
    "companions": "party_dynamics",
    "romance_subplot": "romance_comedy_balance",
    "daily_life": "slice_of_life",
    "war_scale": "war_type_and_scale",
    "strategy": "strategy_logic",
    "battlefield_operations": "tactics_and_operations",
    "military_logistics": "logistics_and_cost",
    "command_chain": "command_structure",
    "force_building": "force_buildup",
    "military_equipment": "equipment_and_tech",
    "combat_scenes": "combat_writing",
    "diplomacy": "political_diplomacy",
    "business_strategy": "business_model",
    "market_dynamics": "market_competition",
    "org_management": "organization_management",
    "career_arc": "career_progression",
    "industry_chain": "supply_chain",
    "office_politics": "corporate_politics",
    "industry_connections": "industry_resources",
    "public_relations": "public_opinion",
    "creative_pipeline": "creative_process",
    "fandom_economy": "fan_economy",
    "survival_resource_pressure": "survival_resources",
    "shelter_order": "shelter_and_order",
    "collapse_rebuild": "social_collapse_and_rebuild",
    "production_system": "production_chain",
    "resource_logic": "resource_management",
    "tech_tree": "technology_progression",
    "civilization_progression": "civilization_level",
    "cultivation_realm": "cultivation_system",
    "level_scaling": "power_scaling",
    "sect_factions": "faction_structure",
    "daoist_theme": "dao_theme",
    "system_balance": "progression_balance",
    "reward_cost": "reward_and_cost",
    "sports_rules": "competition_rules",
    "matchup_tactics": "tactical_matchups",
    "opponent_rivalry": "rivalry_and_opponents",
    "career_team": "career_and_team",
    "case_fairness": "puzzle_fairness",
    "forensic_realism": "forensic_procedure",
    "teamwork": "team_dynamics",
    "anomaly_mechanics": "anomaly_rules",
    "sanity_mechanics": "san_mechanics",
    "corruption_cost": "sanity_and_corruption",
    "campus_life": "campus_setting",
    "youth_growth": "coming_of_age",
    "target_readers": "reader_fit",
    "reader_suitability": "reader_fit",
    "suitable_readers": "reader_fit",
    "final_assessment": "overall_assessment",
    "overall_evaluation": "overall_assessment",
    "final_verdict": "overall_assessment",
}

RADAR_SCORE_DIMENSIONS = {
    "plot": "剧情质量",
    "characters": "人物塑造",
    "worldbuilding": "世界观",
    "pacing": "节奏把控",
    "writing": "文笔水准",
    "emotion": "情绪调动",
}


def summary_field_label(field: str) -> str:
    if field in SUMMARY_FIELD_TITLES:
        return SUMMARY_FIELD_TITLES[field]
    canonical = SUMMARY_FIELD_ALIASES.get(field, field)
    return SUMMARY_FIELD_TITLES.get(canonical, field.replace("_", " "))


def summary_field_candidates(field: str):
    canonical = SUMMARY_FIELD_ALIASES.get(field, field)
    candidate_fields = [field]
    if canonical not in candidate_fields:
        candidate_fields.append(canonical)
    candidate_fields.extend(
        alias
        for alias, target in SUMMARY_FIELD_ALIASES.items()
        if target == canonical and alias not in candidate_fields
    )
    return candidate_fields


def summary_field_values(summary: dict, field: str):
    if not isinstance(summary, dict):
        return []
    values = []
    seen = set()
    for candidate in summary_field_candidates(field):
        value = summary.get(candidate)
        if isinstance(value, list):
            items = value
        elif value:
            items = [value]
        else:
            items = []
        for item in items:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            values.append(item)
    return values


def summary_field_text(summary: dict, field: str, default: str = "未描述") -> str:
    values = summary_field_values(summary, field)
    if not values:
        return default
    return "；".join(str(x).strip() for x in values if str(x).strip()) or default


def init_token_tracker(book_name, run_id=None):
    global token_tracker
    token_tracker = create_default_tracker(
        "report.py",
        book_name=book_name,
        out_path=os.path.join(get_base_dir(), "results", "token_usage.json"),
        run_id=run_id,
    )
    return token_tracker


def record_usage(resp):
    try:
        if token_tracker is not None:
            token_tracker.record(resp)
    except Exception:
        pass


def get_report_logger():
    global _REPORT_LOGGER
    if _REPORT_LOGGER is not None:
        return _REPORT_LOGGER
    logger = logging.getLogger("report_generation")
    logger.propagate = False
    try:
        os.makedirs(os.path.dirname(REPORT_RUN_LOG_PATH), exist_ok=True)
        configure_rotating_file_logger(
            logger,
            REPORT_RUN_LOG_PATH,
            stream=False,
            formatter=logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"),
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("初始化报告生成日志失败: %s", exc)
        logger.setLevel(logging.INFO)
    _REPORT_LOGGER = logger
    return logger


def log_report(msg: str):
    print(msg)
    try:
        get_report_logger().info(msg)
    except Exception:
        pass


# === 配置 ===
BASE_URL = os.environ.get("BASE_URL", "https://api.deepseek.com")
# 模型选择：可修改为其他已部署模型或通过环境变量 MODEL_NAME 覆盖
MODEL = os.environ.get("MODEL_NAME", "deepseek-chat")
# 并发线程数（用于润色并发），可通过环境变量 MAX_WORKERS 覆盖
_base_workers = int(os.environ.get("MAX_WORKERS", "4"))
MAX_WORKERS = _base_workers + 4

# API Key 支持池化：优先 API_KEY_POOL（逗号分隔），否则回退 API_KEY
API_KEY_POOL = [
    k.strip()
    for k in os.environ.get("API_KEY_POOL", os.environ.get("API_KEY", "")).split(",")
    if k.strip()
]
API_KEY = API_KEY_POOL[0] if API_KEY_POOL else ""
BASE_DIR = get_base_dir()
RESULTS_DIR = os.environ.get("RESULTS_DIR") or os.path.join(BASE_DIR, "results")
REPORT_CHECKPOINT_FILE = os.path.join(RESULTS_DIR, "report_checkpoint.json")
REPORT_RUN_LOG_PATH = os.path.join(RESULTS_DIR, "report_generation.log")
_REPORT_LOGGER = None


# ---- API 调用封装：统一收敛到 Timerror.py（只需修改 Timerror.py 即可全局生效）----
MAX_RETRIES = DEFAULT_MAX_RETRIES
MAX_403_RETRIES = DEFAULT_MAX_403_RETRIES
MAX_TIMEOUT_RETRIES = DEFAULT_MAX_TIMEOUT_RETRIES
REQUEST_TIMEOUT = DEFAULT_REQUEST_TIMEOUT

chat_completion = create_chat_completion(
    api_key_pool=API_KEY_POOL,
    base_url=BASE_URL,
    request_timeout=REQUEST_TIMEOUT,
    max_retries=MAX_RETRIES,
    max_403_retries=MAX_403_RETRIES,
    max_timeout_retries=MAX_TIMEOUT_RETRIES,
    base_delay=2,
    logger=None,  # report.py 里原本用 print 输出
)


def _call_json_chat_completion(messages, *, model: str = None, temperature: float = 0.1, max_tokens: int = 1000) -> dict:
    return call_json_chat_completion_with_fallback(
        chat_completion_func=chat_completion,
        model=model or MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        record_usage_func=record_usage,
    )


def find_latest(pattern: str, base_dir: str = RESULTS_DIR):
    paths = glob.glob(os.path.join(base_dir, "**", pattern), recursive=True)
    if not paths:
        return None
    return max(paths, key=os.path.getmtime)


def find_detailed_json(book_key: str, base_dir: str = RESULTS_DIR, detail_path: str = None):
    """
    根据书名 key 查找对应的 *_detailed_*.json 文件
    优先匹配同名书籍的文件
    """
    if detail_path:
        return detail_path
    if not book_key:
        return find_latest("*_detailed_*.json", base_dir)
    
    # 尝试精确匹配书名
    pattern = os.path.join(base_dir, "**", f"{book_key}*_detailed_*.json")
    paths = glob.glob(pattern, recursive=True)
    if paths:
        return max(paths, key=os.path.getmtime)
    
    # 降级：查找任意 detailed 文件
    return find_latest("*_detailed_*.json", base_dir)


def find_general_summary_json(book_key: str, base_dir: str = RESULTS_DIR, profile_name: str = "general"):
    if not book_key:
        return find_latest("*_GENERAL_SUMMARY_latest.json", base_dir)
    if profile_name and profile_name != "general":
        latest_path = os.path.join(base_dir, f"{book_key}_{profile_name}_GENERAL_SUMMARY_latest.json")
        if os.path.exists(latest_path):
            return latest_path
    latest_path = os.path.join(base_dir, f"{book_key}_GENERAL_SUMMARY_latest.json")
    if os.path.exists(latest_path):
        return latest_path
    paths = glob.glob(os.path.join(base_dir, "**", "GENERAL_SUMMARY.json"), recursive=True)
    paths = [p for p in paths if book_key in os.path.basename(os.path.dirname(p))]
    if paths:
        return max(paths, key=os.path.getmtime)
    return None


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    return json.loads(read_file_safely(path))


def _safe_mtime(path: str):
    try:
        if path and os.path.exists(path):
            return os.path.getmtime(path)
    except Exception:
        pass
    return None


def _novel_file_signature(path: str, sample_size: int = 65536):
    try:
        stat = os.stat(path)
        size = int(stat.st_size)
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with open(path, "rb") as f:
            digest.update(f.read(sample_size))
            if size > sample_size:
                f.seek(max(0, size - sample_size))
                digest.update(f.read(sample_size))
        return {
            "size": size,
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            "sample_sha256": digest.hexdigest(),
        }
    except OSError:
        return None


def _general_summary_matches_novel(summary: dict, novel_path: str, profile_name: str = "general") -> bool:
    if not isinstance(summary, dict) or not novel_path:
        return False
    if summary.get("schema_version") != 1:
        return False
    if summary.get("specialty_profile", summary.get("analysis_profile", "general")) != profile_name:
        return False
    if os.path.abspath(str(summary.get("novel_path", "") or "")) != os.path.abspath(novel_path):
        return False
    current_mtime = _safe_mtime(novel_path)
    if current_mtime is None or summary.get("novel_mtime") != current_mtime:
        return False
    current_signature = _novel_file_signature(novel_path)
    stored_signature = summary.get("novel_signature")
    return isinstance(stored_signature, dict) and current_signature == stored_signature


def load_report_checkpoint(path: str = REPORT_CHECKPOINT_FILE) -> dict:
    data = load_json(path)
    if not isinstance(data, dict):
        data = {}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        data["jobs"] = {}
    return data


def save_report_checkpoint(data: dict, path: str = REPORT_CHECKPOINT_FILE):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_report(f"\u5199\u5165\u62a5\u544a\u68c0\u67e5\u70b9\u5931\u8d25: {e}")


def extract_book_key_from_path(path: str) -> str:
    """
    从文件名推断书名 key:
    例: 《书名》_protagonists_20251213_120000.json -> 《书名》
    例: 《书名》_detailed_20251213_120000.json -> 《书名》
    """
    if not path:
        return ""
    base = os.path.basename(path)
    for marker in ["_protagonists_", "_detailed_", "_scan_", "_heroine_"]:
        if marker in base:
            return base.split(marker, 1)[0]
    return os.path.splitext(base)[0]


def infer_book_key_from_results_dir(path: str) -> str:
    """
    当 reviewer 文件名是 VERIFIED_SUMMARY_*.json 这种“无书名”的情况时，
    尝试从其所在 results 子目录名推断书名：
      例：.../《书名》_scan_2025xxxx/VERIFIED_SUMMARY_xxx.json -> 《书名》
    """
    if not path:
        return ""
    try:
        parent = os.path.basename(os.path.dirname(path))
        for marker in ["_scan_", "_heroine_", "_protagonist_", "_review_", "_verify_"]:
            if marker in parent:
                return parent.split(marker, 1)[0]
        # 兜底：如果目录名本身就以《...》开头，取到第一个》为止
        if "《" in parent and "》" in parent:
            l = parent.find("《")
            r = parent.find("》", l + 1)
            if l != -1 and r != -1 and r > l:
                return parent[l : r + 1]
        return ""
    except Exception:
        return ""


def sanitize_book_key(key: str) -> str:
    """将书名 key 转为文件友好格式"""
    if not key:
        return ""
    return re.sub(r"[^\w\-\.\u4e00-\u9fa5]+", "_", key)


def sanitize_filename_part(value: str) -> str:
    text = str(value or "").strip()
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_") or "报告"


def report_suffix_for_profile(profile) -> str:
    if profile.report_mode != "general":
        return "扫书报告"
    if profile.name == "general":
        return "通用小说报告"
    return f"{sanitize_filename_part(profile.display_name)}报告"


def format_book_title_for_filename(book_key: str) -> str:
    """
    生成用于文件名展示的“书名标题”：
    - 如果 book_key 已含《...》前缀（常见：`《书名》完结+番外`），避免再套一层《》
    - 否则使用《书名》包裹
    """
    if not book_key:
        return ""
    s = str(book_key).strip()
    if s.startswith("《") and "》" in s:
        return s
    return f"《{s}》"


def summarize_appearance(name: str, features: list) -> str:
    """调用大模型总结女主外貌特点"""
    if not features:
        return "未描述"
    if not OpenAI or not API_KEY:
        # 无法调用大模型，直接拼接返回
        return '；'.join(features[:3])
    
    features_text = '\n'.join([f"- {f}" for f in features])
    system_prompt = "你是一个专业的小说角色外貌总结助手。请根据提供的外貌描写片段，总结出简洁的外貌特点（30字以内），只保留关键的外貌特征如：容貌、身材、发色、穿着等。不要添加性格描写。"
    user_prompt = f"角色：{name}\n外貌描写片段：\n{features_text}\n\n请用30字以内总结外貌特点："
    
    try:
        resp = chat_completion(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=100,
        )
        record_usage(resp)
        result = resp.choices[0].message.content.strip()
        return result if result else '；'.join(features[:2])
    except Exception as e:
        print(f"外貌总结失败({name}): {e}")
        return '；'.join(features[:2])


def build_features_map(detailed_data) -> dict:
    """从 all_female_characters 构建 name -> features 映射"""
    features_map = {}
    if not detailed_data:
        return features_map
    
    all_chars = detailed_data.get("all_female_characters", {})
    for name, info in all_chars.items():
        features = info.get("features", [])
        if features:
            features_map[name] = features
        # 也保存 other_names 用于模糊匹配
        other_names = info.get("other_names", [])
        for alias in other_names:
            if alias and alias not in features_map:
                features_map[alias] = features
    
    return features_map


def build_report_sections(detailed_data, reviewer, features_map=None):
    """
    整合 detailed_data (*_detailed_*.json) 与 reviewer (VERIFIED_SUMMARY_*.json)
    新格式：女主名字 | 肉体洁 | 精神洁 | 是否被推倒 | 女主特点（身份、外貌、与男主关系）
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [f"精简报告 生成时间：{ts}", "=" * 60]

    # 从 reviewer 构建：肉体/精神状态 + 推倒判定（支持五维洁度判定 + 验证信息）
    purity_map = {}  # name -> {is_virgin, virgin_status, has_other_contact, contact_status, no_partner, partner_status, verification, ...}
    if reviewer:
        for item in reviewer.get("heroines_purity", []):
            name = item.get("name")
            if name:
                verification = item.get("verification", {})
                purity_map[name] = {
                    # 五维洁度判定
                    "is_virgin": item.get("is_virgin", True),
                    "virgin_status": item.get("virgin_status", item.get("body_status", "❓ 未知")),
                    "has_other_contact": item.get("has_other_contact", False),
                    "contact_status": item.get("contact_status", "❓ 未知"),
                    "no_partner": item.get("no_partner", True),
                    "partner_status": item.get("partner_status", "❓ 未知"),
                    # 精神洁度
                    "is_spirit_clean": item.get("is_spirit_clean", True),
                    "spirit_status": item.get("spirit_status", "❓ 未知"),
                    # 综合
                    "is_clean": item.get("is_clean", True),
                    "pushed": item.get("pushed_by_male_lead"),
                    "pushed_reason": item.get("pushed_reason", "未判定"),
                    "summary": item.get("summary", ""),
                    # 兼容旧字段
                    "body_status": item.get("body_status", "❓ 未知"),
                    # 验证信息
                    "verification": {
                        "method": verification.get("method", "unknown"),
                        "llm_agreed": verification.get("llm_agreed", True),
                        "rounds": verification.get("rounds", 0),
                        "first_disagreement": verification.get("first_disagreement", ""),
                        "second_reason": verification.get("second_reason", ""),
                        "program_result": verification.get("program_result", {}),
                        "llm_first_result": verification.get("llm_first_result", {}),
                    },
                }

    # 男主信息
    male_lines = []
    male_lines.append("\n【男主信息】")
    mp = detailed_data.get("male_protagonist") if detailed_data else None
    if mp:
        male_lines.append(f"- 男主：{mp.get('name') or '未识别'}")
        other_names = mp.get("other_names", [])
        if other_names:
            male_lines.append(f"  别名：{', '.join(other_names[:10])}")  # 限制别名数量
        if mp.get("identity"):
            male_lines.append(f"  身份：{mp.get('identity')}")
        summaries = mp.get("summaries", [])
        if summaries:
            male_lines.append("  主要剧情：")
            for s in summaries[:3]:
                male_lines.append(f"    · {s}")

    # 女主信息：整合 detailed + reviewer
    heroine_blocks = []
    heroines = []
    if detailed_data:
        heroines = detailed_data.get("heroine_result", {}).get("heroines", [])
    
    # 构建外貌特征映射（如果未提供）
    if features_map is None:
        features_map = build_features_map(detailed_data)
    
    if heroines:
        for h in heroines:
            lines = []
            name = h.get('name') or '未知'
            rank = h.get('importance_rank', '?')
            aliases_list = h.get('aliases', [])
            aliases = ', '.join(aliases_list[:5]) if aliases_list else '无'
            rel = h.get('relationship_type') or '未知'
            traits = h.get('character_traits') or ''
            summary = h.get('summary') or ''
            
            # 从 features_map 获取外貌描写（支持别名匹配）
            features = features_map.get(name, [])
            if not features:
                for alias in aliases_list:
                    if alias in features_map:
                        features = features_map[alias]
                        break
            
            # 从 summary 中提取身份（通常是第一句）
            identity = ''
            if summary:
                first_sentence = summary.split('。')[0].split('，')[0].strip()
                if first_sentence and len(first_sentence) < 50:
                    identity = first_sentence
            
            # 性格直接使用 traits
            personality = traits or '未描述'
            
            # 从 purity_map 获取初/处和推倒信息（支持模糊匹配）
            purity_info = purity_map.get(name)
            if not purity_info:
                # 尝试模糊匹配
                for pname, pinfo in purity_map.items():
                    if name in pname or pname in name:
                        purity_info = pinfo
                        break
            
            if purity_info:
                # 五维洁度判定
                virgin_status = purity_info.get("virgin_status", purity_info.get("body_status", "❓ 未知"))
                contact_status = purity_info.get("contact_status", "❓ 未知")
                partner_status = purity_info.get("partner_status", "❓ 未知")
                spirit_status = purity_info.get("spirit_status", "❓ 未知")
                pushed = purity_info.get("pushed")
                pushed_reason = purity_info.get("pushed_reason", "未判定")
                verification = purity_info.get("verification", {})
            else:
                virgin_status = "❓ 未知"
                contact_status = "❓ 未知"
                partner_status = "❓ 未知"
                spirit_status = "❓ 未知"
                pushed = None
                pushed_reason = "未判定"
                verification = {}
            
            # 推倒状态显示
            if pushed is True:
                pushed_flag = "✅ 是"
            elif pushed is False:
                pushed_flag = "❌ 否"
            else:
                pushed_flag = "❓ 未知"

            # 新格式输出（五维洁度判定）
            lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"【{rank}】{name}")
            lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"  别名：{aliases}")
            lines.append(f"  是否处女：{virgin_status}")
            lines.append(f"  非男主接触：{contact_status}")
            lines.append(f"  有无男伴：{partner_status}")
            lines.append(f"  精神洁：{spirit_status}")
            lines.append(f"  被推倒：{pushed_flag}")
            
            # 显示验证信息
            method = verification.get("method", "")
            if method == "program_with_llm_agree":
                lines.append(f"  验证：✅ LLM确认程序判定")
            elif method == "program_confirmed_after_second_round":
                lines.append(f"  验证：⚠️ 二次校验后确认")
                if verification.get("first_disagreement"):
                    lines.append(f"    初次分歧：{verification.get('first_disagreement', '')[:60]}")
            elif method == "llm_override_after_disagreement":
                lines.append(f"  验证：⚠️ 程序与LLM存在分歧")
                # 显示程序原判定
                prog = verification.get("program_result", {})
                if prog:
                    lines.append(f"  ┌─ 程序判定（被LLM修正）：")
                    lines.append(f"  │  处女：{prog.get('virgin_status', '?')}")
                    lines.append(f"  │  接触：{prog.get('contact_status', '?')}")
                    lines.append(f"  │  男伴：{prog.get('partner_status', '?')}")
                    lines.append(f"  │  精神：{prog.get('spirit_status', '?')}")
                    lines.append(f"  └─────────────────")
                if verification.get("first_disagreement"):
                    lines.append(f"    分歧原因：{verification.get('first_disagreement', '')[:80]}")
                if verification.get("second_reason"):
                    lines.append(f"    最终理由：{verification.get('second_reason', '')[:80]}")
            elif method == "no_facts_default_clean":
                lines.append(f"  验证：ℹ️ 无事实记录，默认全初")
            
            lines.append(f"  ────────────────")
            if identity:
                lines.append(f"  身份：{identity}")
            # 外貌：使用 features 并标记待总结
            lines.append(f"  外貌：__APPEARANCE__{name}__")
            lines.append(f"  性格：{personality}")
            lines.append(f"  与男主关系：{rel}")
            if summary:
                lines.append(f"  概要：{summary}")
            if pushed_reason and pushed_reason != "未判定":
                lines.append(f"  推倒说明：{pushed_reason}")
            
            heroine_blocks.append("\n".join(lines))
    
    # 补充：reviewer 中有但 detailed 中没有的女主
    detailed_names = {h.get('name') for h in heroines if h.get('name')}
    for pname, pinfo in purity_map.items():
        if pname not in detailed_names:
            # 检查是否已通过模糊匹配处理
            already_matched = any(pname in dn or dn in pname for dn in detailed_names)
            if not already_matched:
                lines = []
                lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"【?】{pname}")
                lines.append(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                # 五维洁度判定
                lines.append(f"  是否处女：{pinfo.get('virgin_status', pinfo.get('body_status', '❓ 未知'))}")
                lines.append(f"  非男主接触：{pinfo.get('contact_status', '❓ 未知')}")
                lines.append(f"  有无男伴：{pinfo.get('partner_status', '❓ 未知')}")
                lines.append(f"  精神洁：{pinfo.get('spirit_status', '❓ 未知')}")
                pushed = pinfo.get("pushed")
                if pushed is True:
                    pushed_flag = "✅ 是"
                elif pushed is False:
                    pushed_flag = "❌ 否"
                else:
                    pushed_flag = "❓ 未知"
                lines.append(f"  被推倒：{pushed_flag}")
                
                # 显示验证信息
                verification = pinfo.get("verification", {})
                method = verification.get("method", "")
                if method == "program_with_llm_agree":
                    lines.append(f"  验证：✅ LLM确认程序判定")
                elif method == "program_confirmed_after_second_round":
                    lines.append(f"  验证：⚠️ 二次校验后确认")
                elif method == "llm_override_after_disagreement":
                    lines.append(f"  验证：⚠️ 程序与LLM存在分歧")
                    prog = verification.get("program_result", {})
                    if prog:
                        lines.append(f"  ┌─ 程序判定（被LLM修正）：")
                        lines.append(f"  │  处女：{prog.get('virgin_status', '?')}")
                        lines.append(f"  │  接触：{prog.get('contact_status', '?')}")
                        lines.append(f"  │  男伴：{prog.get('partner_status', '?')}")
                        lines.append(f"  │  精神：{prog.get('spirit_status', '?')}")
                        lines.append(f"  └─────────────────")
                    if verification.get("first_disagreement"):
                        lines.append(f"    分歧原因：{verification.get('first_disagreement', '')[:80]}")
                elif method == "no_facts_default_clean":
                    lines.append(f"  验证：ℹ️ 无事实记录，默认全初")
                
                lines.append(f"  ────────────────")
                lines.append(f"  （详细信息未找到）")
                heroine_blocks.append("\n".join(lines))

    # 雷/郁闷原文，保持未经润色
    risk = []
    if reviewer:
        lei_points = reviewer.get("lei_points", [])
        yumen_points = reviewer.get("yumen_points", [])
        if lei_points:
            risk.append("【严重雷点】")
            for i, p in enumerate(lei_points, 1):
                risk.append(f"{i}. [{p.get('type','')}] @chunk {p.get('chunk_index')}")
                risk.append(f"   原文：{p.get('content','')}")
                if p.get("review_comment"):
                    risk.append(f"   裁决：{p.get('review_comment')}")
            risk.append("")
        if yumen_points:
            risk.append("【郁闷点】")
            for i, p in enumerate(yumen_points, 1):
                risk.append(f"{i}. [{p.get('type','')}] @chunk {p.get('chunk_index')}")
                risk.append(f"   原文：{p.get('content','')}")
                if p.get("review_comment"):
                    risk.append(f"   裁决：{p.get('review_comment')}")

    return header, male_lines, heroine_blocks, ("\n".join(risk) if risk else "")


# =========================== 新版报告（男主→女主→毒/雷点） ===========================
def _bool_mark(v):
    """把 True/False/None 映射成 ✅/❌/❓"""
    if v is True:
        return "✅"
    if v is False:
        return "❌"
    return "❓"


def build_purity_map(reviewer: dict) -> dict:
    """
    从 novel_reviewer.py 的 VERIFIED_SUMMARY_*.json 构建洁度映射：
      name -> {is_virgin, is_spirit_clean, no_partner, has_other_contact, ...}
    """
    m = {}
    if not reviewer:
        return m
    for item in reviewer.get("heroines_purity", []) or []:
        name = item.get("name")
        if not name:
            continue
        data = {
            "is_clean": item.get("is_clean"),
            "is_virgin": item.get("is_virgin"),
            "is_spirit_clean": item.get("is_spirit_clean"),
            "no_partner": item.get("no_partner"),
            "has_other_contact": item.get("has_other_contact"),
            # status fields
            "virgin_status": item.get("virgin_status", ""),
            "spirit_status": item.get("spirit_status", ""),
            "partner_status": item.get("partner_status", ""),
            "contact_status": item.get("contact_status", ""),
            # conflict detail (LLM vs rule)
            "virgin_judgement_conflict": bool(item.get("virgin_judgement_conflict", False)),
            "llm_virgin_status": item.get("llm_virgin_status", ""),
            "llm_virgin_reason": item.get("llm_virgin_reason", ""),
            "rule_virgin_status": item.get("rule_virgin_status", ""),
            "rule_virgin_reason": item.get("rule_virgin_reason", ""),
            "contact_judgement_conflict": bool(item.get("contact_judgement_conflict", False)),
            "llm_contact_status": item.get("llm_contact_status", ""),
            "llm_contact_reason": item.get("llm_contact_reason", ""),
            "rule_contact_status": item.get("rule_contact_status", ""),
            "rule_contact_reason": item.get("rule_contact_reason", ""),
            "partner_judgement_conflict": bool(item.get("partner_judgement_conflict", False)),
            "llm_partner_status": item.get("llm_partner_status", ""),
            "llm_partner_reason": item.get("llm_partner_reason", ""),
            "rule_partner_status": item.get("rule_partner_status", ""),
            "rule_partner_reason": item.get("rule_partner_reason", ""),
            "spirit_judgement_conflict": bool(item.get("spirit_judgement_conflict", False)),
            "llm_spirit_status": item.get("llm_spirit_status", ""),
            "llm_spirit_reason": item.get("llm_spirit_reason", ""),
            "rule_spirit_status": item.get("rule_spirit_status", ""),
            "rule_spirit_reason": item.get("rule_spirit_reason", ""),
            "partner_exempted_for_clean": item.get("partner_exempted_for_clean", False),
            "partner_exemption_notes": item.get("partner_exemption_notes", []),
            "partner_exemption_reason": item.get("partner_exemption_reason", ""),
            "past_life_clean": item.get("past_life_clean"),
            "past_life_severity": item.get("past_life_severity", ""),
            "past_life_severity_label": item.get("past_life_severity_label", ""),
            "past_life_status": item.get("past_life_status", ""),
            "past_life_reason": item.get("past_life_reason", ""),
            "contact_level": item.get("contact_level", ""),
            "contact_level_label": item.get("contact_level_label", ""),
            "contact_level_reason": item.get("contact_level_reason", ""),
            "pushed_by_male_lead": item.get("pushed_by_male_lead"),
            "pushed_reason": item.get("pushed_reason", ""),
            # original summary and leak status
            "is_leak_heroine": item.get("is_leak_heroine"),
            "leak_reason": item.get("leak_reason", ""),
            "leak_emotional_depth": item.get("leak_emotional_depth"),
            "leak_emotional_depth_reason": item.get("leak_emotional_depth_reason", ""),
            "leak_relationship_confirmed": item.get("leak_relationship_confirmed"),
            "leak_relationship_reason": item.get("leak_relationship_reason", ""),
            "leak_ending_accounted": item.get("leak_ending_accounted"),
            "leak_ending_reason": item.get("leak_ending_reason", ""),
            "summary": item.get("summary", ""),
        }
        m[name] = data
        key = _heroine_name_key(name)
        if key and key not in m:
            m[key] = data
    return m


def summarize_male_profile_llm(male_obj: dict, model: str = None) -> dict:
    """
    输入：*_detailed_*.json 输出中的 male_protagonist 字段
    输出：{identity, personality, experience}
    """
    male_obj = male_obj or {}
    name = male_obj.get("name") or "男主"
    identity = (male_obj.get("identity") or "").strip()
    # 按你的要求：男主不读取 other_names（别称不输出）
    aliases = male_obj.get("aliases") or []
    summaries = male_obj.get("summaries") or []

    # 无法调用大模型时：尽量用原始信息拼
    if not OpenAI or not API_KEY:
        exp = "；".join([s.strip() for s in summaries[:3] if s and str(s).strip()])
        return {
            "identity": identity or "未描述",
            "personality": "未描述",
            "experience": exp or "未描述",
            "aliases": aliases,
        }

    system_prompt = (
        "你是小说人物信息总结助手。请基于提供的原始线索，提炼男主的：身份、性格特点、经历概括。"
        "要求：不杜撰，不加入原文没有的事实；输出简洁；经历用1-3句概括。"
        "请只输出 JSON。"
    )
    user_prompt = json.dumps(
        {
            "name": name,
            "identity": identity,
            # 你要求尽量读全：这里保留到 100
            "aliases": aliases[:100],
            "summaries": summaries[:100],
        },
        ensure_ascii=False,
        indent=2,
    )
    try:
        data = _call_json_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=500,
            model=model or MODEL,
        )
        return {
            "identity": (data.get("identity") or identity or "未描述").strip(),
            "personality": (data.get("personality") or "未描述").strip(),
            "experience": (data.get("experience") or "未描述").strip(),
            "aliases": aliases,
        }
    except Exception:
        exp = "；".join([str(s).strip() for s in summaries[:3] if s and str(s).strip()])
        return {
            "identity": identity or "未描述",
            "personality": "未描述",
            "experience": exp or "未描述",
            "aliases": aliases,
        }


def _pick_first_str(items, default=""):
    for x in items or []:
        s = (str(x) if x is not None else "").strip()
        if s:
            return s
    return default


def _contains_any_text(value, keywords) -> bool:
    if isinstance(value, (list, tuple, set)):
        text = " ".join(str(x) for x in value if x is not None)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    return any(word in text for word in keywords)


def _contains_positive_signal_text(value, keywords) -> bool:
    if isinstance(value, (list, tuple, set)):
        text = " ".join(str(x) for x in value if x is not None)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    if not text:
        return False

    negative_hints = (
        "没有", "没", "无", "未见", "尚未", "并未", "不曾", "不存在", "缺少", "缺乏",
        "无明确", "没有明确", "未确认", "未发生", "未产生", "未建立", "未收入",
        "未与", "未和", "未跟", "未同房", "未双修", "未圆房", "未成为", "未成",
        "并非", "不是", "不算", "不能算", "谈不上", "称不上",
        "不喜欢", "不爱", "并不喜欢", "并不爱", "不是喜欢", "不是爱",
        "谈不上喜欢", "谈不上爱", "称不上喜欢", "称不上爱", "讨厌", "厌恶",
    )
    roleplay_hints = ("假装成", "假装", "假扮成", "假扮", "伪装成", "伪装", "冒充", "扮作", "装作")
    roleplay_after_hints = ("台词", "排练", "演戏", "舞台剧", "剧本")
    non_romantic_like_prefixes = ("读者", "粉丝", "书友")
    non_romantic_like_followers = (
        "什么", "哪", "吃", "喝", "看", "用", "这", "那", "某", "衣", "裙", "书", "菜", "颜色", "东西", "物件",
        "破案", "探案", "推理", "研究", "调查", "冒险", "战斗", "修炼", "种田", "赚钱", "吐槽", "搞笑",
        "权势", "权力", "金钱", "财富", "名利", "地位", "身份", "容貌", "外表",
    )
    non_romantic_love_prefixes = ("可", "讨人喜", "喜", "热")
    non_romantic_love_followers = (
        "吃", "喝", "看", "用", "好", "好者", "好是", "心", "护", "惜", "美", "漂亮", "玩", "闹",
        "丽", "莉", "琳", "莲", "莎", "丝", "薇", "娜", "妮", "德", "尔", "伦", "蜜", "丽丝", "丽莎",
        "哭", "笑", "撒娇", "睡懒觉", "发呆", "吐槽", "冒险", "战斗", "修炼", "赚钱",
    )
    non_romantic_admire_followers = (
        "虚荣", "权势", "权力", "金钱", "财富", "名利", "地位", "身份", "容貌", "外表",
        "者", "对象", "粉丝", "追求者",
    )
    non_romantic_emotion_contexts = ("读者", "书友", "粉丝", "作者", "剧情", "副本", "设定", "文笔", "设计", "说明")
    nonfactual_romance_contexts = ("传言", "传闻", "流言", "谣言", "误会", "误传", "炒作", "营销", "澄清", "伪装")
    system_or_setting_followers = (
        "功法", "理论", "设定", "制度", "体系", "流派", "知识", "丫鬟", "女仆", "安排",
        "规则", "规矩", "桥段", "写法", "模板", "套路",
    )
    non_romantic_intimacy_followers = (
        "度", "等级", "系统", "规则", "设定", "模板", "术语", "说明", "接触史", "接触等级",
    )
    non_romantic_intimacy_contexts = (
        "亲密度", "亲密等级", "亲密系统", "亲密规则", "亲密设定", "亲密模板",
        "亲密术语", "亲密说明", "亲密接触史", "亲密接触等级",
        "讲解", "规则", "设定", "模板", "术语", "指标", "数值",
    )
    nonfactual_ending_contexts = ("梦见", "梦到", "梦境", "幻境", "假死", "制度", "结构", "讲解", "研究", "传说", "故事")
    non_ending_action_followers = ("线索", "证据", "伏笔", "案件", "调查", "评审", "报告", "评价", "评论", "后", "时", "期间", "过程")
    non_romantic_jealousy_contexts = ("厨房", "拌菜", "醋坛", "米醋", "陈醋", "香醋", "调料", "做菜", "事故")
    familial_or_comrade_contexts = (
        "亲情", "战友情", "友情", "家人式", "像兄妹", "像姐弟", "兄妹一样", "姐弟一样",
        "当弟弟", "当哥哥", "当妹妹", "当姐姐", "爱护后辈", "照顾后辈", "照顾晚辈",
        "姐姐照顾", "妹妹照顾", "师徒情", "同伴情", "伙伴情", "父女式", "母女式",
        "兄妹式", "姐弟式",
    )
    physical_intimacy_words = {
        "亲吻", "拥抱", "牵手", "同床", "同房", "双修", "推倒", "成亲", "成婚", "完婚",
        "大婚", "同居", "怀孕", "生下", "私通",
    }
    forced_or_victim_intimacy_contexts = (
        "强行", "强迫", "被迫", "胁迫", "逼迫", "下药", "迷奸", "猥亵", "非礼", "轻薄",
        "侵犯", "未遂", "差点", "险些", "企图", "被反派", "被路人", "被非男主", "被别人", "被他人",
    )
    past_relation_prefixes = ("前", "前任", "前世", "上一世", "过去", "过往", "前史", "重生前", "穿越前")
    current_relation_words = {"妻子", "正妻", "妻室", "夫妻", "夫妇", "未婚妻", "女朋友", "老婆", "恋人", "爱人", "伴侣", "情侣"}
    tight_roleplay_words = {"喜欢", "爱", "动心", "心动", "倾心", "表白", "告白", "暧昧"}
    for word in keywords:
        start = 0
        while word:
            index = text.find(word, start)
            if index < 0:
                break
            window = text[max(0, index - 12):index]
            around_window = text[max(0, index - 8):index + len(word)]
            roleplay_window = text[max(0, index - 8):index]
            tight_roleplay_window = text[max(0, index - 3):index]
            next_text = text[index + len(word):index + len(word) + 4]
            next_context = text[index + len(word):index + len(word) + 10]
            non_romantic_like = word == "喜欢" and (
                text[max(0, index - 2):index] == "喜不"
                or any(text[max(0, index - len(hint)):index] == hint for hint in non_romantic_like_prefixes)
                or any(next_text.startswith(hint) for hint in non_romantic_like_followers)
            )
            non_romantic_love = word == "爱" and (
                any(text[max(0, index - len(hint)):index] == hint for hint in non_romantic_love_prefixes)
                or any(next_text.startswith(hint) for hint in non_romantic_love_followers)
            )
            non_romantic_admire = word == "爱慕" and (
                any(next_context.startswith(hint) for hint in non_romantic_admire_followers)
                or any(hint in text[max(0, index - 4):index] for hint in ("被", "受", "读者", "旁人", "众人"))
            )
            non_romantic_emotion = word in ("动心", "心动", "倾心") and any(
                hint in text[max(0, index - 8):index + len(word) + 8]
                for hint in non_romantic_emotion_contexts
            )
            nonfactual_romance = word == "暧昧" and any(
                hint in text[max(0, index - 10):index + len(word) + 12]
                for hint in nonfactual_romance_contexts
            )
            system_or_setting_relation = word in (
                "双修", "同房", "道侣", "恋人", "情侣", "伴侣", "未婚妻",
                "妻子", "正妻", "妻室", "夫妻", "夫妇", "女朋友", "老婆", "爱人",
            ) and any(
                next_text.startswith(hint) or hint in next_context
                for hint in system_or_setting_followers
            )
            non_romantic_intimacy = word == "亲密" and (
                any(next_text.startswith(hint) for hint in non_romantic_intimacy_followers)
                or any(
                    hint in text[max(0, index - 4):index + len(word) + 12]
                    for hint in non_romantic_intimacy_contexts
                )
            )
            nonfactual_ending = word in ("死亡", "死去", "牺牲", "陨落", "葬", "坟", "墓") and any(
                hint in text[max(0, index - 8):index + len(word) + 8]
                for hint in nonfactual_ending_contexts
            )
            non_ending_action = word in ("留下", "跟随", "同行") and any(
                next_text.startswith(hint) for hint in non_ending_action_followers
            )
            non_romantic_jealousy = word == "吃醋" and any(
                hint in text[max(0, index - 8):index + len(word) + 8]
                for hint in non_romantic_jealousy_contexts
            )
            familial_or_comrade_emotion = word in (
                "亲密", "喜欢", "爱", "爱慕", "动心", "心动", "倾心", "陪伴", "相伴", "跟随", "同行"
            ) and any(
                hint in text[max(0, index - 14):index + len(word) + 14]
                for hint in familial_or_comrade_contexts
            )
            forced_or_victim_intimacy = word in physical_intimacy_words and any(
                hint in text[max(0, index - 12):index + len(word) + 12]
                for hint in forced_or_victim_intimacy_contexts
            )
            past_relation_mention = word in current_relation_words and any(
                text[max(0, index - len(prefix)):index] == prefix
                for prefix in past_relation_prefixes
            )
            roleplay_blocked = (
                any(hint in tight_roleplay_window for hint in roleplay_hints)
                if word in tight_roleplay_words
                else any(hint in roleplay_window for hint in roleplay_hints)
            )
            if (
                not any(hint in window or hint in around_window for hint in negative_hints)
                and not roleplay_blocked
                and not any(hint in next_text for hint in roleplay_after_hints)
                and not non_romantic_like
                and not non_romantic_love
                and not non_romantic_admire
                and not non_romantic_emotion
                and not nonfactual_romance
                and not system_or_setting_relation
                and not non_romantic_intimacy
                and not nonfactual_ending
                and not non_ending_action
                and not non_romantic_jealousy
                and not familial_or_comrade_emotion
                and not forced_or_victim_intimacy
                and not past_relation_mention
            ):
                return True
            start = index + len(word)
    return False


def _has_romance_gap_signal_text(value) -> bool:
    if isinstance(value, (list, tuple, set)):
        text = " ".join(str(x) for x in value if x is not None)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    if not text:
        return False

    negated_gap_patterns = (
        "不是没有恋爱", "并非没有恋爱", "并不是没有恋爱",
        "不是没有恋爱线", "并非没有恋爱线", "并不是没有恋爱线",
        "不是无恋爱线", "并非无恋爱线", "并不是无恋爱线",
        "不是没有暧昧", "并非没有暧昧", "并不是没有暧昧",
        "不是没有感情描写", "并非没有感情描写", "并不是没有感情描写",
        "不是完全没有感情描写", "并非完全没有感情描写", "并不是完全没有感情描写",
        "不是没有任何感情描写", "并非没有任何感情描写", "并不是没有任何感情描写",
        "不是没有感情戏", "并非没有感情戏", "并不是没有感情戏",
        "不是无感情戏", "并非无感情戏", "并不是无感情戏",
        "不是没有感情线", "并非没有感情线", "并不是没有感情线",
        "不是无感情线", "并非无感情线", "并不是无感情线",
        "没有感情戏缺失", "不存在感情戏缺失", "未见感情戏缺失",
        "没有感情描写缺失", "不存在感情描写缺失", "未见感情描写缺失",
        "没有恋爱推进缺失", "不存在恋爱推进缺失", "未见恋爱推进缺失",
        "没有感情推进缺失", "不存在感情推进缺失", "未见感情推进缺失",
    )
    protected_text = text
    for pattern in negated_gap_patterns:
        protected_text = protected_text.replace(pattern, "")

    romance_gap_patterns = (
        "没有感情描写", "无感情描写", "完全没有感情描写", "没有任何感情描写", "感情描写缺失",
        "感情戏缺失", "没有感情戏", "无感情戏", "没有感情线", "无感情线",
        "没有恋爱线", "无恋爱线", "没有恋爱", "没有暧昧", "恋爱推进缺失", "感情推进缺失",
        "缺少感情", "缺乏感情", "缺少恋爱", "缺乏恋爱",
        "没有后宫关系确认", "未确认后宫关系", "无后宫关系确认",
    )
    return any(pattern in protected_text for pattern in romance_gap_patterns)


def _has_positive_leak_relationship_confirmation(text: str) -> bool:
    return _contains_positive_signal_text(
        text,
        [
            "推倒", "同房", "双修", "成婚", "成亲", "完婚", "大婚", "纳妾",
            "妾室", "小妾", "为妾", "收妾", "通房", "侧室", "道侣", "恋人",
            "情侣", "伴侣", "未婚妻", "确认关系", "收入后宫", "收进后宫", "在一起",
        ],
    )


def _has_positive_leak_ending_account(text: str) -> bool:
    return _contains_positive_signal_text(
        text,
        [
            "归宿明确", "去向明确", "留在", "留下", "陪在", "跟随", "同行",
            "同去", "回到", "去了", "相伴", "在一起", "白头", "团聚", "重逢",
            "成婚", "成亲", "完婚", "大婚", "同居", "同房", "怀孕", "生下",
            "后宫", "道侣", "伴侣", "妻子", "妾室", "死亡", "死去", "牺牲",
            "陨落", "葬", "坟", "墓",
        ],
    )


def _has_positive_heroine_position_signal(text: str) -> bool:
    text = str(text or "")
    if not text:
        return False
    return _contains_positive_signal_text(
        text,
        ["主线女主", "目标女主", "核心女主", "第一女主", "正牌女主", "女主角", "女主之一", "女主线"],
    )


_GENERIC_HEROINE_ANCHOR_NAMES = {
    "女主", "女主角", "女角色", "女子", "少女", "女人", "女性", "漂亮女子", "漂亮女人",
    "姑娘", "小姐", "夫人", "太太", "公主", "王妃", "皇后", "贵妃", "妃子", "圣女",
    "修女", "女仆", "侍女", "丫鬟", "丫环", "小女孩", "女孩", "萝莉",
}


def _is_generic_heroine_anchor_name(name: str) -> bool:
    text = str(name or "").strip()
    if not text:
        return True
    normalized = _heroine_name_key(text)
    if text in _GENERIC_HEROINE_ANCHOR_NAMES or normalized in _GENERIC_HEROINE_ANCHOR_NAMES:
        return True
    generic_title_suffixes = ("公主", "王妃", "皇后", "贵妃", "妃子", "圣女", "修女", "女仆", "侍女", "丫鬟", "丫环")
    generic_title_prefixes = ("帝国", "王国", "王室", "皇室", "教会", "皇帝", "国王", "某国", "邻国", "异国", "敌国")
    return (
        2 < len(text) <= 6
        and any(text.endswith(suffix) for suffix in generic_title_suffixes)
        and any(text.startswith(prefix) for prefix in generic_title_prefixes)
    )


def _is_valid_issue_heroine_anchor_alias(alias: str) -> bool:
    text = str(alias or "").strip()
    return len(text) >= 2 and not _is_generic_heroine_anchor_name(text)


def _summarize_heroine_effectiveness(heroine_meta: dict, profile: dict, evidence: dict = None) -> str:
    heroine_meta = heroine_meta or {}
    profile = profile or {}
    evidence = evidence or {}
    signals = []
    risks = []

    if profile.get("relationship_with_protagonist") and profile.get("relationship_with_protagonist") != "未描述":
        signals.append("有男主关系描述")
    if profile.get("key_events") and profile.get("key_events") != "未描述":
        signals.append("有关键事件")
    if profile.get("features") and profile.get("features") != "未描述":
        signals.append("有身份/性格特征")
    if heroine_meta.get("importance_rank") not in (None, "", 9999):
        signals.append(f"重要度排序 {heroine_meta.get('importance_rank')}")
    if int(evidence.get("count") or heroine_meta.get("count") or 0) >= 3:
        signals.append("多次出场")

    raw_text = " ".join(
        str(x or "")
        for x in [
            profile.get("identity"),
            profile.get("relationship_with_protagonist"),
            profile.get("features"),
            profile.get("key_events"),
            " ".join(str(s) for s in (evidence.get("summaries") or [])[:5]),
        ]
    )
    if _has_low_presence_or_tooling_signal(raw_text):
        risks.append("存在工具人/低存在感线索")
    if not signals:
        return "证据不足：缺少稳定关系、关键事件和出场信息。"
    if risks:
        return f"有效性存疑：{'；'.join(signals[:3])}；{'；'.join(risks)}。"
    return f"有效性较明确：{'；'.join(signals[:4])}。"


def _heroine_position_level(heroine_meta: dict, profile: dict, evidence: dict = None, purity_info: dict = None) -> str:
    heroine_meta = heroine_meta or {}
    profile = profile or {}
    evidence = evidence or {}
    purity_info = purity_info or {}

    def _int_value(value, default=9999):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    rank = _int_value(heroine_meta.get("importance_rank"), 9999)
    count = _int_value(evidence.get("count") or heroine_meta.get("count"), 0)
    text = " ".join(
        str(x or "")
        for x in [
            profile.get("identity"),
            profile.get("relationship_with_protagonist"),
            profile.get("features"),
            profile.get("key_events"),
            heroine_meta.get("relationship_type"),
            heroine_meta.get("summary"),
            " ".join(str(s) for s in (evidence.get("summaries") or [])[:8]),
            " ".join(str(s) for s in (evidence.get("relationships") or [])[:8]),
            " ".join(str(s) for s in (evidence.get("interactions") or [])[:8]),
            " ".join(str(s) for s in (evidence.get("emotion_signals") or [])[:8]),
        ]
    )

    score = 0
    signals = []
    risks = []

    pushed_confirmed = (
        purity_info.get("pushed_by_male_lead") is True
        and not _pushed_confirmation_is_nominal_or_negated_for_report(purity_info, text)
    )
    if pushed_confirmed:
        score += 4
        signals.append("已被男主明确推倒/确认关系")
    elif purity_info.get("pushed_by_male_lead") is True:
        risks.append("推倒/确认关系证据疑似名义或否定语境")
    if purity_info.get("is_leak_heroine") is True or purity_info.get("leak_emotional_depth") is True:
        score += 3
        signals.append("漏女/情感深度证据")
    strong_relationship_position_words = ["妻子", "正妻", "妻室", "夫妻", "夫妇", "道侣", "恋人", "爱人", "后宫", "未婚妻", "伴侣", "情侣", "女朋友", "老婆"]
    romance_signal_words = ["喜欢", "爱慕", "表白", "暧昧", "吃醋", "双修", "同房", "亲密", "推倒", "收女", "动心", "心动", "倾心"]
    has_nominal_or_negated_relationship_context = _relationship_position_is_nominal_or_negated_for_report(text)
    has_strong_relationship_position = (
        _contains_positive_signal_text(text, strong_relationship_position_words)
        and not has_nominal_or_negated_relationship_context
    )
    if has_strong_relationship_position or _has_positive_heroine_position_signal(text):
        score += 2
        signals.append("关系定位明确")
    if _contains_positive_signal_text(text, romance_signal_words):
        score += 2
        signals.append("感情/亲密推进")
    if profile.get("relationship_with_protagonist") and profile.get("relationship_with_protagonist") != "未描述":
        score += 1
        signals.append("有男主关系描述")
    if profile.get("key_events") and profile.get("key_events") != "未描述":
        score += 1
        signals.append("有关键事件")
    if count >= 5:
        score += 2
        signals.append(f"高频出场 {count} 次")
    elif count >= 3:
        score += 1
        signals.append(f"多次出场 {count} 次")
    if rank <= 3:
        score += 2
        signals.append(f"重要度排序 {rank}")
    elif rank <= 8:
        score += 1
        signals.append(f"重要度排序 {rank}")

    if _has_low_presence_or_tooling_signal(text):
        score -= 2
        risks.append("低存在感/工具人线索")
    has_romance_gap_signal = _has_romance_gap_signal_text(text)
    if has_romance_gap_signal:
        score -= 1
        risks.append("明确缺少恋爱/后宫推进")
    if not signals:
        risks.append("缺少关系、事件和出场证据")

    has_confirmed_target = (
        pushed_confirmed
        or has_strong_relationship_position
    )
    has_candidate_relationship_signal = (
        has_confirmed_target
        or purity_info.get("is_leak_heroine") is True
        or purity_info.get("leak_emotional_depth") is True
        or _has_positive_heroine_position_signal(text)
        or has_strong_relationship_position
        or _contains_positive_signal_text(text, romance_signal_words)
    )
    if score >= 7 and has_confirmed_target:
        label = "目标女主"
    elif score >= 4 and has_candidate_relationship_signal:
        label = "强准女主"
    elif score >= 2 and not (has_romance_gap_signal and not has_candidate_relationship_signal):
        label = "弱准女主"
    else:
        label = "低证据女角色"

    if (score >= 4 or has_romance_gap_signal) and not has_candidate_relationship_signal:
        risks.append("缺少感情/后宫定位证据")

    detail = "；".join(dict.fromkeys(signals[:4] + risks[:3])) or "证据不足"
    return f"{label}：{detail}"


def _pushed_confirmation_is_nominal_or_negated_for_report(purity_info: dict, context_text: str) -> bool:
    text = " ".join(
        str(part or "")
        for part in [
            purity_info.get("pushed_reason") if isinstance(purity_info, dict) else "",
            purity_info.get("leak_relationship_reason") if isinstance(purity_info, dict) else "",
            context_text,
        ]
    )
    if not text:
        return False
    nominal_or_negated_markers = (
        "只是称呼", "只是个称呼", "只是外号", "只是绰号", "玩笑称呼", "调侃称呼", "口头称呼",
        "只是玩笑", "开玩笑", "读者调侃", "读者脑补", "粉丝称呼", "书友称呼",
        "有名无实", "名义夫妻", "名义上的夫妻", "名义婚约", "名义关系",
        "假结婚", "假扮夫妻", "伪装夫妻", "契约夫妻", "政治婚约", "政治联姻",
        "未圆房", "没有圆房", "未同房", "没有同房", "未发生关系", "没有发生关系",
        "无实质关系", "没有实质关系", "无身体关系", "没有身体关系", "未确认关系",
        "关系确认未知", "非实质确认语境",
    )
    if not any(marker in text for marker in nominal_or_negated_markers):
        return False
    return not _contains_positive_signal_text(
        text,
        [
            "已同房", "已经同房", "明确同房", "发生关系", "发生性关系", "圆房了", "已经圆房",
            "确认推倒", "明确推倒", "收入后宫", "收进后宫", "成为道侣", "确认关系",
        ],
    )


def _relationship_position_is_nominal_or_negated_for_report(text: str) -> bool:
    text = str(text or "")
    if not text:
        return False
    nominal_or_negated_markers = (
        "有名无实", "名义夫妻", "名义上的夫妻", "名义婚约", "名义关系",
        "假结婚", "假扮夫妻", "伪装夫妻", "契约夫妻", "政治婚约", "政治联姻",
        "未圆房", "没有圆房", "未同房", "没有同房", "未发生关系", "没有发生关系",
        "无实质关系", "没有实质关系", "无身体关系", "没有身体关系", "未确认关系",
        "没有确认关系", "未收入后宫", "没有收入后宫", "未收女", "没有收女",
    )
    if not any(marker in text for marker in nominal_or_negated_markers):
        return False
    return not _contains_positive_signal_text(
        text,
        ["已同房", "已经同房", "明确同房", "发生关系", "发生性关系", "圆房了", "已经圆房", "确认关系", "收入后宫"],
    )


def _has_low_presence_or_tooling_signal(text: str) -> bool:
    text = str(text or "")
    if not text:
        return False
    negated_tooling_patterns = (
        "不是工具人", "并非工具人", "非工具人", "不算工具人", "没有工具人化",
        "不是背景板", "并非背景板", "非背景板",
        "不是召唤物", "并非召唤物", "非召唤物", "不是召唤工具", "并非召唤工具",
        "没有召唤功能", "没有召唤物功能", "不负责召唤",
        "不是捧哏", "并非捧哏", "非捧哏", "不负责捧哏",
        "不是客串", "并非客串", "非客串", "不算客串", "没有客串",
        "不是神隐", "并非神隐", "非神隐", "没有神隐", "并未神隐", "未神隐", "不再神隐",
        "不是低存在感", "并非低存在感", "非低存在感", "不算低存在感", "没有低存在感",
        "没有低存在感问题", "没有低存在感风险", "没有低存在感线索",
        "无低存在感", "无低存在感问题", "无低存在感风险", "无低存在感线索",
        "不存在低存在感", "不存在低存在感问题", "存在感不低", "存在感并不低", "存在感不算低",
        "存在感没有那么低", "存在感不是很低", "存在感不是较低", "存在感不是偏低",
    )
    protected_text = text
    for pattern in negated_tooling_patterns:
        protected_text = protected_text.replace(pattern, "")
    tooling_words = ("工具", "捧哏", "客串", "神隐")
    if _contains_any_text(protected_text, tooling_words):
        return True
    summon_tooling_patterns = (
        "召唤物", "召唤工具", "召唤助手", "召唤功能",
        "负责召唤", "承担召唤", "帮男主召唤", "帮主角召唤", "做召唤物",
    )
    if any(pattern in protected_text for pattern in summon_tooling_patterns):
        return True
    background_exposition_patterns = (
        "背景说明", "说明背景", "负责说明", "负责解释", "解释背景", "讲解背景",
        "说明功能", "解释功能", "功能性说明", "设定说明", "世界观说明",
    )
    if any(pattern in text for pattern in background_exposition_patterns):
        return True
    low_frequency_patterns = (
        "偶尔出场", "偶尔登场", "偶尔出现", "偶尔露面", "偶尔客串", "偶尔同场",
        "偶尔在场", "偶尔参与", "偶尔帮忙", "偶尔协助",
    )
    if any(pattern in text for pattern in low_frequency_patterns):
        return True
    low_presence_patterns = (
        "低存在感", "存在感低", "存在感很低", "存在感较低", "存在感偏低", "存在感不高",
        "存在感小", "存在感很小", "存在感较小", "存在感偏小",
        "存在感弱", "存在感很弱", "存在感较弱", "存在感偏弱",
        "存在感约等于没有", "存在感约等于无", "存在感等于没有", "存在感几乎没有",
        "存在感几乎为零", "存在感接近没有", "存在感接近于无", "存在感稀薄",
    )
    return any(pattern in protected_text for pattern in low_presence_patterns)


def _build_heroine_position_contexts(
    heroines: list,
    all_female_characters: dict,
    profile_cache: dict,
    purity_map: dict,
) -> list:
    contexts = []
    seen = set()
    for h in heroines or []:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        if not name:
            continue
        if _is_generic_heroine_anchor_name(name):
            continue
        aliases = [
            str(x).strip()
            for x in (h.get("aliases") or h.get("other_names") or [])
            if str(x).strip()
        ]
        evid = _match_female_evidence(name, aliases, all_female_characters) or {}
        aliases.extend(str(x).strip() for x in (evid.get("other_names") or []) if str(x).strip())
        prof = _normalize_profile_for_report(profile_cache.get(name, {}))
        purity_info = purity_map.get(name) or purity_map.get(_heroine_name_key(name)) or {}
        level = _heroine_position_level(h, prof, evid, purity_info)
        label = level.split("：", 1)[0]

        names = [name, *aliases, _heroine_name_key(name)]
        normalized_names = []
        for candidate in names:
            candidate = str(candidate or "").strip()
            if not _is_valid_issue_heroine_anchor_alias(candidate):
                continue
            key = (name, candidate)
            if key in seen:
                continue
            seen.add(key)
            normalized_names.append(candidate)
        if not normalized_names:
            continue
        contexts.append({"name": name, "aliases": normalized_names, "label": label, "level": level})
    return contexts


def _matched_issue_heroine_contexts(issue: dict, heroine_contexts: list) -> list:
    if not issue or not heroine_contexts:
        return []
    haystack = " ".join(
        str(issue.get(field) or "")
        for field in ["content", "review_comment", "type", "reason", "evidence", "category"]
    )
    if not haystack:
        return []

    matches = []
    for ctx in heroine_contexts:
        aliases = ctx.get("aliases") or []
        matched_aliases = [
            alias
            for alias in aliases
            if _is_valid_issue_heroine_anchor_alias(alias) and alias in haystack
        ]
        if not matched_aliases:
            continue
        best_alias = max(matched_aliases, key=len)
        matches.append((len(best_alias), ctx))

    if not matches:
        return []
    matches.sort(key=lambda item: (-item[0], item[1].get("name", "")))
    unique = []
    seen = set()
    for _, ctx in matches:
        name = ctx.get("name") or ""
        if name in seen:
            continue
        seen.add(name)
        unique.append(ctx)
        if len(unique) >= 3:
            break
    return unique


def _match_issue_heroine_context(issue: dict, heroine_contexts: list) -> str:
    unique = _matched_issue_heroine_contexts(issue, heroine_contexts)
    return "；".join(f"{ctx.get('name')}={ctx.get('label')}" for ctx in unique if ctx.get("name"))


def _issue_definition_review_hint(issue: dict, heroine_contexts: list) -> str:
    issue_type = str((issue or {}).get("type") or "")
    if not is_strict_harem_issue_type(issue_type):
        return ""
    evidence_hint = _strict_issue_evidence_review_hint(issue)
    matched = _matched_issue_heroine_contexts(issue, heroine_contexts)
    if not matched:
        base = "按锁定定义，送女/绿帽必须锚定目标女主或强准女主；当前条目未命中已识别女主名或别名，建议复核对象是否成立。"
        return f"{base}；{evidence_hint}" if evidence_hint else base
    weak_names = [
        ctx.get("name")
        for ctx in matched
        if ctx.get("name") and ctx.get("label") not in ("目标女主", "强准女主")
    ]
    if not weak_names:
        return evidence_hint
    base = f"按锁定定义，送女/绿帽仅适用于目标女主或强准女主；{','.join(weak_names[:3])} 当前定位偏弱，建议复核是否误判。"
    return f"{base}；{evidence_hint}" if evidence_hint else base


def _strict_issue_evidence_review_hint(issue: dict) -> str:
    text = " ".join(
        str((issue or {}).get(field) or "")
        for field in ("content", "review_comment", "reason", "evidence")
    )
    if not text:
        return ""
    hints = []
    send_girl_hint = _strict_send_girl_agency_review_hint(issue, text)
    if send_girl_hint:
        hints.append(send_girl_hint)
    ntr_victim_hint = _strict_ntr_victim_only_review_hint(issue, text)
    if ntr_victim_hint:
        hints.append(ntr_victim_hint)
    ntr_male_lead_expansion_hint = _strict_ntr_male_lead_expansion_review_hint(issue, text)
    if ntr_male_lead_expansion_hint:
        hints.append(ntr_male_lead_expansion_hint)
    ntr_same_subject_hint = _strict_ntr_same_subject_review_hint(issue, text)
    if ntr_same_subject_hint:
        hints.append(ntr_same_subject_hint)
    send_girl_to_male_lead_hint = _strict_send_girl_to_male_lead_review_hint(issue, text)
    if send_girl_to_male_lead_hint:
        hints.append(send_girl_to_male_lead_hint)
    nonfactual_markers = (
        "传闻", "传言", "流言", "谣言", "据说", "听说", "口嗨", "意淫", "误会", "误传",
        "梦境", "梦见", "梦到", "幻境", "弱暗示", "疑似", "嫌疑", "待复核", "待确认",
        "未来计划", "计划中", "打算", "扬言", "威胁", "未遂", "差点", "险些", "未发生",
        "读者调侃", "读者脑补", "脑补", "嗑CP", "磕CP", "CP感", "组CP", "拉郎", "正文没有",
        "正文无", "没有事实", "无事实", "没有实锤", "无实锤",
    )
    if any(marker in text for marker in nonfactual_markers):
        hints.append("证据含传闻/口嗨/误会/未遂/未来计划等非事实或弱证据标记，严格关系雷点需复核事实性。")
    return "；".join(hints)


def _strict_send_girl_agency_review_hint(issue: dict, text: str) -> str:
    issue_type = str((issue or {}).get("type") or "")
    if "送女" not in issue_type:
        return ""
    passive_or_third_party_markers = (
        "被安排嫁给", "被迫嫁给", "被逼嫁给", "被赐婚", "被许配", "被迫成婚",
        "家族逼婚", "家族安排", "父母安排", "师门安排", "皇帝赐婚", "贵族联姻",
        "政治联姻", "普通政治联姻", "背景婚配", "婚配安排",
        "旁人撮合", "别人撮合", "配角撮合", "家族撮合", "长辈撮合", "读者撮合",
        "读者调侃", "读者脑补", "组CP", "拉郎",
    )
    third_party_sender_markers = (
        "反派计划", "反派打算", "反派要把", "配角计划", "配角打算", "家族把", "家族安排",
        "父母把", "师门把", "皇帝把", "皇帝赐婚", "贵族把",
        "旁人撮合", "别人撮合", "配角撮合", "家族撮合", "长辈撮合", "读者撮合",
    )
    male_lead_absent_markers = (
        "男主未参与", "男主没有参与", "男主未主动参与", "男主没有主动参与",
        "主角未参与", "主角没有参与", "主角未主动参与", "主角没有主动参与",
        "男主不知情", "主角不知情",
    )
    has_passive_arrangement = any(marker in text for marker in passive_or_third_party_markers)
    has_third_party_sender = any(marker in text for marker in third_party_sender_markers)
    has_male_lead_absent = any(marker in text for marker in male_lead_absent_markers)
    if not has_passive_arrangement and not (has_third_party_sender and has_male_lead_absent):
        return ""
    male_lead_agency_phrases = (
        "男主主动", "男主默许", "男主认可", "男主同意", "男主促成", "男主撮合", "男主安排",
        "男主送给", "男主让给", "男主让渡", "男主明知", "男主角主动", "男主角默许",
        "主角主动", "主角默许", "主角认可", "主角同意", "主角促成", "主角撮合",
        "主角安排", "主角送给", "主角让给", "主角让渡", "主角明知",
    )
    negated_male_lead_agency_phrases = (
        "没有男主主动", "没有男主默许", "没有男主认可", "没有男主同意", "没有男主促成",
        "没有男主撮合", "没有男主安排", "没有男主送给", "没有男主让给", "没有男主明知",
        "没有男主主动撮合", "没有男主主动安排", "没有男主主动促成", "没有男主主动送给",
        "无男主主动", "无男主默许", "无男主认可", "无男主同意", "无男主促成",
        "无男主撮合", "无男主安排", "无男主送给", "无男主让给", "无男主明知",
        "无男主主动撮合", "无男主主动安排", "无男主主动促成", "无男主主动送给",
        "男主没有主动", "男主没有默许", "男主没有认可", "男主没有同意", "男主没有促成",
        "男主没有撮合", "男主没有安排", "男主没有送给", "男主没有让给", "男主没有明知",
        "男主没有主动撮合", "男主没有主动安排", "男主没有主动促成", "男主没有主动送给",
        "没有主角主动", "没有主角默许", "没有主角认可", "没有主角同意", "没有主角促成",
        "没有主角撮合", "没有主角安排", "没有主角送给", "没有主角让给", "没有主角明知",
        "没有主角主动撮合", "没有主角主动安排", "没有主角主动促成", "没有主角主动送给",
        "主角没有主动", "主角没有默许", "主角没有认可", "主角没有同意", "主角没有促成",
        "主角没有撮合", "主角没有安排", "主角没有送给", "主角没有让给", "主角没有明知",
        "主角没有主动撮合", "主角没有主动安排", "主角没有主动促成", "主角没有主动送给",
    )
    male_lead_prevention_phrases = (
        "男主主动救", "男主救下", "男主营救", "男主解救", "男主阻止", "男主制止",
        "男主破坏联姻", "男主阻止联姻", "男主打断联姻", "男主拒绝联姻",
        "男主拒绝撮合", "男主拒绝安排", "男主没有送出", "男主并未送出",
        "男主未送出", "男主没有让出", "男主并未让出", "男主未让出",
        "男主反对", "男主不同意", "男主不认可", "男主不默许",
        "主角主动救", "主角救下", "主角营救", "主角解救", "主角阻止", "主角制止",
        "主角破坏联姻", "主角阻止联姻", "主角打断联姻", "主角拒绝联姻",
        "主角拒绝撮合", "主角拒绝安排", "主角没有送出", "主角并未送出",
        "主角未送出", "主角没有让出", "主角并未让出", "主角未让出",
        "主角反对", "主角不同意", "主角不认可", "主角不默许",
    )
    if any(phrase in text for phrase in male_lead_agency_phrases) and not any(
        phrase in text for phrase in negated_male_lead_agency_phrases
    ) and not any(phrase in text for phrase in male_lead_prevention_phrases):
        return ""
    return "送女必须有男主主动或默许构成；当前证据更像被动安排/第三方送人/逼婚/政治联姻，需复核是否缺少男主主体。"


def _strict_ntr_victim_only_review_hint(issue: dict, text: str) -> str:
    issue_type = str((issue or {}).get("type") or "")
    upper_type = issue_type.upper()
    if "绿帽" not in issue_type and "NTR" not in upper_type and "牛头人" not in issue_type:
        return ""
    victim_or_threat_markers = (
        "被强迫", "被胁迫", "被囚禁", "被调戏", "被窥视", "被绑走", "被绑架", "被下药",
        "被骚扰", "被侵犯未遂", "强奸未遂", "企图侵犯", "企图强奸", "差点被侵犯", "险些被侵犯",
        "被强吻", "被猥亵", "被占便宜", "被非礼", "被轻薄", "被摸", "被搂抱", "被抱住",
        "强吻", "猥亵", "非礼", "轻薄", "强行亲吻", "强行搂抱", "强行抱住",
        "绑走调戏", "囚禁调戏", "言语调戏", "下药", "药物胁迫",
        "差点被迫同房", "险些被迫同房", "差点被迫圆房", "险些被迫圆房",
        "同房未遂", "圆房未遂", "失身未遂", "被迫同房未遂", "被迫圆房未遂",
        "被男主救下", "被主角救下", "男主救下", "主角救下",
    )
    if not any(marker in text for marker in victim_or_threat_markers):
        return ""
    relationship_fact_markers = (
        "明确性关系", "发生性关系", "同房", "圆房", "失身", "破身", "怀孕", "生下",
        "主动暧昧", "主动恋爱", "主观情感背叛", "情感背叛", "背叛男主", "爱上", "喜欢上",
    )
    negated_relationship_markers = (
        "没有性关系", "未发生性关系", "没有发生关系", "没有同房", "未同房", "没有圆房", "未圆房",
        "没有情感背叛", "无情感背叛", "没有主观背叛", "未背叛男主", "没有背叛男主",
        "没有暧昧", "未暧昧", "没有恋爱", "未恋爱", "没有动心", "未动心", "没有喜欢上", "未喜欢上",
        "没有爱上", "未爱上", "并未喜欢", "并未爱上", "并无暧昧", "并无恋爱",
        "差点同房", "险些同房", "差点圆房", "险些圆房", "差点失身", "险些失身",
        "差点被迫同房", "险些被迫同房", "差点被迫圆房", "险些被迫圆房",
        "同房未遂", "圆房未遂", "失身未遂", "被迫同房未遂", "被迫圆房未遂",
        "未发生同房", "未发生圆房", "未发生失身", "没发生同房", "没发生圆房", "没失身",
        "被男主救下", "被主角救下", "男主救下", "主角救下",
    )
    has_relationship_fact = any(marker in text for marker in relationship_fact_markers)
    has_negated_relationship = any(marker in text for marker in negated_relationship_markers)
    if has_relationship_fact and not has_negated_relationship:
        return ""
    return "绿帽必须有明确暧昧/恋爱/性关系或实质情感背叛；当前证据更像强迫、调戏、绑走或未遂受害，应复核是否应降为亵女/虐女/NTR擦边。"


def _strict_ntr_male_lead_expansion_review_hint(issue: dict, text: str) -> str:
    issue_type = str((issue or {}).get("type") or "")
    upper_type = issue_type.upper()
    if "绿帽" not in issue_type and "NTR" not in upper_type and "牛头人" not in issue_type:
        return ""
    male_lead_relation_markers = (
        "男主与", "男主和", "男主睡", "男主推倒", "男主收", "男主纳", "主角与", "主角和",
        "主角睡", "主角推倒", "主角收", "主角纳",
    )
    female_relative_markers = ("闺蜜", "姐妹", "姐姐", "妹妹", "母亲", "妈妈", "娘亲", "女儿", "亲友", "侍女", "丫鬟")
    if not any(marker in text for marker in male_lead_relation_markers):
        return ""
    if not any(marker in text for marker in female_relative_markers):
        return ""
    return "绿帽排除男主与女主亲友或其他女性发生关系；当前证据更像后宫扩张/推土机情节，需复核是否误标绿帽。"


def _strict_ntr_same_subject_review_hint(issue: dict, text: str) -> str:
    issue_type = str((issue or {}).get("type") or "")
    upper_type = issue_type.upper()
    if "绿帽" not in issue_type and "NTR" not in upper_type and "牛头人" not in issue_type:
        return ""
    same_subject_markers = (
        "男主分身", "主角分身", "男主马甲", "主角马甲", "男主化身", "主角化身",
        "同一灵魂", "同一主体", "同一意识", "身体替代", "灵魂替代", "合法化身",
        "本质上是男主", "本质上是主角", "男主本人", "主角本人", "男主操控", "主角操控",
    )
    if not any(marker in text for marker in same_subject_markers):
        return ""
    independent_markers = (
        "独立人格", "脱离男主控制", "脱离主角控制", "不受男主控制", "不受主角控制",
        "背叛男主", "背叛主角", "独立关系",
    )
    if any(marker in text for marker in independent_markers):
        return ""
    return "绿帽要求对象是非男主男性；当前证据更像男主分身/马甲/同一灵魂/身体替代，应优先按同一主体或分身流风险复核。"


def _strict_send_girl_to_male_lead_review_hint(issue: dict, text: str) -> str:
    issue_type = str((issue or {}).get("type") or "")
    if "送女" not in issue_type:
        return ""
    to_male_lead_markers = (
        "献给男主", "献给主角", "送给男主", "送给主角", "安排给男主", "安排给主角",
        "交给男主", "交给主角", "嫁给男主", "嫁给主角",
        "男主收入后宫", "主角收入后宫", "被男主收入后宫", "被主角收入后宫",
        "男主收进后宫", "主角收进后宫", "被男主收进后宫", "被主角收进后宫",
        "男主接收", "主角接收", "被男主接收", "被主角接收",
        "男主救下", "主角救下", "被男主救下", "被主角救下",
        "男主纳入后宫", "主角纳入后宫", "被男主纳入后宫", "被主角纳入后宫",
        "男主纳为妾", "主角纳为妾", "被男主纳为妾", "被主角纳为妾",
        "男主纳妾", "主角纳妾",
    )
    male_lead_invitation_markers = (
        "向男主发起联姻", "向主角发起联姻", "向男主提出联姻", "向主角提出联姻",
    )
    male_lead_invitation_subject_markers = ("向男主", "向主角")
    invitation_action_markers = ("联姻邀请", "邀请联姻", "发起联姻", "提出联姻")
    male_lead_rejection_markers = ("被男主拒绝", "被主角拒绝", "男主拒绝", "主角拒绝")
    has_to_male_lead = any(marker in text for marker in to_male_lead_markers)
    has_rejected_male_lead_invitation = (
        (
            any(marker in text for marker in male_lead_invitation_markers)
            or (
                any(marker in text for marker in male_lead_invitation_subject_markers)
                and any(marker in text for marker in invitation_action_markers)
            )
        )
        and any(marker in text for marker in male_lead_rejection_markers)
    )
    if not has_to_male_lead and not has_rejected_male_lead_invitation:
        return ""
    return "送女排除配角/家族/反派把女性献给男主或男主接收女性；当前证据更像收女/献女/后宫扩张或联姻邀请/拒绝收女，需复核是否误标送女。"


def _annotate_issue_for_report(issue: dict, heroine_contexts: list) -> dict:
    annotated = dict(issue or {})
    context = _match_issue_heroine_context(annotated, heroine_contexts)
    review_hint = _issue_definition_review_hint(annotated, heroine_contexts)
    if context:
        annotated["heroine_position_context"] = context
    if review_hint:
        annotated["definition_review_hint"] = review_hint
    return annotated


def _annotate_issues_for_report(issues: list, heroine_contexts: list) -> list:
    return [_annotate_issue_for_report(issue, heroine_contexts) for issue in (issues or [])]


def _append_issue_lines(risk_lines: list, issues: list, heroine_contexts: list) -> None:
    for i, p in enumerate(_annotate_issues_for_report(issues, heroine_contexts), 1):
        risk_lines.append(f"{i}. [{p.get('type','')}] @chunk {p.get('chunk_index')}")
        context = p.get("heroine_position_context")
        if context:
            risk_lines.append(f"   女主定位上下文：{context}")
        review_hint = p.get("definition_review_hint")
        if review_hint:
            risk_lines.append(f"   定义复核提示：{review_hint}")
        risk_lines.append(f"   原文：{p.get('content','')}")
        if p.get("review_comment"):
            risk_lines.append(f"   裁决：{p.get('review_comment')}")


def _summarize_heroine_relationship_structure(profile: dict, evidence: dict = None) -> str:
    profile = profile or {}
    evidence = evidence or {}
    text = " ".join(
        str(x or "")
        for x in [
            profile.get("identity"),
            profile.get("relationship_with_protagonist"),
            profile.get("features"),
            profile.get("key_events"),
            " ".join(str(s) for s in (evidence.get("summaries") or [])[:8]),
            " ".join(str(s) for s in (evidence.get("relationships") or [])[:8]),
            " ".join(str(s) for s in (evidence.get("features") or [])[:8]),
            json.dumps((evidence.get("purity_facts") or {}).get("economic_attachments") or [], ensure_ascii=False),
            json.dumps((evidence.get("purity_facts") or {}).get("power_relations") or [], ensure_ascii=False),
            json.dumps((evidence.get("purity_facts") or {}).get("political_marriages") or [], ensure_ascii=False),
            json.dumps((evidence.get("purity_facts") or {}).get("victim_records") or [], ensure_ascii=False),
        ]
    )
    tags = []
    purity_facts = evidence.get("purity_facts") or {}
    if purity_facts.get("economic_attachments") or _contains_any_text(text, ["依附", "供养", "赡养", "债务", "欠债", "卖身", "赎身", "包养", "经济", "资源", "养活"]):
        tags.append("经济依附")
    if purity_facts.get("power_relations") or _contains_any_text(text, ["上司", "下属", "主仆", "主人", "奴", "师徒", "宗主", "皇帝", "女王", "权力", "掌控", "命令", "生杀"]):
        tags.append("权力关系")
    if purity_facts.get("political_marriages") or _contains_any_text(text, ["联姻", "和亲", "赐婚", "婚约", "包办", "政治婚姻", "家族婚约", "逼婚", "被迫嫁"]):
        tags.append("政治联姻/婚约")
    if purity_facts.get("victim_records") or _contains_any_text(text, ["受害", "被迫", "强迫", "胁迫", "囚禁", "绑架", "下药", "侵犯", "猥亵", "偷拍", "直播", "曝光", "洗脑"]):
        tags.append("受害/胁迫记录")
    if not tags:
        return "未见明确经济依附、权力关系、政治联姻或受害记录线索。"
    return "；".join(dict.fromkeys(tags))


def _summarize_leak_three_layers(purity_info: dict, profile: dict) -> str:
    purity_info = purity_info or {}
    profile = profile or {}
    if any(key in purity_info for key in ("leak_emotional_depth", "leak_relationship_confirmed", "leak_ending_accounted")):
        emotional_depth = purity_info.get("leak_emotional_depth")
        relationship_confirmed = purity_info.get("leak_relationship_confirmed")
        ending_accounted = purity_info.get("leak_ending_accounted")
        leak = purity_info.get("is_leak_heroine")
        if leak is True:
            verdict = "疑似漏女"
        elif leak is False:
            verdict = "未判漏女"
        elif leak is None:
            verdict = "暂不判定"
        else:
            verdict = "证据不足"
        return (
            f"情感深度={_bool_mark(emotional_depth)}；"
            f"关系确认={_bool_mark(relationship_confirmed)}；"
            f"结局交代={_bool_mark(ending_accounted)}；"
            f"结论={verdict}"
        )

    relation_text = profile.get("relationship_with_protagonist") or ""
    events_text = profile.get("key_events") or ""
    profile_text = f"{relation_text} {events_text}"

    has_emotional_depth = _contains_positive_signal_text(
        profile_text,
        ["暧昧", "喜欢", "爱", "动心", "心动", "倾心", "表白", "告白", "吃醋", "道侣", "恋人", "未婚妻"],
    )
    pushed = purity_info.get("pushed_by_male_lead")
    leak = purity_info.get("is_leak_heroine")
    has_relationship_confirmed = bool(pushed) or _has_positive_leak_relationship_confirmation(profile_text)
    has_ending_note = _has_positive_leak_ending_account(profile_text)

    if leak is True:
        verdict = "疑似漏女"
    elif leak is False:
        verdict = "未判漏女"
    elif has_emotional_depth and not has_relationship_confirmed:
        verdict = "需关注"
    else:
        verdict = "证据不足"

    return (
        f"情感深度={'有' if has_emotional_depth else '未明'}；"
        f"关系确认={'有' if has_relationship_confirmed else '未明'}；"
        f"结局交代={'有' if has_ending_note else '未明'}；"
        f"结论={verdict}"
    )


def _match_female_evidence(name: str, aliases: list, all_female_characters: dict):
    """
    在 detailed_data["all_female_characters"] 里为某个女主找证据条目。
    """
    if not name or not all_female_characters:
        return None
    if name in all_female_characters:
        return all_female_characters.get(name)
    for a in aliases or []:
        if a in all_female_characters:
            return all_female_characters.get(a)
    # other_names 反向匹配
    for _k, v in (all_female_characters or {}).items():
        others = (v or {}).get("other_names", []) or []
        if name in others:
            return v
        for a in aliases or []:
            if a in others:
                return v
    # 子串兜底（至少 3 字，避免误配）
    if len(name) >= 3:
        for k, v in (all_female_characters or {}).items():
            if k and (name in k or k in name):
                return v
    return None


_TITLE_WORDS = [
    "太后", "皇后", "皇帝", "女帝", "公主", "长公主", "郡主", "王妃", "贵妃", "妃子",
    "圣女", "仙子", "师父", "师傅", "师尊", "师姐", "师妹", "师叔", "师娘",
    "小姐", "大小姐", "夫人", "娘娘", "楼主", "宫主", "宗主", "剑主", "阁主",
    "掌门", "女侠", "姑娘", "姨娘", "姑母", "太妃",
]

_PARALLEL_TIMELINE_NAME_MARKERS = (
    "另一个世界", "其他世界", "异世界", "平行世界", "平行线", "世界线",
    "未来线", "过去线", "原世界", "现世界", "本世界", "前世", "今生", "来世",
    "重生前", "重生后", "轮回前", "轮回后",
)


def _has_parallel_timeline_name_marker(name: str) -> bool:
    text = str(name or "").strip()
    return any(marker in text for marker in _PARALLEL_TIMELINE_NAME_MARKERS)


def _heroine_name_key(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"[\s·・,，。:：；;、'\"“”‘’\-_—]", "", text)
    changed = True
    while changed and text:
        changed = False
        for word in sorted(_TITLE_WORDS, key=len, reverse=True):
            if text.startswith(word) and len(text) > len(word):
                text = text[len(word):]
                changed = True
            if text.endswith(word) and len(text) > len(word):
                text = text[:-len(word)]
                changed = True
    return text


def _heroine_match_keys(name: str, aliases: list = None) -> set:
    keys = set()
    for item in [name, *(aliases or [])]:
        key = _heroine_name_key(item)
        if key and len(key) >= 2 and not _is_generic_heroine_anchor_name(key):
            keys.add(key)
    return keys


def _harem_consistency_warnings(heroines: list, reviewer: dict, all_female_characters: dict = None) -> list:
    scan_by_key = {}
    reviewer_by_key = {}

    for heroine in heroines or []:
        if not isinstance(heroine, dict):
            continue
        name = str(heroine.get("name") or "").strip()
        if not name or _is_generic_heroine_anchor_name(name):
            continue
        aliases = heroine.get("aliases") or heroine.get("other_names") or []
        evidence = _match_female_evidence(name, aliases, all_female_characters or {}) or {}
        evidence_aliases = evidence.get("other_names") or []
        for key in _heroine_match_keys(name, [*aliases, *evidence_aliases]):
            scan_by_key.setdefault(key, name)

    for item in (reviewer or {}).get("heroines_purity", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or _is_generic_heroine_anchor_name(name):
            continue
        for key in _heroine_match_keys(name):
            reviewer_by_key.setdefault(key, name)

    if not scan_by_key and not reviewer_by_key:
        return []

    warnings = []
    scan_keys = set(scan_by_key)
    reviewer_keys = set(reviewer_by_key)
    missing_review = sorted({scan_by_key[key] for key in scan_keys - reviewer_keys})
    extra_review = sorted({reviewer_by_key[key] for key in reviewer_keys - scan_keys})
    if missing_review:
        warnings.append(f"扫描阶段识别到但审核洁度未覆盖：{', '.join(missing_review[:10])}")
    if extra_review:
        warnings.append(f"审核洁度中出现但扫描女主列表未列出：{', '.join(extra_review[:10])}")
    return warnings


def _issue_type_key(issue: dict) -> str:
    text = str((issue or {}).get("type") or (issue or {}).get("category") or "").strip()
    return re.sub(r"\s+", "", text)


def _issue_content_key(issue: dict) -> str:
    text = " ".join(
        str((issue or {}).get(field) or "")
        for field in ("content", "reason", "evidence", "review_comment")
    )
    text = re.sub(r"\s+", "", text)
    return text[:80]


def _is_reviewworthy_scan_issue(issue: dict) -> bool:
    issue_type = _issue_type_key(issue)
    category = str((issue or {}).get("category") or "")
    text = f"{issue_type} {category} {_issue_content_key(issue)}"
    if is_strict_harem_issue_type(issue_type):
        return True
    markers = (
        "雷", "毒", "郁闷", "绿帽", "送女", "牛头人", "NTR", "ntr",
        "漏女", "处女", "破身", "非处", "男伴", "精神不洁", "前世雷",
        "亵女", "辱女", "群交", "多人运动", "雌堕", "洗脑",
    )
    return any(marker in text for marker in markers)


def _issue_is_covered_by_reviewer(scan_issue: dict, review_issues: list) -> bool:
    scan_type = _issue_type_key(scan_issue)
    scan_chunk = (scan_issue or {}).get("chunk_index")
    scan_content = _issue_content_key(scan_issue)
    for review_issue in review_issues or []:
        if not isinstance(review_issue, dict):
            continue
        review_type = _issue_type_key(review_issue)
        if scan_type and review_type and scan_type != review_type:
            continue
        review_chunk = review_issue.get("chunk_index")
        if scan_chunk is not None and review_chunk is not None and scan_chunk == review_chunk:
            return True
        review_content = _issue_content_key(review_issue)
        if scan_content and review_content and (
            scan_content in review_content or review_content in scan_content
        ):
            return True
    return False


def _harem_issue_consistency_warnings(detailed_data: dict, reviewer: dict) -> list:
    scan_issues = [
        issue
        for issue in ((detailed_data or {}).get("issues") or [])
        if isinstance(issue, dict) and _is_reviewworthy_scan_issue(issue)
    ]
    if not scan_issues:
        return []

    reviewed_issues = []
    for key in ("lei_points", "yumen_points", "pending_points", "rejected_issues", "rejected_points"):
        reviewed_issues.extend(
            issue for issue in ((reviewer or {}).get(key) or []) if isinstance(issue, dict)
        )

    missing = [
        issue for issue in scan_issues
        if not _issue_is_covered_by_reviewer(issue, reviewed_issues)
    ]
    if not missing:
        return []

    samples = []
    for issue in missing[:8]:
        issue_type = str(issue.get("type") or issue.get("category") or "未分类").strip() or "未分类"
        chunk = issue.get("chunk_index", "?")
        samples.append(f"{issue_type}@chunk {chunk}")
    return [f"扫描阶段发现但二审输出未覆盖的雷点/郁闷点：{', '.join(samples)}"]


def _mermaid_label(text: str, max_len: int = 24) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
    text = text.replace('"', "'").replace("[", "【").replace("]", "】")
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text or "未知"


def _relationship_graph_clean_label(purity_info: dict) -> str:
    if not purity_info:
        return "洁度: 未知"
    if purity_info.get("is_clean") is False:
        return "洁度: 有瑕"
    clean_flags = [
        purity_info.get("is_virgin"),
        purity_info.get("is_spirit_clean"),
        purity_info.get("no_partner"),
    ]
    no_other_contact = purity_info.get("has_other_contact") is False
    if all(flag is True for flag in clean_flags) and no_other_contact:
        return "洁度: 全初"
    return "洁度: 未知"


def _build_harem_relationship_graph_lines(
    male_name: str,
    heroines: list,
    all_female_characters: dict,
    profile_cache: dict,
    purity_map: dict,
) -> list:
    if not heroines:
        return []
    lines = ["", "【关系图谱】", "```mermaid", "graph TD"]
    lines.append(f'    ML["男主: {_mermaid_label(male_name)}"]')

    for index, heroine in enumerate((heroines or [])[:12], 1):
        if not isinstance(heroine, dict):
            continue
        name = str(heroine.get("name") or "").strip()
        if not name:
            continue
        aliases = heroine.get("aliases") or heroine.get("other_names") or []
        evidence = _match_female_evidence(name, aliases, all_female_characters) or {}
        profile = _normalize_profile_for_report(profile_cache.get(name, {}))
        purity_info = purity_map.get(name) or purity_map.get(_heroine_name_key(name)) or {}
        node_id = f"H{index}"
        level = _heroine_position_level(heroine, profile, evidence, purity_info).split("：", 1)[0]
        clean_label = _relationship_graph_clean_label(purity_info)
        contact_level = purity_info.get("contact_level") or "L?"
        relation = str(heroine.get("relationship_type") or profile.get("relationship_with_protagonist") or level or "关系未明").strip()
        edge_label = _mermaid_label(f"{level} / {clean_label} / {contact_level}", 36)
        node_label = _mermaid_label(f"{name}\\n{relation}", 36)
        lines.append(f'    {node_id}["{node_label}"]')
        lines.append(f'    ML -->|"{edge_label}"| {node_id}')
    lines.append("```")
    return lines


def _markdown_cell(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[\r\n\t]+", " ", str(text or "")).strip()
    text = text.replace("|", "\\|")
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text or "-"


def _iter_indexed_text_items(items, default_chunk=None):
    for item in items or []:
        chunk = default_chunk
        text = ""
        if isinstance(item, (list, tuple)) and len(item) == 2:
            chunk = item[0]
            text = item[1]
        else:
            text = item
        text = str(text or "").strip()
        if not text:
            continue
        try:
            chunk_value = int(chunk) if chunk is not None else 999999
        except (TypeError, ValueError):
            chunk_value = 999999
        yield chunk_value, text


def _build_harem_timeline_lines(heroines: list, all_female_characters: dict, purity_map: dict, reviewer: dict) -> list:
    events = []
    for heroine in heroines or []:
        if not isinstance(heroine, dict):
            continue
        name = str(heroine.get("name") or "").strip()
        if not name:
            continue
        aliases = heroine.get("aliases") or heroine.get("other_names") or []
        evidence = _match_female_evidence(name, aliases, all_female_characters) or {}
        for field, label, impact in (
            ("summaries", "女主剧情", "-"),
            ("interactions", "男主互动", "感情推进"),
            ("emotion_signals", "情感信号", "感情推进"),
        ):
            for chunk, text in list(_iter_indexed_text_items(evidence.get(field), None))[:3]:
                events.append({
                    "chunk": chunk,
                    "type": label,
                    "heroine": name,
                    "event": text,
                    "impact": impact,
                })
        purity_info = purity_map.get(name) or purity_map.get(_heroine_name_key(name)) or {}
        if purity_info.get("pushed_by_male_lead") is True:
            events.append({
                "chunk": 999998,
                "type": "推倒/关系确认",
                "heroine": name,
                "event": purity_info.get("pushed_reason") or "二审确认被男主推倒或关系确认。",
                "impact": "确定伴侣关系",
            })
        if purity_info.get("is_leak_heroine") is True:
            events.append({
                "chunk": 999999,
                "type": "漏女风险",
                "heroine": name,
                "event": purity_info.get("leak_reason") or "二审判定存在漏女风险。",
                "impact": "漏女风险",
            })

    for key, label in (("lei_points", "雷点事件"), ("yumen_points", "郁闷点")):
        for issue in (reviewer or {}).get(key) or []:
            if not isinstance(issue, dict):
                continue
            chunk = issue.get("chunk_index")
            try:
                chunk_value = int(chunk) if chunk is not None else 999999
            except (TypeError, ValueError):
                chunk_value = 999999
            events.append({
                "chunk": chunk_value,
                "type": label,
                "heroine": issue.get("type") or "-",
                "event": issue.get("content") or issue.get("reason") or issue.get("review_comment") or "",
                "impact": issue.get("type") or label,
            })

    if not events:
        return []
    events.sort(key=lambda item: (item["chunk"], item["type"], item["heroine"]))
    lines = [
        "",
        "【关键事件时间线】",
        "| 位置 | 事件类型 | 涉及对象 | 事件 | 洁度/关系影响 |",
        "|---|---|---|---|---|",
    ]
    seen = set()
    for event in events:
        key = (event["chunk"], event["type"], event["heroine"], event["event"])
        if key in seen:
            continue
        seen.add(key)
        position = f"chunk {event['chunk'] + 1}" if event["chunk"] < 900000 else "全书汇总"
        lines.append(
            f"| {_markdown_cell(position, 20)} | "
            f"{_markdown_cell(event['type'], 20)} | "
            f"{_markdown_cell(event['heroine'], 24)} | "
            f"{_markdown_cell(event['event'], 90)} | "
            f"{_markdown_cell(event['impact'], 32)} |"
        )
        if len(lines) >= 25:
            break
    return lines


def _heroine_candidate_duplicate_groups(heroines: list, all_female_characters: dict) -> list:
    groups = []
    used = set()
    items = []
    for index, h in enumerate(heroines or []):
        name = str((h or {}).get("name") or "").strip()
        if not name:
            continue
        aliases = [str(x).strip() for x in ((h or {}).get("aliases") or (h or {}).get("other_names") or []) if str(x).strip()]
        evid = _match_female_evidence(name, aliases, all_female_characters) or {}
        evid_aliases = [str(x).strip() for x in (evid.get("other_names") or []) if str(x).strip()]
        keys = {_heroine_name_key(name), name}
        keys.update(_heroine_name_key(x) for x in aliases + evid_aliases)
        keys = {x for x in keys if x and len(x) >= 2}
        items.append({"index": index, "name": name, "keys": keys, "aliases": aliases + evid_aliases, "meta": h, "evidence": evid})

    for item in items:
        if item["index"] in used:
            continue
        group = [item]
        for other in items:
            if other["index"] == item["index"] or other["index"] in used:
                continue
            shared = item["keys"] & other["keys"]
            contains = any(a in b or b in a for a in item["keys"] for b in other["keys"] if len(a) >= 2 and len(b) >= 2)
            if shared or contains:
                group.append(other)
        if len(group) > 1:
            for x in group:
                used.add(x["index"])
            groups.append(group)
    return groups


def _fallback_same_heroine_group(group: list) -> dict:
    names = [item["name"] for item in group]
    keys = [_heroine_name_key(name) for name in names]
    non_empty = [x for x in keys if x]
    unique_keys = set(non_empty)
    has_timeline_variant = any(_has_parallel_timeline_name_marker(name) for name in names)
    same = bool(non_empty and len(unique_keys) == 1)
    if not same and unique_keys:
        longest = max(unique_keys, key=len)
        same = all(
            key == longest or (len(key) >= 2 and key in longest and not _is_generic_heroine_anchor_name(key))
            for key in unique_keys
        )
    if same and has_timeline_variant:
        same = False
    canonical = max(names, key=lambda n: (len(_heroine_name_key(n)), "（" in n or "(" in n, len(n), n)) if names else ""
    if has_timeline_variant:
        reason = "存在世界线/时间线限定词，兜底判定保守不合并"
    else:
        reason = "称谓归一后相同" if len(unique_keys) == 1 else "称谓归一后为核心名包含关系"
    return {"same_person": same, "canonical_name": canonical, "aliases": names, "reason": reason}


def _llm_judge_heroine_duplicate_group(group: list) -> dict:
    fallback = _fallback_same_heroine_group(group)
    if not OpenAI or not API_KEY_POOL:
        return fallback
    payload = []
    for item in group:
        meta = item.get("meta") or {}
        evid = item.get("evidence") or {}
        payload.append({
            "name": item.get("name"),
            "normalized_name": _heroine_name_key(item.get("name")),
            "aliases": list(dict.fromkeys(item.get("aliases") or []))[:20],
            "relationship_type": meta.get("relationship_type", ""),
            "character_traits": meta.get("character_traits", ""),
            "summary": meta.get("summary", ""),
            "identity": _pick_first_str(evid.get("identities") or evid.get("features") or [], ""),
            "relationships": (evid.get("relationships") or [])[:8],
            "summaries": (evid.get("summaries") or [])[:8],
        })
    system_prompt = """你是小说角色同一性判断助手。请判断给出的女主条目是否指向同一个真实角色。

判断标准：
1. 称谓前后缀、括号身份、尊号变化通常可能是同一人，例如“角色名（身份）”与“身份角色名”。
2. 带有“另一个世界/平行世界/未来线/前世/重生前”等世界线或时间线限定词的同名角色，不能只因核心名相同就合并，除非证据明确说明是同一真实角色。
3. 但亲属关系、身份、姓名核心不同、同时存在为不同角色时，不要合并。
4. 宁可保守，不确定就 same_person=false。

只输出 JSON 对象，不要 Markdown。"""
    user_prompt = json.dumps({
        "candidates": payload,
        "output_schema": {
            "same_person": "boolean",
            "canonical_name": "用于报告展示的规范名",
            "aliases": ["应合并的名字"],
            "reason": "简短理由"
        }
    }, ensure_ascii=False, indent=2)
    try:
        data = _call_json_chat_completion(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
            max_tokens=900,
        )
        if not isinstance(data, dict):
            return fallback
        return {
            "same_person": bool(data.get("same_person")),
            "canonical_name": str(data.get("canonical_name") or fallback["canonical_name"]).strip(),
            "aliases": [str(x).strip() for x in data.get("aliases", []) if str(x).strip()] or fallback["aliases"],
            "reason": str(data.get("reason") or "").strip(),
        }
    except Exception as exc:
        log_report(f"女主重复合并 LLM 判断失败，使用保守兜底: {exc}")
        return fallback


def dedupe_heroines_for_report(heroines: list, all_female_characters: dict) -> list:
    if not heroines:
        return []
    deduped = list(heroines)
    remove_names = set()
    rename = {}
    for group in _heroine_candidate_duplicate_groups(deduped, all_female_characters):
        decision = _llm_judge_heroine_duplicate_group(group)
        if not decision.get("same_person"):
            continue
        group_names = [item["name"] for item in group]
        canonical = decision.get("canonical_name") or group_names[0]
        keep = None
        for item in group:
            if item["name"] == canonical:
                keep = item["name"]
                break
        if keep is None:
            keep = max(group_names, key=lambda n: (len(_heroine_name_key(n)), "（" in n or "(" in n, len(n), n))
            rename[keep] = canonical
        for name in group_names:
            if name != keep:
                remove_names.add(name)
    out = []
    seen = set()
    for h in deduped:
        name = str((h or {}).get("name") or "").strip()
        if not name or name in remove_names:
            continue
        item = dict(h)
        if name in rename:
            item["name"] = rename[name]
        final_name = item.get("name")
        if final_name in seen:
            continue
        seen.add(final_name)
        out.append(item)
    return out


def _normalize_profile_for_report(profile_for_report: dict) -> dict:
    profile_for_report = profile_for_report or {}
    if not isinstance(profile_for_report, dict):
        return {}
    feature_candidates = []
    primary_features = (str(profile_for_report.get("features") or "")).strip()
    if primary_features:
        feature_candidates.append(primary_features)
    else:
        for key in ("appearance", "personality", "traits"):
            value = (str(profile_for_report.get(key) or "")).strip()
            if value and value not in feature_candidates:
                feature_candidates.append(value)
    return {
        "identity": (str(profile_for_report.get("identity") or "")).strip(),
        "features": "；".join(feature_candidates),
        "relationship_with_protagonist": (
            str(
                profile_for_report.get("relationship_with_protagonist")
                or profile_for_report.get("relationship")
                or ""
            )
        ).strip(),
        "key_events": (str(profile_for_report.get("key_events") or "")).strip(),
    }


def summarize_heroine_profile_llm(
    name: str,
    heroine_meta: dict,
    heroine_evidence: dict,
    model: str = None,
) -> dict:
    """
    输入：
    - heroine_meta：*_detailed_*.json 的 heroine_result.heroines[] 单条（关系/性格/概要/别名）
    - heroine_evidence：*_detailed_*.json 的 all_female_characters[name]（features/summaries/interactions 等证据）
    输出：{relationship, appearance, traits}
    """
    heroine_meta = heroine_meta or {}
    heroine_evidence = heroine_evidence or {}

    rel_type = heroine_meta.get("relationship_type") or ""
    traits = heroine_meta.get("character_traits") or ""
    summary = heroine_meta.get("summary") or ""
    aliases = heroine_meta.get("aliases") or heroine_meta.get("other_names") or []

    relationships = heroine_evidence.get("relationships") or []
    features = heroine_evidence.get("features") or []
    interactions = heroine_evidence.get("interactions") or []
    emotions = heroine_evidence.get("emotion_signals") or []
    summaries = heroine_evidence.get("summaries") or []

    # 无法调用大模型时：尽量回退
    if not OpenAI or not API_KEY:
        return {
            "relationship": rel_type or _pick_first_str(relationships, "未描述") or "未描述",
            "appearance": "；".join([f for f in features[:3] if f]) or "未描述",
            "traits": traits or (summary.split("。")[0] if summary else "未描述"),
        }

    system_prompt = (
        "你是小说女主信息总结助手。请基于提供的线索，总结：与男主关系、外貌描写、特点（身份/性格/标签）。"
        "要求：不杜撅，不加入不存在的事实；外貌只写外貌；特点不超过40字；关系尽量短。只输出 JSON。"
    )
    user_payload = {
        "name": name,
        "aliases": aliases[:30],
        "relationship_type": rel_type,
        "relationships": relationships[:30],
        "character_traits_raw": traits,
        "summary_raw": summary,
        "features_raw": features[:80],
        "key_interactions_raw": interactions[:120],
        "emotion_signals_raw": emotions[:60],
        "summaries_raw": summaries[:120],
    }
    try:
        data = _call_json_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
            ],
            temperature=0.2,
            max_tokens=600,
            model=model or MODEL,
        )
        return {
            "relationship": (data.get("relationship") or rel_type or "未描述").strip(),
            "appearance": (data.get("appearance") or "未描述").strip(),
            "traits": (data.get("traits") or traits or "未描述").strip(),
        }
    except Exception:
        return {
            "relationship": rel_type or _pick_first_str(relationships, "未描述") or "未描述",
            "appearance": "；".join([f for f in features[:3] if f]) or "未描述",
            "traits": traits or (summary.split("。")[0] if summary else "未描述"),
        }


def _summarize_harem_romance_overview(detailed_data: dict, reviewer: dict, heroines: list, male_obj: dict) -> dict:
    all_female_characters = (detailed_data or {}).get("all_female_characters") or {}
    heroine_material = []
    intimacy_words = ("亲吻", "拥抱", "牵手", "同床", "双修", "成亲", "表白", "吃醋", "暧昧", "喜欢", "爱慕", "私通", "推倒")
    romance_gap_issue_type_words = ("预期落差", "进度条诈骗", "恋爱推进停滞")
    presence_low = 0
    intimacy_hits = 0
    explicit_romance_gap_hits = 0
    for h in heroines or []:
        name = str((h or {}).get("name") or "").strip()
        if not name:
            continue
        aliases = h.get("aliases") or h.get("other_names") or []
        evid = _match_female_evidence(name, aliases, all_female_characters) or {}
        blob = "；".join(
            str(x)
            for x in [
                h.get("relationship_type", ""),
                h.get("summary", ""),
                h.get("character_traits", ""),
                *(evid.get("relationships") or [])[:8],
                *(evid.get("interactions") or [])[:8],
                *(evid.get("emotion_signals") or [])[:8],
                *(evid.get("summaries") or [])[:8],
            ]
            if x
        )
        if _contains_positive_signal_text(blob, intimacy_words):
            intimacy_hits += 1
        if _has_romance_gap_signal_text(blob):
            explicit_romance_gap_hits += 1
        count = int(evid.get("count") or h.get("count") or 0)
        explicit_high_presence = _contains_any_text(blob, ["存在感高", "存在感较高", "存在感很高", "存在感强", "存在感突出"])
        if _has_low_presence_or_tooling_signal(blob) or (count <= 2 and len(blob) < 120 and not explicit_high_presence):
            presence_low += 1
        heroine_material.append({"name": name, "count": count, "material": blob[:900]})

    issue_blob = "；".join(
        str((item or {}).get(field) or "")
        for item in [
            *((reviewer or {}).get("lei_points") or []),
            *((reviewer or {}).get("yumen_points") or []),
        ]
        if isinstance(item, dict)
        for field in ("type", "content", "review_comment", "reason")
    )
    has_romance_gap_issue = _has_romance_gap_signal_text(issue_blob) or any(word in issue_blob for word in romance_gap_issue_type_words)
    has_tooling_issue = _has_low_presence_or_tooling_signal(issue_blob)

    male_blob = "；".join(
        str(x)
        for x in [
            (male_obj or {}).get("identity", ""),
            *((male_obj or {}).get("summaries") or [])[:80],
            *((male_obj or {}).get("relationships") or [])[:40],
        ]
        if x
    )
    heroine_count = len(heroines or [])
    if heroine_count and intimacy_hits == 0:
        romance_density = "极低：识别到女角色但未见明确恋爱/暧昧/亲密推进材料。"
        romance_progression = "未见明确恋爱推进，疑似长期停留在工具、案件、战斗或背景功能。"
        expectation_gap = "若作品标题、标签或读者期待包含后宫/恋爱，本报告材料显示存在明显感情戏缺失风险。"
    elif explicit_romance_gap_hits:
        romance_density = "偏低：材料虽出现亲密/关系事件，但同时明确提示感情描写缺失。"
        romance_progression = "存在行为或关系节点，但缺少可确认的恋爱/情绪推进。"
        expectation_gap = "若作品标题、标签或读者期待包含后宫/恋爱，本报告材料显示存在感情戏兑现不足风险。"
    elif has_romance_gap_issue:
        romance_density = "偏低：二审已命中感情戏缺失/预期落差类郁闷点。"
        romance_progression = "存在感情线推进不足风险，需结合郁闷点条目复核。"
        expectation_gap = "已出现感情戏缺失或预期落差线索。"
    elif heroine_count > 1 and intimacy_hits <= max(1, heroine_count // 4):
        romance_density = "偏低"
        romance_progression = "存在少量亲密/暧昧推进，但覆盖女角色比例偏低。"
        expectation_gap = "若读者期待高密度恋爱互动，需要关注实际感情戏占比。"
    else:
        romance_density = "中等或以上"
        romance_progression = "存在部分亲密/暧昧推进，需结合具体女主条目查看。"
        expectation_gap = "未见明显感情密度预期落差，但仍需结合具体女主条目查看。"

    tooling_threshold = max(2, heroine_count // 2)
    if has_tooling_issue:
        tooling_risk = "已命中工具人女主/女角色工具化线索，需重点复核角色塑造。"
    elif heroine_count and presence_low >= tooling_threshold:
        tooling_risk = "女角色可能偏工具人"
    else:
        tooling_risk = "未见明显大面积工具人风险"

    fallback = {
        "romance_density": romance_density,
        "female_presence": f"共识别 {heroine_count} 位女主/准女主，低存在感条目约 {presence_low} 位。",
        "romance_progression": romance_progression,
        "female_tooling_risk": tooling_risk,
        "romance_expectation_gap": expectation_gap,
        "male_past_romance_risk": "未见明确男主前史情感雷点。",
    }
    if _has_male_past_romance_risk(male_blob):
        fallback["male_past_romance_risk"] = "男主材料中出现前妻/前女友/前世婚恋等前史线索，需人工关注是否构成情感背景雷点。"

    if not OpenAI or not API_KEY_POOL:
        return fallback

    system_prompt = """你是男性向后宫小说感情线审稿助手。请基于材料评估：
1. 感情戏密度是否充足；
2. 女角色是否有存在感和塑造；
3. 恋爱/暧昧/亲密关系是否有推进；
4. 女角色是否偏工具人；
5. 标题、标签、女角色数量与实际感情戏是否有预期落差；
6. 男主前史情感雷点，如前妻、前女友、前世婚姻、丧偶、被卷钱跑路等。

只根据材料输出，不要编造。只输出 JSON 对象。"""
    user_prompt = json.dumps({
        "male_material": male_blob[:4000],
        "heroine_count": len(heroines or []),
        "heroine_material": heroine_material[:40],
        "reviewer_heroine_purity_names": [x.get("name") for x in (reviewer or {}).get("heroines_purity", [])[:60]],
        "output_schema": fallback,
    }, ensure_ascii=False, indent=2)
    try:
        data = _call_json_chat_completion(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=1400,
        )
        if not isinstance(data, dict):
            return fallback
        return {
            key: str(data.get(key) or fallback[key]).strip()
            for key in fallback
        }
    except Exception as exc:
        log_report(f"后宫感情概览生成失败，使用保守兜底: {exc}")
        return fallback


def _has_male_past_romance_risk(text: str) -> bool:
    text = str(text or "")
    if not text:
        return False
    text = _male_past_romance_effective_risk_text(text)
    if not text:
        return False
    if _male_past_romance_text_has_only_non_partner_homonyms(text):
        return False
    if _male_past_romance_text_has_only_predecessor_partner(text):
        return False
    explicit_past_partner_words = (
        "前妻", "前女友", "前任女友", "前任妻子", "前任恋人", "前任爱人", "前夫", "前未婚妻", "前世老婆", "前世妻子", "前世爱人",
        "上一世老婆", "上一世妻子", "原配", "亡妻",
    )
    if any(word in text for word in explicit_past_partner_words):
        return True
    if _has_male_romantic_ex_partner_context(text):
        return True

    past_context_words = ("前世", "上一世", "前史", "过往", "过去", "穿越前", "重生前", "原世界", "原故事线")
    partner_words = ("老婆", "妻子", "女友", "爱人", "未婚妻", "恋人", "婚姻", "结婚", "离婚", "丧偶")
    negative_words = ("卷走", "卷光", "跑路", "背叛", "抛弃", "离婚", "分手", "绝症", "丧偶", "去世", "死亡")
    return (
        any(word in text for word in past_context_words)
        and any(word in text for word in partner_words)
    ) or (
        any(word in text for word in partner_words)
        and any(word in text for word in negative_words)
    )


def _male_past_romance_text_has_only_non_partner_homonyms(text: str) -> bool:
    non_partner_words = _male_past_romance_non_partner_homonyms(text)
    protected = text
    for word in non_partner_words:
        protected = protected.replace(word, "")
    risk_markers = (
        "前妻", "前女友", "前任女友", "前任妻子", "前任恋人", "前任爱人", "前夫", "前未婚妻",
        "前世老婆", "前世妻子", "前世爱人", "上一世老婆", "上一世妻子", "原配", "亡妻",
        "老婆", "妻子", "女友", "爱人", "未婚妻", "恋人", "婚姻", "结婚", "离婚", "丧偶",
    )
    return any(word in text for word in non_partner_words) and not any(marker in protected for marker in risk_markers)


def _male_past_romance_text_has_only_predecessor_partner(text: str) -> bool:
    matches = _male_past_romance_predecessor_partner_mentions(text)
    if not matches:
        return False
    protected = text
    for word in matches:
        protected = protected.replace(word, "")
    risk_markers = (
        "前妻", "前女友", "前任女友", "前任妻子", "前任恋人", "前任爱人", "前夫", "前未婚妻",
        "前世老婆", "前世妻子", "前世爱人", "上一世老婆", "上一世妻子", "原配", "亡妻",
        "老婆", "妻子", "女友", "爱人", "未婚妻", "恋人", "婚姻", "结婚", "离婚", "丧偶",
    )
    return not any(marker in protected for marker in risk_markers)


def _male_past_romance_predecessor_partner_mentions(text: str) -> list[str]:
    predecessor_roles = (
        "勇者", "宿主", "主人", "掌门", "门主", "宗主", "家主", "族长", "皇帝", "国王",
        "队长", "团长", "会长", "老板", "上司", "领导", "主管", "城主", "校长", "导师",
        "师父", "师傅", "上任", "前代", "前朝",
    )
    partner_words = ("老婆", "妻子", "女友", "爱人", "未婚妻", "恋人", "夫人", "丈夫")
    role_pattern = "|".join(re.escape(word) for word in predecessor_roles)
    partner_pattern = "|".join(re.escape(word) for word in partner_words)
    pattern = rf"前任(?:{role_pattern})(?:的)?(?:{partner_pattern})|(?:上任|前代|前朝)(?:{role_pattern})?(?:的)?(?:{partner_pattern})"
    return re.findall(pattern, str(text or ""))


def _male_past_romance_non_partner_homonyms(text: str) -> list[str]:
    relation_roots = ("前妻", "前夫")
    non_partner_suffixes = ("子", "人", "弟", "兄", "姐", "妹", "侄", "甥", "父", "母", "哥", "嫂", "叔", "婶", "舅", "姨")
    matches: list[str] = []
    for root in relation_roots:
        pattern = rf"{re.escape(root)}(?:{'|'.join(re.escape(s) for s in non_partner_suffixes)})+"
        matches.extend(re.findall(pattern, text))
    return matches


def _has_male_romantic_ex_partner_context(text: str) -> bool:
    if "前任" not in text:
        return False
    non_romantic_followers = (
        "掌门", "门主", "宗主", "家主", "族长", "皇帝", "国王", "队长", "团长", "会长",
        "老板", "上司", "领导", "主管", "城主", "校长", "导师", "师父", "师傅",
        "职位", "职务", "留下", "继承",
    )
    if any(f"前任{word}" in text for word in non_romantic_followers):
        return False
    romantic_context_words = (
        "女友", "女朋友", "妻子", "老婆", "未婚妻", "恋人", "爱人", "情人",
        "恋爱", "感情", "分手", "复合", "结婚", "离婚", "婚姻",
    )
    return any(word in text for word in romantic_context_words)


def _male_past_romance_effective_risk_text(text: str) -> str:
    chunks = re.split(r"[\n，,。；;！？!?]+", str(text or ""))
    effective: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if _male_past_romance_blob_is_nonfactual_or_negated(chunk):
            continue
        effective.append(chunk)
    return "\n".join(effective)


def _male_past_romance_blob_is_nonfactual_or_negated(text: str) -> bool:
    nonfactual_words = (
        "传言", "传闻", "据说", "听说", "流言", "谣言", "误传", "谣传", "猜测", "疑似",
        "梦见", "梦到", "梦境", "误认为", "被误认", "误认成", "误会成",
    )
    resolved_words = (
        "证实是误会", "证实不成立", "后来证实", "澄清", "不属实", "并非事实", "不是事实",
        "并非真的", "不是真的", "假的", "假消息",
    )
    negated_patterns = (
        "没有恋爱经历", "没有感情经历", "没有前女友", "没有前妻", "没有前任",
        "没有老婆", "没有妻子", "没有女友", "没有爱人", "没有恋人",
        "没谈过恋爱", "从未恋爱", "未恋爱", "无恋爱经历", "无感情经历",
        "没有结婚", "未结婚", "未婚", "没有婚史", "无婚史", "无感情",
    )
    nickname_or_joke_patterns = (
        "只是称呼", "只是个称呼", "只是外号", "只是绰号", "只是玩笑", "开玩笑",
        "玩笑称呼", "调侃称呼", "口头称呼",
    )
    roleplay_or_setting_patterns = (
        "剧本里", "剧本设定", "副本设定", "游戏设定", "系统设定", "角色设定",
        "身份设定", "背景设定", "人设", "扮演", "假扮", "伪装成", "模拟",
        "梦境副本", "幻境副本",
    )
    nominal_relation_patterns = (
        "只是政治婚约", "仅是政治婚约", "只是名义婚约", "仅是名义婚约",
        "有名无实", "名义夫妻", "名义婚约",
    )
    strong_factual_anchors = ("确实", "明确", "实锤", "证据显示", "事实是", "实际发生", "真的发生", "已确认", "确认了")
    if any(word in text for word in resolved_words):
        return True
    if any(pattern in text for pattern in negated_patterns):
        return True
    if any(pattern in text for pattern in nickname_or_joke_patterns):
        return True
    if any(pattern in text for pattern in roleplay_or_setting_patterns):
        return True
    if any(pattern in text for pattern in nominal_relation_patterns):
        return True
    if any(word in text for word in nonfactual_words):
        return not any(anchor in text for anchor in strong_factual_anchors)
    return False


def build_report_v2(book_key: str, detailed_data: dict, reviewer: dict) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [f"书名：{book_key or '未识别'}", f"报告生成时间：{ts}", "=" * 60]

    # ---------------- 男主 ----------------
    male_obj = (detailed_data or {}).get("male_protagonist") or {}
    male_name = male_obj.get("name") or (reviewer or {}).get("male_lead") or "未识别"
    male_summary = summarize_male_profile_llm(male_obj)
    male_lines = [
        "【男主】",
        f"姓名：{male_name}",
        f"身份：{male_summary.get('identity', '未描述')}",
        f"性格：{male_summary.get('personality', '未描述')}",
        f"经历：{male_summary.get('experience', '未描述')}",
    ]
    # 按你的要求：男主不输出别称（不读取 other_names）

    # ---------------- 女主 ----------------
    purity_map = build_purity_map(reviewer)
    heroine_result = (detailed_data or {}).get("heroine_result") or {}
    heroines = heroine_result.get("heroines") or []
    all_female_characters = (detailed_data or {}).get("all_female_characters") or {}
    # 按 importance_rank 排序（没有就放最后）
    def _rank_key(h):
        try:
            return int(h.get("importance_rank", 9999))
        except Exception:
            return 9999
    heroines = sorted(heroines, key=_rank_key)
    heroines = dedupe_heroines_for_report(heroines, all_female_characters)

    heroine_lines = ["", "【女主】"]

    # 并发总结（避免女主多时太慢）
    profile_cache = {}
    if heroines:
        def _work(h):
            n = h.get("name") or ""
            if not n:
                return (
                    "",
                    {
                        "identity": "未描述",
                        "features": "未描述",
                        "relationship_with_protagonist": "未描述",
                        "key_events": "未描述",
                    },
                )
            aliases = h.get("aliases") or h.get("other_names") or []
            evid = _match_female_evidence(n, aliases, all_female_characters) or {}
            profile_for_report = _normalize_profile_for_report(evid.get("profile_for_report"))
            if any(profile_for_report.values()):
                return (n, profile_for_report)
            return (n, summarize_heroine_profile_llm(n, h, evid))

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for n, prof in tqdm(ex.map(_work, heroines), total=len(heroines), desc="女主总结"):
                if n:
                    profile_cache[n] = prof

    for h in heroines:
        name = h.get("name") or "未知"
        prof = _normalize_profile_for_report(profile_cache.get(name, {}))
        aliases = h.get("aliases") or h.get("other_names") or []
        evid = _match_female_evidence(name, aliases, all_female_characters) or {}
        p = purity_map.get(name) or purity_map.get(_heroine_name_key(name))
        # 初摸：无非男主接触 => ✅
        virgin = _bool_mark(p.get("is_virgin")) if p else "❓"
        spirit = _bool_mark(p.get("is_spirit_clean")) if p else "❓"
        first_marriage = _bool_mark(p.get("no_partner")) if p else "❓"
        first_touch = _bool_mark((not p.get("has_other_contact")) if p and p.get("has_other_contact") is not None else None) if p else "\u2753"
        purity_summary = (p.get("summary") or "").strip() if p else ""
        pushed = _bool_mark(p.get("pushed_by_male_lead")) if p else "❓"
        pushed_reason = (p.get("pushed_reason") or "").strip() if p else ""
        leak = _bool_mark(p.get("is_leak_heroine")) if p else "❓"
        leak_reason = (p.get("leak_reason") or "").strip() if p else ""
        virgin_conflict = bool(p.get("virgin_judgement_conflict", False)) if p else False
        llm_virgin_status = (p.get("llm_virgin_status") or p.get("virgin_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        llm_virgin_reason = (p.get("llm_virgin_reason") or "").strip() if p else ""
        rule_virgin_status = (p.get("rule_virgin_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        rule_virgin_reason = (p.get("rule_virgin_reason") or "").strip() if p else ""
        contact_conflict = bool(p.get("contact_judgement_conflict", False)) if p else False
        llm_contact_status = (p.get("llm_contact_status") or p.get("contact_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        llm_contact_reason = (p.get("llm_contact_reason") or "").strip() if p else ""
        rule_contact_status = (p.get("rule_contact_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        rule_contact_reason = (p.get("rule_contact_reason") or "").strip() if p else ""
        partner_conflict = bool(p.get("partner_judgement_conflict", False)) if p else False
        llm_partner_status = (p.get("llm_partner_status") or p.get("partner_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        llm_partner_reason = (p.get("llm_partner_reason") or "").strip() if p else ""
        rule_partner_status = (p.get("rule_partner_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        rule_partner_reason = (p.get("rule_partner_reason") or "").strip() if p else ""
        spirit_conflict = bool(p.get("spirit_judgement_conflict", False)) if p else False
        llm_spirit_status = (p.get("llm_spirit_status") or p.get("spirit_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        llm_spirit_reason = (p.get("llm_spirit_reason") or "").strip() if p else ""
        rule_spirit_status = (p.get("rule_spirit_status") or "\u672a\u77e5") if p else "\u672a\u77e5"
        rule_spirit_reason = (p.get("rule_spirit_reason") or "").strip() if p else ""
        partner_exempted = bool(p.get("partner_exempted_for_clean", False)) if p else False
        partner_exemption_reason = (p.get("partner_exemption_reason") or "").strip() if p else ""
        past_life_severity = (p.get("past_life_severity") or "none") if p else "none"
        past_life_severity_label = (p.get("past_life_severity_label") or "") if p else ""
        past_life_status = (p.get("past_life_status") or "未见前世/原故事线洁度线索") if p else "未见前世/原故事线洁度线索"
        past_life_reason = (p.get("past_life_reason") or "").strip() if p else ""
        contact_level = (p.get("contact_level") or "L0") if p else "L0"
        contact_level_label = (p.get("contact_level_label") or "无非男主接触事实") if p else "无非男主接触事实"
        contact_level_reason = (p.get("contact_level_reason") or "").strip() if p else ""
        missing_desc = "\u672a\u63cf\u8ff0"
        empty_text = "\uff08\u65e0\uff09"

        block = [
            "",
            f"{name}:",
            f"\u5904\u5973\uff1a{virgin}",
            f"\u7cbe\u795e\u521d\uff1a{spirit}",
            f"\u521d\u5a5a\uff1a{first_marriage}",
            f"\u521d\u6478\uff1a{first_touch}",
            f"partner豁免：{_bool_mark(partner_exempted)}",
            f"partner豁免说明：{partner_exemption_reason or empty_text}",
            f"接触等级：{contact_level}（{contact_level_label}）",
            f"接触等级说明：{contact_level_reason or empty_text}",
            f"前世洁度：{past_life_status}",
            f"前世风险等级：{past_life_severity}（{past_life_severity_label or empty_text}）",
            f"前世洁度说明：{past_life_reason or empty_text}",
            f"\u8eab\u4efd\uff1a{prof.get('identity') or missing_desc}",
            f"\u4e0e\u7537\u4e3b\u5173\u7cfb\uff1a{prof.get('relationship_with_protagonist') or missing_desc}",
            f"\u7279\u70b9\uff1a{prof.get('features') or missing_desc}",
            f"\u5173\u952e\u4e8b\u4ef6\uff1a{prof.get('key_events') or missing_desc}",
            f"关系结构标签：{_summarize_heroine_relationship_structure(prof, evid)}",
            f"女主定位分级：{_heroine_position_level(h, prof, evid, p)}",
            f"女主有效性：{_summarize_heroine_effectiveness(h, prof, evid)}",
            f"\u56db\u7ef4\u7eaf\u6d01\u5ea6summary\uff1a{purity_summary or empty_text}",
            f"是否被推倒：{pushed}",
            f"推倒说明：{pushed_reason or empty_text}",
            f"漏女三层判定：{_summarize_leak_three_layers(p or {}, prof)}",
            f"是否漏女：{leak}",
            f"漏女说明：{leak_reason or empty_text}",
        ]

        if virgin_conflict:
            block.extend([
                "\u5904\u5973\u5224\u5b9a\u51b2\u7a81\uff1a\u26a0\ufe0f \u89c4\u5219\u4e0e\u5927\u6a21\u578b\u4e0d\u4e00\u81f4",
                f"  \u5927\u6a21\u578b\u5224\u65ad\uff1a{llm_virgin_status}",
                f"  \u5927\u6a21\u578b\u7406\u7531\uff1a{llm_virgin_reason or empty_text}",
                f"  \u89c4\u5219\u5224\u65ad\uff1a{rule_virgin_status}",
                f"  \u89c4\u5219\u7406\u7531\uff1a{rule_virgin_reason or empty_text}",
            ])
        if contact_conflict:
            block.extend([
                "\u63a5\u89e6\u5224\u5b9a\u51b2\u7a81\uff1a\u26a0\ufe0f \u89c4\u5219\u4e0e\u5927\u6a21\u578b\u4e0d\u4e00\u81f4",
                f"  \u5927\u6a21\u578b\u5224\u65ad\uff1a{llm_contact_status}",
                f"  \u5927\u6a21\u578b\u7406\u7531\uff1a{llm_contact_reason or empty_text}",
                f"  \u89c4\u5219\u5224\u65ad\uff1a{rule_contact_status}",
                f"  \u89c4\u5219\u7406\u7531\uff1a{rule_contact_reason or empty_text}",
            ])
        if partner_conflict:
            block.extend([
                "\u7537\u4f34\u5224\u5b9a\u51b2\u7a81\uff1a\u26a0\ufe0f \u89c4\u5219\u4e0e\u5927\u6a21\u578b\u4e0d\u4e00\u81f4",
                f"  \u5927\u6a21\u578b\u5224\u65ad\uff1a{llm_partner_status}",
                f"  \u5927\u6a21\u578b\u7406\u7531\uff1a{llm_partner_reason or empty_text}",
                f"  \u89c4\u5219\u5224\u65ad\uff1a{rule_partner_status}",
                f"  \u89c4\u5219\u7406\u7531\uff1a{rule_partner_reason or empty_text}",
            ])
        if spirit_conflict:
            block.extend([
                "\u7cbe\u795e\u521d\u5224\u5b9a\u51b2\u7a81\uff1a\u26a0\ufe0f \u89c4\u5219\u4e0e\u5927\u6a21\u578b\u4e0d\u4e00\u81f4",
                f"  \u5927\u6a21\u578b\u5224\u65ad\uff1a{llm_spirit_status}",
                f"  \u5927\u6a21\u578b\u7406\u7531\uff1a{llm_spirit_reason or empty_text}",
                f"  \u89c4\u5219\u5224\u65ad\uff1a{rule_spirit_status}",
                f"  \u89c4\u5219\u7406\u7531\uff1a{rule_spirit_reason or empty_text}",
            ])

        heroine_lines.extend(block)

    romance_overview = _summarize_harem_romance_overview(detailed_data, reviewer, heroines, male_obj)
    romance_lines = [
        "",
        "【感情线与女角色有效性】",
        f"感情戏密度：{romance_overview.get('romance_density') or '未描述'}",
        f"女角色存在感：{romance_overview.get('female_presence') or '未描述'}",
        f"恋爱推进：{romance_overview.get('romance_progression') or '未描述'}",
        f"工具人风险：{romance_overview.get('female_tooling_risk') or '未描述'}",
        f"预期落差：{romance_overview.get('romance_expectation_gap') or '未描述'}",
        f"男主前史情感雷点：{romance_overview.get('male_past_romance_risk') or '未描述'}",
    ]
    consistency_warnings = [
        *_harem_consistency_warnings(heroines, reviewer, all_female_characters),
        *_harem_issue_consistency_warnings(detailed_data, reviewer),
    ]
    if consistency_warnings:
        romance_lines.extend(["", "【交叉验证提示】"])
        romance_lines.extend(f"- {item}" for item in consistency_warnings)
    relationship_graph_lines = _build_harem_relationship_graph_lines(
        male_name,
        heroines,
        all_female_characters,
        profile_cache,
        purity_map,
    )
    timeline_lines = _build_harem_timeline_lines(heroines, all_female_characters, purity_map, reviewer)

    # ---------------- 毒点/雷点（原样输出，不润色） ----------------
    heroine_contexts = _build_heroine_position_contexts(heroines, all_female_characters, profile_cache, purity_map)
    risk_lines = ["", "【毒点】"]
    yumen = (reviewer or {}).get("yumen_points") or []
    if yumen:
        _append_issue_lines(risk_lines, yumen, heroine_contexts)
    else:
        risk_lines.append("（无）")

    risk_lines.extend(["", "【雷点】"])
    lei = (reviewer or {}).get("lei_points") or []
    if lei:
        _append_issue_lines(risk_lines, lei, heroine_contexts)
    else:
        risk_lines.append("（无）")

    return "\n".join([*header, "", *male_lines, *heroine_lines, *romance_lines, *relationship_graph_lines, *timeline_lines, *risk_lines])


def _clean_text_items(items, limit=5, max_len=120):
    out = []
    for item in items or []:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > max_len:
            text = text[:max_len].rstrip() + "..."
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _general_character_rows(detailed_data: dict, limit=20):
    chars = []
    for info in (detailed_data or {}).get("characters") or []:
        if not isinstance(info, dict):
            continue
        name = info.get("name")
        if not name or info.get("role_type") == "protagonist":
            continue
        chars.append(
            {
                "name": name,
                "aliases": info.get("aliases") or [],
                "importance": float(info.get("importance") or 0),
                "count": int(info.get("count") or 0),
                "role_type": info.get("role_type") or "supporting",
                "identity": info.get("identity") or "未描述",
                "factions": info.get("factions") or ([info.get("faction")] if info.get("faction") else []),
                "summaries": _clean_text_items(info.get("key_events") or [], limit=3),
                "relationships": _clean_text_items(info.get("relationships") or [], limit=3),
                "features": _clean_text_items(info.get("features") or [], limit=3),
            }
        )
    if chars:
        chars.sort(key=lambda x: (x["importance"], x["count"]), reverse=True)
        return chars[:limit]

    all_female_characters = (detailed_data or {}).get("all_female_characters") or {}
    for name, info in all_female_characters.items():
        if not name or not isinstance(info, dict):
            continue
        chars.append(
            {
                "name": name,
                "aliases": info.get("other_names") or [],
                "importance": float(info.get("avg_score") or 0),
                "count": int(info.get("count") or 0),
                "role_type": "supporting",
                "identity": _pick_first_str(info.get("relationships") or info.get("features") or [], "未描述"),
                "factions": info.get("factions") or [],
                "summaries": _clean_text_items(info.get("summaries") or [], limit=3),
                "relationships": _clean_text_items(info.get("relationships") or [], limit=3),
                "features": _clean_text_items(info.get("features") or [], limit=3),
            }
        )
    chars.sort(key=lambda x: (x["importance"], x["count"]), reverse=True)
    return chars[:limit]


def _clamp_score(value, default: float = 6.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = default
    return round(max(0.0, min(10.0, score)), 1)


WRITING_QUALITY_DIMENSION_LABELS = {
    "prose_quality": "文笔质量",
    "character_depth": "人物塑造",
    "narrative_technique": "叙事技巧",
    "dialogue_quality": "对话质量",
    "scene_description": "场景描写",
    "emotional_impact": "情感渲染",
    "info_density": "信息密度",
    "worldbuilding_integration": "世界观融入",
}


GENERAL_CRAFT_SUMMARY_FIELDS = {
    "writing_quality_overall",
    "pacing_analysis_overall",
    "information_density_audit",
    "water_chapter_analysis",
}


GENERAL_NARRATIVE_ARCHITECTURE_FIELDS = {
    "narrative_structure_analysis",
    "outline_architecture_overall",
}


GENERAL_FORESHADOWING_ENGINEERING_FIELDS = {
    "foreshadowing_engineering_analysis",
}


GENERAL_SEMANTIC_LAYER_FIELDS = {
    "semantic_layers_analysis",
}


GENERAL_READER_EXPERIENCE_FIELDS = {
    "reader_experience_analysis",
}


def _append_text_block(lines: list, title: str, value, *, limit: int = 8):
    lines.extend(["", f"【{title}】"])
    if isinstance(value, list):
        items = _clean_text_items(value, limit=limit, max_len=180)
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("未描述")
    elif isinstance(value, dict):
        added = False
        for key, item in value.items():
            label = summary_field_label(str(key))
            if isinstance(item, list):
                items = _clean_text_items(item, limit=limit, max_len=160)
                if items:
                    lines.append(f"- {label}：{'；'.join(items)}")
                    added = True
            elif isinstance(item, dict):
                nested = []
                for sub_key, sub_value in item.items():
                    if isinstance(sub_value, (list, dict)):
                        continue
                    text = str(sub_value or "").strip()
                    if text:
                        nested.append(f"{summary_field_label(str(sub_key))}={text}")
                if nested:
                    lines.append(f"- {label}：{'；'.join(nested[:4])}")
                    added = True
            else:
                text = str(item or "").strip()
                if text:
                    lines.append(f"- {label}：{text[:220]}")
                    added = True
        if not added:
            lines.append("未描述")
    else:
        text = str(value or "").strip()
        lines.append(text if text else "未描述")


def _append_general_craft_sections(lines: list, general_summary: dict):
    summary = (general_summary or {}).get("summary") or {}
    writing = summary.get("writing_quality_overall")
    pacing = summary.get("pacing_analysis_overall")
    density = summary.get("information_density_audit")
    water = summary_field_values(summary, "water_chapter_analysis")
    has_any = any([
        isinstance(writing, dict) and writing,
        isinstance(pacing, dict) and pacing,
        isinstance(density, dict) and density,
        bool(water),
    ])
    if not has_any:
        return

    lines.extend(["", "【写作质量分析】"])
    if isinstance(writing, dict) and writing:
        overall_score = writing.get("overall_score") or writing.get("score")
        grade = str(writing.get("grade") or "").strip()
        assessment = str(writing.get("assessment") or writing.get("overall_assessment") or "").strip()
        if overall_score is not None or grade:
            score_text = f"{_clamp_score(overall_score):.1f}/10" if overall_score is not None else "未评分"
            lines.append(f"总体：{score_text}" + (f"（{grade}）" if grade else ""))
        if assessment:
            lines.append(assessment)
        dimension_scores = writing.get("dimension_scores") if isinstance(writing.get("dimension_scores"), dict) else {}
        if dimension_scores:
            lines.extend(["", "| 维度 | 分数 |", "|---|---:|"])
            for key, label in WRITING_QUALITY_DIMENSION_LABELS.items():
                if key in dimension_scores:
                    lines.append(f"| {label} | {_clamp_score(dimension_scores.get(key)):.1f}/10 |")
        for title, key in (("写作优势", "strengths"), ("写作短板", "weaknesses"), ("写作证据", "evidence")):
            items = _clean_text_items(writing.get(key) or [], limit=5, max_len=160)
            if items:
                lines.extend(["", f"【{title}】"])
                lines.extend(f"- {item}" for item in items)
        frontend = {
            "overall_score": _clamp_score(overall_score) if overall_score is not None else None,
            "grade": grade,
            "dimension_scores": {
                key: _clamp_score(value)
                for key, value in dimension_scores.items()
            },
        }
        lines.extend(["", "写作质量JSON：", "```json", json.dumps(frontend, ensure_ascii=False, indent=2), "```"])

    if isinstance(pacing, dict) and pacing:
        _append_text_block(lines, "节奏曲线分析", pacing)
    if isinstance(density, dict) and density:
        _append_text_block(lines, "信息密度审计", density)
    if water:
        _append_text_block(lines, "水文与冗余分析", water)


def _append_general_narrative_architecture_sections(lines: list, general_summary: dict):
    summary = (general_summary or {}).get("summary") or {}
    structure = summary.get("narrative_structure_analysis")
    architecture = summary.get("outline_architecture_overall")
    has_any = any([
        isinstance(structure, dict) and structure,
        isinstance(architecture, dict) and architecture,
    ])
    if not has_any:
        return
    if isinstance(structure, dict) and structure:
        _append_text_block(lines, "叙事结构分析", structure)
    if isinstance(architecture, dict) and architecture:
        score = architecture.get("architecture_score")
        rating = str(architecture.get("overall_architecture_rating") or "").strip()
        if score is not None or rating:
            lines.extend(["", "【大纲架构分析】"])
            if score is not None:
                lines.append(f"架构评分：{_clamp_score(score):.1f}/10" + (f"（{rating}）" if rating else ""))
            elif rating:
                lines.append(f"架构评级：{rating}")
            rest = {
                key: value
                for key, value in architecture.items()
                if key not in {"architecture_score", "overall_architecture_rating"}
            }
            if rest:
                _append_text_block(lines, "大纲架构细项", rest)
        else:
            _append_text_block(lines, "大纲架构分析", architecture)


def _append_general_foreshadowing_engineering_sections(lines: list, general_summary: dict):
    summary = (general_summary or {}).get("summary") or {}
    engineering = summary.get("foreshadowing_engineering_analysis")
    if not isinstance(engineering, dict) or not engineering:
        return
    setup_quality = str(engineering.get("setup_quality") or "").strip()
    payoff_satisfaction = str(engineering.get("payoff_satisfaction") or "").strip()
    recycling_rate = str(engineering.get("recycling_rate_estimate") or engineering.get("recycling_rate") or "").strip()
    lines.extend(["", "【伏笔工程分析】"])
    if setup_quality or payoff_satisfaction or recycling_rate:
        parts = []
        if setup_quality:
            parts.append(f"设置质量：{setup_quality}")
        if payoff_satisfaction:
            parts.append(f"回收满足度：{payoff_satisfaction}")
        if recycling_rate:
            parts.append(f"回收率估计：{recycling_rate}")
        lines.append("；".join(parts))
    rest = {
        key: value
        for key, value in engineering.items()
        if key not in {"setup_quality", "payoff_satisfaction", "recycling_rate_estimate", "recycling_rate"}
    }
    if rest:
        _append_text_block(lines, "伏笔工程细项", rest)


def _append_general_semantic_layers_sections(lines: list, general_summary: dict):
    summary = (general_summary or {}).get("summary") or {}
    semantic = summary.get("semantic_layers_analysis")
    if not isinstance(semantic, dict) or not semantic:
        return
    lines.extend(["", "【深层语义分析】"])
    lead_parts = []
    for key, label in (
        ("dominant_author_intent", "作者意图"),
        ("reader_effect_pattern", "读者效果"),
        ("deep_semantic_pattern", "深层语义"),
    ):
        text = str(semantic.get(key) or "").strip()
        if text:
            lead_parts.append(f"{label}：{text[:180]}")
    if lead_parts:
        lines.extend(lead_parts)
    rest = {
        key: value
        for key, value in semantic.items()
        if key not in {"dominant_author_intent", "reader_effect_pattern", "deep_semantic_pattern"}
    }
    if rest:
        _append_text_block(lines, "语义细项", rest)


def _append_general_reader_experience_sections(lines: list, general_summary: dict):
    summary = (general_summary or {}).get("summary") or {}
    experience = summary.get("reader_experience_analysis")
    if not isinstance(experience, dict) or not experience:
        return
    lines.extend(["", "【读者体验分析】"])
    rating = str(experience.get("reader_experience_rating") or "").strip()
    engagement_curve = str(experience.get("engagement_curve") or "").strip()
    anticipation = str(experience.get("anticipation_management") or "").strip()
    if rating:
        lines.append(f"体验评级：{rating}")
    if engagement_curve:
        lines.append(f"投入曲线：{engagement_curve[:220]}")
    if anticipation:
        lines.append(f"期待管理：{anticipation[:220]}")
    rest = {
        key: value
        for key, value in experience.items()
        if key not in {"reader_experience_rating", "engagement_curve", "anticipation_management"}
    }
    if rest:
        _append_text_block(lines, "体验细项", rest)


def _text_signal_count(*items) -> int:
    count = 0
    for item in items:
        if isinstance(item, list):
            count += len([x for x in item if str(x or "").strip()])
        elif isinstance(item, dict):
            count += len([x for x in item.values() if str(x or "").strip()])
        elif str(item or "").strip():
            count += 1
    return count


def _normalize_general_radar_scores(general_summary: dict, detailed_data: dict = None) -> dict:
    summary = (general_summary or {}).get("summary") or {}
    raw_scores = summary.get("radar_scores") or (general_summary or {}).get("radar_scores")
    normalized = {}
    if isinstance(raw_scores, dict):
        for key, label in RADAR_SCORE_DIMENSIONS.items():
            raw = raw_scores.get(key)
            reason = ""
            if isinstance(raw, dict):
                score_value = raw.get("score")
                reason = str(raw.get("reason") or raw.get("comment") or "").strip()
            else:
                score_value = raw
            if score_value is None:
                continue
            normalized[key] = {
                "label": label,
                "score": _clamp_score(score_value),
                "reason": reason[:120],
            }
    if len(normalized) == len(RADAR_SCORE_DIMENSIONS):
        return normalized

    risks = summary_field_values(summary, "risks_or_issues")
    strengths = summary_field_values(summary, "strengths")
    characters = _general_character_rows(detailed_data or {}, limit=20)
    chunk_results = (general_summary or {}).get("chunk_results") or []
    quality_notes = []
    for chunk in chunk_results:
        if isinstance(chunk, dict):
            quality_notes.extend(_clean_text_items(chunk.get("quality_notes") or [], limit=5, max_len=120))
    risk_penalty = min(2.5, 0.4 * len(risks))
    strength_bonus = min(1.5, 0.3 * len(strengths))

    fallback_specs = {
        "plot": (
            _text_signal_count(summary_field_values(summary, "main_plot"), summary_field_values(summary, "core_conflicts")),
            "按主线剧情和核心冲突材料完整度估算。",
        ),
        "characters": (
            _text_signal_count(summary_field_values(summary, "character_highlights"), characters),
            "按重要角色数量、角色亮点和人物材料估算。",
        ),
        "worldbuilding": (
            _text_signal_count(summary_field_values(summary, "worldbuilding")),
            "按世界观/设定材料完整度估算。",
        ),
        "pacing": (
            _text_signal_count(summary_field_values(summary, "pacing_and_emotion"), quality_notes),
            "按节奏与片段质量记录估算。",
        ),
        "writing": (
            _text_signal_count(strengths, quality_notes),
            "按优点、文笔和片段质量记录估算。",
        ),
        "emotion": (
            _text_signal_count(summary_field_values(summary, "themes"), summary_field_values(summary, "pacing_and_emotion")),
            "按主题表达和情绪曲线材料估算。",
        ),
    }
    for key, label in RADAR_SCORE_DIMENSIONS.items():
        if key in normalized:
            continue
        evidence_count, reason = fallback_specs[key]
        score = 5.5 + min(2.5, evidence_count * 0.7) + strength_bonus - risk_penalty
        normalized[key] = {
            "label": label,
            "score": _clamp_score(score),
            "reason": reason,
        }
    return normalized


def _append_general_radar_score_section(lines: list, general_summary: dict, detailed_data: dict = None):
    scores = _normalize_general_radar_scores(general_summary, detailed_data)
    if not scores:
        return
    frontend_json = {
        key: {
            "label": item["label"],
            "score": item["score"],
            "reason": item.get("reason") or "",
        }
        for key, item in scores.items()
    }
    lines.extend([
        "",
        "【多维度评分】",
        "| 维度 | 分数 | 依据 |",
        "|---|---:|---|",
    ])
    for item in scores.values():
        lines.append(
            f"| {_markdown_cell(item['label'], 24)} | {item['score']:.1f}/10 | {_markdown_cell(item.get('reason') or '未描述', 90)} |"
        )
    lines.extend([
        "",
        "前端评分JSON：",
        "```json",
        json.dumps(frontend_json, ensure_ascii=False, indent=2),
        "```",
    ])


def _append_general_scan_section(lines: list, general_summary: dict, detailed_data: dict = None):
    summary = (general_summary or {}).get("summary") or {}
    if not summary:
        lines.extend(["", "【剧情与主题】", "未找到通用剧情扫描结果。"])
        return

    def add_list(title, items):
        lines.extend(["", f"【{title}】"])
        values = _clean_text_items(items or [], limit=10, max_len=180)
        if values:
            for item in values:
                lines.append(f"- {item}")
        else:
            lines.append("未描述")

    lines.extend(["", "【作品概览】"])
    lines.append(summary_field_text(summary, "story_overview"))
    _append_general_radar_score_section(lines, general_summary, detailed_data)
    add_list("主线剧情", summary_field_values(summary, "main_plot"))
    add_list("核心冲突", summary_field_values(summary, "core_conflicts"))
    add_list("世界观/设定", summary_field_values(summary, "worldbuilding"))
    add_list("主题表达", summary_field_values(summary, "themes"))
    _append_general_semantic_layers_sections(lines, general_summary)
    _append_general_reader_experience_sections(lines, general_summary)
    add_list("伏笔与回收", summary_field_values(summary, "foreshadowing_and_payoff"))
    _append_general_foreshadowing_engineering_sections(lines, general_summary)
    _append_general_narrative_architecture_sections(lines, general_summary)
    _append_general_craft_sections(lines, general_summary)
    base_summary_fields = {
        "main_plot",
        "core_conflicts",
        "worldbuilding",
        "themes",
        "foreshadowing_and_payoff",
        *GENERAL_FORESHADOWING_ENGINEERING_FIELDS,
        *GENERAL_SEMANTIC_LAYER_FIELDS,
        *GENERAL_READER_EXPERIENCE_FIELDS,
        *GENERAL_CRAFT_SUMMARY_FIELDS,
        *GENERAL_NARRATIVE_ARCHITECTURE_FIELDS,
        "strengths",
        "risks_or_issues",
        "reader_fit",
        "overall_assessment",
    }
    specialty_fields = [
        x for x in (general_summary or {}).get("summary_fields", [])
        if not (set(summary_field_candidates(x)) & base_summary_fields)
    ]
    for field in specialty_fields:
        add_list(summary_field_label(field), summary_field_values(summary, field))
    specialty_notes = _general_specialty_notes(general_summary, summary)
    if specialty_notes:
        add_list("专项命中要点", specialty_notes)
    add_list("优点", summary_field_values(summary, "strengths"))
    add_list("问题与阅读门槛", summary_field_values(summary, "risks_or_issues"))
    lines.extend(["", "【适合读者】", summary_field_text(summary, "reader_fit")])
    lines.extend(["", "【总体评价】", summary_field_text(summary, "overall_assessment")])


def _general_specialty_notes(general_summary: dict, summary: dict) -> list:
    notes = []

    def add_items(items):
        for item in _clean_text_items(items or [], limit=80, max_len=180):
            if item not in notes:
                notes.append(item)

    add_items((summary or {}).get("specialty_notes"))
    for chunk in (general_summary or {}).get("chunk_results") or []:
        if isinstance(chunk, dict):
            add_items(chunk.get("specialty_notes"))
        if len(notes) >= 10:
            break
    return notes[:10]


def build_general_report(book_key: str, detailed_data: dict, general_summary: dict = None) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    male_obj = (detailed_data or {}).get("male_protagonist") or {}
    male_name = male_obj.get("name") or "未识别"
    male_aliases = male_obj.get("other_names") or male_obj.get("aliases") or []
    male_summaries = _clean_text_items(male_obj.get("summaries") or [], limit=5, max_len=140)
    characters = _general_character_rows(detailed_data)

    lines = [
        f"书名：{book_key or '未识别'}",
        f"报告生成时间：{ts}",
        f"分析模式：{(general_summary or {}).get('profile_display_name') or '通用小说分析'}",
        "=" * 60,
    ]
    _append_general_scan_section(lines, general_summary, detailed_data)
    lines.extend([
        "",
        "【分析标准】",
        "本报告使用通用小说模板生成，关注核心角色、剧情、冲突、设定、主题和阅读体验；不执行后宫洁度、初处、漏女或排雷判定。",
        "",
        "【核心人物】",
        f"主角/视角核心：{male_name}",
    ])
    if male_aliases:
        lines.append(f"别名：{', '.join([str(x) for x in male_aliases[:8]])}")
    if male_obj.get("identity"):
        lines.append(f"身份：{male_obj.get('identity')}")
    if male_summaries:
        lines.append("主要经历：")
        for item in male_summaries:
            lines.append(f"- {item}")
    else:
        lines.append("主要经历：未描述")

    lines.extend(["", "【重要角色】"])
    if characters:
        for idx, char in enumerate(characters, 1):
            lines.extend(
                [
                    "",
                    f"{idx}. {char['name']}",
                    f"角色类型：{char.get('role_type', 'supporting')} | 重要度：{char['importance']:.1f} | 出现次数：{char['count']}",
                ]
            )
            if char["aliases"]:
                lines.append(f"别名：{', '.join([str(x) for x in char['aliases'][:8]])}")
            if char.get("factions"):
                lines.append(f"阵营/势力：{', '.join([str(x) for x in char['factions'][:5] if x])}")
            if char["relationships"]:
                lines.append("关系/身份线索：")
                for item in char["relationships"]:
                    lines.append(f"- {item}")
            if char["features"]:
                lines.append("特征：")
                for item in char["features"]:
                    lines.append(f"- {item}")
            if char["summaries"]:
                lines.append("关键事件：")
                for item in char["summaries"]:
                    lines.append(f"- {item}")
    else:
        lines.append("未识别到足够稳定的重要角色。")

    lines.extend(
        [
            "",
            "【后续扩展位】",
            "通用 profile 已预留剧情主线、冲突结构、世界观设定、主题表达、节奏评价和类型专长分析入口。",
            "如果需要历史、硬科幻、悬疑等专项分析，可以在 profiles/ 下新增对应 profile 并接入专用扫描和报告模板。",
        ]
    )
    return "\n".join(lines)


def polish_text(text: str, model: str = None):
    if not OpenAI:
        print("未安装 openai，跳过润色。")
        return text
    if not API_KEY:
        print("未设置 API_KEY，跳过润色。")
        return text
    model = model or MODEL
    system_prompt = "你是专业的中文编辑，请在不改变事实的前提下，提升流畅度与可读性，保持格式和段落结构。"
    user_prompt = f"请润色以下报告：\n{text}"
    try:
        resp = chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        record_usage(resp)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"润色失败，返回原文：{e}")
        return text


def main(novel_path=None, book_name=None, run_id=None, detail_path=None):
    global _REPORT_LOGGER
    _REPORT_LOGGER = None
    profile = load_analysis_profile(os.environ.get("ANALYSIS_PROFILE"))

    parser = argparse.ArgumentParser()
    parser.add_argument("--polish", action="store_true", help="调用大模型润色输出（已默认开启）")
    parser.add_argument("--no-polish", action="store_true", help="禁用润色，直接输出原文")
    parser.add_argument("--skip-existing", action="store_true", help="若已存在同书名报告则跳过生成（旧行为）")
    parser.add_argument("--force-regenerate", action="store_true", help="\u5ffd\u7565\u68c0\u67e5\u70b9\uff0c\u5f3a\u5236\u91cd\u65b0\u751f\u6210\u62a5\u544a")
    parse_cli_args = novel_path is None and book_name is None and run_id is None
    args = parser.parse_args() if parse_cli_args else parser.parse_args([])

    if novel_path:
        os.environ["NOVEL_PATH"] = novel_path
    log_report("=" * 80)
    log_report(f"[START] report generation @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_report(
        f"args: force_regenerate={args.force_regenerate}, "
        f"skip_existing={args.skip_existing}, polish={args.polish}, no_polish={args.no_polish}"
    )
    log_report(f"analysis profile: {profile.display_name} ({profile.name})")

    # 每次调用重新读取 NOVEL_PATH（支持多本小说循环）
    novel_path_env = os.environ.get("NOVEL_PATH")
    env_book_key = ""
    if novel_path_env:
        env_book_key = os.path.splitext(os.path.basename(novel_path_env))[0].strip()

    # 查找数据文件。通用模式不依赖后宫 reviewer，避免误读其他书的 VERIFIED_SUMMARY。
    reviewer_path = None if profile.report_mode == "general" else find_latest("VERIFIED_SUMMARY_*.json")
    reviewer = None if profile.report_mode == "general" else load_json(reviewer_path)
    
    # 先从环境变量/文件名尝试推断书名；若 reviewer 是 VERIFIED_SUMMARY_*.json，则需要进一步纠正
    book_key = env_book_key or extract_book_key_from_path(reviewer_path)
    detailed_path = None
    if book_key:
        detailed_path = find_detailed_json(book_key, detail_path=detail_path)
    elif detail_path:
        detailed_path = detail_path
    if not detailed_path:
        detailed_path = find_latest("*_detailed_*.json")
    
    detailed_data = load_json(detailed_path)

    # 纠正“无书名 reviewer 文件名”的场景：优先用 detailed/protagonists 文件名或 reviewer 目录名反推
    def _looks_like_non_book_key(k: str) -> bool:
        if not k:
            return True
        return k.startswith("VERIFIED_") or k.startswith("AGGREGATED_") or k.startswith("VERIFIED_SUMMARY_")

    if _looks_like_non_book_key(book_key):
        fixed = (
            extract_book_key_from_path(detailed_path)
            or infer_book_key_from_results_dir(reviewer_path)
        )
        if fixed:
            book_key = fixed

    log_report(f"using detailed: {detailed_path or '<not found>'}")
    log_report(f"using reviewer: {reviewer_path or '<not found>'}")
    log_report(f"book key: {book_key or '<unknown>'}")
    general_summary_path = find_general_summary_json(book_key, profile_name=profile.name) if profile.report_mode == "general" else None
    general_summary = load_json(general_summary_path) if general_summary_path else None
    if profile.report_mode == "general":
        log_report(f"using general summary: {general_summary_path or '<not found>'}")

    resolved_book_name = (book_name or book_key or "").strip()
    if not resolved_book_name:
        novel_path_for_book = novel_path or os.environ.get("NOVEL_PATH", "")
        if novel_path_for_book:
            resolved_book_name = os.path.splitext(os.path.basename(novel_path_for_book))[0].strip()
    if resolved_book_name:
        init_token_tracker(resolved_book_name, run_id=run_id)

    checkpoint_data = load_report_checkpoint()
    jobs = checkpoint_data.get("jobs", {})
    removed_legacy_keys = 0
    if isinstance(jobs, dict):
        for key in list(jobs.keys()):
            if "||" in str(key):
                jobs.pop(key, None)
                removed_legacy_keys += 1
    else:
        jobs = {}
        checkpoint_data["jobs"] = jobs
    if removed_legacy_keys:
        log_report(f"cleaned {removed_legacy_keys} legacy checkpoint keys")
        save_report_checkpoint(checkpoint_data)

    report_job_key = f"{profile.name}::{book_key}" if book_key else profile.name
    checkpoint_hit = jobs.get(report_job_key) if isinstance(jobs, dict) else None
    if not isinstance(checkpoint_hit, dict) and profile.name == "harem":
        checkpoint_hit = jobs.get(book_key, {}) if isinstance(jobs, dict) else {}
    if not isinstance(checkpoint_hit, dict):
        checkpoint_hit = {}
    checkpoint_status = checkpoint_hit.get("status")
    checkpoint_out = checkpoint_hit.get("out_file")
    saved_reviewer_mtime = checkpoint_hit.get("reviewer_mtime")
    current_reviewer_mtime = _safe_mtime(reviewer_path)

    if not args.force_regenerate:
        if checkpoint_status == "pending":
            log_report("checkpoint status is pending, forcing regenerate")
        elif (
            checkpoint_status == "completed"
            and checkpoint_out
            and os.path.exists(checkpoint_out)
            and current_reviewer_mtime == saved_reviewer_mtime
        ):
            log_report(f"\u2605 \u68c0\u67e5\u70b9\u547d\u4e2d\uff0c\u62a5\u544a\u5df2\u751f\u6210\uff0c\u8df3\u8fc7\uff1a{checkpoint_out}")
            return
        elif checkpoint_status == "completed" and current_reviewer_mtime != saved_reviewer_mtime:
            log_report("checkpoint reviewer_mtime changed, regenerate report")

    book_key_safe = sanitize_book_key(book_key)

    # 若已存在同书名的聚合报告，则可选跳过（默认不跳过，方便你改格式后重生成）
    if book_key_safe:
        # 兼容旧命名和新命名
        title_for_file = format_book_title_for_filename(book_key)
        title_wrapped = f"《{book_key}》" if book_key else ""
        report_suffix = report_suffix_for_profile(profile)
        patterns = [
            os.path.join(RESULTS_DIR, f"AGGREGATED_REPORT_{book_key_safe}_*.txt"),
            # 旧：总是外层包《》
            os.path.join(RESULTS_DIR, f"{title_wrapped}扫书报告*.txt") if title_wrapped else "",
            # 新：如果 book_key 本身已包含《》，则不再外包
            os.path.join(RESULTS_DIR, f"{title_for_file}{report_suffix}*.txt") if title_for_file else "",
        ]
        existing_reports = []
        for p in patterns:
            if not p:
                continue
            existing_reports.extend(glob.glob(p))
        if existing_reports and args.skip_existing:
            existing_reports = sorted(existing_reports)
            log_report(f"★ 已找到该书的聚合报告，跳过生成：{existing_reports[-1]}")
            return

    # 按 profile 选择报告模板。harem 保持旧版男主→女主→毒/雷点。
    log_report("building report content...")
    if profile.report_mode == "general":
        report = build_general_report(book_key, detailed_data, general_summary)
    else:
        report = build_report_v2(book_key, detailed_data, reviewer)
        harem_plus_summary_path = find_general_summary_json(book_key, profile_name="general") if profile.name == "harem" else None
        harem_plus_summary = load_json(harem_plus_summary_path) if harem_plus_summary_path else None
        if harem_plus_summary and _general_summary_matches_novel(harem_plus_summary, novel_path or novel_path_env, "general"):
            log_report(f"using harem+ general summary: {harem_plus_summary_path or '<not found>'}")
            harem_plus_lines = ["", "【作品整体评价】"]
            _append_general_scan_section(harem_plus_lines, harem_plus_summary, detailed_data)
            report = f"{report}\n" + "\n".join(harem_plus_lines)
    log_report(f"report content built, chars={len(report)}")

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    if book_key:
        # 新命名： <书名>扫书报告_时间戳.txt（避免《《书名》...》双层括号）
        title_for_file = format_book_title_for_filename(book_key)
        suffix = report_suffix_for_profile(profile)
        out_file = os.path.join(RESULTS_DIR, f"{title_for_file}{suffix}_{ts}.txt")
    else:
        out_file = os.path.join(RESULTS_DIR, f"AGGREGATED_REPORT_{ts}.txt")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)
    log_report(f"report written: {out_file}")

    checkpoint_data.setdefault("jobs", {})[report_job_key] = {
        "book_key": book_key,
        "profile": profile.name,
        "status": "completed",
        "reviewer_mtime": _safe_mtime(reviewer_path),
        "out_file": out_file,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_report_checkpoint(checkpoint_data)
    log_report(f"checkpoint updated: {REPORT_CHECKPOINT_FILE}")
    log_report(f"\n已生成：{out_file}")
    if token_tracker is not None:
        snap = token_tracker.snapshot()
        log_report(
            f"Token 统计：输入 {snap.get('input', 0)} ，输出 {snap.get('output', 0)} ，总计 {snap.get('total', 0)}"
        )
        token_tracker.flush(status="finished")
    log_report("[END] report generation finished")


if __name__ == "__main__":
    main()
