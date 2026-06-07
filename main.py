"""
main.py - PyInstaller 打包入口，替代 run_all_novels.bat。
功能：读取配置 -> 扫描 novels 目录 -> 依次调用四个业务脚本的 main()。
"""

import json
import multiprocessing
import os
import sys
import time
import uuid


_DEFAULT_ENV_SETTINGS = {
    "BASE_URL": "https://api.deepseek.com",
    "MODEL_NAME": "deepseek-chat",
    "ANALYSIS_PROFILE": "harem",
    "MAX_WORKERS": "6",
    "RATE_LIMIT_SCOPE": "global",
    "DIM_BOOST_MAX_PER_CHUNK": "3",
    "RESCAN_ROUNDS": "3",
    "MAX_MIDDLE_SUMMARY_CALLS": "10",
    "RESCAN_MAX_HITS": "4",
    "RESCAN_PRE_FILTER_THRESHOLD": "1.0",
    "RESCAN_MAX_WINDOW": "2000",
    "RESCAN_MAX_PROMPT_HEROINES": "4",
}
_PASSTHROUGH_SETTING_KEYS = {"BASE_URL", "MODEL_NAME", "ANALYSIS_PROFILE", "MAX_WORKERS", "RPM_LIMIT", "TPM_LIMIT", "RATE_LIMIT_SCOPE"}
_VALIDATED_NON_NEGATIVE_INT_KEYS = {
    "DIM_BOOST_MAX_PER_CHUNK": _DEFAULT_ENV_SETTINGS["DIM_BOOST_MAX_PER_CHUNK"],
    "RESCAN_ROUNDS": _DEFAULT_ENV_SETTINGS["RESCAN_ROUNDS"],
    "MAX_MIDDLE_SUMMARY_CALLS": _DEFAULT_ENV_SETTINGS["MAX_MIDDLE_SUMMARY_CALLS"],
    "RESCAN_MAX_HITS": _DEFAULT_ENV_SETTINGS["RESCAN_MAX_HITS"],
    "RESCAN_MAX_WINDOW": _DEFAULT_ENV_SETTINGS["RESCAN_MAX_WINDOW"],
    "RESCAN_MAX_PROMPT_HEROINES": _DEFAULT_ENV_SETTINGS["RESCAN_MAX_PROMPT_HEROINES"],
}
_VALIDATED_NON_NEGATIVE_FLOAT_KEYS = {
    "RESCAN_PRE_FILTER_THRESHOLD": _DEFAULT_ENV_SETTINGS["RESCAN_PRE_FILTER_THRESHOLD"],
}


def get_base_dir():
    """返回程序根目录：打包后为 exe 所在目录，开发时为脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _read_file_safely(file_path):
    """安全读取文件：先尝试 UTF-8，失败则回退 GB18030。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="gb18030") as f:
            return f.read()


def _set_non_negative_int_env(key, raw_value, default_value):
    try:
        parsed = int(raw_value)
        if parsed < 0:
            raise ValueError("negative value")
    except (TypeError, ValueError):
        print(f"[WARN] setting.txt 中 {key}={raw_value!r} 非法，已回退默认值 {default_value}")
        os.environ[key] = default_value
        return

    os.environ[key] = str(parsed)


def _set_non_negative_float_env(key, raw_value, default_value):
    try:
        parsed = float(raw_value)
        if parsed < 0:
            raise ValueError("negative value")
    except (TypeError, ValueError):
        print(f"[WARN] setting.txt 中 {key}={raw_value!r} 非法，已回退默认值 {default_value}")
        os.environ[key] = default_value
        return

    os.environ[key] = str(parsed)


def _exit_config_error(message, interactive=True):
    print(message)
    if interactive:
        input("按回车键退出...")
    sys.exit(1)


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None, None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
        value = value[1:-1]
    return key, value


def _load_dotenv(base_dir):
    env_file = os.path.join(base_dir, ".env")
    if not os.path.exists(env_file):
        return
    text = _read_file_safely(env_file)
    for line in text.splitlines():
        key, value = _parse_env_line(line)
        if not key:
            continue
        os.environ.setdefault(key, value)


