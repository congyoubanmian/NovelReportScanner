import os
import glob
import json
import argparse
from datetime import datetime
import concurrent.futures
import time
from tqdm import tqdm
import threading
import re
import logging
from Timerror import make_chat_completion
from shared_utils import get_base_dir, read_file_safely
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
    "scientific_assumptions": "科学假设",
    "technology_chain": "技术链与工程约束",
    "science_consistency": "科学设定自洽性",
    "scale_and_wonder": "尺度感与科幻奇观",
    "social_ethical_impact": "社会与伦理影响",
    "character_highlights": "角色亮点",
    "pacing_and_emotion": "节奏与情绪曲线",
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
    "unit_plot_mainline_link": "单元剧情与主线连接度",
    "cheat_detection_dependency": "外挂破案依赖度",
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
}

SUMMARY_FIELD_ALIASES = {
    "main_story": "main_plot",
    "plot_summary": "main_plot",
    "character_arcs": "character_highlights",
    "character_moments": "character_highlights",
    "pacing": "pacing_and_emotion",
    "emotion_curve": "pacing_and_emotion",
    "humanity_and_morality": "humanity_moral_dilemmas",
    "power_system": "power_evolution_system",
    "exploration_and_adventure": "exploration_adventure",
    "case_design": "case_structure",
    "case_logic": "logic_chain_integrity",
    "clue_logic": "clue_fairness",
    "social_relevance": "social_reflection",
    "tech_plausibility": "tech_feasibility",
    "technology_feasibility": "tech_feasibility",
    "adventure_structure": "adventure_system",
    "companions": "party_dynamics",
    "romance_subplot": "romance_comedy_balance",
    "daily_life": "slice_of_life",
}


def summary_field_label(field: str) -> str:
    if field in SUMMARY_FIELD_TITLES:
        return SUMMARY_FIELD_TITLES[field]
    canonical = SUMMARY_FIELD_ALIASES.get(field, field)
    return SUMMARY_FIELD_TITLES.get(canonical, field.replace("_", " "))


def summary_field_values(summary: dict, field: str):
    if not isinstance(summary, dict):
        return []
    values = []
    canonical = SUMMARY_FIELD_ALIASES.get(field, field)
    candidate_fields = [field]
    if canonical not in candidate_fields:
        candidate_fields.append(canonical)
    candidate_fields.extend(
        alias
        for alias, target in SUMMARY_FIELD_ALIASES.items()
        if target == canonical and alias not in candidate_fields
    )
    for candidate in candidate_fields:
        value = summary.get(candidate)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    return values


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
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            os.makedirs(os.path.dirname(REPORT_RUN_LOG_PATH), exist_ok=True)
            fh = logging.FileHandler(REPORT_RUN_LOG_PATH, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(fh)
        except Exception:
            pass
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
MAX_403_RETRIES = 3
MAX_TIMEOUT_RETRIES = 3  # 连续超时 3 次则标记 key 不可用
REQUEST_TIMEOUT = 120  # 请求超时时间（秒）

def _openai_client_factory(api_key: str, base_url: str, timeout: int):
    """
    创建 OpenAI 客户端，关闭 SDK 暗重试并使用细粒度 timeout。
    
    【关键】max_retries=0 关闭 SDK 自动重试：
    - SDK 默认会重试 2 次，每次都有 timeout
    - 外层 Timerror.py 再重试 5 次
    - 不关闭的话，总耗时可能达到 120s * 3 * 5 = 1800s
    
    【关键】使用 httpx.Timeout 细粒度配置：
    - connect: 连接超时（10s）
    - read: 读取超时（根据请求规模动态调整）
    - write: 写入超时（30s）
    - pool: 连接池超时（10s）
    """
    try:
        import httpx
        http_timeout = httpx.Timeout(
            connect=10.0,
            read=float(timeout),
            write=30.0,
            pool=10.0,
        )
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=http_timeout,
            max_retries=0,  # 关闭 SDK 自动重试
        )
    except ImportError:
        # 没有 httpx 时使用简单 timeout
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # 关闭 SDK 自动重试
        )


