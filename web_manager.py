import json
import logging
import os
import queue
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning, message="'cgi' is deprecated*")
import cgi
import sys

from analysis_profiles import (
    PROFILE_INFERENCE_ALGORITHM_VERSION,
    infer_profile_candidates_for_novel,
    infer_profiles_for_novel,
    list_available_profiles,
    normalize_profile_name,
    profile_options,
)
from main import _WEB_SCAN_RESULT_PREFIX, _generate_run_id, get_base_dir, get_novels_dir, load_configs
from shared_utils import configure_rotating_file_logger, read_float_env, read_int_env


STATE_LOCK = threading.RLock()
RUNTIME_CONFIG_LOCK = threading.RLock()
STORAGE_HEALTH_LOCK = threading.RLock()
TASK_QUEUE = queue.Queue()
TASK_QUEUE_IDS = set()
RUNNING_TASK_PROCS = {}
WORKER_STARTED = False
STATE = {"books": {}, "tasks": []}
CONFIG_READY = False
MAX_UPLOAD_SIZE = read_int_env("MAX_UPLOAD_SIZE", 100 * 1024 * 1024, min_value=1)
MAX_JSON_BODY_SIZE = read_int_env("MAX_JSON_BODY_SIZE", 64 * 1024, min_value=1)
FILE_RESPONSE_CHUNK_SIZE = read_int_env("FILE_RESPONSE_CHUNK_SIZE", 1024 * 1024, min_value=1)
WEB_REQUEST_TIMEOUT_SECONDS = read_float_env("WEB_REQUEST_TIMEOUT", 60.0, min_value=0.0)
SYNC_BOOKS_TTL_SECONDS = read_float_env("SYNC_BOOKS_TTL_SECONDS", 5.0, min_value=0.0)
OUTPUTS_CACHE_TTL_SECONDS = read_float_env("OUTPUTS_CACHE_TTL_SECONDS", 5.0, min_value=0.0)
SSE_STATE_INTERVAL_SECONDS = read_float_env("SSE_STATE_INTERVAL_SECONDS", 3.0, min_value=0.0)
SSE_SYNC_INTERVAL_SECONDS = read_float_env(
    "SSE_SYNC_INTERVAL_SECONDS",
    max(SYNC_BOOKS_TTL_SECONDS, SSE_STATE_INTERVAL_SECONDS),
    min_value=0.0,
)
SSE_MAX_CONNECTION_SECONDS = read_float_env("SSE_MAX_CONNECTION_SECONDS", 300.0, min_value=0.0)
SCAN_CANCEL_TIMEOUT_SECONDS = read_float_env("SCAN_CANCEL_TIMEOUT_SECONDS", 5.0, min_value=0.0)
SCAN_HEARTBEAT_INTERVAL_SECONDS = read_float_env("SCAN_HEARTBEAT_INTERVAL_SECONDS", 10.0, min_value=0.0)
SCAN_STALL_TIMEOUT_SECONDS = read_float_env("SCAN_STALL_TIMEOUT_SECONDS", 1200.0, min_value=0.0)
SCAN_FUTURE_STALL_TIMEOUT_SECONDS = read_float_env("SCAN_FUTURE_STALL_TIMEOUT_SECONDS", 0.0, min_value=0.0)
SCAN_LOG_DEGRADED_MIN_SECONDS = read_float_env("SCAN_LOG_DEGRADED_MIN_SECONDS", 3600.0, min_value=0.0)
SCAN_LOG_DEGRADED_JSON_PARSE_THRESHOLD = read_int_env("SCAN_LOG_DEGRADED_JSON_PARSE_THRESHOLD", 50, min_value=0)
SCAN_LOG_DEGRADED_504_THRESHOLD = read_int_env("SCAN_LOG_DEGRADED_504_THRESHOLD", 20, min_value=0)
SCAN_LOG_DEGRADED_LEGACY_TOKEN_THRESHOLD = read_int_env("SCAN_LOG_DEGRADED_LEGACY_TOKEN_THRESHOLD", 50, min_value=0)
STORAGE_HEALTH_TTL_SECONDS = read_float_env("STORAGE_HEALTH_TTL_SECONDS", 10.0, min_value=0.0)
APP_VERSION = os.environ.get("APP_VERSION", "dev").strip() or "dev"
APP_COMMIT = os.environ.get("APP_COMMIT", "").strip() or "unknown"
APP_BUILD_DATE = os.environ.get("APP_BUILD_DATE", "").strip()
LAST_BOOK_SYNC_AT = 0.0
LAST_SSE_SYNC_AT = 0.0
OUTPUTS_CACHE = {}
STORAGE_HEALTH_CACHE = {"base_dir": None, "checked_at": 0.0, "summary": None}
ACCESS_LOGGER = None
EDITABLE_RUNTIME_CONFIG = {
    "max_workers": {"env": "MAX_WORKERS", "type": "int", "min": 1, "max": 64},
    "rpm_limit": {"env": "RPM_LIMIT", "type": "int", "min": 0, "max": 1000000, "empty": True},
    "tpm_limit": {"env": "TPM_LIMIT", "type": "int", "min": 0, "max": 1000000000, "empty": True},
    "rate_limit_scope": {"env": "RATE_LIMIT_SCOPE", "type": "choice", "choices": {"auto", "global", "per_key"}},
    "api_server_error_max_retries": {"env": "API_SERVER_ERROR_MAX_RETRIES", "type": "int", "min": 1, "max": 10},
    "api_server_error_fast_fail_input_chars": {"env": "API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS", "type": "int", "min": 0, "max": 1000000},
    "harem_scan_chunk_size": {"env": "HAREM_SCAN_CHUNK_SIZE", "type": "int", "min": 1000, "max": 20000},
    "harem_scan_max_tokens": {"env": "HAREM_SCAN_MAX_TOKENS", "type": "int", "min": 500, "max": 8000},
    "harem_scan_retry_workers": {"env": "HAREM_SCAN_RETRY_WORKERS", "type": "int", "min": 1, "max": 16},
    "harem_scan_api_downshift_max_depth": {"env": "HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH", "type": "int", "min": 0, "max": 4},
    "harem_scan_api_downshift_min_chars": {"env": "HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS", "type": "int", "min": 0, "max": 50000},
    "general_scan_max_chunks": {"env": "GENERAL_SCAN_MAX_CHUNKS", "type": "int", "min": 0, "max": 100000},
    "general_character_max_tokens": {"env": "GENERAL_CHARACTER_MAX_TOKENS", "type": "int", "min": 500, "max": 8000},
    "general_character_retry_max_tokens": {"env": "GENERAL_CHARACTER_RETRY_MAX_TOKENS", "type": "int", "min": 500, "max": 8000},
    "general_character_max_per_chunk": {"env": "GENERAL_CHARACTER_MAX_PER_CHUNK", "type": "int", "min": 3, "max": 30},
    "general_character_api_downshift_max_depth": {"env": "GENERAL_CHARACTER_API_DOWNSHIFT_MAX_DEPTH", "type": "int", "min": 0, "max": 3},
    "general_character_api_downshift_min_chars": {"env": "GENERAL_CHARACTER_API_DOWNSHIFT_MIN_CHARS", "type": "int", "min": 500, "max": 50000},
    "general_scan_smart_density": {"env": "GENERAL_SCAN_SMART_DENSITY", "type": "bool"},
    "general_scan_content_aware_sampling": {"env": "GENERAL_SCAN_CONTENT_AWARE_SAMPLING", "type": "bool"},
    "general_scan_incremental_reuse": {"env": "GENERAL_SCAN_INCREMENTAL_REUSE", "type": "bool"},
    "general_scan_writing_quality": {"env": "GENERAL_SCAN_WRITING_QUALITY", "type": "bool"},
    "general_scan_narrative_architecture": {"env": "GENERAL_SCAN_NARRATIVE_ARCHITECTURE", "type": "bool"},
    "general_scan_foreshadowing_engineering": {"env": "GENERAL_SCAN_FORESHADOWING_ENGINEERING", "type": "bool"},
    "general_scan_semantic_layers": {"env": "GENERAL_SCAN_SEMANTIC_LAYERS", "type": "bool"},
    "general_scan_reader_experience": {"env": "GENERAL_SCAN_READER_EXPERIENCE", "type": "bool"},
    "general_scan_continuity_audit": {"env": "GENERAL_SCAN_CONTINUITY_AUDIT", "type": "bool"},
    "general_scan_rolling_context": {"env": "GENERAL_SCAN_ROLLING_CONTEXT", "type": "bool"},
    "general_scan_entity_prescan": {"env": "GENERAL_SCAN_ENTITY_PRESCAN", "type": "bool"},
    "general_scan_knowledge_base_llm_merge": {"env": "GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE", "type": "bool"},
    "general_scan_context_max_chars": {"env": "GENERAL_SCAN_CONTEXT_MAX_CHARS", "type": "int", "min": 0, "max": 10000},
    "rescan_skip_chronic_parse_failure_after": {"env": "RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER", "type": "int", "min": 0, "max": 20},
    "scan_stall_timeout_seconds": {"env": "SCAN_STALL_TIMEOUT_SECONDS", "type": "int", "min": 0, "max": 86400},
    "scan_future_stall_timeout_seconds": {"env": "SCAN_FUTURE_STALL_TIMEOUT_SECONDS", "type": "int", "min": 0, "max": 86400},
    "harem_plus_general_scan": {"env": "HAREM_PLUS_GENERAL_SCAN", "type": "bool"},
}
BOOK_ID_PAYLOAD_SCHEMA = {
    "required": ["book_id"],
    "fields": {"book_id": {"type": str, "non_empty": True}},
}
BOOK_IDS_PAYLOAD_SCHEMA = {
    "required": ["book_ids"],
    "fields": {"book_ids": {"type": list, "item_type": str, "non_empty_items": True}},
}
RETRY_FAILED_PAYLOAD_SCHEMA = {
    "required": [],
    "fields": {"failure_types": {"type": list, "item_type": str, "non_empty_items": True}},
}
PROFILE_PAYLOAD_SCHEMA = {
    "required": ["book_id"],
    "fields": {
        "book_id": {"type": str, "non_empty": True},
        "profile": {"type": (str, list), "item_type": str, "non_empty_items": True},
    },
}
CONFIG_PAYLOAD_SCHEMA = {
    "required": ["config"],
    "fields": {"config": {"type": dict}},
}
MOVE_QUEUE_PAYLOAD_SCHEMA = {
    "required": ["book_id", "direction"],
    "fields": {
        "book_id": {"type": str, "non_empty": True},
        "direction": {"type": str, "choices": {"up", "down"}},
    },
}


