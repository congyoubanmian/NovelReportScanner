import json
import os
import queue
import subprocess
import threading
import time
import uuid
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning, message="'cgi' is deprecated*")
import cgi
import sys

from analysis_profiles import (
    infer_profile_candidates_for_novel,
    infer_profiles_for_novel,
    list_available_profiles,
    normalize_profile_name,
    profile_options,
)
from main import _WEB_SCAN_RESULT_PREFIX, _generate_run_id, get_base_dir, load_configs


STATE_LOCK = threading.RLock()
TASK_QUEUE = queue.Queue()
TASK_QUEUE_IDS = set()
WORKER_STARTED = False
STATE = {"books": {}, "tasks": []}
CONFIG_READY = False
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(100 * 1024 * 1024)))
MAX_JSON_BODY_SIZE = int(os.environ.get("MAX_JSON_BODY_SIZE", str(64 * 1024)))
FILE_RESPONSE_CHUNK_SIZE = int(os.environ.get("FILE_RESPONSE_CHUNK_SIZE", str(1024 * 1024)))
WEB_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("WEB_REQUEST_TIMEOUT", "60"))
SYNC_BOOKS_TTL_SECONDS = float(os.environ.get("SYNC_BOOKS_TTL_SECONDS", "5"))
OUTPUTS_CACHE_TTL_SECONDS = float(os.environ.get("OUTPUTS_CACHE_TTL_SECONDS", "5"))
SSE_STATE_INTERVAL_SECONDS = float(os.environ.get("SSE_STATE_INTERVAL_SECONDS", "3"))
LAST_BOOK_SYNC_AT = 0.0
OUTPUTS_CACHE = {}


def _state_path():
    return os.path.join(get_base_dir(), "results", "web_manager_state.json")


def _static_dir():
    """前端构建产物目录"""
    return os.path.join(get_base_dir(), "frontend", "dist")


def _static_file_path(path):
    """安全解析静态文件路径，防止目录穿越"""
    base = os.path.abspath(_static_dir())
    if not os.path.isdir(base):
        return None
    # 去掉开头的 /
    rel = path.lstrip("/")
    target = os.path.abspath(os.path.join(base, rel))
    # 安全检查：确保在 base 目录内
    if not target.startswith(base + os.sep) and target != base:
        return None
    if os.path.isfile(target):
        return target
    return None


def _serve_index_html():
    """返回前端入口 HTML"""
    index_path = os.path.join(_static_dir(), "index.html")
    if os.path.isfile(index_path):
        with open(index_path, "rb") as f:
            return f.read()
    return None


def _task_log_dir():
    path = os.path.join(get_base_dir(), "results", "web_logs")
    os.makedirs(path, exist_ok=True)
    return path


def _task_log_path(task_id):
    return os.path.join(_task_log_dir(), f"{task_id}.log")


class TimeoutHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, request_timeout=WEB_REQUEST_TIMEOUT_SECONDS, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_timeout = request_timeout

    def get_request(self):
        request, client_address = super().get_request()
        if self.request_timeout and self.request_timeout > 0:
            request.settimeout(self.request_timeout)
        return request, client_address


def _novels_dir():
    path = os.path.join(get_base_dir(), "novels")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_filename(name):
    base = os.path.basename(name or "").strip() or "novel.txt"
    if not base.lower().endswith(".txt"):
        base += ".txt"
    return "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in base)


def _save_upload_file(file_item, dest_path):
    size = 0
    try:
        with open(dest_path, "wb") as f:
            while True:
                chunk = file_item.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise ValueError(f"file too large, max {MAX_UPLOAD_SIZE} bytes")
                f.write(chunk)
    except Exception:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise
    return size


def _validate_upload_target(book_id, path, overwrite=False):
    existing_book = STATE["books"].get(book_id)
    if os.path.exists(path) and not overwrite:
        return False, "file already exists"
    if overwrite and existing_book and existing_book.get("status") in {"queued", "running"}:
        return False, "book is queued or running"
    return True, ""


def _load_state():
    global STATE
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                STATE = {"books": data.get("books", {}) or {}, "tasks": data.get("tasks", []) or []}
        except Exception:
            pass
    _recover_incomplete_tasks()
    _sync_books_from_disk()


