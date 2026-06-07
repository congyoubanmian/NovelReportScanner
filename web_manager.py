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
    infer_profile_for_novel,
    list_available_profiles,
    normalize_profile_name,
    profile_options,
)
from main import _generate_run_id, get_base_dir, load_configs, process_single_novel


STATE_LOCK = threading.RLock()
TASK_QUEUE = queue.Queue()
TASK_QUEUE_IDS = set()
WORKER_STARTED = False
STATE = {"books": {}, "tasks": []}
CONFIG_READY = False


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
    os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)


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
        return infer_profile_candidates_for_novel(path, book_name, min_score=1)[:4]
    except Exception as exc:
        return [{"name": "general", "display_name": "通用小说分析", "score": 0, "confidence": 1.0, "matched_keywords": [], "error": str(exc)}]


def _valid_profile_names():
    return {item["name"] for item in profile_options(include_auto=True)}


def _normalize_web_profile(value):
    profile_name = normalize_profile_name(value or "auto")
    if profile_name not in _valid_profile_names():
        return None
    return profile_name


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
    with STATE_LOCK:
        for root, _dirs, files in os.walk(_novels_dir()):
            for filename in files:
                if not filename.lower().endswith(".txt"):
                    continue
                path = os.path.join(root, filename)
                book_id = _book_id_from_path(path)
                entry = STATE["books"].setdefault(book_id, {})
                entry.setdefault("id", book_id)
                entry.setdefault("name", book_id)
                entry["path"] = path
                entry.setdefault("profile", "auto")
                entry.setdefault("status", "idle")
                entry.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
                _refresh_book_suggestions(entry)
        _save_state()


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
    results_dir = os.path.join(get_base_dir(), "results")
    outputs_by_path = {}
    if not os.path.isdir(results_dir):
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
    return outputs[:100]


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
        profile_name = normalize_profile_name(book.get("profile", "auto"))
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
                        profile_name = infer_profile_for_novel(book["path"], book.get("name", ""))
                        task["resolved_profile"] = profile_name
                    result = process_single_novel(book["path"], profile_name=profile_name, run_id=_generate_run_id(), skip_fresh=True)
            with STATE_LOCK:
                task["status"] = "completed" if result.get("status") in {"ok", "skipped"} else "failed"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["result"] = result
                book["status"] = task["status"]
                book["active_profile"] = result.get("profile", profile_name)
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


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NovelReportScanner</title>
  <style>
    :root {
      --bg: #f0f2f5;
      --card: #ffffff;
      --text: #1f2937;
      --text-secondary: #6b7280;
      --border: #e5e7eb;
      --primary: #4f46e5;
      --primary-hover: #4338ca;
      --primary-light: #eef2ff;
      --success: #10b981;
      --success-bg: #ecfdf5;
      --warning: #f59e0b;
      --warning-bg: #fffbeb;
      --danger: #ef4444;
      --danger-bg: #fef2f2;
      --info: #3b82f6;
      --info-bg: #eff6ff;
      --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 12px rgba(0,0,0,0.05);
      --shadow-lg: 0 10px 25px rgba(0,0,0,0.08), 0 4px 10px rgba(0,0,0,0.04);
      --radius: 12px;
      --radius-sm: 8px;
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      margin: 0; padding: 0;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
    }
    .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
    header {
      background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
      color: white;
      padding: 40px 24px;
      margin-bottom: 32px;
      box-shadow: var(--shadow-lg);
    }
    header .container { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px; }
    header h1 { margin: 0; font-size: 1.75rem; font-weight: 700; letter-spacing: -0.5px; }
    header p { margin: 4px 0 0; opacity: 0.9; font-size: 0.95rem; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 4px 12px; border-radius: 999px; font-size: 0.75rem; font-weight: 600;
      background: rgba(255,255,255,0.2); color: white;
    }
    .card {
      background: var(--card);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 24px;
      margin-bottom: 24px;
      border: 1px solid var(--border);
      transition: box-shadow 0.2s;
    }
    .card:hover { box-shadow: var(--shadow-lg); }
    .card-title {
      font-size: 1.15rem; font-weight: 600; margin: 0 0 16px;
      display: flex; align-items: center; gap: 10px;
    }
    .card-title .icon { font-size: 1.3rem; }

    /* Upload area */
    .upload-wrap { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; }
    .file-input-wrapper {
      position: relative; flex: 1; min-width: 240px;
      border: 2px dashed var(--border); border-radius: var(--radius-sm);
      padding: 16px; text-align: center; cursor: pointer;
      transition: border-color 0.2s, background 0.2s;
    }
    .file-input-wrapper:hover { border-color: var(--primary); background: var(--primary-light); }
    .file-input-wrapper input[type="file"] {
      position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
    }
    .file-input-wrapper .label { color: var(--text-secondary); font-size: 0.9rem; pointer-events: none; }
    .file-input-wrapper .label strong { color: var(--primary); }
    select, button {
      font-family: inherit; font-size: 0.95rem; border-radius: var(--radius-sm); outline: none;
      transition: all 0.15s;
    }
    select {
      padding: 10px 14px; border: 1px solid var(--border); background: var(--card); color: var(--text); cursor: pointer;
    }
    select:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(79,70,229,0.12); }
    .btn {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 10px 20px; border: none; cursor: pointer; font-weight: 500;
      background: var(--primary); color: white;
    }
    .btn:hover { background: var(--primary-hover); transform: translateY(-1px); }
    .btn:active { transform: translateY(0); }
    .btn:disabled { background: #c7c7c7; cursor: not-allowed; transform: none; }
    .btn-secondary { background: #6b7280; }
    .btn-secondary:hover { background: #4b5563; }
    .btn-sm { padding: 6px 12px; font-size: 0.85rem; }

    /* Warning banner */
    .banner {
      display: none; align-items: center; gap: 10px;
      padding: 14px 18px; border-radius: var(--radius-sm); margin-bottom: 24px;
      font-size: 0.9rem; font-weight: 500;
    }
    .banner.warn { background: var(--warning-bg); color: #92400e; border: 1px solid #fcd34d; }

    /* Status tags */
    .tag {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 3px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 600;
      white-space: nowrap;
    }
    .tag-running { background: var(--info-bg); color: #1e40af; }
    .tag-queued  { background: var(--warning-bg); color: #92400e; }
    .tag-completed { background: var(--success-bg); color: #065f46; }
    .tag-failed { background: var(--danger-bg); color: #991b1b; }
    .tag-interrupted { background: #f3f4f6; color: #4b5563; }
    .tag-idle { background: #f3f4f6; color: #6b7280; }
    .tag-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    .tag-running .tag-dot { animation: pulse 1.5s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

    /* Tables */
    .table-wrap { overflow-x: auto; border-radius: var(--radius-sm); border: 1px solid var(--border); }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    thead { background: #f9fafb; }
    th { padding: 12px 16px; text-align: left; font-weight: 600; color: #374151; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.4px; border-bottom: 1px solid var(--border); white-space: nowrap; }
    td { padding: 14px 16px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    tbody tr { transition: background 0.12s; }
    tbody tr:hover { background: #f9fafb; }
    tbody tr:last-child td { border-bottom: none; }
    .col-name { font-weight: 500; color: #111827; }
    .col-msg { color: var(--text-secondary); font-size: 0.85rem; max-width: 280px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }

    /* Detail panel */
    .detail-empty { color: var(--text-secondary); text-align: center; padding: 40px 20px; font-style: italic; }
    .detail-header { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
    .detail-header h3 { margin: 0; font-size: 1.25rem; }
    .detail-meta { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 20px; font-size: 0.9rem; color: var(--text-secondary); }
    .detail-meta span { display: inline-flex; align-items: center; gap: 6px; }
    .detail-meta .label { font-weight: 500; color: var(--text); }
    .file-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
    .file-list li a {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 14px; border-radius: var(--radius-sm);
      background: var(--primary-light); color: var(--primary);
      text-decoration: none; font-weight: 500; font-size: 0.9rem;
      transition: background 0.15s;
    }
    .file-list li a:hover { background: #e0e7ff; }
    .file-list li a::before { content: "📄"; }
    .suggestion-chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .chip {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 10px; border-radius: 999px; font-size: 0.8rem;
      background: #f3f4f6; color: #374151; border: 1px solid var(--border);
    }
    .chip .score { font-weight: 600; color: var(--primary); }

    /* Responsive */
    @media (max-width: 768px) {
      header h1 { font-size: 1.4rem; }
      .container { padding: 16px; }
      .card { padding: 16px; }
      th, td { padding: 10px 12px; }
    }
  </style>
</head>
<body>
<header>
  <div class="container">
    <div>
      <h1>📚 NovelReportScanner</h1>
      <p>小说扫书分析工具 — 上传、分类、扫描、报告一站式管理</p>
    </div>
    <div class="badge" id="configBadge">⚙️ 配置检查中</div>
  </div>
</header>

<div class="container">
  <div class="banner warn" id="configWarning">
    <span>⚠️</span> API 配置未就绪：可以先上传和排队，但开始扫描前需要在 api.txt 中写入可用 API Key。
  </div>

  <div class="card">
    <div class="card-title"><span class="icon">⬆️</span> 上传小说</div>
    <form action="/upload" method="post" enctype="multipart/form-data">
      <div class="upload-wrap">
        <div class="file-input-wrapper">
          <input type="file" name="file" id="fileInput" accept=".txt" required onchange="document.getElementById('fileLabel').textContent=this.files[0]?this.files[0].name:'点击或拖拽上传 .txt 小说文件'">
          <div class="label" id="fileLabel">点击或拖拽上传 <strong>.txt</strong> 小说文件</div>
        </div>
        <select id="uploadProfile" name="profile">
          <option value="auto">自动识别</option>
        </select>
        <button type="submit" class="btn">上传</button>
      </div>
    </form>
  </div>

  <div class="card">
    <div class="card-title"><span class="icon">📖</span> 书籍列表</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>书名</th>
            <th>分类</th>
            <th>自动建议</th>
            <th>状态</th>
            <th>消息</th>
            <th style="text-align:right">操作</th>
          </tr>
        </thead>
        <tbody id="books"></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="card-title"><span class="icon">🔍</span> 书籍详情</div>
    <div id="detail" class="detail-empty">点击书籍列表中的「详情」查看任务历史和输出文件。</div>
  </div>
</div>

<script>
let profiles = [{name: 'auto', display_name: '自动识别'}];
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function renderProfileOptions(selected) {
  return profiles.map(p => `<option value="${esc(p.name)}" ${selected === p.name ? 'selected' : ''}>${esc(p.display_name || p.name)}</option>`).join('');
}
function statusTag(status) {
  const map = {
    running: {cls:'tag-running', icon:'▶️', label:'扫描中'},
    queued:  {cls:'tag-queued',  icon:'⏳', label:'排队中'},
    completed:{cls:'tag-completed', icon:'✅', label:'已完成'},
    failed:  {cls:'tag-failed',  icon:'❌', label:'失败'},
    interrupted:{cls:'tag-interrupted', icon:'⏸️', label:'中断'},
    idle:    {cls:'tag-idle',    icon:'💤', label:'空闲'},
  };
  const m = map[status] || {cls:'tag-idle', icon:'', label:status};
  return `<span class="tag ${m.cls}"><span class="tag-dot"></span>${m.icon} ${esc(m.label)}</span>`;
}
function renderSuggestions(book) {
  const suggestions = book.profile_suggestions || [];
  if (!suggestions.length) return '<span style="color:var(--text-secondary);font-size:0.85rem">暂无</span>';
  return '<div class="suggestion-chips">' + suggestions.map(s => {
    const words = (s.matched_keywords || []).slice(0, 5).join('、');
    return `<span class="chip" title="${esc(words)}">${esc(s.display_name || s.name)} <span class="score">${esc(s.score)}</span></span>`;
  }).join('') + '</div>';
}
function syncUploadProfileOptions() {
  const select = document.getElementById('uploadProfile');
  const current = select.value || 'auto';
  select.innerHTML = renderProfileOptions(current);
  if (![...select.options].some(o => o.value === current)) select.value = 'auto';
}
async function refresh() {
  const data = await api('/api/state');
  profiles = data.profiles || profiles;
  syncUploadProfileOptions();

  const warn = document.getElementById('configWarning');
  warn.style.display = data.config_ready ? 'none' : 'flex';

  const badge = document.getElementById('configBadge');
  if (data.config_ready) {
    badge.innerHTML = '✅ 配置就绪';
    badge.style.background = 'rgba(255,255,255,0.25)';
  } else {
    badge.innerHTML = '⚠️ 配置未就绪';
    badge.style.background = 'rgba(255,255,255,0.25)';
  }

  const tbody = document.getElementById('books');
  if (!data.books.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-secondary);padding:28px">暂无书籍，请先上传小说</td></tr>';
  } else {
    tbody.innerHTML = data.books.map(book => {
      const opts = renderProfileOptions(book.profile);
      const disabled = book.status === 'queued' || book.status === 'running' ? 'disabled' : '';
      return `<tr>
        <td class="col-name">${esc(book.name)}</td>
        <td><select data-profile="${esc(book.id)}" ${disabled} style="min-width:120px">${opts}</select></td>
        <td>${renderSuggestions(book)}</td>
        <td>${statusTag(book.status || 'idle')}</td>
        <td class="col-msg">${esc(book.message || '')}</td>
        <td style="text-align:right"><div class="actions"><button class="btn btn-sm" data-scan="${esc(book.id)}" ${disabled}>加入队列</button><button class="btn btn-sm btn-secondary" data-detail="${esc(book.id)}">详情</button></div></td>
      </tr>`;
    }).join('');
  }
  document.querySelectorAll('[data-profile]').forEach(sel => {
    sel.onchange = () => api('/api/profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({book_id: sel.dataset.profile, profile: sel.value})}).then(refresh);
  });
  document.querySelectorAll('[data-scan]').forEach(btn => {
    btn.onclick = () => api('/api/enqueue', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({book_id: btn.dataset.scan})}).then(refresh);
  });
  document.querySelectorAll('[data-detail]').forEach(btn => {
    btn.onclick = () => showDetail(btn.dataset.detail);
  });
}
async function showDetail(bookId) {
  const book = await api('/api/book?id=' + encodeURIComponent(bookId));
  const outputs = (book.outputs || []).map(f => `<li><a href="${esc(f.url)}" target="_blank">${esc(f.name)}</a></li>`).join('') || '<li style="color:var(--text-secondary)">暂无输出文件</li>';
  const suggestions = renderSuggestions(book);
  const tasks = (book.tasks || []).map(t => {
    const log = t.log_file ? `<a href="${esc(t.log_file.url)}" target="_blank" style="color:var(--primary);text-decoration:none;font-weight:500">📋 日志</a>` : '';
    const status = t.queue_position ? `${t.status} #${t.queue_position}` : t.status;
    return `<tr><td style="font-family:monospace;font-size:0.82rem">${esc(t.id)}</td><td>${esc(t.profile)}</td><td>${esc(t.resolved_profile || '—')}</td><td>${statusTag(status)}</td><td style="font-size:0.82rem;white-space:nowrap">${esc(t.created_at || '—')}</td><td style="font-size:0.82rem;color:var(--text-secondary)">${esc(t.finished_at || t.error || '—')}</td><td style="text-align:center">${log}</td></tr>`;
  }).join('') || '<tr><td colspan="7" style="text-align:center;color:var(--text-secondary);padding:20px">暂无任务</td></tr>';

  document.getElementById('detail').innerHTML = `
    <div class="detail-header">
      <h3>${esc(book.name)}</h3>
      ${statusTag(book.status)}
    </div>
    <div class="detail-meta">
      <span><span class="label">当前分类:</span> ${esc(book.profile)}</span>
      <span><span class="label">实际扫描:</span> ${esc(book.active_profile || '—')}</span>
      <span><span class="label">路径:</span> <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:0.82rem">${esc(book.path)}</code></span>
    </div>
    <div style="margin-bottom:20px">
      <div style="font-size:0.85rem;font-weight:600;color:#374151;margin-bottom:8px">🎯 自动建议</div>
      ${suggestions}
    </div>
    <div style="margin-bottom:20px">
      <div style="font-size:0.85rem;font-weight:600;color:#374151;margin-bottom:10px">📁 输出文件</div>
      <ul class="file-list">${outputs}</ul>
    </div>
    <div>
      <div style="font-size:0.85rem;font-weight:600;color:#374151;margin-bottom:10px">📜 任务历史</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>任务ID</th><th>分类</th><th>实际分类</th><th>状态</th><th>创建时间</th><th>结束/错误</th><th style="text-align:center">日志</th></tr></thead>
          <tbody>${tasks}</tbody>
        </table>
      </div>
    </div>
  `;
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path="/"):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")})
            file_item = form["file"] if "file" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                self.send_error(400, "missing file")
                return
            filename = _safe_filename(file_item.filename)
            path = os.path.join(_novels_dir(), filename)
            with open(path, "wb") as f:
                f.write(file_item.file.read())
            profile = _normalize_web_profile(form.getfirst("profile", "auto")) or "auto"
            book_id = _book_id_from_path(path)
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
                    "message": "已上传",
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                _save_state()
            self._redirect("/")
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