def _state_path():
    return os.path.join(get_base_dir(), "results", "web_manager_state.json")


def _static_dir():
    """前端构建产物目录"""
    return os.path.join(get_base_dir(), "frontend", "dist")


def _is_path_inside(path, root):
    try:
        ap = os.path.abspath(path)
        ar = os.path.abspath(root)
        return os.path.commonpath([ap, ar]) == ar
    except (OSError, ValueError):
        return False


def _static_file_path(path):
    """安全解析静态文件路径，防止目录穿越"""
    base = os.path.abspath(_static_dir())
    if not os.path.isdir(base):
        return None
    # 去掉开头的 /
    rel = path.lstrip("/")
    target = os.path.abspath(os.path.join(base, rel))
    # 安全检查：确保在 base 目录内
    if not _is_path_inside(target, base):
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


def _web_access_log_path():
    return os.path.join(_task_log_dir(), "web_access.log")


def _access_logger():
    global ACCESS_LOGGER
    if ACCESS_LOGGER is None:
        ACCESS_LOGGER = logging.getLogger("web_manager.access")
        ACCESS_LOGGER.propagate = False
        configure_rotating_file_logger(ACCESS_LOGGER, _web_access_log_path(), stream=False)
    return ACCESS_LOGGER


def _sanitize_log_path(path):
    parsed = urlparse(path or "")
    if not parsed.query:
        return parsed.path or "/"
    params = parse_qs(parsed.query, keep_blank_values=True)
    for key in list(params.keys()):
        if key.lower() in {"token", "access_token", "web_access_token"}:
            params[key] = ["***"]
    query_parts = []
    for key in sorted(params):
        for value in params[key]:
            query_parts.append(f"{quote(str(key), safe='')}={quote(str(value), safe='')}")
    return parsed.path + ("?" + "&".join(query_parts) if query_parts else "")


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
    path = get_novels_dir(get_base_dir())
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


def _book_source_missing_message(book):
    path = (book or {}).get("path")
    if not path:
        return "源文件路径缺失"
    novels_root = os.path.abspath(_novels_dir())
    ap = os.path.abspath(path)
    if not _is_path_inside(ap, novels_root):
        return ""
    if not os.path.isfile(ap):
        return "源文件不存在，请重新上传小说文件"
    return ""


def _mark_book_source_status(book):
    message = _book_source_missing_message(book)
    if message:
        changed = False
        if book.get("file_missing") is not True:
            book["file_missing"] = True
            changed = True
        if book.get("source_error") != message:
            book["source_error"] = message
            changed = True
        if book.get("status") not in {"queued", "running"} and book.get("message") != message:
            book["message"] = message
            changed = True
        return changed
    changed = False
    for key in ("file_missing", "source_error"):
        if key in book:
            book.pop(key, None)
            changed = True
    if book.get("message") in {"源文件路径缺失", "源文件不存在，请重新上传小说文件"}:
        book.pop("message", None)
        changed = True
    return changed


def _load_state():
    global STATE
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                STATE = {"books": data.get("books", {}) or {}, "tasks": data.get("tasks", []) or []}
        except Exception as exc:
            logging.getLogger("web_manager").warning(
                "读取 Web 状态文件失败，将使用空状态并重新同步目录: %s",
                exc,
            )
    _recover_incomplete_tasks()
    _sync_books_from_disk()


def _save_state():
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    try:
        with STATE_LOCK:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(STATE, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


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
    return f"{stat.st_mtime}:{stat.st_size}:profile-v{PROFILE_INFERENCE_ALGORITHM_VERSION}"


def _sync_books_from_disk():
    global LAST_BOOK_SYNC_AT
    now = time.monotonic()
    if LAST_BOOK_SYNC_AT and now - LAST_BOOK_SYNC_AT < SYNC_BOOKS_TTL_SECONDS:
        return
    discovered = []
    discovered_ids = set()
    for root, _dirs, files in os.walk(_novels_dir()):
        for filename in files:
            if not filename.lower().endswith(".txt"):
                continue
            path = os.path.join(root, filename)
            try:
                signature = _book_suggestion_signature(path)
            except OSError:
                continue
            book_id = _book_id_from_path(path)
            discovered.append((path, book_id, signature))
            discovered_ids.add(book_id)
    refresh_jobs = []
    state_changed = False
    with STATE_LOCK:
        for path, book_id, signature in discovered:
            entry = STATE["books"].get(book_id)
            if entry is None:
                entry = {}
                STATE["books"][book_id] = entry
                state_changed = True
            defaults = {
                "id": book_id,
                "name": book_id,
                "profile": "auto",
                "status": "idle",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for key, value in defaults.items():
                if key not in entry:
                    entry[key] = value
                    state_changed = True
            if entry.get("path") != path:
                entry["path"] = path
                state_changed = True
            if _mark_book_source_status(entry):
                state_changed = True
            if entry.get("status") not in {"queued", "running"}:
                if entry.get("suggestion_signature") != signature or not entry.get("profile_suggestions"):
                    refresh_jobs.append((book_id, path, entry.get("name", ""), signature))
        for book_id, entry in STATE["books"].items():
            if book_id in discovered_ids:
                continue
            if entry.get("status") in {"queued", "running"}:
                continue
            if _mark_book_source_status(entry):
                state_changed = True
    refreshed = []
    for book_id, path, book_name, signature in refresh_jobs:
        refreshed.append((book_id, signature, _profile_suggestions(path, book_name)))

    with STATE_LOCK:
        suggestions_changed = False
        for book_id, signature, suggestions in refreshed:
            entry = STATE["books"].get(book_id)
            if not entry or entry.get("status") in {"queued", "running"}:
                continue
            try:
                if _book_suggestion_signature(entry.get("path")) != signature:
                    continue
            except (OSError, TypeError):
                continue
            if entry.get("profile_suggestions") != suggestions:
                entry["profile_suggestions"] = suggestions
                suggestions_changed = True
            if entry.get("suggestion_signature") != signature:
                entry["suggestion_signature"] = signature
                suggestions_changed = True
        if state_changed or suggestions_changed:
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


def _sync_books_from_disk_for_sse():
    global LAST_SSE_SYNC_AT
    now = time.monotonic()
    with STATE_LOCK:
        if LAST_SSE_SYNC_AT and SSE_SYNC_INTERVAL_SECONDS > 0 and now - LAST_SSE_SYNC_AT < SSE_SYNC_INTERVAL_SECONDS:
            return False
        LAST_SSE_SYNC_AT = now
    _sync_books_from_disk()
    return True


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
        "rate_limit_scope": os.environ.get("RATE_LIMIT_SCOPE", "auto"),
        "api_server_error_max_retries": os.environ.get("API_SERVER_ERROR_MAX_RETRIES", "2"),
        "api_server_error_fast_fail_input_chars": os.environ.get("API_SERVER_ERROR_FAST_FAIL_INPUT_CHARS", "20000"),
        "harem_scan_chunk_size": os.environ.get("HAREM_SCAN_CHUNK_SIZE", "7000"),
        "harem_scan_max_tokens": os.environ.get("HAREM_SCAN_MAX_TOKENS", "3000"),
        "harem_scan_retry_workers": os.environ.get("HAREM_SCAN_RETRY_WORKERS", "1"),
        "harem_scan_api_downshift_max_depth": os.environ.get("HAREM_SCAN_API_DOWNSHIFT_MAX_DEPTH", "1"),
        "harem_scan_api_downshift_min_chars": os.environ.get("HAREM_SCAN_API_DOWNSHIFT_MIN_CHARS", "1200"),
        "general_scan_max_chunks": os.environ.get("GENERAL_SCAN_MAX_CHUNKS", "80"),
        "general_character_max_tokens": os.environ.get("GENERAL_CHARACTER_MAX_TOKENS", "2400"),
        "general_character_retry_max_tokens": os.environ.get("GENERAL_CHARACTER_RETRY_MAX_TOKENS", "1400"),
        "general_character_max_per_chunk": os.environ.get("GENERAL_CHARACTER_MAX_PER_CHUNK", "8"),
        "general_character_api_downshift_max_depth": os.environ.get("GENERAL_CHARACTER_API_DOWNSHIFT_MAX_DEPTH", "1"),
        "general_character_api_downshift_min_chars": os.environ.get("GENERAL_CHARACTER_API_DOWNSHIFT_MIN_CHARS", "4000"),
        "general_scan_smart_density": _env_bool_value(os.environ.get("GENERAL_SCAN_SMART_DENSITY", "1")),
        "general_scan_content_aware_sampling": _env_bool_value(os.environ.get("GENERAL_SCAN_CONTENT_AWARE_SAMPLING", "1")),
        "general_scan_incremental_reuse": _env_bool_value(os.environ.get("GENERAL_SCAN_INCREMENTAL_REUSE", "1")),
        "general_scan_writing_quality": _env_bool_value(os.environ.get("GENERAL_SCAN_WRITING_QUALITY", "1")),
        "general_scan_narrative_architecture": _env_bool_value(os.environ.get("GENERAL_SCAN_NARRATIVE_ARCHITECTURE", "1")),
        "general_scan_foreshadowing_engineering": _env_bool_value(os.environ.get("GENERAL_SCAN_FORESHADOWING_ENGINEERING", "1")),
        "general_scan_semantic_layers": _env_bool_value(os.environ.get("GENERAL_SCAN_SEMANTIC_LAYERS", "1")),
        "general_scan_reader_experience": _env_bool_value(os.environ.get("GENERAL_SCAN_READER_EXPERIENCE", "1")),
        "general_scan_continuity_audit": _env_bool_value(os.environ.get("GENERAL_SCAN_CONTINUITY_AUDIT", "1")),
        "general_scan_rolling_context": _env_bool_value(os.environ.get("GENERAL_SCAN_ROLLING_CONTEXT", "1")),
        "general_scan_entity_prescan": _env_bool_value(os.environ.get("GENERAL_SCAN_ENTITY_PRESCAN", "1")),
        "general_scan_knowledge_base_llm_merge": _env_bool_value(os.environ.get("GENERAL_SCAN_KNOWLEDGE_BASE_LLM_MERGE", "0")),
        "general_scan_context_max_chars": os.environ.get("GENERAL_SCAN_CONTEXT_MAX_CHARS", "1600"),
        "rescan_skip_chronic_parse_failure_after": os.environ.get("RESCAN_SKIP_CHRONIC_PARSE_FAILURE_AFTER", "2"),
        "scan_future_stall_timeout_seconds": os.environ.get("SCAN_FUTURE_STALL_TIMEOUT_SECONDS", "0"),
        "harem_plus_general_scan": _env_bool_value(os.environ.get("HAREM_PLUS_GENERAL_SCAN", "0")),
        "editable": sorted(EDITABLE_RUNTIME_CONFIG.keys()),
        "runtime_only": True,
        "api_key_configured": bool(key_pool or has_single_key),
        "api_key_count": len(key_pool) if key_pool else (1 if has_single_key else 0),
        "app": _app_version_summary(),
        "web": {
            "max_upload_size": MAX_UPLOAD_SIZE,
            "max_json_body_size": MAX_JSON_BODY_SIZE,
            "file_response_chunk_size": FILE_RESPONSE_CHUNK_SIZE,
            "request_timeout": WEB_REQUEST_TIMEOUT_SECONDS,
            "scan_cancel_timeout_seconds": SCAN_CANCEL_TIMEOUT_SECONDS,
            "scan_heartbeat_interval_seconds": SCAN_HEARTBEAT_INTERVAL_SECONDS,
            "scan_stall_timeout_seconds": SCAN_STALL_TIMEOUT_SECONDS,
            "scan_stall_watchdog_enabled": SCAN_STALL_TIMEOUT_SECONDS > 0,
            "cors_allow_origin": os.environ.get("WEB_CORS_ALLOW_ORIGIN", "*"),
            "sync_books_ttl_seconds": SYNC_BOOKS_TTL_SECONDS,
            "outputs_cache_ttl_seconds": OUTPUTS_CACHE_TTL_SECONDS,
            "sse_state_interval_seconds": SSE_STATE_INTERVAL_SECONDS,
            "sse_sync_interval_seconds": SSE_SYNC_INTERVAL_SECONDS,
            "sse_max_connection_seconds": SSE_MAX_CONNECTION_SECONDS,
            "storage_health_ttl_seconds": STORAGE_HEALTH_TTL_SECONDS,
            "auth_enabled": _web_auth_enabled(),
            "allow_no_auth": _env_bool_value(os.environ.get("WEB_ALLOW_NO_AUTH", "0")),
            "api_key_required_on_start": _env_bool_value(os.environ.get("NOVEL_REPORT_SCANNER_REQUIRE_API_KEY", "1")),
            "storage": _storage_health_summary(),
        },
    }


def _app_version_summary():
    return {
        "version": APP_VERSION,
        "commit": APP_COMMIT,
        "build_date": APP_BUILD_DATE,
    }


def _health_summary():
    storage = _storage_health_summary()
    issues = _health_issues(CONFIG_READY, storage, include_details=False)
    return {
        "ok": True,
        "ready": not issues,
        "config_ready": CONFIG_READY,
        "app": _app_version_summary(),
        "scan_stall_timeout_seconds": SCAN_STALL_TIMEOUT_SECONDS,
        "scan_stall_watchdog_enabled": SCAN_STALL_TIMEOUT_SECONDS > 0,
        "storage_ready": _storage_ready(storage),
        "health_issues": issues,
    }


def _storage_ready(storage):
    if not isinstance(storage, dict):
        return False
    for name in ("novels", "results"):
        status = storage.get(name)
        if not isinstance(status, dict) or status.get("writable") is not True:
            return False
    return True


def _health_issues(config_ready, storage, stale_running_count=0, failed_count=0, include_details=True):
    issues = []
    if config_ready is False:
        issues.append({
            "type": "config",
            "message": "runtime config is not ready",
        })
    storage_failures = []
    if not isinstance(storage, dict):
        storage_failures.append({"name": "storage", "path": "", "error": "storage status unavailable"})
    else:
        for name in ("novels", "results"):
            status = storage.get(name)
            if not isinstance(status, dict) or status.get("writable") is True:
                continue
            storage_failures.append({
                "name": name,
                "path": status.get("path", ""),
                "error": status.get("error", "") or "not writable",
            })
    if storage_failures:
        issue = {
            "type": "storage",
            "message": "runtime storage is not writable",
        }
        if include_details:
            issue["details"] = storage_failures
        issues.append(issue)
    if stale_running_count:
        issues.append({
            "type": "stale_tasks",
            "message": f"{stale_running_count} running task(s) stalled without output",
            "count": stale_running_count,
        })
    if failed_count:
        issues.append({
            "type": "failed_tasks",
            "message": f"{failed_count} failed task(s)",
            "count": failed_count,
        })
    return issues


def _safe_tail_text(path, max_bytes=256 * 1024):
    try:
        if not path or not os.path.isfile(path):
            return ""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes)
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


