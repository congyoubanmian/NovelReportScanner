# NovelReportScanner 优化报告

> 生成时间：2026-06-07
> 覆盖范围：后端、前端、Docker、部署、功能、代码规范

---

## 目录

1. [后端安全与性能](#1-后端安全与性能)
2. [前端代码质量](#2-前端代码质量)
3. [Docker & 部署](#3-docker--部署)
4. [缺失的核心功能](#4-缺失的核心功能)
5. [代码规范与仓库整洁度](#5-代码规范与仓库整洁度)
6. [推荐执行顺序](#6-推荐执行顺序)

---

## 1. 后端安全与性能

### 🔴 P0-1: 文件上传无大小限制，存在 DoS 风险

**问题描述**：
`/upload` 接口使用 `cgi.FieldStorage` 和 `file_item.file.read()` 一次性将整个上传文件读入内存，且没有文件大小限制。攻击者可上传数 GB 的超大文件导致容器 OOM 或磁盘耗尽。

```python
# 当前代码（web_manager.py）
with open(path, "wb") as f:
    f.write(file_item.file.read())   # 无限制读取
```

**优化方案**：
- 增加上传大小限制（如 50MB）
- 使用分块写入代替一次性 `read()`

```python
_MAX_UPLOAD_SIZE = 50 * 1024 * 1024

def _save_upload(file_item, dest_path):
    size = 0
    with open(dest_path, "wb") as f:
        while chunk := file_item.file.read(65536):
            size += len(chunk)
            if size > _MAX_UPLOAD_SIZE:
                raise ValueError("file too large")
            f.write(chunk)
```

---

### 🔴 P0-2: 状态锁持有时间过长，阻塞所有 API 请求

**问题描述**：
`/api/state` 每次调用都会触发 `_sync_books_from_disk()`，其在 `STATE_LOCK` 保护下遍历磁盘并调用 `_refresh_book_suggestions()`（会读取文件并做文本分析）。如果 novels 目录文件多，所有请求都会被阻塞。

```python
# 当前代码：锁内做 IO 和 CPU 密集型分析
with STATE_LOCK:
    for root, _dirs, files in os.walk(_novels_dir()):
        ...
        _refresh_book_suggestions(entry)
    _save_state()
```

**优化方案**：
- 将 `os.walk` 收集到的路径信息在锁外预处理，锁内只做轻量状态合并
- `_refresh_book_suggestions` 改为异步/延迟更新，或在单独的后台线程中批量更新，而非每次 API 调用时同步执行

---

### 🟡 P1-1: `_worker_loop` 全局重定向 stdout/stderr，影响多线程安全

**问题描述**：
Worker 使用 `contextlib.redirect_stdout(tee_out)` 会在全局范围内替换 `sys.stdout`，虽然当前只有一个 worker 线程，但如果业务代码内部再开线程，输出会被错误地交叉写入日志。

**优化方案**：
- 不要全局重定向 `sys.stdout`。改为在每个子模块的入口显式传入 `logger` 参数，或在 `process_single_novel` 中使用 Python `logging` 模块的 `FileHandler` + `StreamHandler`

---

### 🟡 P1-2: `_find_book_outputs` 每次全量遍历 results 目录

**问题描述**：
每次查看书籍详情都会递归遍历整个 `results/` 目录，文件多了性能很差。

**优化方案**：
- 建立反向索引（如 `STATE["book_outputs"]`），在任务完成时直接记录输出文件路径，查询时 O(1)
- 或至少增加缓存和目录监听（如 `watchdog`）增量更新

---

### 🟡 P1-3: 容器以 root 运行

**问题描述**：
Dockerfile 没有创建非 root 用户。

**优化方案**：

```dockerfile
RUN useradd -m appuser && chown -R appuser /app
USER appuser
```

> 注意：`novels/` 和 `results/` 目录的权限需要兼容宿主机卷映射

---

### 🟢 P2-1: 使用 `http.server` 生产环境不够稳健

**问题描述**：
`ThreadingHTTPServer` 缺少 graceful shutdown、请求超时、最大请求体限制等。

**优化方案**：
- **短期**：增加 `socket.timeout` 和 `request` 超时
- **中期**：迁移到 `uvicorn` + `fastapi` 或 `flask` + `gunicorn`，可自动获得 CORS、请求验证、自动文档、更好的并发模型

---

### 🟢 P2-2: 缺少 CORS 配置

**问题描述**：
如果前端独立部署（如 Vercel）或移动端访问，会产生跨域错误。

**优化方案**：

```python
# 在 Handler 中添加
class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()
```

---

### 🟢 P2-3: 状态持久化 JSON 存在并发写入风险

**问题描述**：
`_save_state()` 直接 `json.dump` 覆盖文件，如果进程此时崩溃，文件可能损坏成半写入状态。

**优化方案**：
- 使用原子写入：先写入临时文件，再 `os.replace()`

```python
def _save_state():
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
```

---

## 2. 前端代码质量

### 🟡 P1-4: 大量 CSS 重复，维护困难

**问题描述**：
`.card`、`.card-title`、`.table-wrap`、`table/th/td` 等样式在 `BookList.vue`、`BookDetail.vue` 中几乎完全一致。一旦要改设计系统，需要修改多处。

**优化方案**：
- 提取公共基础样式到 `frontend/src/styles/base.css`，在 `main.js` 中全局引入
- 或使用 CSS 变量定义设计令牌

```css
/* styles/vars.css */
:root {
  --color-primary: #4f46e5;
  --radius-card: 14px;
  --shadow-card: 0 1px 3px rgba(0,0,0,0.06), 0 4px 12px rgba(0,0,0,0.04);
}
```

---

### 🟡 P1-5: `BookList.vue` 中 `renderSuggestions` 重复计算

**问题描述**：
```vue
<div class="suggestion-chips" v-if="renderSuggestions(book)?.length">
  <span v-for="(s, i) in renderSuggestions(book)" :key="i">
```
每次渲染会对每本书调用两次 `renderSuggestions`。

**优化方案**：
- 将 suggestions 计算逻辑提升到父组件 `App.vue` 或使用 `computed` 在 `BookList` 中为每本书预计算（Vue 无法对方法返回值做缓存）

---

### 🟢 P2-4: `App.vue` 导入了未使用的 `StatusTag`

**优化方案**：移除未使用的 import

---

### 🟢 P2-5: `BookUpload.vue` 上传失败用原生 `alert`

**问题描述**：
```javascript
} catch (e) {
    alert('上传失败: ' + e.message)
}
```

**优化方案**：通过 `emit('error', e.message)` 交由 `App.vue` 的 `showToast` 统一展示

---

### 🟢 P2-6: 缺少前端请求超时和错误拦截

**问题描述**：
`api.js` 中直接使用 `fetch`，没有超时控制，网络异常时会挂起。

**优化方案**：

```javascript
async function api(path, options = {}) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 15000)
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal
    })
    // ...
  } finally {
    clearTimeout(timeout)
  }
}
```

---

### 🟢 P2-7: 轮询没有页面可见性控制

**问题描述**：
`setInterval(refresh, 3000)` 即使页面在后台标签页也会持续发送请求。

**优化方案**：

```javascript
onMounted(() => {
  refresh()
  timer = setInterval(refresh, 3000)
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearInterval(timer)
    else timer = setInterval(refresh, 3000)
  })
})
```

---

### 🟢 P2-8: `frontend/src/composables/` 目录为空

**问题描述**：
项目结构预留了 `composables/` 但没有使用，`showToast`、polling 逻辑等都可以提取到这里。

**优化方案**：
- 创建 `useToast.js`、`usePolling.js`、`useApi.js` 提取公共逻辑

---

## 3. Docker & 部署

### 🟡 P1-6: `COPY . .` 复制了过多不需要的文件

**问题描述**：
`.dockerignore` 虽然排除了 `.git`、`__pycache__` 等，但缺少：
- `*.md`（README 等不需要在运行时）
- `tests/`
- `frontend/src/`（构建后源码不需要）
- `*.command`, `*.bat`

**优化方案**：完善 `.dockerignore`

```
.git
.gitignore
.env
.env.*
.venv
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
results/
novels/
api.txt
setting.txt
*.log
*.md
tests/
frontend/src/
frontend/public/
*.command
*.bat
*.tar
```

---

### 🟡 P1-7: `docker-compose.yml` 卷映射可能引发权限问题

**问题描述**：
容器内以 root 运行，写入 `./results` 和 `./novels` 的文件在 Linux 宿主机上会变成 root 所有，导致宿主机用户无法直接管理。

**优化方案**：
- Dockerfile 中创建 UID/GID 与宿主机一致的 `appuser`（或通过 compose 的 `user: "${UID}:${GID}"` 映射）
- 或至少在 README 中提示用户注意权限

---

### 🟢 P2-9: Docker 中 `load_configs` 会尝试读取 `api.txt`

**问题描述**：
`docker-compose.yml` 没有挂载 `api.txt` 和 `setting.txt`，web_manager 启动时依赖环境变量。但 `main.py` 的 `load_configs` 在找不到 key 时会尝试读取 `api.txt` 并调用 `sys.exit()`，这在 worker 中会导致任务直接失败。

**优化方案**：
- 确保 Docker 环境下 `API_KEY` 或 `API_KEY_POOL` 环境变量始终被设置
- 在 `docker-compose.yml` 中标注 `API_KEY` 为 required（去掉默认空值），并在 README 中强调

---

### 🟢 P2-10: 缺少资源限制

**问题描述**：
LLM 扫描可能消耗大量内存，容器没有限制。

**优化方案**：

```yaml
deploy:
  resources:
    limits:
      memory: 2G
    reservations:
      memory: 512M
```

---

## 4. 缺失的核心功能

按用户价值和实现成本排序：

| 优先级 | 功能 | 说明 |
|--------|------|------|
| **P1** | **批量加入队列 / 扫描全部** | 当前只能单本操作，用户上传 10 本书后需要点 10 次 |
| **P1** | **任务取消 / 删除书籍** | 一旦排队无法取消；书籍无法从 Web 端移除 |
| **P1** | **实时日志/进度推送** | 用 SSE (EventSource) 替代 3 秒轮询，大幅降低服务端压力和提升实时性 |
| **P2** | **扫描结果在线预览** | 当前只能下载 `.txt` 报告，Web 端直接渲染 Markdown/纯文本能极大提升体验 |
| **P2** | **队列优先级调整** | 支持拖拽排序或置顶某本书 |
| **P2** | **Web 端配置管理** | 在 UI 上设置 API Key、模型、限流参数，无需 SSH 进容器改文件 |
| **P2** | **Token 用量可视化** | 读取 `results/token_usage.json` 展示每本书/每次运行的 token 消耗 |
| **P3** | **重复上传检测** | 同名文件直接覆盖，应提示用户并支持"覆盖"或"重命名" |
| **P3** | **夜间模式** | 纯前端增强，提升长时间阅读报告的体验 |

**推荐最先做的三个功能**：

1. **SSE 实时推送**（替代轮询）：后端增加 `/api/events` SSE 端点，任务状态变化时推送；前端移除 `setInterval`
2. **批量操作**：在 `BookList` 增加全选框和"批量加入队列"按钮
3. **报告预览**：在 `BookDetail` 中增加一个标签页，点击后直接请求 `/files?path=...` 并展示文本内容

---

## 5. 代码规范与仓库整洁度

### 🟡 P1-8: Git 追踪了不应提交的大文件和构建产物

**问题**：
- `novel-report-scanner_latest.tar`（约 50MB）被提交到 git
- `results/learned_keywords/` 下的运行时生成文件被提交
- `novels/你想扫描的书.txt` 作为示例文件被提交（可以接受，但需警惕用户上传其他小说被误提交）

**优化方案**：

```bash
git rm --cached novel-report-scanner_latest.tar
git rm --cached results/learned_keywords/learned_*.json
# 保留 seed.json 作为模板
```

---

### 🟢 P2-11: `.gitignore` 不完善

**问题**：缺少常见的编辑器/系统文件排除。

**优化方案**：

```
# 追加到 .gitignore
.DS_Store
Thumbs.db
*.swp
*.swo
*.swn
.idea/
.vscode/
*.log
```

---

### 🟢 P2-12: 根目录配置文件混乱

**问题**：
- `api.txt` 是空文件但已追踪，功能与 `.env` 重复
- `setting.txt` 被追踪且包含具体配置，更像环境配置而非代码
- `rules2.json` 在根目录没有说明

**优化方案**：
- `api.txt` 和 `setting.txt` 从 git 中移除，改为 `.env.sample` 和 `setting.txt.sample` 作为模板
- 或在 README 中明确说明各配置文件的作用

---

### 🟢 P2-13: `frontend/src/composables/` 为空

**优化方案**：
- 目录已创建但未使用，建议要么删除，要么放入实际的 composable（如 `useToast`, `usePolling`）

---

### 🟢 P3-1: 前端缺少 ESLint / Prettier 配置

**优化方案**：
- 增加 `eslint.config.js` 和 `.prettierrc`
- 在 `package.json` 中添加 lint 脚本

---

## 6. 推荐执行顺序

### 阶段一：安全与性能（1-2 天）

1. **修复 P0-1 上传无大小限制**（安全）
2. **修复 P0-2 状态锁过重**（性能，影响所有 API）
3. **增加上传分块写入 + 非 root 容器用户**（安全 + 部署）
4. **原子写入 state.json**（数据安全）
5. **从 git 移除 `*.tar` 和运行时数据**（仓库健康）

### 阶段二：可维护性（1-2 天）

6. **提取公共 CSS**（前端可维护性）
7. **修复 P1-5 重复计算**（前端性能）
8. **fetch 超时 + 可见性轮询控制**（前端健壮性）
9. **完善 .gitignore / .dockerignore**

### 阶段三：功能增强（3-5 天）

10. **SSE 实时推送**（替代轮询）
11. **批量操作**（全选加入队列、删除书籍）
12. **报告在线预览**
13. **Token 用量可视化**

---

*报告结束。如需针对某一项深入展开并直接修改代码，请告知具体编号。*