def load_configs(base_dir, interactive=True):
    """
    读取 setting.txt 和 api.txt，注入到 os.environ。
    对应 bat 中的 setting.txt 解析和 API_KEY_POOL 构建逻辑。
    """
    _load_dotenv(base_dir)

    for key, default_value in _DEFAULT_ENV_SETTINGS.items():
        os.environ.setdefault(key, default_value)

    setting_file = os.path.join(base_dir, "setting.txt")
    if os.path.exists(setting_file):
        text = _read_file_safely(setting_file)
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().upper()
            value = value.strip()
            if key in os.environ:
                continue
            if key in _PASSTHROUGH_SETTING_KEYS:
                os.environ[key] = value
            elif key in _VALIDATED_NON_NEGATIVE_INT_KEYS:
                _set_non_negative_int_env(key, value, _VALIDATED_NON_NEGATIVE_INT_KEYS[key])
            elif key in _VALIDATED_NON_NEGATIVE_FLOAT_KEYS:
                _set_non_negative_float_env(key, value, _VALIDATED_NON_NEGATIVE_FLOAT_KEYS[key])

    env_key_pool = os.environ.get("API_KEY_POOL", "").strip()
    env_key = os.environ.get("API_KEY", "").strip()
    keys = [k.strip() for k in env_key_pool.split(",") if k.strip()]
    if not keys and env_key:
        keys = [env_key]

    api_file = os.path.join(base_dir, "api.txt")
    if not keys:
        if not os.path.exists(api_file):
            _exit_config_error(f"[ERROR] 未找到 {api_file}，请创建并写入可用的 API Key（每行一条）。", interactive=interactive)

        text = _read_file_safely(api_file)
        keys = [k.strip() for k in text.splitlines() if k.strip()]
    if not keys:
        _exit_config_error(f"[ERROR] 未读取到任何 API Key：请设置 API_KEY/API_KEY_POOL，或在 {api_file} 中写入 key", interactive=interactive)

    os.environ["API_KEY_POOL"] = ",".join(keys)
    os.environ["API_KEY"] = keys[0]

    try:
        from analysis_profiles import AUTO_PROFILE, load_analysis_profile, normalize_profile_name

        requested_profile = normalize_profile_name(os.environ.get("ANALYSIS_PROFILE"))
        if requested_profile == AUTO_PROFILE:
            profile = None
            os.environ["ANALYSIS_PROFILE"] = AUTO_PROFILE
            os.environ.pop("ANALYSIS_RULES_FILE", None)
        else:
            profile = load_analysis_profile(requested_profile)
            os.environ["ANALYSIS_PROFILE"] = profile.name
            os.environ["ANALYSIS_RULES_FILE"] = profile.rules_file
    except Exception as exc:
        profile = None
        print(f"[WARN] 加载分析 profile 失败，继续使用默认配置: {exc}")

    print(
        f"配置已加载：BASE_URL={os.environ['BASE_URL']}  "
        f"MODEL_NAME={os.environ['MODEL_NAME']}  "
        f"MAX_WORKERS={os.environ['MAX_WORKERS']}"
    )
    if profile is not None:
        print(
            f"分析模式：{profile.display_name} ({profile.name})  "
            f"规则文件={profile.rules_file}"
        )
    else:
        print("分析模式：自动识别 (auto)，将按每本小说选择 profile")
    print(
        f"扫描调优配置：DIM_BOOST_MAX_PER_CHUNK={os.environ['DIM_BOOST_MAX_PER_CHUNK']}  "
        f"RESCAN_ROUNDS={os.environ['RESCAN_ROUNDS']}  "
        f"MAX_MIDDLE_SUMMARY_CALLS={os.environ['MAX_MIDDLE_SUMMARY_CALLS']}"
    )
    print(
        f"全局补扫优化：RESCAN_MAX_HITS={os.environ['RESCAN_MAX_HITS']}  "
        f"RESCAN_PRE_FILTER_THRESHOLD={os.environ['RESCAN_PRE_FILTER_THRESHOLD']}  "
        f"RESCAN_MAX_WINDOW={os.environ['RESCAN_MAX_WINDOW']}  "
        f"RESCAN_MAX_PROMPT_HEROINES={os.environ['RESCAN_MAX_PROMPT_HEROINES']}"
    )
    print(
        f"本地限流：RPM_LIMIT={os.environ.get('RPM_LIMIT', '')}  "
        f"TPM_LIMIT={os.environ.get('TPM_LIMIT', '')}  "
        f"RATE_LIMIT_SCOPE={os.environ.get('RATE_LIMIT_SCOPE', 'global')}"
    )
    print(f"API Key 数量: {len(keys)}")
    print()