def _scan_log_health_summary(tasks, limit=20):
    recent = [
        dict(task)
        for task in tasks
        if task.get("log_path") and task.get("status") in {"running", "completed", "failed", "interrupted"}
    ]
    recent.sort(key=_task_recency_key, reverse=True)
    summary = {
        "checked_task_count": 0,
        "json_parse_errors": 0,
        "server_504_errors": 0,
        "server_5xx_errors": 0,
        "permission_errors": 0,
        "legacy_pr_errors": 0,
        "multi_name_warnings": 0,
        "downshift_events": 0,
        "legacy_max_tokens_4000": 0,
        "latest_logs": [],
        "running_log_issues": [],
        "issues": [],
    }
    for task in recent[:max(0, int(limit or 0))]:
        text = _safe_tail_text(task.get("log_path"))
        if not text:
            continue
        item = {
            "task_id": task.get("id"),
            "book_id": task.get("book_id"),
            "status": task.get("status"),
            "json_parse_errors": text.count("JSON解析失败"),
            "server_504_errors": text.count("服务器错误(504)"),
            "server_5xx_errors": sum(text.count(f"服务器错误({code})") for code in ("500", "502", "503", "504")),
            "permission_errors": text.count("PermissionError") + text.count("Permission denied"),
            "legacy_pr_errors": text.count("cannot access local variable 'pr'"),
            "multi_name_warnings": text.count("女性角色字段疑似包含多个名字"),
            "downshift_events": text.count("触发降载切分"),
            "legacy_max_tokens_4000": text.count("max_tokens=4000"),
            "log_file": _file_link(task.get("log_path")),
        }
        summary["checked_task_count"] += 1
        for key in (
            "json_parse_errors",
            "server_504_errors",
            "server_5xx_errors",
            "permission_errors",
            "legacy_pr_errors",
            "multi_name_warnings",
            "downshift_events",
            "legacy_max_tokens_4000",
        ):
            summary[key] += item[key]
        if any(item[key] for key in (
            "json_parse_errors",
            "server_504_errors",
            "permission_errors",
            "legacy_pr_errors",
            "multi_name_warnings",
            "legacy_max_tokens_4000",
        )):
            summary["latest_logs"].append(item)
        if task.get("status") == "running" and any(item[key] for key in (
            "json_parse_errors",
            "server_504_errors",
            "legacy_max_tokens_4000",
            "legacy_pr_errors",
        )):
            summary["running_log_issues"].append(item)

    if summary["json_parse_errors"] >= 10:
        summary["issues"].append({
            "type": "json_parse_errors",
            "message": "recent scan logs contain frequent JSON parse failures",
            "count": summary["json_parse_errors"],
        })
    if summary["server_504_errors"] >= 10:
        summary["issues"].append({
            "type": "server_504_errors",
            "message": "recent scan logs contain frequent upstream 504 errors",
            "count": summary["server_504_errors"],
        })
    if summary["legacy_max_tokens_4000"] and not os.environ.get("GENERAL_CHARACTER_MAX_TOKENS"):
        summary["issues"].append({
            "type": "legacy_runtime_config",
            "message": "recent logs still use max_tokens=4000 and runtime config does not expose GENERAL_CHARACTER_MAX_TOKENS",
            "count": summary["legacy_max_tokens_4000"],
        })
    if summary["legacy_pr_errors"]:
        summary["issues"].append({
            "type": "legacy_pr_error",
            "message": "recent scan logs contain legacy local variable pr errors",
            "count": summary["legacy_pr_errors"],
        })
    if summary["multi_name_warnings"]:
        summary["issues"].append({
            "type": "legacy_multi_name_warning",
            "message": "recent scan logs contain old female-character multi-name warnings",
            "count": summary["multi_name_warnings"],
        })
    return summary


def _log_item_degraded_reason(item):
    if not isinstance(item, dict):
        return ""
    if SCAN_LOG_DEGRADED_JSON_PARSE_THRESHOLD > 0 and item.get("json_parse_errors", 0) >= SCAN_LOG_DEGRADED_JSON_PARSE_THRESHOLD:
        return "frequent_json_parse_errors"
    if SCAN_LOG_DEGRADED_504_THRESHOLD > 0 and item.get("server_504_errors", 0) >= SCAN_LOG_DEGRADED_504_THRESHOLD:
        return "frequent_504_errors"
    if (
        SCAN_LOG_DEGRADED_LEGACY_TOKEN_THRESHOLD > 0
        and item.get("legacy_max_tokens_4000", 0) >= SCAN_LOG_DEGRADED_LEGACY_TOKEN_THRESHOLD
        and not os.environ.get("GENERAL_CHARACTER_MAX_TOKENS")
    ):
        return "legacy_runtime_config"
    if item.get("legacy_pr_errors", 0):
        return "legacy_pr_error"
    return ""