chat_completion = make_chat_completion(
    openai_client_factory=_openai_client_factory,
    api_key_pool=API_KEY_POOL,
    base_url=BASE_URL,
    request_timeout=REQUEST_TIMEOUT,
    max_retries=5,
    max_403_retries=MAX_403_RETRIES,
    max_timeout_retries=MAX_TIMEOUT_RETRIES,
    base_delay=2,
    logger=None,  # report.py 里原本用 print 输出
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
    return current_mtime is not None and summary.get("novel_mtime") == current_mtime


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
        resp = chat_completion(
            model=model or MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        record_usage(resp)
        data = json.loads(resp.choices[0].message.content.strip())
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
        "没有", "没", "无", "未见", "未", "尚未", "并未", "不曾", "不存在", "缺少", "缺乏",
        "无明确", "没有明确", "未确认", "未发生", "未产生", "未建立", "未收入",
        "不喜欢", "不爱", "并不喜欢", "并不爱", "不是喜欢", "不是爱",
        "谈不上喜欢", "谈不上爱", "称不上喜欢", "称不上爱", "讨厌", "厌恶",
    )
    roleplay_hints = ("假装成", "假扮成", "伪装成", "冒充", "扮作", "装作")
    non_romantic_like_followers = ("什么", "哪", "吃", "喝", "看", "用", "这", "那", "某", "衣", "裙", "书", "菜", "颜色", "东西", "物件")
    for word in keywords:
        start = 0
        while word:
            index = text.find(word, start)
            if index < 0:
                break
            window = text[max(0, index - 12):index]
            around_window = text[max(0, index - 8):index + len(word)]
            roleplay_window = text[max(0, index - 8):index]
            next_text = text[index + len(word):index + len(word) + 4]
            non_romantic_like = word == "喜欢" and (
                text[max(0, index - 2):index] == "喜不"
                or any(next_text.startswith(hint) for hint in non_romantic_like_followers)
            )
            if (
                not any(hint in window or hint in around_window for hint in negative_hints)
                and not any(hint in roleplay_window for hint in roleplay_hints)
                and not non_romantic_like
            ):
                return True
            start = index + len(word)
    return False


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
    return text in _GENERIC_HEROINE_ANCHOR_NAMES or normalized in _GENERIC_HEROINE_ANCHOR_NAMES


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
    if _contains_any_text(raw_text, ["工具", "召唤", "捧哏", "背景", "说明", "偶尔", "客串", "存在感", "神隐"]):
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

    if purity_info.get("pushed_by_male_lead") is True:
        score += 4
        signals.append("已被男主明确推倒/确认关系")
    if purity_info.get("is_leak_heroine") is True or purity_info.get("leak_emotional_depth") is True:
        score += 3
        signals.append("漏女/情感深度证据")
    strong_relationship_position_words = ["妻子", "正妻", "妻室", "夫妻", "夫妇", "道侣", "恋人", "爱人", "后宫", "未婚妻", "伴侣", "情侣", "女朋友", "老婆"]
    romance_signal_words = ["喜欢", "爱慕", "表白", "暧昧", "吃醋", "双修", "同房", "亲密", "推倒", "收女", "动心", "倾心"]
    if _contains_positive_signal_text(text, strong_relationship_position_words) or _has_positive_heroine_position_signal(text):
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

    if _contains_any_text(text, ["工具", "召唤", "捧哏", "背景", "说明", "偶尔", "客串", "存在感", "神隐"]):
        score -= 2
        risks.append("低存在感/工具人线索")
    has_romance_gap_signal = _contains_any_text(text, ["没有恋爱", "没有暧昧", "没有感情描写", "无感情描写", "感情戏缺失", "缺少感情", "没有后宫关系确认", "未确认后宫关系", "无后宫关系确认"])
    if has_romance_gap_signal:
        score -= 1
        risks.append("明确缺少恋爱/后宫推进")
    if not signals:
        risks.append("缺少关系、事件和出场证据")

    has_confirmed_target = (
        purity_info.get("pushed_by_male_lead") is True
        or _contains_positive_signal_text(text, ["妻子", "正妻", "妻室", "夫妻", "夫妇", "道侣", "恋人", "爱人", "后宫", "未婚妻", "伴侣", "情侣", "女朋友", "老婆"])
    )
    has_candidate_relationship_signal = (
        has_confirmed_target
        or purity_info.get("is_leak_heroine") is True
        or purity_info.get("leak_emotional_depth") is True
        or _has_positive_heroine_position_signal(text)
        or _contains_positive_signal_text(text, strong_relationship_position_words)
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
            if len(candidate) < 2:
                continue
            if _is_generic_heroine_anchor_name(candidate):
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
        matched_aliases = [alias for alias in aliases if alias and alias in haystack]
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
    matched = _matched_issue_heroine_contexts(issue, heroine_contexts)
    if not matched:
        return "按锁定定义，送女/绿帽必须锚定目标女主或强准女主；当前条目未命中已识别女主名或别名，建议复核对象是否成立。"
    weak_names = [
        ctx.get("name")
        for ctx in matched
        if ctx.get("name") and ctx.get("label") not in ("目标女主", "强准女主")
    ]
    if not weak_names:
        return ""
    return f"按锁定定义，送女/绿帽仅适用于目标女主或强准女主；{','.join(weak_names[:3])} 当前定位偏弱，建议复核是否误判。"


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

    has_emotional_depth = _contains_any_text(
        profile_text,
        ["暧昧", "喜欢", "爱", "动心", "倾心", "表白", "告白", "吃醋", "承诺", "救赎", "道侣", "恋人", "未婚妻"],
    )
    pushed = purity_info.get("pushed_by_male_lead")
    leak = purity_info.get("is_leak_heroine")
    has_relationship_confirmed = bool(pushed) or _contains_any_text(
        profile_text,
        ["推倒", "同房", "双修", "成婚", "纳妾", "妾", "道侣", "恋人", "确认关系", "收入后宫"],
    )
    has_ending_note = _contains_any_text(
        profile_text,
        ["结局", "最终", "最后", "归宿", "留在", "成婚", "同居", "后宫", "道侣"],
    )

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
    same = bool(non_empty and len(set(non_empty)) == 1)
    canonical = max(names, key=lambda n: (len(_heroine_name_key(n)), "（" in n or "(" in n, len(n), n)) if names else ""
    return {"same_person": same, "canonical_name": canonical, "aliases": names, "reason": "称谓归一后相同"}


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
2. 但亲属关系、身份、姓名核心不同、同时存在为不同角色时，不要合并。
3. 宁可保守，不确定就 same_person=false。

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
        resp = chat_completion(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.0,
            max_tokens=900,
            response_format={"type": "json_object"},
        )
        record_usage(resp)
        data = json.loads(resp.choices[0].message.content or "{}")
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
        resp = chat_completion(
            model=model or MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
            ],
            temperature=0.2,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        record_usage(resp)
        data = json.loads(resp.choices[0].message.content.strip())
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
    romance_gap_words = ("感情戏缺失", "预期落差", "进度条诈骗", "恋爱推进停滞", "感情描写缺失")
    tooling_words = ("工具人女主", "工具人", "捧哏", "召唤物", "背景说明", "客串", "神隐")
    presence_low = 0
    intimacy_hits = 0
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
        count = int(evid.get("count") or h.get("count") or 0)
        if count <= 2 and len(blob) < 120:
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
    has_romance_gap_issue = any(word in issue_blob for word in romance_gap_words)
    has_tooling_issue = any(word in issue_blob for word in tooling_words)

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
        resp = chat_completion(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=1400,
            response_format={"type": "json_object"},
        )
        record_usage(resp)
        data = json.loads(resp.choices[0].message.content or "{}")
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
    if _male_past_romance_blob_is_nonfactual_or_negated(text):
        return False
    if _male_past_romance_text_has_only_non_partner_homonyms(text):
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


def _male_past_romance_blob_is_nonfactual_or_negated(text: str) -> bool:
    nonfactual_words = ("传言", "传闻", "据说", "听说", "流言", "谣言", "误传", "谣传", "猜测", "疑似", "梦见", "梦到", "梦境")
    resolved_words = ("证实是误会", "证实不成立", "后来证实", "澄清", "不属实", "并非事实", "假的", "假消息")
    negated_patterns = (
        "没有恋爱经历", "没有感情经历", "没有前女友", "没有前妻", "没有前任",
        "没谈过恋爱", "从未恋爱", "未恋爱", "无恋爱经历", "无感情经历",
        "没有结婚", "未结婚", "未婚", "没有婚史", "无婚史",
    )
    strong_factual_anchors = ("确实", "明确", "实锤", "证据显示", "事实是", "实际发生", "真的发生", "已确认", "确认了")
    if any(word in text for word in resolved_words):
        return True
    if any(pattern in text for pattern in negated_patterns):
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

    return "\n".join([*header, "", *male_lines, *heroine_lines, *romance_lines, *risk_lines])


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


def _append_general_scan_section(lines: list, general_summary: dict):
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
    lines.append(summary.get("story_overview") or "未描述")
    add_list("主线剧情", summary.get("main_plot"))
    add_list("核心冲突", summary.get("core_conflicts"))
    add_list("世界观/设定", summary.get("worldbuilding"))
    add_list("主题表达", summary.get("themes"))
    add_list("伏笔与回收", summary.get("foreshadowing_and_payoff"))
    specialty_fields = [
        x for x in (general_summary or {}).get("summary_fields", [])
        if x not in {
            "main_plot",
            "core_conflicts",
            "worldbuilding",
            "themes",
            "foreshadowing_and_payoff",
            "strengths",
            "risks_or_issues",
        }
    ]
    for field in specialty_fields:
        add_list(summary_field_label(field), summary_field_values(summary, field))
    add_list("优点", summary.get("strengths"))
    add_list("问题与阅读门槛", summary.get("risks_or_issues"))
    lines.extend(["", "【适合读者】", summary.get("reader_fit") or "未描述"])
    lines.extend(["", "【总体评价】", summary.get("overall_assessment") or "未描述"])


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
    _append_general_scan_section(lines, general_summary)
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
            _append_general_scan_section(harem_plus_lines, harem_plus_summary)
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