def _save_state():
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _recover_incomplete_tasks():
    with STATE_LOCK:
        active_book_ids = set()
        for task in STATE.get("tasks", []):
            if task.get("status") == "running":
                task["status"] = "interrupted"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["error"] = "Web 管理端重启，运行中的任务已中断，请重新加入队列"
                continue
            if task.get("status") == "queued":
                task.setdefault("message", "服务重启后恢复排队")
                _put_task_queue(task.get("id"))
                active_book_ids.add(task.get("book_id"))

        for book_id, book in STATE.get("books", {}).items():
            if book_id in active_book_ids:
                book["status"] = "queued"
                book["message"] = "排队中"
            elif book.get("status") == "running":
                book["status"] = "interrupted"
                book["message"] = "Web 管理端重启，任务已中断"


def _book_id_from_path(path):
    return os.path.splitext(os.path.basename(path))[0]


def _profile_suggestions(path, book_name):
    try:
        return infer_profile_candidates_for_novel(path, book_name, min_score=1)[:8]
    except Exception as exc:
        return [{"name": "general", "display_name": "通用小说分析", "score": 0, "confidence": 1.0, "matched_keywords": [], "error": str(exc)}]


def _valid_profile_names():
    return {item["name"] for item in profile_options(include_auto=True)}


def _normalize_web_profile(value):
    if isinstance(value, list):
        profiles = []
        for item in value:
            profile_name = _normalize_web_profile(item)
            if not profile_name:
                continue
            if profile_name == "auto":
                return ["auto"]
            if profile_name not in profiles:
                profiles.append(profile_name)
        return profiles or None
    profile_name = normalize_profile_name(value or "auto")
    if profile_name not in _valid_profile_names():
        return None
    return profile_name


def _profile_display_value(value):
    if isinstance(value, list):
        return "、".join(value)
    return value or "auto"


def _refresh_book_suggestions(book):
    if not book or not book.get("path") or not os.path.exists(book.get("path")):
        return
    if book.get("status") in {"queued", "running"}:
        return
    signature = _book_suggestion_signature(book["path"])
    if book.get("suggestion_signature") == signature and book.get("profile_suggestions"):
        return
    book["profile_suggestions"] = _profile_suggestions(book["path"], book.get("name", ""))
    book["suggestion_signature"] = signature


def _book_suggestion_signature(path):
    stat = os.stat(path)
    return f"{stat.st_mtime}:{stat.st_size}"


def _sync_books_from_disk():
    global LAST_BOOK_SYNC_AT
    now = time.monotonic()
    if LAST_BOOK_SYNC_AT and now - LAST_BOOK_SYNC_AT < SYNC_BOOKS_TTL_SECONDS:
        return
    discovered = []
    for root, _dirs, files in os.walk(_novels_dir()):
        for filename in files:
            if not filename.lower().endswith(".txt"):
                continue
            path = os.path.join(root, filename)
            discovered.append((path, _book_id_from_path(path)))
    refresh_jobs = []
    with STATE_LOCK:
        for path, book_id in discovered:
            entry = STATE["books"].setdefault(book_id, {})
            entry.setdefault("id", book_id)
            entry.setdefault("name", book_id)
            entry["path"] = path
            entry.setdefault("profile", "auto")
            entry.setdefault("status", "idle")
            entry.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            if entry.get("status") not in {"queued", "running"}:
                try:
                    signature = _book_suggestion_signature(path)
                except OSError:
                    continue
                if entry.get("suggestion_signature") != signature or not entry.get("profile_suggestions"):
                    refresh_jobs.append((book_id, path, entry.get("name", ""), signature))
        _save_state()

    refreshed = []
    for book_id, path, book_name, signature in refresh_jobs:
        refreshed.append((book_id, signature, _profile_suggestions(path, book_name)))

    with STATE_LOCK:
        for book_id, signature, suggestions in refreshed:
            entry = STATE["books"].get(book_id)
            if not entry or entry.get("status") in {"queued", "running"}:
                continue
            try:
                if _book_suggestion_signature(entry.get("path")) != signature:
                    continue
            except (OSError, TypeError):
                continue
            entry["profile_suggestions"] = suggestions
            entry["suggestion_signature"] = signature
        _save_state()
        LAST_BOOK_SYNC_AT = now