def scan_novels(base_dir):
    """递归扫描 novels 目录下所有 .txt 文件（匹配 bat 的 for /r 行为）。"""
    novels_dir = os.path.join(base_dir, "novels")
    if not os.path.isdir(novels_dir):
        print(f"[ERROR] 未找到 novels 目录: {novels_dir}")
        input("按回车键退出...")
        sys.exit(1)

    novel_files = []
    for root, _dirs, files in os.walk(novels_dir):
        for name in files:
            if name.lower().endswith(".txt"):
                novel_files.append(os.path.join(root, name))

    if not novel_files:
        print(f"[ERROR] novels 目录下没有 txt 文件: {novels_dir}")
        input("按回车键退出...")
        sys.exit(1)

    return novel_files


def _read_json_safely(file_path):
    if not os.path.exists(file_path):
        return None
    try:
        return json.loads(_read_file_safely(file_path))
    except Exception as exc:
        print(f"[WARN] 读取 JSON 失败: {file_path} ({exc})")
        return None


def _report_job_key(book_name, profile_name):
    return f"{profile_name}::{book_name}" if profile_name else book_name


def _report_is_fresh(base_dir, book_name, profile_name=None):
    checkpoint_path = os.path.join(base_dir, "results", "report_checkpoint.json")
    checkpoint_data = _read_json_safely(checkpoint_path)
    if not isinstance(checkpoint_data, dict):
        return False, None

    jobs = checkpoint_data.get("jobs", {})
    if not isinstance(jobs, dict):
        return False, None

    job = jobs.get(_report_job_key(book_name, profile_name))
    if not isinstance(job, dict) and (profile_name in (None, "harem")):
        job = jobs.get(book_name)
    if not isinstance(job, dict):
        return False, None
    if job.get("status") != "completed":
        return False, None

    out_file = job.get("out_file")
    if not out_file or not os.path.exists(out_file):
        return False, None

    return True, out_file


def print_pending_novels(novel_files):
    print("待扫描书籍：")
    for index, novel_path in enumerate(novel_files, start=1):
        print(f"  {index}. {os.path.basename(novel_path)}")
    print()


def _generate_run_id():
    return f"Run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:5]}"


def _get_working_detail_path(protagonist_module, book_name):
    helper = getattr(protagonist_module, "get_latest_report_files", None)
    if not callable(helper):
        return None
    try:
        report_files = helper(book_name) or {}
    except Exception as exc:
        print(f"[WARN] 读取 protagonist report_files 失败: {exc}")
        return None
    return (report_files or {}).get("detailed")