def _degraded_running_tasks(running_tasks, log_health):
    log_items_by_task = {
        item.get("task_id"): item
        for item in (log_health or {}).get("running_log_issues", [])
        if item.get("task_id")
    }
    degraded = []
    for task in running_tasks or []:
        seconds = task.get("seconds_since_started")
        if seconds is None or seconds < SCAN_LOG_DEGRADED_MIN_SECONDS:
            continue
        item = log_items_by_task.get(task.get("task_id"))
        reason = _log_item_degraded_reason(item)
        if not reason:
            continue
        copied = dict(task)
        copied["log_degraded"] = True
        copied["degraded_reason"] = reason
        copied["log_issue"] = item
        degraded.append(copied)
    return degraded


def _task_diagnostic(task, book):
    last_log_at = task.get("last_log_at") or task.get("updated_at") or book.get("last_log_at") or book.get("updated_at")
    seconds_since_last_log = _seconds_since_state_time(last_log_at)
    seconds_since_created = _seconds_since_state_time(task.get("created_at"))
    seconds_since_started = _seconds_since_state_time(task.get("started_at"))
    log_path = task.get("log_path", "")
    stale = (
        SCAN_STALL_TIMEOUT_SECONDS > 0
        and seconds_since_last_log is not None
        and seconds_since_last_log >= SCAN_STALL_TIMEOUT_SECONDS
    )
    return {
        "task_id": task.get("id"),
        "book_id": task.get("book_id"),
        "book_name": book.get("name") or task.get("book_id"),
        "profile": task.get("profile"),
        "status": task.get("status"),
        "created_at": task.get("created_at", ""),
        "started_at": task.get("started_at", ""),
        "updated_at": task.get("updated_at", ""),
        "last_log_at": last_log_at or "",
        "last_log": task.get("last_log") or book.get("last_log") or "",
        "seconds_since_created": seconds_since_created,
        "seconds_since_started": seconds_since_started,
        "seconds_since_last_log": seconds_since_last_log,
        "stale_without_log": stale,
        "log_path": log_path,
        "log_file": _file_link(log_path) if log_path else None,
    }


def _queued_task_diagnostic(task, book, position):
    return {
        "task_id": task.get("id"),
        "book_id": task.get("book_id"),
        "book_name": book.get("name") or task.get("book_id"),
        "profile": task.get("profile"),
        "status": task.get("status"),
        "created_at": task.get("created_at", ""),
        "updated_at": task.get("updated_at", ""),
        "queue_position": position,
        "seconds_since_created": _seconds_since_state_time(task.get("created_at")),
    }


def _task_error_text(task):
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return str(
        task.get("error")
        or result.get("error")
        or task.get("message")
        or ""
    ).strip()


def _failure_reason_label(error_text):
    text = str(error_text or "").strip()
    lower = text.lower()
    if not text:
        return "unknown failure"
    if "permission denied" in lower or "errno 13" in lower or "storage write failed" in lower:
        return "permission denied"
    if "api key" in lower or "api_key" in lower or "未读取到任何 api key" in lower:
        return "api key missing"
    if any(token in lower for token in ("api调用失败", "api 调用失败", "服务器错误", "rate limit", "429", "403", "500", "502", "503", "504")):
        return "api failure"
    if "timeout" in lower or "timed out" in lower or "超时" in lower:
        return "api failure"
    if "json" in lower and ("parse" in lower or "invalid" in lower or "解析" in lower or "truncated" in lower):
        return "json parse failure"
    if "stalled without output" in lower:
        return "stalled without output"
    if "exited with code" in lower or "exited without result" in lower:
        return "scan process exited"
    if "config" in lower or "配置" in lower:
        return "runtime config error"
    return text[:120]


def _failure_retry_type(error_text):
    reason = _failure_reason_label(error_text)
    lower = str(error_text or "").lower()
    if reason == "api key missing":
        return "config_failure"
    if reason == "api failure":
        return "api_failure"
    if reason == "json parse failure":
        return "parse_failure"
    if "context_overflow" in lower or "context length" in lower or "上下文" in lower:
        return "context_overflow"
    if reason == "stalled without output":
        return "stalled"
    if reason == "runtime config error":
        return "config_failure"
    if reason == "permission denied":
        return "storage_failure"
    return "unknown"