def _public_state():
    with STATE_LOCK:
        books = _with_queue_positions(sorted(STATE["books"].values(), key=lambda x: x.get("created_at", ""), reverse=True))
        tasks = _with_queue_positions(list(STATE["tasks"]))
    return {
        "books": books,
        "tasks": tasks,
        "config_ready": CONFIG_READY,
        "config": _runtime_config_summary(),
        "profiles": profile_options(include_auto=True),
    }


def _runtime_config_summary():
    key_pool = [key for key in os.environ.get("API_KEY_POOL", "").split(",") if key.strip()]
    has_single_key = bool(os.environ.get("API_KEY", "").strip())
    return {
        "base_url": os.environ.get("BASE_URL", ""),
        "model_name": os.environ.get("MODEL_NAME", ""),
        "analysis_profile": os.environ.get("ANALYSIS_PROFILE", ""),
        "max_workers": os.environ.get("MAX_WORKERS", ""),
        "rpm_limit": os.environ.get("RPM_LIMIT", ""),
        "tpm_limit": os.environ.get("TPM_LIMIT", ""),
        "rate_limit_scope": os.environ.get("RATE_LIMIT_SCOPE", "global"),
        "api_key_configured": bool(key_pool or has_single_key),
        "api_key_count": len(key_pool) if key_pool else (1 if has_single_key else 0),
        "web": {
            "max_upload_size": MAX_UPLOAD_SIZE,
            "max_json_body_size": MAX_JSON_BODY_SIZE,
            "file_response_chunk_size": FILE_RESPONSE_CHUNK_SIZE,
            "request_timeout": WEB_REQUEST_TIMEOUT_SECONDS,
            "cors_allow_origin": os.environ.get("WEB_CORS_ALLOW_ORIGIN", "*"),
            "sync_books_ttl_seconds": SYNC_BOOKS_TTL_SECONDS,
            "outputs_cache_ttl_seconds": OUTPUTS_CACHE_TTL_SECONDS,
            "sse_state_interval_seconds": SSE_STATE_INTERVAL_SECONDS,
        },
    }


def _put_task_queue(task_id):
    if not task_id or task_id in TASK_QUEUE_IDS:
        return False
    TASK_QUEUE_IDS.add(task_id)
    TASK_QUEUE.put(task_id)
    return True


def _queued_task_positions():
    queued = [task for task in STATE.get("tasks", []) if task.get("status") == "queued"]
    queued.sort(key=_queued_task_sort_key)
    return {task.get("id"): index + 1 for index, task in enumerate(queued)}


def _queued_task_sort_key(task):
    try:
        queue_order = float(task.get("queue_order"))
    except (TypeError, ValueError):
        queue_order = float("inf")
    return (queue_order, task.get("created_at", ""), task.get("id", ""))


def _next_queue_order_locked():
    orders = []
    for task in STATE.get("tasks", []):
        if task.get("status") != "queued":
            continue
        try:
            orders.append(float(task.get("queue_order")))
        except (TypeError, ValueError):
            continue
    return (max(orders) + 1) if orders else time.time()


def _reorder_task_queue_locked():
    desired = [
        task.get("id")
        for task in sorted(STATE.get("tasks", []), key=_queued_task_sort_key)
        if task.get("status") == "queued" and task.get("id") in TASK_QUEUE_IDS
    ]
    with TASK_QUEUE.mutex:
        current = list(TASK_QUEUE.queue)
        desired_set = set(desired)
        current_set = set(current)
        reordered = [task_id for task_id in desired if task_id in current_set]
        reordered.extend(task_id for task_id in current if task_id not in desired_set)
        TASK_QUEUE.queue.clear()
        TASK_QUEUE.queue.extend(reordered)


def _renumber_queued_tasks_locked(queued):
    for index, task in enumerate(queued):
        task["queue_order"] = index


def _with_queue_positions(items):
    positions = _queued_task_positions()
    out = []
    for item in items:
        copied = dict(item)
        task_id = copied.get("task_id") or copied.get("id")
        if task_id in positions and copied.get("status") == "queued":
            copied["queue_position"] = positions[task_id]
            copied["message"] = f"排队中（第 {positions[task_id]} 位）"
        out.append(copied)
    return out


def _is_safe_public_file(path):
    if not path:
        return False
    base = os.path.abspath(get_base_dir())
    allowed = [
        os.path.abspath(os.path.join(base, "results")),
        os.path.abspath(os.path.join(base, "novels")),
    ]
    ap = os.path.abspath(path)
    return any(ap == root or ap.startswith(root + os.sep) for root in allowed) and os.path.isfile(ap)