def print_token_summary(base_dir, token_usage_path=None):
    """读取 results/token_usage.json，打印当前运行批次的 Token 用量汇总。"""
    token_log = token_usage_path or os.path.join(base_dir, "results", "token_usage.json")
    if not os.path.exists(token_log):
        return

    print()
    print("Token 用量汇总")
    total = {"input": 0, "output": 0, "total": 0}
    active_run_id = os.environ.get("TOKEN_RUN_ID", "").strip()

    try:
        with open(token_log, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  读取 token 日志失败: {e}")
        return

    books = data.get("books", {}) if isinstance(data, dict) else {}
    if active_run_id:
        for book_entry in books.values():
            if not isinstance(book_entry, dict):
                continue
            runs = book_entry.get("runs", {})
            run_entry = runs.get(active_run_id) if isinstance(runs, dict) else None
            if not isinstance(run_entry, dict):
                continue
            total["input"] += int(run_entry.get("run_total_input", 0))
            total["output"] += int(run_entry.get("run_total_output", 0))
            total["total"] += int(run_entry.get("run_total_tokens", 0))
    else:
        for book_entry in books.values():
            if not isinstance(book_entry, dict):
                continue
            total["input"] += int(book_entry.get("book_total_input", 0))
            total["output"] += int(book_entry.get("book_total_output", 0))
            total["total"] += int(book_entry.get("book_total_tokens", 0))

    print(f"  input:  {total['input']}")
    print(f"  output: {total['output']}")
    print(f"  total:  {total['total']}")


def process_single_novel(novel_path, profile_name=None, run_id=None, skip_fresh=True):
    from analysis_profiles import AUTO_PROFILE, infer_profile_for_novel, load_analysis_profile, normalize_profile_name

    import protagonist
    import novel_scan
    import novel_reviewer
    import general_scan
    import report

    base_dir = get_base_dir()
    run_id = run_id or os.environ.get("TOKEN_RUN_ID") or _generate_run_id()
    os.environ["TOKEN_RUN_ID"] = run_id
    os.environ["NOVEL_PATH"] = novel_path
    book_name = os.path.splitext(os.path.basename(novel_path))[0]

    requested = normalize_profile_name(profile_name or os.environ.get("ANALYSIS_PROFILE"))
    if requested == AUTO_PROFILE:
        requested = infer_profile_for_novel(novel_path, book_name)
    active_profile = load_analysis_profile(requested)
    os.environ["ANALYSIS_PROFILE"] = active_profile.name
    os.environ["ANALYSIS_RULES_FILE"] = active_profile.rules_file

    print("=" * 40)
    print(f"正在处理: {os.path.basename(novel_path)}")
    print(f"NOVEL_PATH={novel_path}")
    print(f"分析模式: {active_profile.display_name} ({active_profile.name})")

    if skip_fresh:
        should_skip, out_file = _report_is_fresh(base_dir, book_name, active_profile.name)
        if should_skip:
            print(f"★ 检测到该书报告已正常生成，跳过后续流程：{out_file}")
            return {"status": "skipped", "book_name": book_name, "profile": active_profile.name, "out_file": out_file}

    status = "ok"
    error = ""

    try:
        ret = protagonist.main(novel_path=novel_path, book_name=book_name, run_id=run_id)
        if ret is not None and ret != 0:
            status = "fail"
            error = "protagonist returned non-zero"
    except Exception as e:
        print(f"[protagonist] 异常: {e}")
        status = "fail"
        error = f"protagonist: {e}"

    detail_path = None
    if status == "ok":
        detail_path = _get_working_detail_path(protagonist, book_name)

    if status == "ok" and active_profile.uses_harem_reviewer:
        try:
            novel_scan.main(novel_path=novel_path, book_name=book_name, run_id=run_id, detail_path=detail_path)
        except Exception as e:
            print(f"[novel_scan] 异常: {e}")
            status = "fail"
            error = f"novel_scan: {e}"

    if status == "ok" and active_profile.uses_harem_reviewer:
        try:
            novel_reviewer.main(novel_path=novel_path, book_name=book_name, run_id=run_id, detail_path=detail_path)
        except Exception as e:
            print(f"[novel_reviewer] 异常: {e}")
            status = "fail"
            error = f"novel_reviewer: {e}"

    if status == "ok" and active_profile.uses_general_scan:
        try:
            general_scan.main(novel_path=novel_path, book_name=book_name, run_id=run_id, detail_path=detail_path)
        except Exception as e:
            print(f"[general_scan] 异常: {e}")
            status = "fail"
            error = f"general_scan: {e}"

    if status == "ok":
        try:
            report.main(novel_path=novel_path, book_name=book_name, run_id=run_id, detail_path=detail_path)
        except Exception as e:
            print(f"[report] 异常: {e}")
            status = "fail"
            error = f"report: {e}"

    if status == "ok":
        print(f"成功: {os.path.basename(novel_path)}")
    else:
        print(f"失败: {os.path.basename(novel_path)}")
    return {"status": status, "book_name": book_name, "profile": active_profile.name, "error": error}


def _merge_profile_results(book_name, results):
    statuses = [result.get("status") for result in results if isinstance(result, dict)]
    if statuses and all(status == "skipped" for status in statuses):
        status = "skipped"
    elif statuses and all(status in {"ok", "skipped"} for status in statuses):
        status = "ok"
    else:
        status = "fail"
    profiles = [result.get("profile") for result in results if result.get("profile")]
    errors = [result.get("error") for result in results if result.get("error")]
    return {
        "status": status,
        "book_name": book_name,
        "profile": profiles[0] if profiles else "",
        "profiles": profiles,
        "results": results,
        "error": "；".join(errors),
    }


def _normalize_requested_profiles(value):
    from analysis_profiles import AUTO_PROFILE, normalize_profile_name, resolve_profile_name

    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_text = str(value or os.environ.get("ANALYSIS_PROFILE") or AUTO_PROFILE)
        raw_values = [part for part in raw_text.replace("，", ",").split(",")]

    profiles = []
    for item in raw_values:
        name = normalize_profile_name(str(item or "").strip())
        if not name:
            continue
        if name == AUTO_PROFILE:
            return [AUTO_PROFILE]
        resolved = resolve_profile_name(name)
        if resolved not in profiles:
            profiles.append(resolved)
    return profiles or [AUTO_PROFILE]


def process_novel_with_profiles(novel_path, profile_name=None, run_id=None, skip_fresh=True):
    from analysis_profiles import AUTO_PROFILE, infer_profiles_for_novel, normalize_profile_name

    book_name = os.path.splitext(os.path.basename(novel_path))[0]
    requested_profiles = _normalize_requested_profiles(profile_name)
    if requested_profiles[0] == AUTO_PROFILE:
        profiles = infer_profiles_for_novel(novel_path, book_name)
        print(f"自动识别命中 {len(profiles)} 个分类: {', '.join(profiles)}")
    else:
        profiles = requested_profiles

    if len(profiles) == 1:
        return process_single_novel(novel_path, profile_name=profiles[0], run_id=run_id, skip_fresh=skip_fresh)

    print(f"将按 {len(profiles)} 个分类分别扫描: {', '.join(profiles)}")
    results = []
    for profile in profiles:
        profile_run_id = run_id or _generate_run_id()
        results.append(process_single_novel(novel_path, profile_name=profile, run_id=profile_run_id, skip_fresh=skip_fresh))
    return _merge_profile_results(book_name, results)


def run():
    base_dir = get_base_dir()
    run_id = _generate_run_id()
    os.environ["TOKEN_RUN_ID"] = run_id

    print("=" * 60)
    print("小说分析全流程工具")
    print("=" * 60)
    print()

    load_configs(base_dir)
    from analysis_profiles import AUTO_PROFILE, infer_profile_for_novel, load_analysis_profile, normalize_profile_name

    requested_profile_name = normalize_profile_name(os.environ.get("ANALYSIS_PROFILE"))
    profile = load_analysis_profile("harem" if requested_profile_name == AUTO_PROFILE else requested_profile_name)
    if requested_profile_name != AUTO_PROFILE:
        os.environ["ANALYSIS_PROFILE"] = profile.name
        os.environ["ANALYSIS_RULES_FILE"] = profile.rules_file
    novel_files = scan_novels(base_dir)

    total = len(novel_files)
    done = 0
    skipped = 0
    failed = 0

    print_pending_novels(novel_files)

    if requested_profile_name == AUTO_PROFILE:
        print(f"扫描 novels 目录下所有 txt，共 {total} 本，分析模式：自动识别 (auto)")
    else:
        print(f"扫描 novels 目录下所有 txt，共 {total} 本，分析模式：{profile.display_name} ({profile.name})")
    print("  1) protagonist.py   - 角色识别")
    if requested_profile_name == AUTO_PROFILE:
        print("  2) 自动识别一个或多个分类后分别执行对应扫描")
        print("  3) report.py        - 分别生成对应报告")
    elif profile.uses_harem_reviewer:
        print("  2) novel_scan.py    - 后宫/排雷深度扫描")
        print("  3) novel_reviewer.py - 后宫毒点二审与洁度鉴定")
        print("  4) report.py        - 生成后宫专长报告")
    else:
        print("  2) general_scan.py  - 通用剧情/主题/设定扫描")
        print("  3) report.py        - 生成通用小说报告")
    print()

    for novel_path in novel_files:
        book_name = os.path.splitext(os.path.basename(novel_path))[0]
        time.sleep(5)
        result = process_novel_with_profiles(novel_path, profile_name=requested_profile_name, run_id=run_id, skip_fresh=True)
        if result.get("status") == "skipped":
            skipped += 1
        elif result.get("status") == "ok":
            done += 1
        else:
            failed += 1
        print()

    print("=" * 40)
    print("处理完成")
    print(f"总计: {total} 本  成功: {done}  跳过: {skipped}  失败: {failed}")
    print("=" * 40)

    print_token_summary(base_dir)

    print()
    input("所有任务完成，按回车键退出...")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
