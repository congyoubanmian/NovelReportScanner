import json
import os
import queue
import threading
import time
import uuid
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning, message="'cgi' is deprecated*")
import cgi
import contextlib
import sys

from analysis_profiles import (
    infer_profile_candidates_for_novel,
    infer_profiles_for_novel,
    list_available_profiles,
    normalize_profile_name,
    profile_options,
)
from main import _generate_run_id, get_base_dir, load_configs, process_novel_with_profiles


STATE_LOCK = threading.RLock()
TASK_QUEUE = queue.Queue()
TASK_QUEUE_IDS = set()
WORKER_STARTED = False
STATE = {"books": {}, "tasks": []}
CONFIG_READY = False
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(100 * 1024 * 1024)))
SYNC_BOOKS_TTL_SECONDS = float(os.environ.get("SYNC_BOOKS_TTL_SECONDS", "5"))
OUTPUTS_CACHE_TTL_SECONDS = float(os.environ.get("OUTPUTS_CACHE_TTL_SECONDS", "5"))
LAST_BOOK_SYNC_AT = 0.0
OUTPUTS_CACHE = {}


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


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
    stat = os.stat(book["path"])
    signature = f"{stat.st_mtime}:{stat.st_size}"
    if book.get("suggestion_signature") == signature and book.get("profile_suggestions"):
        return
    book["profile_suggestions"] = _profile_suggestions(book["path"], book.get("name", ""))
    book["suggestion_signature"] = signature


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
    with STATE_LOCK:
        for path, book_id in discovered:
            entry = STATE["books"].setdefault(book_id, {})
            entry.setdefault("id", book_id)
            entry.setdefault("name", book_id)
            entry["path"] = path
            entry.setdefault("profile", "auto")
            entry.setdefault("status", "idle")
            entry.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            _refresh_book_suggestions(entry)
        _save_state()
        LAST_BOOK_SYNC_AT = now


def _public_state():
    with STATE_LOCK:
        books = _with_queue_positions(sorted(STATE["books"].values(), key=lambda x: x.get("created_at", ""), reverse=True))
        tasks = _with_queue_positions(list(STATE["tasks"]))
    return {"books": books, "tasks": tasks, "config_ready": CONFIG_READY, "profiles": profile_options(include_auto=True)}


def _put_task_queue(task_id):
    if not task_id or task_id in TASK_QUEUE_IDS:
        return False
    TASK_QUEUE_IDS.add(task_id)
    TASK_QUEUE.put(task_id)
    return True


def _queued_task_positions():
    queued = [task for task in STATE.get("tasks", []) if task.get("status") == "queued"]
    queued.sort(key=lambda x: x.get("created_at", ""))
    return {task.get("id"): index + 1 for index, task in enumerate(queued)}


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


def _find_book_outputs(book_id):
    now = time.monotonic()
    results_dir = os.path.join(get_base_dir(), "results")
    cache_key = (os.path.abspath(results_dir), book_id)
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
    if outputs:
        OUTPUTS_CACHE[cache_key] = {"time": now, "outputs": outputs}
    else:
        OUTPUTS_CACHE.pop(cache_key, None)
    return outputs


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
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        STATE["tasks"].append(task)
        book["status"] = "queued"
        book["task_id"] = task_id
        book["message"] = "排队中"
        _save_state()
    _put_task_queue(task_id)
    return True, task_id


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
                tee_out = _Tee(sys.stdout, log_file)
                tee_err = _Tee(sys.stderr, log_file)
                with contextlib.redirect_stdout(tee_out), contextlib.redirect_stderr(tee_err):
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
                    result = process_novel_with_profiles(book["path"], profile_name=profile_name, run_id=_generate_run_id(), skip_fresh=True)
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
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            if not _is_safe_public_file(path):
                self.send_error(403, "file is not allowed")
                return
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            content_type = "application/json; charset=utf-8" if path.lower().endswith(".json") else "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/profile":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
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
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            ok, result = _enqueue(payload.get("book_id"))
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
            try:
                uploaded_size = _save_upload_file(file_item, path)
            except ValueError as exc:
                self.send_error(413, str(exc))
                return
            profile_values = form.getlist("profile")
            if not profile_values:
                profile_values = [form.getfirst("profile", "auto")]
            profile = _normalize_web_profile(profile_values if len(profile_values) > 1 else profile_values[0]) or "auto"
            book_id = _book_id_from_path(path)
            OUTPUTS_CACHE.pop((os.path.abspath(os.path.join(get_base_dir(), "results")), book_id), None)
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
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"Web 管理端已启动: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8765"))
    run_server(host, port)