def _is_safe_novel_file(path):
    if not path:
        return False
    root = os.path.abspath(_novels_dir())
    ap = os.path.abspath(path)
    return (ap == root or ap.startswith(root + os.sep)) and os.path.isfile(ap)


def _file_link(path):
    if not _is_safe_public_file(path):
        return None
    return {"path": path, "name": os.path.basename(path), "url": f"/files?path={quote(path)}"}


def _read_json_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _add_output_link(outputs_by_path, path, kind=None):
    link = _file_link(path)
    if not link:
        return
    try:
        link["mtime"] = os.path.getmtime(path)
    except OSError:
        return
    ap = os.path.abspath(path)
    if kind:
        link["kind"] = kind
    elif ap in outputs_by_path and outputs_by_path[ap].get("kind"):
        link["kind"] = outputs_by_path[ap]["kind"]
    outputs_by_path[ap] = link


def _checkpoint_report_outputs(book_id):
    checkpoint_path = os.path.join(get_base_dir(), "results", "report_checkpoint.json")
    data = _read_json_file(checkpoint_path)
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs", {})
    if not isinstance(jobs, dict):
        return []

    paths = []
    for job_key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if job.get("book_key") != book_id and not str(job_key).endswith(f"::{book_id}") and job_key != book_id:
            continue
        out_file = job.get("out_file")
        if out_file:
            paths.append(out_file)
    return paths


def _outputs_cache_key(book_id):
    results_dir = os.path.join(get_base_dir(), "results")
    return (os.path.abspath(results_dir), book_id)


def _invalidate_book_outputs(book_id):
    if not book_id:
        return
    OUTPUTS_CACHE.pop(_outputs_cache_key(book_id), None)


def _find_book_outputs(book_id):
    now = time.monotonic()
    results_dir = os.path.join(get_base_dir(), "results")
    cache_key = _outputs_cache_key(book_id)
    cached = OUTPUTS_CACHE.get(cache_key)
    if cached and now - cached["time"] < OUTPUTS_CACHE_TTL_SECONDS:
        return cached["outputs"]

    outputs_by_path = {}
    if not os.path.isdir(results_dir):
        OUTPUTS_CACHE.pop(cache_key, None)
        return []

    for path in _checkpoint_report_outputs(book_id):
        _add_output_link(outputs_by_path, path, "final_report")

    profile_names = [profile.name for profile in list_available_profiles()]
    filename_patterns = [
        f"{book_id}扫书报告",
        f"《{book_id}》扫书报告",
        f"{book_id}通用小说报告",
        f"《{book_id}》通用小说报告",
        f"{book_id}_GENERAL_SUMMARY_latest.json",
    ]
    filename_patterns.extend(f"{book_id}_{name}_GENERAL_SUMMARY_latest.json" for name in profile_names if name != "general")
    scan_dir_outputs = {"GENERAL_SUMMARY.json", "VERIFIED_REPORT.txt", "FULL_REPORT.txt"}
    output_exts = {".txt", ".json", ".log", ".md", ".csv"}

    for root, _dirs, files in os.walk(results_dir):
        parent = os.path.basename(root)
        in_book_dir = book_id in parent
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in output_exts:
                continue
            path = os.path.join(root, filename)
            matched = (
                any(pattern in filename for pattern in filename_patterns)
                or book_id in filename
                or (in_book_dir and (filename in scan_dir_outputs or ext in {".txt", ".json"}))
            )
            if matched:
                _add_output_link(outputs_by_path, path)

    outputs = list(outputs_by_path.values())
    outputs.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    outputs = outputs[:100]
    OUTPUTS_CACHE[cache_key] = {"time": now, "outputs": outputs}
    return outputs


