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

from analysis_profiles import infer_profile_for_novel, load_analysis_profile, normalize_profile_name
from main import _generate_run_id, get_base_dir, load_configs, process_single_novel


STATE_LOCK = threading.RLock()
TASK_QUEUE = queue.Queue()
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
    _sync_books_from_disk()


def _save_state():
    os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)


def _book_id_from_path(path):
    return os.path.splitext(os.path.basename(path))[0]


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
        _save_state()


def _public_state():
    with STATE_LOCK:
        books = sorted(STATE["books"].values(), key=lambda x: x.get("created_at", ""), reverse=True)
        tasks = list(STATE["tasks"])
    return {"books": books, "tasks": tasks, "config_ready": CONFIG_READY}


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


def _find_book_outputs(book_id):
    results_dir = os.path.join(get_base_dir(), "results")
    outputs = []
    if not os.path.isdir(results_dir):
        return outputs
    patterns = [
        f"{book_id}扫书报告",
        f"{book_id}通用小说报告",
        f"{book_id}_GENERAL_SUMMARY_latest.json",
        f"{book_id}_history_GENERAL_SUMMARY_latest.json",
        f"{book_id}_hard_sci_fi_GENERAL_SUMMARY_latest.json",
    ]
    for root, _dirs, files in os.walk(results_dir):
        for filename in files:
            path = os.path.join(root, filename)
            parent = os.path.basename(root)
            if any(p in filename for p in patterns) or book_id in parent and filename in {"GENERAL_SUMMARY.json", "VERIFIED_REPORT.txt", "FULL_REPORT.txt"}:
                link = _file_link(path)
                if link:
                    link["mtime"] = os.path.getmtime(path)
                    outputs.append(link)
    outputs.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return outputs[:20]


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
    book["outputs"] = _find_book_outputs(book_id)
    book["tasks"] = sorted(tasks, key=lambda x: x.get("created_at", ""), reverse=True)
    return book


def _enqueue(book_id):
    with STATE_LOCK:
        book = STATE["books"].get(book_id)
        if not book:
            return False, "book not found"
        if book.get("status") in {"queued", "running"}:
            return False, "book already queued or running"
        task_id = uuid.uuid4().hex[:12]
        profile_name = normalize_profile_name(book.get("profile", "auto"))
        task = {
            "id": task_id,
            "book_id": book_id,
            "profile": profile_name,
            "status": "queued",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        STATE["tasks"].append(task)
        book["status"] = "queued"
        book["task_id"] = task_id
        book["message"] = "排队中"
        _save_state()
    TASK_QUEUE.put(task_id)
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
                        profile_name = infer_profile_for_novel(book["path"], book.get("name", ""))
                    result = process_single_novel(book["path"], profile_name=profile_name, run_id=_generate_run_id(), skip_fresh=True)
            with STATE_LOCK:
                task["status"] = "completed" if result.get("status") in {"ok", "skipped"} else "failed"
                task["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                task["result"] = result
                book["status"] = task["status"]
                book["active_profile"] = result.get("profile", profile_name)
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
        load_configs(get_base_dir())
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
  <title>NovelReportScanner</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 24px; background: #f7f7f8; color: #222; }
    main { max-width: 1100px; margin: 0 auto; }
    section { background: white; border: 1px solid #ddd; padding: 16px; margin-bottom: 16px; border-radius: 6px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align: left; vertical-align: middle; }
    select, button, input[type=file] { padding: 6px; }
    .status { font-weight: 600; }
    .muted { color: #666; }
  </style>
</head>
<body>
<main>
  <h1>NovelReportScanner</h1>
  <section id="configWarning" style="display:none; border-color:#e0a800; background:#fff8e1;">
    API 配置未就绪：可以先上传和排队，但开始扫描前需要在 api.txt 中写入可用 API Key。
  </section>
  <section>
    <h2>上传小说</h2>
    <form action="/upload" method="post" enctype="multipart/form-data">
      <input type="file" name="file" accept=".txt" required>
      <select name="profile">
        <option value="auto">自动识别</option>
        <option value="harem">后宫/男性向</option>
        <option value="general">通用</option>
        <option value="history">历史</option>
        <option value="hard_sci_fi">硬科幻</option>
      </select>
      <button type="submit">上传</button>
    </form>
  </section>
  <section>
    <h2>书籍列表</h2>
    <table>
      <thead><tr><th>书名</th><th>分类</th><th>状态</th><th>消息</th><th>操作</th></tr></thead>
      <tbody id="books"></tbody>
    </table>
  </section>
  <section>
    <h2>书籍详情</h2>
    <div id="detail" class="muted">点击书籍列表中的“详情”查看任务历史和输出文件。</div>
  </section>
</main>
<script>
const profiles = [
  ["auto", "自动识别"], ["harem", "后宫/男性向"], ["general", "通用"], ["history", "历史"], ["hard_sci_fi", "硬科幻"]
];
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function refresh() {
  const data = await api('/api/state');
  document.getElementById('configWarning').style.display = data.config_ready ? 'none' : 'block';
  const tbody = document.getElementById('books');
  tbody.innerHTML = data.books.map(book => {
    const opts = profiles.map(([v, label]) => `<option value="${v}" ${book.profile === v ? 'selected' : ''}>${label}</option>`).join('');
    const disabled = book.status === 'queued' || book.status === 'running' ? 'disabled' : '';
    return `<tr>
      <td>${esc(book.name)}</td>
      <td><select data-profile="${esc(book.id)}" ${disabled}>${opts}</select></td>
      <td class="status">${esc(book.status || 'idle')}</td>
      <td class="muted">${esc(book.message || '')}</td>
      <td><button data-scan="${esc(book.id)}" ${disabled}>加入队列</button> <button data-detail="${esc(book.id)}">详情</button></td>
    </tr>`;
  }).join('');
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
  const outputs = (book.outputs || []).map(f => `<li><a href="${esc(f.url)}" target="_blank">${esc(f.name)}</a></li>`).join('') || '<li>暂无输出文件</li>';
  const tasks = (book.tasks || []).map(t => {
    const log = t.log_file ? `<a href="${esc(t.log_file.url)}" target="_blank">日志</a>` : '';
    return `<tr><td>${esc(t.id)}</td><td>${esc(t.profile)}</td><td>${esc(t.status)}</td><td>${esc(t.created_at || '')}</td><td>${esc(t.finished_at || t.error || '')}</td><td>${log}</td></tr>`;
  }).join('') || '<tr><td colspan="6">暂无任务</td></tr>';
  document.getElementById('detail').innerHTML = `
    <h3>${esc(book.name)}</h3>
    <p>当前分类：${esc(book.profile)}；实际扫描分类：${esc(book.active_profile || '')}；状态：${esc(book.status)}</p>
    <p>路径：${esc(book.path)}</p>
    <h4>输出文件</h4>
    <ul>${outputs}</ul>
    <h4>任务历史</h4>
    <table><thead><tr><th>任务</th><th>分类</th><th>状态</th><th>创建</th><th>结束/错误</th><th>日志</th></tr></thead><tbody>${tasks}</tbody></table>
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
                book["profile"] = normalize_profile_name(payload.get("profile", "auto"))
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
            profile = normalize_profile_name(form.getfirst("profile", "auto"))
            book_id = _book_id_from_path(path)
            with STATE_LOCK:
                STATE["books"][book_id] = {
                    "id": book_id,
                    "name": book_id,
                    "path": path,
                    "profile": profile,
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
