import json
import os
from datetime import datetime
from typing import Any, Dict, List

from tqdm import tqdm

from analysis_profiles import load_analysis_profile
from shared_utils import MODEL, chat_completion, get_base_dir, init_token_tracker, read_file_safely, record_usage
from shared_utils import _safe_json_loads_maybe
from text_anchor import build_chunk_manifest, save_chunk_manifest


CHUNK_SIZE = int(os.environ.get("GENERAL_SCAN_CHUNK_SIZE", "12000"))
CHUNK_OVERLAP = int(os.environ.get("GENERAL_SCAN_CHUNK_OVERLAP", "1000"))
MAX_CHUNKS = int(os.environ.get("GENERAL_SCAN_MAX_CHUNKS", "80"))


def _read_novel(path: str) -> str:
    return read_file_safely(path)


def _latest_summary_path(results_dir: str, clean_name: str, profile_name: str = "general") -> str:
    if profile_name == "general":
        return os.path.join(results_dir, f"{clean_name}_GENERAL_SUMMARY_latest.json")
    return os.path.join(results_dir, f"{clean_name}_{profile_name}_GENERAL_SUMMARY_latest.json")


def _read_json(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _novel_mtime(path: str):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _is_fresh_summary(data: Dict[str, Any], novel_file: str, profile_name: str = "general") -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("schema_version") != 1:
        return False
    if data.get("analysis_profile") not in {"general", profile_name}:
        return False
    if data.get("specialty_profile", data.get("analysis_profile", "general")) != profile_name:
        return False
    if os.path.abspath(data.get("novel_path", "")) != os.path.abspath(novel_file):
        return False
    if data.get("chunk_size") != CHUNK_SIZE or data.get("chunk_overlap") != CHUNK_OVERLAP:
        return False
    if data.get("max_chunks") != MAX_CHUNKS:
        return False
    current_mtime = _novel_mtime(novel_file)
    if current_mtime is None or data.get("novel_mtime") != current_mtime:
        return False
    return bool((data.get("summary") or {}).get("story_overview") or data.get("chunk_results"))


def _safe_list(value: Any, limit: int = 20) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = [value]
    out = []
    for item in items:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _rules_lines_from_file(rules_file: str, import_categories=None, import_points=None) -> List[str]:
    data = _read_json(rules_file)
    if not isinstance(data, dict):
        return []
    category_filter = {str(x) for x in (import_categories or []) if str(x).strip()}
    point_filter = {str(x) for x in (import_points or []) if str(x).strip()}
    lines = []
    for category in data.get("categories", []) or []:
        if not isinstance(category, dict):
            continue
        name = str(category.get("name") or "").strip()
        description = str(category.get("description") or "").strip()
        include_category = not category_filter or name in category_filter
        point_lines = []
        for point in category.get("points", []) or []:
            if not isinstance(point, dict):
                continue
            point_name = str(point.get("name") or "").strip()
            point_desc = str(point.get("description") or "").strip()
            if point_filter and point_name not in point_filter:
                continue
            if point_name:
                point_lines.append(f"- {point_name}: {point_desc}")
        if not include_category and not point_lines:
            continue
        if name:
            lines.append(f"【{name}】{description}")
        lines.extend(point_lines)
    return lines


def _cross_profile_rules_text(profile) -> str:
    cross_rules = getattr(profile, "cross_profile_rules", {}) or {}
    if not isinstance(cross_rules, dict):
        return ""
    sections = []
    for source_name, config in cross_rules.items():
        if not isinstance(config, dict):
            continue
        source_profile = load_analysis_profile(str(source_name))
        lines = _rules_lines_from_file(
            getattr(source_profile, "rules_file", "") or "",
            import_categories=config.get("import_categories") if isinstance(config.get("import_categories"), list) else None,
            import_points=config.get("import_points") if isinstance(config.get("import_points"), list) else None,
        )
        if lines:
            sections.append(f"【跨类型导入：{source_profile.display_name}】\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _profile_rules_text(profile) -> str:
    lines = _rules_lines_from_file(getattr(profile, "rules_file", "") or "")
    cross_text = _cross_profile_rules_text(profile)
    if cross_text:
        lines.append(cross_text)
    return "\n".join(lines) if lines else "（无专项规则）"


def _call_json(messages, max_tokens=3000) -> Dict[str, Any]:
    response = chat_completion(
        model=MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    record_usage(response)
    content = response.choices[0].message.content
    data, err = _safe_json_loads_maybe(content)
    if data is None:
        raise ValueError(err)
    return data


def _focus_text(profile) -> str:
    focus = profile.scan_focus or [
        "剧情主线与关键事件",
        "核心冲突与人物目标",
        "世界观、时代背景或制度设定",
        "主题表达与情绪基调",
        "伏笔、悬念与回收",
        "节奏、逻辑、人物动机、优点和阅读门槛",
    ]
    return "\n".join(f"- {item}" for item in focus)


def _scan_chunk(text_chunk: str, chunk_index: int, total_chunks: int, profile=None) -> Dict[str, Any]:
    profile = profile or load_analysis_profile("general")
    rules_text = _profile_rules_text(profile)
    system_prompt = f"""你是{profile.display_name}助手。请从片段中抽取对整本小说分析有用的信息。

关注范围：
- plot_events: 推动主线或支线的关键事件
- conflicts: 人物、阵营、目标、价值观或外部危机冲突
- worldbuilding: 世界观、时代背景、制度、科技/魔法/功法/历史设定
- themes: 反复出现的主题、价值观、情绪母题
- foreshadowing: 伏笔、悬念、未解决问题
- quality_notes: 节奏、逻辑、人物动机、爽点、虐点、亮点或明显问题

本 profile 的专项关注：
{_focus_text(profile)}

本 profile 的专项规则：
{rules_text}

要求：
1. 只根据片段内容输出，不要凭空补全。
2. 每条尽量短，保留可复核的具体信息。
3. specialty_notes 必须围绕专项规则记录命中点、疑点或亮点；若片段没有专项内容，输出空数组。
4. 输出 JSON 对象，不要 Markdown。"""
    user_prompt = f"""片段 {chunk_index + 1}/{total_chunks}：

--- 开始 ---
{text_chunk}
--- 结束 ---

请输出：
{{
  "plot_events": ["..."],
  "conflicts": ["..."],
  "worldbuilding": ["..."],
  "themes": ["..."],
  "foreshadowing": ["..."],
  "quality_notes": ["..."],
  "specialty_notes": ["专项规则相关要点"],
  "one_sentence_summary": "本片段一句话概要"
}}"""
    data = _call_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=3000,
    )
    return {
        "chunk_index": chunk_index,
        "plot_events": _safe_list(data.get("plot_events")),
        "conflicts": _safe_list(data.get("conflicts")),
        "worldbuilding": _safe_list(data.get("worldbuilding")),
        "themes": _safe_list(data.get("themes")),
        "foreshadowing": _safe_list(data.get("foreshadowing")),
        "quality_notes": _safe_list(data.get("quality_notes")),
        "specialty_notes": _safe_list(data.get("specialty_notes")),
        "one_sentence_summary": str(data.get("one_sentence_summary", "") or "").strip(),
    }


def _merge_items(chunk_results: List[Dict[str, Any]], key: str, limit: int = 80) -> List[str]:
    seen = set()
    out = []
    for item in chunk_results:
        for text in item.get(key, []) or []:
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                return out
    return out


def _summarize_book(book_name: str, chunk_results: List[Dict[str, Any]], profile=None) -> Dict[str, Any]:
    profile = profile or load_analysis_profile("general")
    material = {
        "chunk_summaries": [
            {"chunk_index": x.get("chunk_index"), "summary": x.get("one_sentence_summary")}
            for x in chunk_results
            if x.get("one_sentence_summary")
        ][:120],
        "plot_events": _merge_items(chunk_results, "plot_events"),
        "conflicts": _merge_items(chunk_results, "conflicts"),
        "worldbuilding": _merge_items(chunk_results, "worldbuilding"),
        "themes": _merge_items(chunk_results, "themes"),
        "foreshadowing": _merge_items(chunk_results, "foreshadowing"),
        "quality_notes": _merge_items(chunk_results, "quality_notes"),
        "specialty_notes": _merge_items(chunk_results, "specialty_notes"),
    }
    specialty_fields = [x for x in profile.summary_fields if x not in {
        "main_plot",
        "core_conflicts",
        "worldbuilding",
        "themes",
        "foreshadowing_and_payoff",
        "strengths",
        "risks_or_issues",
    }]
    specialty_json_hint = ""
    if specialty_fields:
        specialty_json_hint = "\n".join(f'  "{field}": ["{field} 专项分析要点"],' for field in specialty_fields)
    rules_text = _profile_rules_text(profile)

    system_prompt = f"""你是{profile.display_name}总评分析师。请基于分块抽取结果，形成整本书的分析结论。

本 profile 的专项规则：
{rules_text}

输出必须是 JSON 对象。不要使用后宫、初处、漏女、排雷等专用标准。"""
    user_prompt = f"""书名：{book_name}

分块材料：
{json.dumps(material, ensure_ascii=False, indent=2)}

请输出：
{{
  "story_overview": "整本书概览，100-200字",
  "main_plot": ["主线剧情要点"],
  "core_conflicts": ["核心冲突"],
  "worldbuilding": ["世界观/设定要点"],
  "themes": ["主题表达"],
  "foreshadowing_and_payoff": ["伏笔、悬念、回收情况"],
{specialty_json_hint}
  "strengths": ["作品优点"],
  "risks_or_issues": ["可能的问题或阅读门槛"],
  "reader_fit": "适合什么读者",
  "overall_assessment": "总体评价"
}}"""
    data = _call_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4000,
    )
    summary = {
        "story_overview": str(data.get("story_overview", "") or "").strip(),
        "main_plot": _safe_list(data.get("main_plot"), limit=20),
        "core_conflicts": _safe_list(data.get("core_conflicts"), limit=20),
        "worldbuilding": _safe_list(data.get("worldbuilding"), limit=20),
        "themes": _safe_list(data.get("themes"), limit=20),
        "foreshadowing_and_payoff": _safe_list(data.get("foreshadowing_and_payoff"), limit=20),
        "strengths": _safe_list(data.get("strengths"), limit=20),
        "risks_or_issues": _safe_list(data.get("risks_or_issues"), limit=20),
        "reader_fit": str(data.get("reader_fit", "") or "").strip(),
        "overall_assessment": str(data.get("overall_assessment", "") or "").strip(),
    }
    for field in specialty_fields:
        summary[field] = _safe_list(data.get(field), limit=20)
    return summary


def main(novel_path=None, book_name=None, run_id=None, detail_path=None):
    base = get_base_dir()
    if novel_path:
        os.environ["NOVEL_PATH"] = novel_path
    novel_file = novel_path or os.environ.get("NOVEL_PATH", os.path.join(base, "novels", "default.txt"))
    clean_name = (book_name or os.path.splitext(os.path.basename(novel_file))[0]).strip()
    profile = load_analysis_profile(os.environ.get("ANALYSIS_PROFILE", "general"))
    init_token_tracker(clean_name, run_id=run_id, out_path=os.path.join(base, "results", "token_usage.json"))

    results_dir = os.path.join(base, "results")
    latest_file = _latest_summary_path(results_dir, clean_name, profile.name)
    latest_data = _read_json(latest_file)
    if _is_fresh_summary(latest_data, novel_file, profile.name):
        print(f"★ 通用扫描已是最新，复用: {latest_file}")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_dir = os.path.join(results_dir, f"{clean_name}_{profile.name}_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    text = _read_novel(novel_file)
    manifest = build_chunk_manifest(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    save_chunk_manifest(manifest, os.path.join(output_dir, "chunk_manifest.json"))
    chunks = [x.get("text", "") for x in manifest.get("chunks", [])]
    if MAX_CHUNKS > 0:
        chunks = chunks[:MAX_CHUNKS]

    print(f"★ {profile.display_name}：{clean_name}，共 {len(chunks)} 个片段")
    chunk_results = []
    failed = []
    for idx, chunk in enumerate(tqdm(chunks, desc="通用扫描")):
        try:
            chunk_results.append(_scan_chunk(chunk, idx, len(chunks), profile=profile))
        except Exception as exc:
            failed.append({"chunk_index": idx, "error": str(exc)})

    summary = _summarize_book(clean_name, chunk_results, profile=profile) if chunk_results else {}
    out = {
        "schema_version": 1,
        "analysis_profile": "general",
        "specialty_profile": profile.name,
        "profile_display_name": profile.display_name,
        "scan_focus": profile.scan_focus,
        "summary_fields": profile.summary_fields,
        "book_name": clean_name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "novel_path": novel_file,
        "novel_mtime": _novel_mtime(novel_file),
        "detail_path": detail_path,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "max_chunks": MAX_CHUNKS,
        "chunk_count": len(chunks),
        "failed_chunks": failed,
        "chunk_results": chunk_results,
        "summary": summary,
    }
    out_file = os.path.join(output_dir, "GENERAL_SUMMARY.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"★ 通用扫描结果: {out_file}")
    return 0


if __name__ == "__main__":
    main()