def _safe_token_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _book_token_usage(book_id, max_runs=5):
    path = os.path.join(get_base_dir(), "results", "token_usage.json")
    data = _read_json_file(path)
    if not isinstance(data, dict):
        return None
    books = data.get("books")
    if not isinstance(books, dict):
        return None
    book_entry = books.get(book_id)
    if not isinstance(book_entry, dict):
        return None

    runs = []
    run_entries = book_entry.get("runs", {})
    if isinstance(run_entries, dict):
        for run_id, run in run_entries.items():
            if not isinstance(run, dict):
                continue
            scripts = run.get("scripts", {})
            script_count = len(scripts) if isinstance(scripts, dict) else 0
            runs.append({
                "run_id": run.get("run_id") or run_id,
                "input": _safe_token_int(run.get("run_total_input")),
                "output": _safe_token_int(run.get("run_total_output")),
                "total": _safe_token_int(run.get("run_total_tokens")),
                "started_at": run.get("started_at", ""),
                "updated_at": run.get("updated_at", ""),
                "script_count": script_count,
            })

    runs.sort(key=lambda item: item.get("updated_at") or item.get("started_at") or "", reverse=True)
    return {
        "book_name": book_entry.get("book_name") or book_id,
        "input": _safe_token_int(book_entry.get("book_total_input")),
        "output": _safe_token_int(book_entry.get("book_total_output")),
        "total": _safe_token_int(book_entry.get("book_total_tokens")),
        "updated_at": book_entry.get("updated_at", ""),
        "runs": runs[:max_runs],
        "run_count": len(runs),
    }


def _book_detail(book_id):
    with STATE_LOCK:
        book = dict(STATE["books"].get(book_id) or {})
        tasks = [dict(t) for t in STATE["tasks"] if t.get("book_id") == book_id]
    if not book:
        return None
    for task in tasks:
        if task.get("log_path"):
            task["log_file"] = _file_link(task.get("log_path"))
    book["novel_file"] = _file_link(book.get("path"))
    _refresh_book_suggestions(book)
    book["outputs"] = _find_book_outputs(book_id)
    book["token_usage"] = _book_token_usage(book_id)
    book["tasks"] = sorted(_with_queue_positions(tasks), key=lambda x: x.get("created_at", ""), reverse=True)
    book["profiles"] = profile_options(include_auto=True)
    return book