def _failed_task_diagnostic(task, book):
    log_path = task.get("log_path", "")
    error_text = _task_error_text(task)
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    return {
        "task_id": task.get("id"),
        "book_id": task.get("book_id"),
        "book_name": book.get("name") or task.get("book_id"),
        "profile": task.get("profile"),
        "status": task.get("status"),
        "created_at": task.get("created_at", ""),
        "started_at": task.get("started_at", ""),
        "finished_at": task.get("finished_at", ""),
        "updated_at": task.get("updated_at", ""),
        "error": error_text,
        "failure_reason": _failure_reason_label(error_text),
        "retry_type": _failure_retry_type(error_text),
        "log_path": log_path,
        "log_file": _file_link(log_path) if log_path else None,
        "return_code": result.get("return_code"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "last_output": result.get("last_output", ""),
        "last_result_payload_preview": result.get("last_result_payload_preview", ""),
        "killed_by_stall_watchdog": bool(result.get("killed_by_stall_watchdog")),
    }


def _failure_reason_summary(failed_tasks, limit=10):
    counts = {}
    latest = {}
    for task in failed_tasks:
        label = task.get("failure_reason") or "unknown failure"
        counts[label] = counts.get(label, 0) + 1
        current_time = task.get("finished_at") or task.get("updated_at") or task.get("created_at") or ""
        if label not in latest or current_time > latest[label]:
            latest[label] = current_time
    rows = [
        {"reason": reason, "count": count, "latest_at": latest.get(reason, "")}
        for reason, count in counts.items()
    ]
    rows.sort(key=lambda item: item["reason"])
    rows.sort(key=lambda item: item.get("latest_at", ""), reverse=True)
    rows.sort(key=lambda item: item["count"], reverse=True)
    return rows[:limit]


def _retry_type_summary(failed_tasks):
    counts = {}
    for task in failed_tasks:
        retry_type = str(task.get("retry_type") or "").strip()
        if retry_type:
            counts[retry_type] = counts.get(retry_type, 0) + 1
    return [
        {"type": retry_type, "count": count}
        for retry_type, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _task_recency_key(task):
    return task.get("finished_at") or task.get("updated_at") or task.get("created_at") or ""


def _latest_failed_tasks_for_current_failed_books(books_by_id, tasks):
    latest_by_book = {}
    for task in tasks:
        book_id = task.get("book_id")
        book = books_by_id.get(book_id) or {}
        if task.get("status") != "failed" or book.get("status") != "failed":
            continue
        previous = latest_by_book.get(book_id)
        if previous is None or _task_recency_key(task) > _task_recency_key(previous):
            latest_by_book[book_id] = task
    return [
        _failed_task_diagnostic(task, books_by_id.get(task.get("book_id"), {}))
        for task in latest_by_book.values()
    ]


def _diagnostics_summary():
    generated_at = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")
    with STATE_LOCK:
        books_by_id = {book_id: dict(book) for book_id, book in STATE.get("books", {}).items()}
        tasks = [dict(task) for task in STATE.get("tasks", [])]
        running_tasks = [
            _task_diagnostic(task, books_by_id.get(task.get("book_id"), {}))
            for task in tasks
            if task.get("status") == "running"
        ]
        status_counts = {}
        for task in tasks:
            status = task.get("status") or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
        queued_positions = _queued_task_positions()
        queued_tasks = [
            _queued_task_diagnostic(task, books_by_id.get(task.get("book_id"), {}), queued_positions.get(task.get("id")))
            for task in tasks
            if task.get("status") == "queued"
        ]
        failed_tasks = _latest_failed_tasks_for_current_failed_books(books_by_id, tasks)
        failed_book_count = sum(1 for book in books_by_id.values() if book.get("status") == "failed")

    running_tasks.sort(key=lambda item: item.get("started_at") or item.get("created_at") or "")
    queued_tasks.sort(key=lambda item: item.get("queue_position") or float("inf"))
    failed_tasks.sort(key=lambda item: item.get("finished_at") or item.get("updated_at") or item.get("created_at") or "", reverse=True)
    queued_count = status_counts.get("queued", 0)
    failed_task_history_count = status_counts.get("failed", 0)
    failed_count = failed_book_count
    stale_running_count = sum(1 for item in running_tasks if item.get("stale_without_log"))
    storage = _storage_health_summary()
    health_issues = _health_issues(CONFIG_READY, storage, stale_running_count, failed_count)
    log_health = _scan_log_health_summary(tasks)
    degraded_running_tasks = _degraded_running_tasks(running_tasks, log_health)
    degraded_running_count = len(degraded_running_tasks)
    if degraded_running_count:
        health_issues.append({
            "type": "degraded_running_tasks",
            "message": f"{degraded_running_count} running task(s) show repeated API/JSON errors",
            "count": degraded_running_count,
        })
    if log_health.get("issues"):
        health_issues.append({
            "type": "scan_log_errors",
            "message": "recent scan logs contain recurring errors",
            "count": len(log_health.get("issues") or []),
            "details": log_health.get("issues") or [],
        })
    longest_running_seconds = max(
        (item.get("seconds_since_started") or 0 for item in running_tasks),
        default=0,
    )
    oldest_queue_wait_seconds = max(
        (item.get("seconds_since_created") or 0 for item in queued_tasks),
        default=0,
    )
    return {
        "generated_at": generated_at,
        "ok": not health_issues,
        "ready": not health_issues,
        "config_ready": CONFIG_READY,
        "app": _app_version_summary(),
        "scan_stall_timeout_seconds": SCAN_STALL_TIMEOUT_SECONDS,
        "scan_stall_watchdog_enabled": SCAN_STALL_TIMEOUT_SECONDS > 0,
        "storage_ready": _storage_ready(storage),
        "health_issues": health_issues,
        "task_counts": status_counts,
        "queue_length": queued_count,
        "queue_runtime_length": len(queued_positions),
        "running_count": len(running_tasks),
        "stale_running_count": stale_running_count,
        "degraded_running_count": degraded_running_count,
        "failed_count": failed_count,
        "failed_book_count": failed_book_count,
        "failed_task_history_count": failed_task_history_count,
        "oldest_queue_wait_seconds": oldest_queue_wait_seconds,
        "longest_running_seconds": longest_running_seconds,
        "running_tasks": running_tasks,
        "degraded_running_tasks": degraded_running_tasks,
        "queued_tasks": queued_tasks,
        "recent_failed_tasks": failed_tasks[:10],
        "failure_reasons": _failure_reason_summary(failed_tasks),
        "retry_types": _retry_type_summary(failed_tasks),
        "log_health": log_health,
        "storage": storage,
    }


def _storage_health_summary(force_refresh=False):
    def copy_summary(summary):
        return {name: dict(status) for name, status in summary.items()}

    now = time.monotonic()
    base_dir = os.path.abspath(get_base_dir())
    with STORAGE_HEALTH_LOCK:
        cached = STORAGE_HEALTH_CACHE.get("summary")
        if (
            not force_refresh
            and cached is not None
            and STORAGE_HEALTH_CACHE.get("base_dir") == base_dir
            and STORAGE_HEALTH_TTL_SECONDS > 0
            and now - STORAGE_HEALTH_CACHE.get("checked_at", 0.0) < STORAGE_HEALTH_TTL_SECONDS
        ):
            return copy_summary(cached)

    summary = {
        "novels": _directory_write_status(_novels_dir),
        "results": _directory_write_status(lambda: os.path.join(get_base_dir(), "results")),
    }
    with STORAGE_HEALTH_LOCK:
        STORAGE_HEALTH_CACHE.update({
            "base_dir": base_dir,
            "checked_at": now,
            "summary": summary,
        })
    return copy_summary(summary)


def _directory_write_status(path_factory):
    try:
        path = path_factory()
        os.makedirs(path, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-test-", dir=path, delete=True):
            pass
        return {"path": path, "writable": True, "error": ""}
    except Exception as exc:
        try:
            path = path_factory()
        except Exception:
            path = ""
        return {"path": path, "writable": False, "error": str(exc) or exc.__class__.__name__}


def _env_bool_value(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_config_value(field, value):
    spec = EDITABLE_RUNTIME_CONFIG.get(field)
    if not spec:
        raise ValueError(f"unsupported config field: {field}")
    if spec["type"] == "int":
        if value in (None, "") and spec.get("empty"):
            return ""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be an integer") from None
        if parsed < spec["min"] or parsed > spec["max"]:
            raise ValueError(f"{field} must be between {spec['min']} and {spec['max']}")
        return str(parsed)
    if spec["type"] == "choice":
        normalized = str(value or "").strip().lower()
        if normalized not in spec["choices"]:
            raise ValueError(f"{field} must be one of: {', '.join(sorted(spec['choices']))}")
        return normalized
    if spec["type"] == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return "1"
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return "0"
        raise ValueError(f"{field} must be a boolean")
    raise ValueError(f"unsupported config type for {field}")


def _persist_runtime_config_to_env_file(values):
    """将运行时配置变更安全地写回 .env 文件。

    只更新 EDITABLE_RUNTIME_CONFIG 中列出的非敏感字段；保留其他所有行
    （包括注释、空行、API Key 等敏感信息）。使用原子写入避免文件损坏。
    """
    base_dir = get_base_dir()
    env_path = os.path.join(base_dir, ".env")
    tmp_path = None

    with RUNTIME_CONFIG_LOCK:
        # 构建 env_name -> (field_name, value) 映射
        env_to_field = {}
        for field, spec in EDITABLE_RUNTIME_CONFIG.items():
            env_name = spec["env"]
            if field in values:
                env_to_field[env_name] = (field, values[field])

        lines = []
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except Exception:
                lines = []

        updated_envs = set()
        new_lines = []
        for line in lines:
            stripped = line.lstrip()
            # 保留注释、空行、无等号行原样
            if not stripped or stripped.startswith("#") or "=" not in line:
                new_lines.append(line)
                continue
            # 解析 KEY=VALUE（不拆分值中的等号）
            eq_idx = line.index("=")
            key = line[:eq_idx].rstrip()
            if key in env_to_field:
                _field, new_value = env_to_field[key]
                new_lines.append(f"{key}={new_value}")
                updated_envs.add(key)
            else:
                new_lines.append(line)

        # 追加尚未存在的字段
        for env_name, (field, value) in env_to_field.items():
            if env_name not in updated_envs:
                new_lines.append(f"{env_name}={value}")

        # 原子写入
        try:
            tmp_path = f"{env_path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines))
                if new_lines and not new_lines[-1].endswith("\n"):
                    f.write("\n")
            os.replace(tmp_path, env_path)
        except Exception as exc:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False, f"failed to persist config: {exc}"
    return True, ""


def _update_runtime_config(values):
    global SCAN_STALL_TIMEOUT_SECONDS, SCAN_FUTURE_STALL_TIMEOUT_SECONDS
    if not isinstance(values, dict):
        return False, "config must be an object"
    normalized = {}
    for field, value in values.items():
        if field not in EDITABLE_RUNTIME_CONFIG:
            return False, f"unsupported config field: {field}"
        try:
            normalized[field] = _normalize_config_value(field, value)
        except ValueError as exc:
            return False, str(exc)
    for field, value in normalized.items():
        os.environ[EDITABLE_RUNTIME_CONFIG[field]["env"]] = value
        if field == "scan_stall_timeout_seconds":
            SCAN_STALL_TIMEOUT_SECONDS = float(value or 0)
        elif field == "scan_future_stall_timeout_seconds":
            SCAN_FUTURE_STALL_TIMEOUT_SECONDS = float(value or 0)
    # 尝试持久化到 .env 文件（失败不影响内存中的更新）
    _persist_runtime_config_to_env_file(normalized)
    return True, _runtime_config_summary()


def _validate_json_payload_schema(payload, schema):
    if not isinstance(payload, dict):
        return False, "json body must be an object"
    fields = schema.get("fields", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", []) if isinstance(schema, dict) else [])
    for field in required:
        if field not in payload:
            return False, f"{field} is required"
    for field, rules in fields.items():
        if field not in payload:
            continue
        value = payload.get(field)
        if value is None and not rules.get("nullable"):
            return False, f"{field} is required"
        allowed_types = rules.get("type")
        if allowed_types:
            if not isinstance(allowed_types, tuple):
                allowed_types = (allowed_types,)
            if not isinstance(value, allowed_types):
                names = "/".join(t.__name__ for t in allowed_types)
                return False, f"{field} must be {names}"
        if rules.get("non_empty") and isinstance(value, str) and not value.strip():
            return False, f"{field} must not be empty"
        choices = rules.get("choices")
        if choices is not None and value not in choices:
            return False, f"{field} must be one of: {', '.join(sorted(choices))}"
        item_type = rules.get("item_type")
        if item_type and isinstance(value, list):
            if any(not isinstance(item, item_type) for item in value):
                return False, f"{field} items must be {item_type.__name__}"
            if rules.get("non_empty_items") and any(not str(item).strip() for item in value):
                return False, f"{field} items must not be empty"
    return True, ""


def _operation_response(ok, result):
    response = {"ok": ok, "result": result}
    if not ok and isinstance(result, str):
        response["error"] = result
    return response


def _web_access_token():
    return os.environ.get("WEB_ACCESS_TOKEN", "").strip()


def _web_auth_enabled():
    return bool(_web_access_token())


def _extract_bearer_token(value):
    if not value:
        return ""
    parts = value.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return value.strip()


def _request_access_token(headers, query=""):
    header_token = _extract_bearer_token(headers.get("Authorization", "") if headers else "")
    if header_token:
        return header_token
    fallback_header = headers.get("X-Web-Access-Token", "") if headers else ""
    if fallback_header:
        return fallback_header.strip()
    params = parse_qs(query or "")
    for key in ("token", "access_token", "web_access_token"):
        value = (params.get(key) or [""])[0].strip()
        if value:
            return value
    return ""


def _is_authorized_request(headers, query=""):
    expected = _web_access_token()
    if not expected:
        return True
    provided = _request_access_token(headers, query)
    return bool(provided) and secrets.compare_digest(provided, expected)


def _unsafe_write_confirmed(headers):
    if _web_auth_enabled():
        return True
    value = headers.get("X-Web-Unsafe-Action", "") if headers else ""
    return str(value).strip().lower() in {"confirm", "confirmed", "true", "1", "yes"}


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
    return _public_file_access_error(path) is None


def _public_file_access_error(path):
    if not path:
        return 400, "missing file path"
    base = os.path.abspath(get_base_dir())
    allowed = [
        os.path.abspath(os.path.join(base, "results")),
        os.path.abspath(os.path.join(base, "novels")),
    ]
    ap = os.path.abspath(path)
    if not any(_is_path_inside(ap, root) for root in allowed):
        return 403, "file is not allowed"
    if not os.path.exists(ap):
        return 404, "file not found"
    if not os.path.isfile(ap):
        return 403, "file is not a regular file"
    return None


def _is_safe_novel_file(path):
    if not path:
        return False
    root = os.path.abspath(_novels_dir())
    ap = os.path.abspath(path)
    return _is_path_inside(ap, root) and os.path.isfile(ap)


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


def _merge_output_links(*output_lists):
    outputs_by_path = {}
    for outputs in output_lists:
        for item in outputs or []:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not path:
                continue
            _add_output_link(outputs_by_path, path, item.get("kind"))
    outputs = list(outputs_by_path.values())
    outputs.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return outputs


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


def _fresh_cached_output_links(outputs):
    if not outputs:
        return []
    expected_paths = []
    for item in outputs:
        if not isinstance(item, dict) or not item.get("path"):
            return None
        expected_paths.append(os.path.abspath(item["path"]))
    refreshed = _merge_output_links(outputs)
    if {os.path.abspath(item.get("path")) for item in refreshed} != set(expected_paths):
        return None
    return refreshed


def _book_output_index(book_id):
    with STATE_LOCK:
        book = STATE.get("books", {}).get(book_id) or {}
        indexed = list(book.get("output_index") or [])
    return _merge_output_links(indexed)[:100]


def _extract_result_output_paths(result):
    paths = []
    if not isinstance(result, dict):
        return paths
    out_file = result.get("out_file")
    if out_file:
        paths.append({"path": out_file, "kind": "final_report"})
    for item in result.get("results") or []:
        paths.extend(_extract_result_output_paths(item))
    return paths


def _candidate_latest_summary_paths(book_id, profiles=None):
    results_dir = os.path.join(get_base_dir(), "results")
    profile_names = []
    for profile_name in profiles or []:
        if profile_name and profile_name not in profile_names:
            profile_names.append(profile_name)

    paths = [os.path.join(results_dir, f"{book_id}_GENERAL_SUMMARY_latest.json")]
    for profile_name in profile_names:
        if profile_name != "general":
            paths.append(os.path.join(results_dir, f"{book_id}_{profile_name}_GENERAL_SUMMARY_latest.json"))
    return paths


def _record_book_outputs_from_result(book_id, result, profiles=None):
    if not book_id:
        return []
    outputs = _collect_book_outputs_from_result(book_id, result, profiles)
    if not outputs:
        return []
    with STATE_LOCK:
        book = STATE.get("books", {}).get(book_id)
        if not book:
            return []
        book["output_index"] = outputs
    _invalidate_book_outputs(book_id)
    return outputs


def _collect_book_outputs_from_result(book_id, result, profiles=None):
    candidates = []
    candidates.extend(_extract_result_output_paths(result))
    candidates.extend({"path": path, "kind": "summary"} for path in _candidate_latest_summary_paths(book_id, profiles))
    candidates.extend({"path": path, "kind": "final_report"} for path in _checkpoint_report_outputs(book_id))
    return _merge_output_links(candidates)[:100]


def _book_title_variants(book_id):
    if not book_id:
        return []
    text = str(book_id).strip()
    variants = [text]
    if text.startswith("《") and "》" in text:
        variants.append(text.split("》", 1)[0] + "》")
    else:
        variants.append(f"《{text}》")
    deduped = []
    for item in variants:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _result_dir_matches_book(parent, book_id):
    if not parent or not book_id:
        return False
    for marker in ("_scan_", "_general_", "_characters_", "_heroine_", "_protagonist_", "_review_", "_verify_"):
        if marker in parent:
            return parent.split(marker, 1)[0] == book_id
    return parent in _book_title_variants(book_id)


def _output_filename_matches_book(filename, book_id, profile_names):
    if not filename or not book_id:
        return False
    exact_filenames = {f"{book_id}_GENERAL_SUMMARY_latest.json"}
    exact_filenames.update(
        f"{book_id}_{name}_GENERAL_SUMMARY_latest.json"
        for name in profile_names
        if name and name != "general"
    )
    if filename in exact_filenames:
        return True
    report_suffixes = ("扫书报告", "通用小说报告")
    return any(
        filename.startswith(f"{title}{suffix}_") or filename.startswith(f"{title}{suffix}.")
        for title in _book_title_variants(book_id)
        for suffix in report_suffixes
    )


def _find_book_outputs(book_id):
    now = time.monotonic()
    results_dir = os.path.join(get_base_dir(), "results")
    cache_key = _outputs_cache_key(book_id)
    cached = OUTPUTS_CACHE.get(cache_key)
    if cached and now - cached["time"] < OUTPUTS_CACHE_TTL_SECONDS:
        outputs = _fresh_cached_output_links(cached.get("outputs"))
        if outputs is not None:
            cached["outputs"] = outputs
            return outputs
        OUTPUTS_CACHE.pop(cache_key, None)

    indexed_outputs = _book_output_index(book_id)
    if indexed_outputs:
        OUTPUTS_CACHE[cache_key] = {"time": now, "outputs": indexed_outputs}
        return indexed_outputs

    outputs_by_path = {}
    if not os.path.isdir(results_dir):
        OUTPUTS_CACHE.pop(cache_key, None)
        return []

    for path in _checkpoint_report_outputs(book_id):
        _add_output_link(outputs_by_path, path, "final_report")

    profile_names = [profile.name for profile in list_available_profiles()]
    scan_dir_outputs = {"GENERAL_SUMMARY.json", "VERIFIED_REPORT.txt", "FULL_REPORT.txt"}
    output_exts = {".txt", ".json", ".log", ".md", ".csv"}

    for root, _dirs, files in os.walk(results_dir):
        parent = os.path.basename(root)
        in_book_dir = _result_dir_matches_book(parent, book_id)
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in output_exts:
                continue
            path = os.path.join(root, filename)
            matched = (
                _output_filename_matches_book(filename, book_id, profile_names)
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


def _time_value(value):
    return str(value or "").replace("T", " ").strip()


def _parse_state_time(value):
    text = _time_value(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _seconds_since_state_time(value):
    parsed = _parse_state_time(value)
    if parsed is None:
        return None
    return max(0, int(time.time() - parsed.timestamp()))


def _book_token_usage(book_id, max_runs=5, since=None):
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

    if since:
        since_key = _time_value(since)
        runs = [
            run
            for run in runs
            if _time_value(run.get("updated_at") or run.get("started_at")) >= since_key
        ]
        if not runs:
            return None

    runs.sort(key=lambda item: item.get("updated_at") or item.get("started_at") or "", reverse=True)
    if since:
        total_input = sum(run["input"] for run in runs)
        total_output = sum(run["output"] for run in runs)
        total_tokens = sum(run["total"] for run in runs)
        updated_at = runs[0].get("updated_at") or runs[0].get("started_at") or ""
    else:
        total_input = _safe_token_int(book_entry.get("book_total_input"))
        total_output = _safe_token_int(book_entry.get("book_total_output"))
        total_tokens = _safe_token_int(book_entry.get("book_total_tokens"))
        updated_at = book_entry.get("updated_at", "")
    return {
        "book_name": book_entry.get("book_name") or book_id,
        "input": total_input,
        "output": total_output,
        "total": total_tokens,
        "updated_at": updated_at,
        "runs": runs[:max_runs],
        "run_count": len(runs),
    }


def _book_detail(book_id):
    with STATE_LOCK:
        book = dict(STATE["books"].get(book_id) or {})
        tasks = [dict(t) for t in STATE["tasks"] if t.get("book_id") == book_id]
    if not book:
        return None
    history_reset_at = book.get("history_reset_at")
    if history_reset_at:
        reset_key = _time_value(history_reset_at)
        tasks = [task for task in tasks if _time_value(task.get("created_at")) >= reset_key]
    for task in tasks:
        if task.get("log_path"):
            task["log_file"] = _file_link(task.get("log_path"))
    book["novel_file"] = _file_link(book.get("path"))
    _refresh_book_suggestions(book)
    outputs = _find_book_outputs(book_id)
    outputs_reset_after = book.get("outputs_reset_after")
    if outputs_reset_after is not None:
        try:
            reset_mtime = float(outputs_reset_after)
            outputs = [item for item in outputs if float(item.get("mtime") or 0) >= reset_mtime]
        except (TypeError, ValueError):
            pass
    book["outputs"] = outputs
    book["token_usage"] = _book_token_usage(book_id, since=history_reset_at)
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
        source_error = _book_source_missing_message(book)
        if source_error:
            if _mark_book_source_status(book):
                _save_state()
            return False, source_error
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
    return {"queued": queued, "skipped": skipped, "skipped_reasons": _reason_count_summary(skipped)}


def _reason_count_summary(items, reason_key="reason"):
    counts = {}
    for item in items or []:
        reason = str((item or {}).get(reason_key) or "unknown").strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _latest_failed_task_for_book_locked(book_id):
    failed = [
        task for task in STATE.get("tasks", [])
        if task.get("book_id") == book_id and task.get("status") == "failed"
    ]
    if not failed:
        return None
    failed.sort(key=lambda item: item.get("finished_at") or item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return failed[0]


def _retry_failed_tasks_by_type(failure_types):
    requested_types = {
        str(item or "").strip()
        for item in (failure_types or [])
        if str(item or "").strip()
    }
    if not requested_types:
        requested_types = {"api_failure", "parse_failure"}
    matched = []
    skipped = []
    with STATE_LOCK:
        for book_id, book in STATE.get("books", {}).items():
            if book.get("status") != "failed":
                continue
            task = _latest_failed_task_for_book_locked(book_id)
            if not task:
                continue
            error_text = _task_error_text(task)
            retry_type = _failure_retry_type(error_text)
            diagnostic = _failed_task_diagnostic(task, book)
            if retry_type in requested_types or diagnostic.get("failure_reason") in requested_types:
                matched.append({
                    "book_id": book_id,
                    "task_id": task.get("id"),
                    "retry_type": retry_type,
                    "failure_reason": diagnostic.get("failure_reason"),
                })
            else:
                skipped.append({
                    "book_id": book_id,
                    "task_id": task.get("id"),
                    "retry_type": retry_type,
                    "failure_reason": diagnostic.get("failure_reason"),
                    "reason": "failure type not selected",
                })
    result = _enqueue_many([item["book_id"] for item in matched])
    result["matched"] = matched
    result["unmatched_failed"] = skipped
    result["unmatched_reasons"] = _reason_count_summary(skipped)
    result["failure_types"] = sorted(requested_types)
    return result


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


def _terminate_running_task_process(task_id):
    with STATE_LOCK:
        proc = RUNNING_TASK_PROCS.get(task_id)
    if not proc:
        return False
    try:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (AttributeError, OSError):
            proc.terminate()
        proc.wait(timeout=SCAN_CANCEL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        proc.wait(timeout=SCAN_CANCEL_TIMEOUT_SECONDS)
    except OSError:
        return False
    return True


def _cancel_book_task(book_id):
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") not in {"queued", "running"}:
            return False, "book is not queued or running"
        task_id = book.get("task_id")
        task = _find_task(task_id)
        if not task or task.get("status") not in {"queued", "running"}:
            return False, "active task not found"
        was_running = task.get("status") == "running"
        task["status"] = "canceled"
        task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        task["error"] = "用户取消扫描" if was_running else "用户取消排队"
        book["status"] = "idle"
        book["message"] = "已取消扫描" if was_running else "已取消排队"
        book.pop("task_id", None)
        TASK_QUEUE_IDS.discard(task_id)
        _save_state()
    if was_running:
        _terminate_running_task_process(task_id)
    return True, task_id


def _cancel_queued_book(book_id):
    return _cancel_book_task(book_id)


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


def _delete_many_books(book_ids):
    requested = []
    for book_id in book_ids or []:
        if not book_id or book_id in requested:
            continue
        requested.append(book_id)

    deleted = []
    skipped = []
    for book_id in requested:
        ok, result = _delete_book(book_id)
        if ok:
            deleted.append({"book_id": book_id})
        else:
            skipped.append({"book_id": book_id, "reason": result})
    return {"deleted": deleted, "skipped": skipped, "skipped_reasons": _reason_count_summary(skipped)}


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


def _run_scan_subprocess(book_path, profile_name, run_id, log_file, task_id=None, heartbeat_callback=None):
    cmd = _web_scan_command(book_path, profile_name, run_id)
    started_monotonic = time.monotonic()
    popen_kwargs = {
        "cwd": get_base_dir(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        **popen_kwargs,
    )
    if task_id:
        with STATE_LOCK:
            RUNNING_TASK_PROCS[task_id] = proc
    result = None
    last_heartbeat_at = 0.0
    last_output_line = ""
    last_result_payload = ""
    killed_by_stall_watchdog = False
    output_queue = queue.Queue()

    def reader():
        try:
            assert proc.stdout is not None
            for item in proc.stdout:
                output_queue.put(item)
        finally:
            output_queue.put(None)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    try:
        last_output_at = time.monotonic()
        while True:
            try:
                line = output_queue.get(timeout=1)
            except queue.Empty:
                if (
                    SCAN_STALL_TIMEOUT_SECONDS > 0
                    and proc.poll() is None
                    and time.monotonic() - last_output_at >= SCAN_STALL_TIMEOUT_SECONDS
                ):
                    killed_by_stall_watchdog = True
                    result = {
                        "status": "fail",
                        "error": f"scan process stalled without output for {SCAN_STALL_TIMEOUT_SECONDS:g}s",
                    }
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (AttributeError, OSError):
                        proc.terminate()
                    try:
                        proc.wait(timeout=SCAN_CANCEL_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (AttributeError, OSError):
                            proc.kill()
                        proc.wait(timeout=SCAN_CANCEL_TIMEOUT_SECONDS)
                    break
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            last_output_at = time.monotonic()
            last_output_line = str(line or "").strip()[:500]
            if line.startswith(_WEB_SCAN_RESULT_PREFIX):
                payload = line[len(_WEB_SCAN_RESULT_PREFIX):].strip()
                last_result_payload = payload[:1000]
                try:
                    result = json.loads(payload)
                except json.JSONDecodeError as exc:
                    result = {"status": "fail", "error": f"invalid scan result: {exc}"}
                continue
            log_file.write(line)
            log_file.flush()
            if heartbeat_callback is not None:
                now = time.monotonic()
                if last_heartbeat_at <= 0 or SCAN_HEARTBEAT_INTERVAL_SECONDS <= 0 or now - last_heartbeat_at >= SCAN_HEARTBEAT_INTERVAL_SECONDS:
                    heartbeat_callback(line)
                    last_heartbeat_at = now
        return_code = proc.wait()
    finally:
        if task_id:
            with STATE_LOCK:
                RUNNING_TASK_PROCS.pop(task_id, None)
    if result is None:
        result = {"status": "fail", "error": f"scan process exited without result (code {return_code})"}
    elif return_code != 0 and result.get("status") in {"ok", "skipped"}:
        result = dict(result)
        result["status"] = "fail"
        result["error"] = f"scan process exited with code {return_code}"
    if isinstance(result, dict):
        result.setdefault("return_code", return_code)
        result.setdefault("elapsed_seconds", round(max(0.0, time.monotonic() - started_monotonic), 3))
        result.setdefault("last_output", last_output_line)
        result.setdefault("last_result_payload_preview", last_result_payload)
        result.setdefault("killed_by_stall_watchdog", killed_by_stall_watchdog)
    return result


def _find_task(task_id):
    for task in STATE["tasks"]:
        if task.get("id") == task_id:
            return task
    return None


def _clear_book_active_task_locked(book, task_id):
    if book and book.get("task_id") == task_id:
        book.pop("task_id", None)


def _scan_log_heartbeat(task_id, line):
    text = str(line or "").strip()
    with STATE_LOCK:
        task = _find_task(task_id)
        if not task or task.get("status") != "running":
            return
        book = STATE["books"].get(task.get("book_id"))
        now_text = time.strftime("%Y-%m-%d %H:%M:%S")
        task["updated_at"] = now_text
        task["last_log_at"] = now_text
        task["message"] = "扫描中"
        if text:
            task["last_log"] = text[:240]
        if book and book.get("status") == "running":
            book["message"] = "扫描中"
            book["updated_at"] = now_text
            book["last_log_at"] = now_text
            if text:
                book["last_log"] = text[:240]
        _save_state()


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
            source_error = _book_source_missing_message(book)
            if source_error:
                now_text = time.strftime("%Y-%m-%d %H:%M:%S")
                task["status"] = "failed"
                task["finished_at"] = now_text
                task["updated_at"] = now_text
                task["error"] = source_error
                task["message"] = source_error
                book["status"] = "failed"
                book["message"] = source_error
                _mark_book_source_status(book)
                _clear_book_active_task_locked(book, task_id)
                _save_state()
                TASK_QUEUE.task_done()
                continue
            task["status"] = "running"
            task["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            task["updated_at"] = task["started_at"]
            task["message"] = "扫描中"
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
                result = _run_scan_subprocess(
                    book["path"],
                    profile_name,
                    _generate_run_id(),
                    log_file,
                    task_id=task_id,
                    heartbeat_callback=lambda line: _scan_log_heartbeat(task_id, line),
                )
            output_index = []
            if result.get("status") in {"ok", "skipped"}:
                result_profiles = result.get("profiles")
                if not result_profiles:
                    fallback_profile = result.get("profile") or task.get("profile", "auto")
                    result_profiles = fallback_profile if isinstance(fallback_profile, list) else [fallback_profile]
                output_index = _collect_book_outputs_from_result(task.get("book_id"), result, result_profiles)

            with STATE_LOCK:
                if task.get("status") == "canceled":
                    continue
                task["status"] = "completed" if result.get("status") in {"ok", "skipped"} else "failed"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["updated_at"] = task["finished_at"]
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
                if task["status"] == "completed" and output_index:
                    book["output_index"] = output_index
                _clear_book_active_task_locked(book, task_id)
                _invalidate_book_outputs(task.get("book_id"))
                _save_state()
        except Exception as exc:
            try:
                with open(task.get("log_path") or _task_log_path(task_id), "a", encoding="utf-8") as log_file:
                    log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {exc}\n")
            except Exception:
                pass
            with STATE_LOCK:
                if task.get("status") == "canceled":
                    continue
                task["status"] = "failed"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["updated_at"] = task["finished_at"]
                task["error"] = str(exc)
                book["status"] = "failed"
                book["message"] = str(exc)
                _clear_book_active_task_locked(book, task_id)
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
        _refresh_runtime_constants_from_env()
        CONFIG_READY = True
        return True, ""
    except BaseException as exc:
        CONFIG_READY = False
        msg = f"{interactive_context} runtime config not ready: {exc}"
        print(f"[WARN] {msg}")
        return False, msg


def _refresh_runtime_constants_from_env():
    global MAX_UPLOAD_SIZE, MAX_JSON_BODY_SIZE, FILE_RESPONSE_CHUNK_SIZE
    global WEB_REQUEST_TIMEOUT_SECONDS, SYNC_BOOKS_TTL_SECONDS, OUTPUTS_CACHE_TTL_SECONDS
    global SSE_STATE_INTERVAL_SECONDS, SSE_SYNC_INTERVAL_SECONDS, SSE_MAX_CONNECTION_SECONDS
    global SCAN_CANCEL_TIMEOUT_SECONDS, SCAN_HEARTBEAT_INTERVAL_SECONDS
    global SCAN_STALL_TIMEOUT_SECONDS, SCAN_FUTURE_STALL_TIMEOUT_SECONDS
    global STORAGE_HEALTH_TTL_SECONDS

    MAX_UPLOAD_SIZE = read_int_env("MAX_UPLOAD_SIZE", 100 * 1024 * 1024, min_value=1)
    MAX_JSON_BODY_SIZE = read_int_env("MAX_JSON_BODY_SIZE", 64 * 1024, min_value=1)
    FILE_RESPONSE_CHUNK_SIZE = read_int_env("FILE_RESPONSE_CHUNK_SIZE", 1024 * 1024, min_value=1)
    WEB_REQUEST_TIMEOUT_SECONDS = read_float_env("WEB_REQUEST_TIMEOUT", 60.0, min_value=0.0)
    SYNC_BOOKS_TTL_SECONDS = read_float_env("SYNC_BOOKS_TTL_SECONDS", 5.0, min_value=0.0)
    OUTPUTS_CACHE_TTL_SECONDS = read_float_env("OUTPUTS_CACHE_TTL_SECONDS", 5.0, min_value=0.0)
    SSE_STATE_INTERVAL_SECONDS = read_float_env("SSE_STATE_INTERVAL_SECONDS", 3.0, min_value=0.0)
    SSE_SYNC_INTERVAL_SECONDS = read_float_env(
        "SSE_SYNC_INTERVAL_SECONDS",
        max(SYNC_BOOKS_TTL_SECONDS, SSE_STATE_INTERVAL_SECONDS),
        min_value=0.0,
    )
    SSE_MAX_CONNECTION_SECONDS = read_float_env("SSE_MAX_CONNECTION_SECONDS", 300.0, min_value=0.0)
    SCAN_CANCEL_TIMEOUT_SECONDS = read_float_env("SCAN_CANCEL_TIMEOUT_SECONDS", 5.0, min_value=0.0)
    SCAN_HEARTBEAT_INTERVAL_SECONDS = read_float_env("SCAN_HEARTBEAT_INTERVAL_SECONDS", 10.0, min_value=0.0)
    SCAN_STALL_TIMEOUT_SECONDS = read_float_env("SCAN_STALL_TIMEOUT_SECONDS", 1200.0, min_value=0.0)
    SCAN_FUTURE_STALL_TIMEOUT_SECONDS = read_float_env("SCAN_FUTURE_STALL_TIMEOUT_SECONDS", 0.0, min_value=0.0)
    STORAGE_HEALTH_TTL_SECONDS = read_float_env("STORAGE_HEALTH_TTL_SECONDS", 10.0, min_value=0.0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = str(args[1]) if len(args) > 1 else "-"
        size = str(args[2]) if len(args) > 2 else "-"
        _access_logger().info(
            "%s %s %s %s %s",
            self.address_string(),
            getattr(self, "command", "-"),
            _sanitize_log_path(getattr(self, "path", "")),
            status,
            size,
        )

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", os.environ.get("WEB_CORS_ALLOW_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID, Authorization, X-Web-Access-Token, X-Web-Unsafe-Action")
        super().end_headers()

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_storage_error(self, exc):
        message = str(exc) or exc.__class__.__name__
        storage = _storage_health_summary(force_refresh=True)
        self._send_json(
            {
                "error": "storage write failed",
                "detail": message,
                "hint": "检查宿主机挂载的 novels/results 目录是否允许容器运行用户写入。",
                "storage": storage,
                "health_issues": _health_issues(CONFIG_READY, storage),
            },
            500,
        )

    def _send_not_found(self, path):
        if path == "/api" or path.startswith("/api/") or path in {"/files", "/upload"}:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_error(404)

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

    def _read_json_payload_schema(self, schema):
        payload = self._read_json_payload()
        if payload is None:
            return None
        ok, error = _validate_json_payload_schema(payload, schema)
        if not ok:
            self._send_json({"error": error}, 400)
            return None
        return payload

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

    def _require_auth(self, parsed=None):
        parsed = parsed or urlparse(self.path)
        if _is_authorized_request(self.headers, parsed.query):
            return True
        self._send_json({"error": "unauthorized"}, 401)
        return False

    def _require_write_confirmation(self):
        if _unsafe_write_confirmed(self.headers):
            return True
        self._send_json(
            {
                "error": "unsafe action requires confirmation",
                "hint": "WEB_ACCESS_TOKEN 未设置时，写操作必须携带 X-Web-Unsafe-Action: confirm；公网部署建议设置 WEB_ACCESS_TOKEN。",
            },
            403,
        )
        return False

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
        started_at = time.monotonic()
        while SSE_MAX_CONNECTION_SECONDS <= 0 or time.monotonic() - started_at < SSE_MAX_CONNECTION_SECONDS:
            try:
                _sync_books_from_disk_for_sse()
                payload = json.dumps(_public_state(), ensure_ascii=False)
                self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(max(SSE_STATE_INTERVAL_SECONDS, 0.01))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            except OSError:
                return
        try:
            self.wfile.write(b"event: retry\ndata: {}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
        return

    def _send_public_file(self, path):
        access_error = _public_file_access_error(path)
        if access_error is not None:
            status, message = access_error
            self._send_json({"error": message}, status)
            return
        try:
            size = os.path.getsize(path)
            f = open(path, "rb")
        except FileNotFoundError:
            self._send_json({"error": "file not found"}, 404)
            return
        except OSError as exc:
            self._send_json({"error": "file read failed", "detail": str(exc)}, 500)
            return
        try:
            content_type = "application/json; charset=utf-8" if path.lower().endswith(".json") else "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with f:
                while True:
                    chunk = f.read(FILE_RESPONSE_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            return

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
            self._send_json(_health_summary())
            return
        if parsed.path == "/api/state":
            if not self._require_auth(parsed):
                return
            _sync_books_from_disk()
            self._send_json(_public_state())
            return
        if parsed.path == "/api/diagnostics":
            if not self._require_auth(parsed):
                return
            self._send_json(_diagnostics_summary())
            return
        if parsed.path == "/api/events":
            if not self._require_auth(parsed):
                return
            self._send_sse_state_stream()
            return
        if parsed.path == "/api/book":
            if not self._require_auth(parsed):
                return
            params = parse_qs(parsed.query)
            book_id = (params.get("id") or [""])[0]
            detail = _book_detail(book_id)
            if not detail:
                self._send_json({"error": "book not found"}, 404)
                return
            self._send_json(detail)
            return
        if parsed.path == "/files":
            if not self._require_auth(parsed):
                return
            params = parse_qs(parsed.query)
            path = unquote((params.get("path") or [""])[0])
            self._send_public_file(path)
            return
        self._send_not_found(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/profile":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(PROFILE_PAYLOAD_SCHEMA)
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
                try:
                    _save_state()
                except (PermissionError, OSError) as exc:
                    self._send_storage_error(exc)
                    return
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/config":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(CONFIG_PAYLOAD_SCHEMA)
            if payload is None:
                return
            ok, result = _update_runtime_config(payload.get("config"))
            if not ok:
                self._send_json({"error": result}, 400)
                return
            self._send_json({"ok": True, "config": result})
            return
        if parsed.path == "/api/enqueue":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(BOOK_ID_PAYLOAD_SCHEMA)
            if payload is None:
                return
            try:
                ok, result = _enqueue(payload.get("book_id"))
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json(_operation_response(ok, result), 200 if ok else 409)
            return
        if parsed.path == "/api/enqueue-batch":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(BOOK_IDS_PAYLOAD_SCHEMA)
            if payload is None:
                return
            book_ids = payload.get("book_ids")
            try:
                result = _enqueue_many(book_ids)
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json({"ok": bool(result["queued"]), "result": result}, 200)
            return
        if parsed.path == "/api/retry-failed":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(RETRY_FAILED_PAYLOAD_SCHEMA)
            if payload is None:
                return
            try:
                result = _retry_failed_tasks_by_type(payload.get("failure_types") or [])
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json({"ok": bool(result["queued"]), "result": result}, 200)
            return
        if parsed.path == "/api/cancel":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(BOOK_ID_PAYLOAD_SCHEMA)
            if payload is None:
                return
            try:
                ok, result = _cancel_queued_book(payload.get("book_id"))
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json(_operation_response(ok, result), 200 if ok else 409)
            return
        if parsed.path == "/api/prioritize":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(BOOK_ID_PAYLOAD_SCHEMA)
            if payload is None:
                return
            try:
                ok, result = _prioritize_queued_book(payload.get("book_id"))
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json(_operation_response(ok, result), 200 if ok else 409)
            return
        if parsed.path == "/api/move-queue":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(MOVE_QUEUE_PAYLOAD_SCHEMA)
            if payload is None:
                return
            try:
                ok, result = _move_queued_book(payload.get("book_id"), payload.get("direction"))
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json(_operation_response(ok, result), 200 if ok else 409)
            return
        if parsed.path == "/api/delete":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(BOOK_ID_PAYLOAD_SCHEMA)
            if payload is None:
                return
            try:
                ok, result = _delete_book(payload.get("book_id"))
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json(_operation_response(ok, result), 200 if ok else 409)
            return
        if parsed.path == "/api/delete-batch":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            payload = self._read_json_payload_schema(BOOK_IDS_PAYLOAD_SCHEMA)
            if payload is None:
                return
            book_ids = payload.get("book_ids")
            try:
                result = _delete_many_books(book_ids)
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            self._send_json({"ok": bool(result["deleted"]), "result": result}, 200)
            return
        if parsed.path == "/upload":
            if not self._require_auth(parsed):
                return
            if not self._require_write_confirmation():
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json({"error": "invalid content length"}, 400)
                return
            if content_length > MAX_UPLOAD_SIZE + 1024 * 1024:
                self._send_json({"error": f"file too large, max {MAX_UPLOAD_SIZE} bytes"}, 413)
                return
            try:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")},
                )
            except Exception as exc:
                self._send_json({"error": "invalid multipart form", "detail": str(exc)}, 400)
                return
            file_item = form["file"] if "file" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                self._send_json({"error": "missing file"}, 400)
                return
            filename = _safe_filename(file_item.filename)
            try:
                path = os.path.join(_novels_dir(), filename)
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
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
                self._send_json({"error": str(exc)}, 413)
                return
            except (PermissionError, OSError) as exc:
                self._send_storage_error(exc)
                return
            profile_values = form.getlist("profile")
            if not profile_values:
                profile_values = [form.getfirst("profile", "auto")]
            profile = _normalize_web_profile(profile_values if len(profile_values) > 1 else profile_values[0]) or "auto"
            _invalidate_book_outputs(book_id)
            suggestions = _profile_suggestions(path, book_id)
            try:
                suggestion_signature = _book_suggestion_signature(path)
            except (PermissionError, OSError) as exc:
                self._send_json({"error": "uploaded file metadata unavailable", "detail": str(exc)}, 500)
                return
            uploaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
            outputs_reset_after = None
            if overwrite:
                try:
                    outputs_reset_after = float(suggestion_signature.split(":", 1)[0])
                except (IndexError, TypeError, ValueError):
                    outputs_reset_after = time.time()
            with STATE_LOCK:
                if overwrite and book_id in STATE["books"]:
                    for task in STATE.get("tasks", []):
                        if task.get("book_id") == book_id:
                            task["book_replaced"] = True
                STATE["books"][book_id] = {
                    "id": book_id,
                    "name": book_id,
                    "path": path,
                    "profile": profile,
                    "profile_suggestions": suggestions,
                    "suggestion_signature": suggestion_signature,
                    "status": "idle",
                    "message": f"已上传（{uploaded_size} 字节）",
                    "created_at": uploaded_at,
                }
                if overwrite:
                    STATE["books"][book_id]["history_reset_at"] = uploaded_at
                    STATE["books"][book_id]["outputs_reset_after"] = outputs_reset_after
                try:
                    _save_state()
                except (PermissionError, OSError) as exc:
                    self._send_storage_error(exc)
                    return
            self._send_json({"ok": True, "book_id": book_id})
            return
        self._send_not_found(parsed.path)


def run_server(host=None, port=None):
    _try_load_runtime_config("web")
    host = os.environ.get("WEB_HOST", "127.0.0.1") if host is None else host
    port = os.environ.get("WEB_PORT", "8765") if port is None else port
    if not _web_auth_enabled():
        print("[WARN] WEB_ACCESS_TOKEN 未设置：读接口保持开放，写操作需要 X-Web-Unsafe-Action: confirm；公网部署建议设置访问令牌。")
    _load_state()
    _start_worker_once()
    server = TimeoutHTTPServer((host, int(port)), Handler, request_timeout=WEB_REQUEST_TIMEOUT_SECONDS)
    print(f"Web 管理端已启动: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