def _enqueue(book_id):
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") in {"queued", "running"}:
            return False, "book already queued or running"
        _refresh_book_suggestions(book)
        task_id = uuid.uuid4().hex[:12]
        profile_name = _normalize_web_profile(book.get("profile", "auto")) or "auto"
        task = {
            "id": task_id,
            "book_id": book_id,
            "profile": profile_name,
            "profile_suggestions": book.get("profile_suggestions", []),
            "status": "queued",
            "queue_order": _next_queue_order_locked(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        STATE["tasks"].append(task)
        book["status"] = "queued"
        book["task_id"] = task_id
        book["message"] = "排队中"
        _save_state()
    _put_task_queue(task_id)
    return True, task_id


def _enqueue_many(book_ids):
    requested = []
    for book_id in book_ids or []:
        if not book_id or book_id in requested:
            continue
        requested.append(book_id)

    queued = []
    skipped = []
    for book_id in requested:
        ok, result = _enqueue(book_id)
        if ok:
            queued.append({"book_id": book_id, "task_id": result})
        else:
            skipped.append({"book_id": book_id, "reason": result})
    return {"queued": queued, "skipped": skipped}


def _prioritize_queued_book(book_id):
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") != "queued":
            return False, "book is not queued"
        task = _find_task(book.get("task_id"))
        if not task or task.get("status") != "queued":
            return False, "queued task not found"
        queued = sorted(
            [item for item in STATE.get("tasks", []) if item.get("status") == "queued"],
            key=_queued_task_sort_key,
        )
        queued = [item for item in queued if item.get("id") != task.get("id")]
        queued.insert(0, task)
        _renumber_queued_tasks_locked(queued)
        task["message"] = "已置顶排队"
        _reorder_task_queue_locked()
        _save_state()
    return True, task.get("id")


def _move_queued_book(book_id, direction):
    if direction not in {"up", "down"}:
        return False, "invalid direction"
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") != "queued":
            return False, "book is not queued"
        task = _find_task(book.get("task_id"))
        if not task or task.get("status") != "queued":
            return False, "queued task not found"
        queued = sorted([item for item in STATE.get("tasks", []) if item.get("status") == "queued"], key=_queued_task_sort_key)
        task_ids = [item.get("id") for item in queued]
        try:
            index = task_ids.index(task.get("id"))
        except ValueError:
            return False, "queued task not found"
        new_index = index - 1 if direction == "up" else index + 1
        if new_index < 0 or new_index >= len(queued):
            return False, "already at boundary"
        queued[index], queued[new_index] = queued[new_index], queued[index]
        _renumber_queued_tasks_locked(queued)
        task["message"] = "已调整排队顺序"
        _reorder_task_queue_locked()
        _save_state()
    return True, task.get("id")


def _cancel_queued_book(book_id):
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") != "queued":
            return False, "book is not queued"
        task_id = book.get("task_id")
        task = _find_task(task_id)
        if not task or task.get("status") != "queued":
            return False, "queued task not found"
        task["status"] = "canceled"
        task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        task["error"] = "用户取消排队"
        book["status"] = "idle"
        book["message"] = "已取消排队"
        book.pop("task_id", None)
        TASK_QUEUE_IDS.discard(task_id)
        _save_state()
    return True, task_id


def _delete_book(book_id):
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") in {"queued", "running"}:
            return False, "book is queued or running"
        path = book.get("path")
        if not _is_safe_novel_file(path):
            return False, "novel file is not allowed"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return False, str(exc)
        STATE["books"].pop(book_id, None)
        for task in STATE.get("tasks", []):
            if task.get("book_id") == book_id:
                task["book_deleted"] = True
        _invalidate_book_outputs(book_id)
        _save_state()
    return True, book_id


def _web_scan_command(book_path, profile_name, run_id):
    task_args = [
        "--web-scan-task",
        "--novel-path",
        book_path,
        "--profile-json",
        json.dumps(profile_name, ensure_ascii=False),
        "--run-id",
        run_id,
        "--skip-fresh",
    ]
    if getattr(sys, "frozen", False):
        return [sys.executable, *task_args]
    return [sys.executable, os.path.join(get_base_dir(), "main.py"), *task_args]


def _run_scan_subprocess(book_path, profile_name, run_id, log_file):
    cmd = _web_scan_command(book_path, profile_name, run_id)
    proc = subprocess.Popen(
        cmd,
        cwd=get_base_dir(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    result = None
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith(_WEB_SCAN_RESULT_PREFIX):
            payload = line[len(_WEB_SCAN_RESULT_PREFIX):].strip()
            try:
                result = json.loads(payload)
            except json.JSONDecodeError as exc:
                result = {"status": "fail", "error": f"invalid scan result: {exc}"}
            continue
        log_file.write(line)
        log_file.flush()
    return_code = proc.wait()
    if result is None:
        result = {"status": "fail", "error": f"scan process exited without result (code {return_code})"}
    elif return_code != 0 and result.get("status") in {"ok", "skipped"}:
        result = dict(result)
        result["status"] = "fail"
        result["error"] = f"scan process exited with code {return_code}"
    return result


def _find_task(task_id):
    for task in STATE["tasks"]:
        if task.get("id") == task_id:
            return task
    return None


def _worker_loop():
    while True:
        task_id = TASK_QUEUE.get()
        with STATE_LOCK:
            TASK_QUEUE_IDS.discard(task_id)
        with STATE_LOCK:
            task = _find_task(task_id)
            if not task:
                TASK_QUEUE.task_done()
                continue
            if task.get("status") == "canceled":
                TASK_QUEUE.task_done()
                continue
            book = STATE["books"].get(task.get("book_id"))
            if not book:
                task["status"] = "failed"
                task["error"] = "book missing"
                _save_state()
                TASK_QUEUE.task_done()
                continue
            task["status"] = "running"
            task["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            task["log_path"] = _task_log_path(task_id)
            book["status"] = "running"
            book["message"] = "扫描中"
            _save_state()

        try:
            with open(task["log_path"], "a", encoding="utf-8") as log_file:
                log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] task {task_id} started\n")
                log_file.flush()
                ok, config_error = _try_load_runtime_config("scan")
                if not ok:
                    raise RuntimeError(config_error)
                profile_name = task.get("profile", "auto")
                if profile_name == "auto":
                    suggestions = infer_profile_candidates_for_novel(book["path"], book.get("name", ""), min_score=1)
                    task["profile_suggestions"] = suggestions
                    resolved_profiles = infer_profiles_for_novel(book["path"], book.get("name", ""))
                    task["resolved_profiles"] = resolved_profiles
                    task["resolved_profile"] = "、".join(resolved_profiles)
                elif isinstance(profile_name, list):
                    task["resolved_profiles"] = profile_name
                    task["resolved_profile"] = "、".join(profile_name)
                result = _run_scan_subprocess(book["path"], profile_name, _generate_run_id(), log_file)
            with STATE_LOCK:
                task["status"] = "completed" if result.get("status") in {"ok", "skipped"} else "failed"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["result"] = result
                book["status"] = task["status"]
                active_profiles = result.get("profiles")
                if not active_profiles:
                    fallback_profile = result.get("profile") or profile_name
                    active_profiles = fallback_profile if isinstance(fallback_profile, list) else [fallback_profile]
                book["active_profile"] = result.get("profile", _profile_display_value(profile_name))
                book["active_profiles"] = active_profiles
                book["profile_suggestions"] = task.get("profile_suggestions", book.get("profile_suggestions", []))
                book["message"] = "完成" if task["status"] == "completed" else result.get("error", "失败")
                _invalidate_book_outputs(task.get("book_id"))
                _save_state()
        except Exception as exc:
            try:
                with open(task.get("log_path") or _task_log_path(task_id), "a", encoding="utf-8") as log_file:
                    log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {exc}\n")
            except Exception:
                pass
            with STATE_LOCK:
                task["status"] = "failed"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["error"] = str(exc)
                book["status"] = "failed"
                book["message"] = str(exc)
                _invalidate_book_outputs(task.get("book_id"))
                _save_state()
        finally:
            TASK_QUEUE.task_done()


def _start_worker_once():
    global WORKER_STARTED
    if WORKER_STARTED:
        return
    WORKER_STARTED = True
    thread = threading.Thread(target=_worker_loop, daemon=True)
    thread.start()


def _try_load_runtime_config(interactive_context: str = "web"):
    global CONFIG_READY
    try:
        load_configs(get_base_dir(), interactive=False)
        CONFIG_READY = True
        return True, ""
    except BaseException as exc:
        CONFIG_READY = False
        msg = f"{interactive_context} runtime config not ready: {exc}"
        print(f"[WARN] {msg}")
        return False, msg


class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", os.environ.get("WEB_CORS_ALLOW_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")
        super().end_headers()

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_payload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"error": "invalid content length"}, 400)
            return None
        if length < 0:
            self._send_json({"error": "invalid content length"}, 400)
            return None
        if length > MAX_JSON_BODY_SIZE:
            self._send_json({"error": f"json body too large, max {MAX_JSON_BODY_SIZE} bytes"}, 413)
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid json"}, 400)
            return None

    def _guess_mime(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".ttf": "font/ttf",
        }.get(ext, "application/octet-stream")

    def _serve_static(self, path):
        file_path = _static_file_path(path)
        if not file_path:
            return False
        try:
            with open(file_path, "rb") as f:
                body = f.read()
        except OSError:
            return False
        self.send_response(200)
        self.send_header("Content-Type", self._guess_mime(file_path))
        self.send_header("Content-Length", str(len(body)))
        # 静态资源可加缓存
        if "/assets/" in path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _send_sse_state_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        while True:
            try:
                _sync_books_from_disk()
                payload = json.dumps(_public_state(), ensure_ascii=False)
                self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(SSE_STATE_INTERVAL_SECONDS)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            except OSError:
                return

    def _send_public_file(self, path):
        if not _is_safe_public_file(path):
            self.send_error(403, "file is not allowed")
            return
        try:
            size = os.path.getsize(path)
            content_type = "application/json; charset=utf-8" if path.lower().endswith(".json") else "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(FILE_RESPONSE_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        # 静态文件和前端入口
        if parsed.path == "/":
            body = _serve_index_html()
            if body is None:
                self.send_error(503, "frontend build not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 尝试作为静态文件服务（前端构建产物中的 js/css 等）
        if self._serve_static(parsed.path):
            return
        if parsed.path == "/healthz":
            self._send_json({"ok": True, "config_ready": CONFIG_READY})
            return
        if parsed.path == "/api/state":
            _sync_books_from_disk()
            self._send_json(_public_state())
            return
        if parsed.path == "/api/events":
            self._send_sse_state_stream()
            return
        if parsed.path == "/api/book":
            params = parse_qs(parsed.query)
            book_id = (params.get("id") or [""])[0]
            detail = _book_detail(book_id)
            if not detail:
                self._send_json({"error": "book not found"}, 404)
                return
            self._send_json(detail)
            return
        if parsed.path == "/files":
            params = parse_qs(parsed.query)
            path = unquote((params.get("path") or [""])[0])
            self._send_public_file(path)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/profile":
            payload = self._read_json_payload()
            if payload is None:
                return
            with STATE_LOCK:
                book = STATE["books"].get(payload.get("book_id"))
                if not book:
                    self._send_json({"error": "book not found"}, 404)
                    return
                if book.get("status") in {"queued", "running"}:
                    self._send_json({"error": "book is queued or running"}, 409)
                    return
                profile_name = _normalize_web_profile(payload.get("profile", "auto"))
                if not profile_name:
                    self._send_json({"error": "invalid profile"}, 400)
                    return
                book["profile"] = profile_name
                book["message"] = "分类已更新"
                _save_state()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/enqueue":
            payload = self._read_json_payload()
            if payload is None:
                return
            ok, result = _enqueue(payload.get("book_id"))
            self._send_json({"ok": ok, "result": result}, 200 if ok else 409)
            return
        if parsed.path == "/api/enqueue-batch":
            payload = self._read_json_payload()
            if payload is None:
                return
            book_ids = payload.get("book_ids")
            if not isinstance(book_ids, list):
                self._send_json({"error": "book_ids must be a list"}, 400)
                return
            result = _enqueue_many(book_ids)
            self._send_json({"ok": bool(result["queued"]), "result": result}, 200)
            return
        if parsed.path == "/api/cancel":
            payload = self._read_json_payload()
            if payload is None:
                return
            ok, result = _cancel_queued_book(payload.get("book_id"))
            self._send_json({"ok": ok, "result": result}, 200 if ok else 409)
            return
        if parsed.path == "/api/prioritize":
            payload = self._read_json_payload()
            if payload is None:
                return
            ok, result = _prioritize_queued_book(payload.get("book_id"))
            self._send_json({"ok": ok, "result": result}, 200 if ok else 409)
            return
        if parsed.path == "/api/move-queue":
            payload = self._read_json_payload()
            if payload is None:
                return
            ok, result = _move_queued_book(payload.get("book_id"), payload.get("direction"))
            self._send_json({"ok": ok, "result": result}, 200 if ok else 409)
            return
        if parsed.path == "/api/delete":
            payload = self._read_json_payload()
            if payload is None:
                return
            ok, result = _delete_book(payload.get("book_id"))
            self._send_json({"ok": ok, "result": result}, 200 if ok else 409)
            return
        if parsed.path == "/upload":
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "invalid content length")
                return
            if content_length > MAX_UPLOAD_SIZE + 1024 * 1024:
                self.send_error(413, f"file too large, max {MAX_UPLOAD_SIZE} bytes")
                return
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")})
            file_item = form["file"] if "file" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                self.send_error(400, "missing file")
                return
            filename = _safe_filename(file_item.filename)
            path = os.path.join(_novels_dir(), filename)
            book_id = _book_id_from_path(path)
            overwrite = str(form.getfirst("overwrite", "")).lower() in {"1", "true", "yes", "on"}
            ok, reason = _validate_upload_target(book_id, path, overwrite=overwrite)
            if not ok and reason == "file already exists":
                self._send_json({"error": "file already exists", "book_id": book_id}, 409)
                return
            if not ok:
                self._send_json({"error": reason}, 409)
                return
            try:
                uploaded_size = _save_upload_file(file_item, path)
            except ValueError as exc:
                self.send_error(413, str(exc))
                return
            profile_values = form.getlist("profile")
            if not profile_values:
                profile_values = [form.getfirst("profile", "auto")]
            profile = _normalize_web_profile(profile_values if len(profile_values) > 1 else profile_values[0]) or "auto"
            _invalidate_book_outputs(book_id)
            suggestions = _profile_suggestions(path, book_id)
            with STATE_LOCK:
                STATE["books"][book_id] = {
                    "id": book_id,
                    "name": book_id,
                    "path": path,
                    "profile": profile,
                    "profile_suggestions": suggestions,
                    "suggestion_signature": f"{os.path.getmtime(path)}:{os.path.getsize(path)}",
                    "status": "idle",
                    "message": f"已上传（{uploaded_size} 字节）",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _save_state()
            self._send_json({"ok": True, "book_id": book_id})
            return
        self.send_error(404)


def run_server(host="127.0.0.1", port=8765):
    _try_load_runtime_config("web")
    _load_state()
    _start_worker_once()
    server = TimeoutHTTPServer((host, int(port)), Handler)
    print(f"Web 管理端已启动: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8765"))
    run_server(host, port)
