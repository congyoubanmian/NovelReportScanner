
# NovelReportScanner

一个用于扫描男性向小说的程序
============================

# 小说扫书分析工具

一个面向长篇小说 `.txt` 文本的多阶段分析流水线。项目会按配置的分析模式完成角色识别、正文扫描、结果复核和最终报告生成，并把中间产物与可读报告统一输出到 `results/` 目录。

默认模式仍然保留原有“男性向/后宫扫书、排雷、女主事实提取、最终汇总”能力；同时新增 `general` 通用小说分析入口，用于后续扩展剧情、主题、设定、历史、硬科幻等专项分析。

项目地址：<https://github.com/congyoubanmian/NovelReportScanner>

## 近期更新

- 新增本地 Web 管理端：支持上传小说、管理书籍列表、调整每本书分析分类、查看书籍详情、任务历史、任务日志和输出文件。
- Web 端采用单 worker 串行扫描：后台一次只扫一本书，未轮到的任务显示排队中和队列位置。
- 新增 profile 化分析模式：`harem` 保留后宫/男性向专长分析，`general`、`history`、`hard_sci_fi` 走通用小说/类型专长分析。
- 自动分类会给出多个候选建议：例如一本书同时有历史背景和后宫结构时，可以在 Web 页面手动选择更合适的 profile。
- 新增 `.env` 本地配置支持：真实 API Key、模型地址、限流参数可写入本地 `.env`，仓库只保留 `.env.sample` 模板。

## Web 端

启动：

```powershell
python web_manager.py
```

默认访问：

```text
http://127.0.0.1:8765
```

Web 端适合管理多本书：先上传 `.txt`，根据自动建议调整分类，再加入队列扫描。服务重启后，尚未开始的 queued 任务会恢复排队；已经 running 的任务会标记为 `interrupted`，需要手动重新加入队列。

## 核心能力

- 批量扫描 `novels/` 目录下的所有小说 `.txt` 文件。
- 自动识别核心角色、后宫模式下的男主/女主候选及其别名，并输出角色中间结果。
- `harem` 模式按分块方式扫描全书正文，提取雷点、郁闷点和女主相关事实。
- `harem` 模式在 reviewer 阶段对扫描结果做二次复核，生成更稳定的洁度和毒点结论。
- `general` 模式跳过后宫毒点二审，基于角色识别产物生成通用小说报告。
- 自动生成最终可读的报告文本。
- 记录阶段性中间文件、断点信息、日志和 token 使用情况。
- Windows 下可直接通过 `win一键运行脚本.bat` 启动，并在首次运行时自动创建 `.venv`。
- 支持 Docker 镜像部署，容器默认启动 Web 管理端，后续可以直接拉镜像运行并通过浏览器管理。

## 项目结构

```text
.
├─ win一键运行脚本.bat     # Windows 启动入口，负责调用 bootstrap_venv.py
├─ Dockerfile              # Docker 镜像构建文件，默认启动 Web 管理端
├─ docker-compose.yml      # 拉镜像部署用 Compose 文件
├─ docker-compose.build.yml # 本地源码构建用 Compose 覆盖文件
├─ requirements.txt        # 容器和手动安装依赖清单
├─ bootstrap_venv.py       # 创建 .venv、安装基础依赖、启动 main.py
├─ main.py                 # 主流程入口，批量扫描 novels/ 并串联四个阶段
├─ protagonist.py          # 角色识别与女主候选提取
├─ novel_scan.py           # 分块扫描正文，提取问题点和结构化事实
├─ novel_reviewer.py       # 二次复核与汇总结论生成
├─ general_scan.py         # 通用小说剧情、冲突、主题、设定扫描
├─ web_manager.py          # 本地 Web 管理端：上传、分类、排队、单本扫描
├─ report.py               # 生成最终面向阅读的报告
├─ shared_utils.py         # 共享配置、API 调用封装、通用工具
├─ text_anchor.py          # chunk manifest 与证据定位相关逻辑
├─ token_tracker.py        # token 统计
├─ analysis_profiles.py    # 分析 profile 加载与流程能力描述
├─ profiles/               # 不同小说类型/分析模式的规则和模板入口
├─ rules2.json             # 规则库，定义雷点/郁闷点及其说明
├─ .env.sample             # .env 本地配置模板
├─ setting.txt.sample      # setting.txt 本地配置模板
├─ setting.txt             # 本地运行配置，已被 .gitignore 忽略
├─ api.txt                 # 本地 API Key 列表，已被 .gitignore 忽略
├─ novels/                 # 输入小说文本目录
├─ results/                # 输出目录
   └─ learned_keywords/    # 扫描阶段生成的增量关键词快照

```

## 快速开始

### 方式一：Windows 下直接运行

这是当前仓库最直接的启动方式。

1. 安装 Python 3.10 或更高版本。
2. 把待分析的小说 `.txt` 放进 `novels/` 目录。
3. 复制 `.env.sample` 为 `.env`，填写 `API_KEY` 或 `API_KEY_POOL`。
4. 如仍沿用旧方式，也可以在项目根目录创建 `api.txt`，每行写一个可用的 API Key；`api.txt` 只作为 `.env` 未配置时的回退。
5. 如需旧式配置文件，复制 `setting.txt.sample` 为 `setting.txt` 后修改。
6. 双击运行 `win一键运行脚本.bat`。

`win一键运行脚本.bat` 会优先使用本地 `.venv\Scripts\python.exe`。如果 `.venv` 还不存在，它会调用 `bootstrap_venv.py` 自动完成以下动作：

- 检查 Python 版本是否至少为 3.10
- 创建本地 `.venv`
- 安装基础依赖：`openai`、`tqdm`、`httpx`
- 使用该环境运行 `main.py`

### 方式二：手动运行 Python

如果你不想走批处理入口，也可以手动创建虚拟环境并运行：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install openai tqdm httpx
python main.py
```

### 方式三：Docker Web 管理端

Docker 镜像默认启动 Web 管理端，监听容器内 `8765` 端口。小说文本、扫描结果和 Web 状态都建议挂载到宿主机目录，避免容器重建后丢失。

容器入口会默认检查 `API_KEY` 或 `API_KEY_POOL`，至少需要配置其中一个，否则容器会直接退出，避免 Web 页面能打开但扫描任务启动后才失败。如果你只是临时启动 Web UI、不准备扫描，可以设置 `NOVEL_REPORT_SCANNER_REQUIRE_API_KEY=0` 跳过这个启动校验。

容器默认不再以 root 身份运行，默认使用 `1000:1000`。首次部署前建议先创建宿主机挂载目录，并让运行容器的 UID / GID 拥有读写权限：

```bash
export PUID="${PUID:-1000}"
export PGID="${PGID:-1000}"
mkdir -p novels results
chown -R "$PUID:$PGID" novels results
```

本地源码构建镜像：

```bash
docker build -t novel-report-scanner:latest .
```

如果构建机器访问 PyPI 较慢，可以临时指定镜像源：

```bash
docker build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t novel-report-scanner:latest .
```

直接运行镜像：

```bash
docker run -d \
  --name novel-report-scanner \
  --restart unless-stopped \
  --user "${PUID:-1000}:${PGID:-1000}" \
  -p 8765:8765 \
  --env-file .env \
  -v "$PWD/novels:/app/novels" \
  -v "$PWD/results:/app/results" \
  novel-report-scanner:latest
```

访问：

```text
http://服务器IP:8765
```

如果不走镜像仓库，也可以在本机导出镜像包，上传到服务器后部署：

```bash
docker save -o novel-report-scanner_latest.tar novel-report-scanner:latest
```

服务器导入镜像：

```bash
docker load -i novel-report-scanner_latest.tar
mkdir -p novels results
```

然后直接启动：

```bash
docker run -d \
  --name novel-report-scanner \
  --restart unless-stopped \
  --user "${PUID:-1000}:${PGID:-1000}" \
  -p 8765:8765 \
  --env-file .env \
  -v "$PWD/novels:/app/novels" \
  -v "$PWD/results:/app/results" \
  novel-report-scanner:latest
```

如果使用 Compose，本地源码构建并启动：

```bash
export PUID="${PUID:-1000}"
export PGID="${PGID:-1000}"
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Compose 构建同样可以先设置 `PIP_INDEX_URL` 来切换 pip 源。`PUID` / `PGID` 也可以写入 `.env`，用于让容器内进程用宿主机用户身份写入 `novels/` 和 `results/`。推送到 `main` 后可以在 Actions 页面查看自动镜像构建结果。

Compose 默认限制容器内存为 `2G`，预留 `512M`。如果一次扫描的小说很长、并发或上下文窗口较大，可以在 `.env` 中调整：

```ini
CONTAINER_MEMORY_LIMIT=4g
CONTAINER_MEMORY_RESERVATION=1g
```

推送到 GitHub `main` 分支后，CI 会先运行后端单测、前端 audit/lint/format/build；Docker 发布工作流会自动构建并推送镜像到 GHCR 和 Docker Hub。推送 `v*` 版本 tag 或手动触发 Docker 发布工作流时，也会发布对应镜像标签。服务器只需要准备 `.env`、`novels/`、`results/` 和 `docker-compose.yml`。例如：

```bash
export NOVEL_REPORT_SCANNER_IMAGE=ghcr.io/congyoubanmian/novel-report-scanner:main
export PUID="${PUID:-1000}"
export PGID="${PGID:-1000}"
docker compose pull
docker compose up -d
```

Compose 会把 `.env` 中的模型、限流、后宫增强、上传限制、SSE 间隔、输出缓存等运行参数显式传入容器。改动 `.env` 后需要重新执行 `docker compose up -d` 让容器重建并读取新环境变量。

`.env.sample` 已包含 Compose 支持的常用部署变量和扫描调优变量，可以复制为 `.env` 后按需修改；不要把真实 `.env` 提交到仓库。

没有 Compose 的环境也可以直接拉镜像运行：

```bash
docker pull ghcr.io/congyoubanmian/novel-report-scanner:main
docker run -d \
  --name novel-report-scanner \
  --restart unless-stopped \
  --user "${PUID:-1000}:${PGID:-1000}" \
  -p 8765:8765 \
  --env-file .env \
  -v "$PWD/novels:/app/novels" \
  -v "$PWD/results:/app/results" \
  ghcr.io/congyoubanmian/novel-report-scanner:main
```

生产部署注意事项：

- `.env` 保存真实 API Key 和模型配置，不要打进镜像，也不要提交到仓库。
- Docker/Compose 默认要求 `.env` 中存在 `API_KEY` 或 `API_KEY_POOL`；多 Key 时优先使用 `API_KEY_POOL=sk-key-1,sk-key-2`。
- `novels/` 用于放上传或预置的 `.txt` 小说，`results/` 用于保存报告、任务日志和 Web 管理状态。
- 如果容器无法上传小说或写报告，优先检查宿主机 `novels/`、`results/` 的属主是否匹配 `PUID` / `PGID`。
- 容器内 Web 服务默认绑定 `0.0.0.0:8765`，宿主机端口可通过 `-p 宿主机端口:8765` 或 Compose 里的 `WEB_PORT` 调整。
- 容器内存限制可通过 `CONTAINER_MEMORY_LIMIT` 和 `CONTAINER_MEMORY_RESERVATION` 调整；超长小说或多分类扫描建议适当调高。
- 镜像健康检查使用 `/healthz`，只证明 Web 管理端进程可访问；是否能真正扫描取决于 `.env` / API Key 是否配置正确。

公网反向代理 / TLS 建议：

如果 Web 管理端暴露到公网，建议不要直接开放裸 `8765` 端口。更稳妥的做法是让容器只监听本机回环地址，再用 Caddy 或 Nginx 负责 HTTPS、域名和访问入口：

```yaml
# docker-compose.yml 可把端口改成只绑定本机
ports:
  - "127.0.0.1:${WEB_PORT:-8765}:8765"
```

同时在 `.env` 中设置访问令牌，并把 CORS 收窄到你的域名：

```ini
WEB_ACCESS_TOKEN=换成一段长随机字符串
WEB_CORS_ALLOW_ORIGIN=https://scanner.example.com
```

Caddy 示例：

```caddyfile
scanner.example.com {
  reverse_proxy 127.0.0.1:8765
}
```

Nginx 示例：

```nginx
server {
    listen 80;
    server_name scanner.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name scanner.example.com;

    ssl_certificate /etc/letsencrypt/live/scanner.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/scanner.example.com/privkey.pem;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/events {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

反向代理配置完成后，浏览器访问 `https://scanner.example.com/?token=你的令牌` 可首次保存令牌；后续页面会把令牌放入 `Authorization: Bearer ...` 头中请求 API。`/healthz` 不需要令牌，只能证明 Web 进程可达，不应当作为 API Key 或扫描能力是否正常的判断。

### 方式四：本地 Web 管理端

如果你需要管理多本书、上传后手动调整分类，可以启动本地 Web 管理端：

```powershell
python web_manager.py
```

默认访问：

```text
http://127.0.0.1:8765
```

Web 管理端能力：

- 上传 `.txt` 小说到 `novels/`；同名文件默认拒绝覆盖，需要显式勾选“覆盖同名文件”。
- 为每本书选择 `auto`、`harem`、`general`、`history`、`hard_sci_fi` 等分析模式；下拉选项会从 `profiles/` 自动发现。
- 上传或同步书籍后显示自动分类建议。一本书如果同时像历史小说和后宫小说，会同时展示候选 profile、分数和命中关键词，用户可以在开始扫描前手动调整。
- 支持单本或批量加入扫描队列，尚未开始的排队任务可以置顶、上移、下移或取消；空闲或已完成书籍可以单本或批量删除上传的小说文件，历史报告会保留。
- 列表状态通过 SSE 实时推送更新，连接异常时会回退到定时刷新。
- 顶部展示非敏感运行配置摘要，例如模型、并发、限流、上传上限、API Key 配置数量和容器 Key 启动校验状态；不会展示 Key 明文。
- 查看每本书详情、分类建议、Token 用量、任务历史、任务日志、最近输出报告和 summary 文件。
- 状态持久化到 `results/web_manager_state.json`。
- Web 管理端重启后，尚未开始的 queued 任务会恢复排队；已经 running 的任务会标记为 `interrupted`，需要用户重新加入队列。

也可以通过环境变量改监听地址：

```powershell
set WEB_HOST=0.0.0.0
set WEB_PORT=8765
python web_manager.py
```

## 配置说明

### `.env` / `api.txt`

推荐把本机运行配置写在 `.env` 中。仓库只保留 `.env.sample` 模板，真实 `.env` 已在 `.gitignore` 中忽略，不会被提交。

```ini
BASE_URL=https://your-openai-compatible-endpoint/v1
MODEL_NAME=your-model-name
MAX_WORKERS=2
RPM_LIMIT=100
TPM_LIMIT=10000000
RATE_LIMIT_SCOPE=auto
API_KEY=sk-your-key
# API_KEY_POOL=sk-key-1,sk-key-2

WEB_HOST=0.0.0.0
WEB_PORT=8765
WEB_CORS_ALLOW_ORIGIN=*
WEB_ACCESS_TOKEN=
WEB_REQUEST_TIMEOUT=60
MAX_UPLOAD_SIZE=104857600
MAX_JSON_BODY_SIZE=65536
FILE_RESPONSE_CHUNK_SIZE=1048576
SYNC_BOOKS_TTL_SECONDS=5
OUTPUTS_CACHE_TTL_SECONDS=5
SSE_STATE_INTERVAL_SECONDS=3
SSE_SYNC_INTERVAL_SECONDS=5
SSE_MAX_CONNECTION_SECONDS=300
```

配置加载优先级是：进程环境变量 / `.env` > `setting.txt` > 默认值。API Key 优先读取 `API_KEY_POOL` 或 `API_KEY`；如果没有设置，才会回退读取根目录 `api.txt`。

请只在本地保存真实 key，不要把 `.env` 或真实 `api.txt` 提交到公开仓库。

### `setting.txt`

`main.py` 会从本地 `setting.txt` 中读取常用配置，并写入环境变量。仓库只保留 `setting.txt.sample`，真实 `setting.txt` 已在 `.gitignore` 中忽略。需要使用旧式配置文件时，可以复制 `setting.txt.sample` 为 `setting.txt` 后再修改。

最常用的几个配置是：

- `BASE_URL`：OpenAI 兼容接口地址。
- `MODEL_NAME`：调用的模型名称。
- `MAX_WORKERS`：并发规模基线。
- `ANALYSIS_PROFILE`：分析模式。`harem` 为默认后宫/男性向排雷模式；`auto` 可按每本书自动识别；`general`、`history`、`hard_sci_fi` 为通用和类型专长入口。
- `HAREM_PLUS_GENERAL_SCAN`：后宫增强模式开关。设为 `1` 时，`harem` 流程会在后宫排雷后额外运行一次通用剧情扫描，并在后宫报告末尾追加“作品整体评价”；默认 `0`，不增加额外调用。
- `GENERAL_SCAN_MAX_CHUNKS`：通用/专项剧情扫描的基础片段预算，默认 `80`。程序会按小说长度自动提高有效上限：100 万字内 80、100-300 万字 120、300-500 万字 160、500-1000 万字 300、1000 万字以上 400；手动配置更高值时会尊重配置，设为 `0` 表示不限制。超出预算时会按全书时间线均匀抽样，而不是只扫描开头。
- `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`：扫描日志轮转配置，默认单个 `analysis.log` / `reviewer.log` 最大 `10485760` 字节并保留 `5` 份历史；`LOG_MAX_BYTES=0` 表示不按大小轮转。
- `RPM_LIMIT` / `TPM_LIMIT`：程序本地预限流配置，用来在请求发出前控制最近 60 秒内的请求数和预估 token 数。
- `RATE_LIMIT_SCOPE`：本地限流作用域。`auto` 表示多个 API Key 时每个 key 单独计数、单个 key 时使用全局桶；`global` 表示当前进程内所有线程和 key 共用一个限流桶；`per_key` 表示每个 key 单独计数。默认推荐 `auto`，能减少多 key 场景下的无谓串行等待，同时避免单 key 场景误放大并发。

Web 管理端常用配置：

- `WEB_HOST` / `WEB_PORT`：Web 管理端监听地址和端口。
- `WEB_CORS_ALLOW_ORIGIN`：CORS 允许来源，默认 `*`。
- `WEB_ACCESS_TOKEN`：Web 管理端可选访问令牌。留空时读接口仍可访问，但上传、扫描、删除、队列调整和运行配置修改等写操作需要请求携带 `X-Web-Unsafe-Action: confirm`，Web 前端会自动携带；公网部署仍强烈建议设置令牌。设置后 `/api/*`、`/files`、`/upload` 和 SSE 状态流都需要携带 token。浏览器可在页面输入令牌保存，也可首次访问时使用 `http://host:port/?token=你的令牌` 自动保存。
- `WEB_REQUEST_TIMEOUT`：单个 HTTP 连接的 socket 超时时间，默认 `60` 秒；设为 `0` 可关闭。
- `MAX_UPLOAD_SIZE`：单个上传 `.txt` 文件大小上限，默认 `104857600` 字节。
- `MAX_JSON_BODY_SIZE`：JSON API 请求体大小上限，默认 `65536` 字节。
- `FILE_RESPONSE_CHUNK_SIZE`：`/files` 下载或预览文件时的流式输出块大小，默认 `1048576` 字节。
- `SYNC_BOOKS_TTL_SECONDS`：同步 `novels/` 目录的最短间隔，默认 `5` 秒。
- `OUTPUTS_CACHE_TTL_SECONDS`：书籍输出文件列表缓存时间，默认 `5` 秒。
- `SSE_STATE_INTERVAL_SECONDS`：SSE 状态推送间隔，默认 `3` 秒。
- `SSE_SYNC_INTERVAL_SECONDS`：SSE 状态流触发 `novels/` 目录同步的最短间隔，默认 `5` 秒；多个 SSE 连接会共用这个节流。
- `SSE_MAX_CONNECTION_SECONDS`：单个 SSE 连接最大生命周期，默认 `300` 秒；到期后浏览器 `EventSource` 会自动重连，避免服务端线程长期占用。

Web 页面顶部可直接调整部分非敏感运行配置，包括 `MAX_WORKERS`、`RPM_LIMIT`、`TPM_LIMIT`、`RATE_LIMIT_SCOPE`、`GENERAL_SCAN_MAX_CHUNKS` 和 `HAREM_PLUS_GENERAL_SCAN`。这些修改会立即影响当前 Web 服务进程及其后续扫描子进程，并会安全写回 `.env` 文件以便重启后继续生效；写回时只更新白名单内的非敏感字段，保留注释、空行和 API Key 等敏感信息，不会展示或修改 API Key。`setting.txt` 仍作为样例/兼容配置来源，不会被 Web 配置页写回。

前端开发检查：

```bash
cd frontend
npm run lint
npm run format:check
npm run build
```

`npm run lint` 使用 ESLint 检查 Vue/JS 代码，并设置为零 warning 通过；`npm run format:check` 使用 Prettier 校验格式。

仓库 CI 也会执行：

- `python -m unittest discover -s tests -v`
- `npm audit --audit-level=moderate`
- `npm run lint`
- `npm run format:check`
- `npm run build`

### 分析模式

项目现在通过 `ANALYSIS_PROFILE` 区分不同分析模式：

- `harem`：默认模式，保留原有男主、女主、初处、漏女、毒点/雷点分析流程。
- `harem` 可选增强：设置 `HAREM_PLUS_GENERAL_SCAN=1` 后，会额外补充通用剧情/设定/节奏评价，适合后宫和历史、科幻、仙侠等元素交叉的小说。
- `auto`：自动识别模式，会根据书名和正文前段启发式选择 `harem`、`history`、`hard_sci_fi` 或 `general`。
- `general`：通用小说分析入口，会运行角色识别、剧情/冲突/主题/设定扫描并生成通用小说报告，不执行初处、漏女、后宫毒点二审。
- `history`：历史小说专长分析，在通用流程上额外关注时代制度、战争权谋、派系逻辑、人物立场和历史氛围。
- `hard_sci_fi`：硬科幻专长分析，在通用流程上额外关注科学假设、技术链、工程约束、因果推演和设定自洽。

当前所有 `report_mode=general` 的 profile 都会运行通用角色识别，并继续执行 `general_scan.py` 抽取剧情主线、核心冲突、世界观设定、主题表达、伏笔回收、优点和问题。角色明细 JSON 中会输出通用 `characters` 列表；后宫类 `harem` 才继续使用男主/女主、初处、漏女和毒点/雷点专长流程。

`auto` 不是强制唯一分类。代码会先给出候选建议，Web 管理端会展示多个候选；例如“开头是历史背景，同时又是后宫结构”的书，可以在 Web 页面里从建议中手动选择 `history`、`harem` 等一个或多个分类后再加入队列。命令行批量模式也会按得分阈值自动执行最多 3 个 profile；如果没有候选达到阈值，则回退到 `general`。

新增 profile 时通常只需要在 `profiles/<name>/profile.json` 中声明 `display_name`、`enabled_stages`、`report_mode`、`scan_focus`、`summary_fields`。如果想让 `auto` 识别这个新类型，可以额外添加 `inference_keywords`：

```json
[
  {"word": "关键词", "weight": 3},
  ["另一个关键词", 2],
  "普通关键词"
]
```

对应资源位于：

```text
profiles/
├─ harem/
│  ├─ profile.json
│  └─ rules.json
├─ general/
│  ├─ profile.json
│  └─ rules.json
├─ history/
│  ├─ profile.json
│  └─ rules.json
└─ hard_sci_fi/
   ├─ profile.json
   └─ rules.json
```

旧的 `rules2.json` 仍然保留，用于兼容历史路径；新的后宫规则主路径是 `profiles/harem/rules.json`。

其余几个 `RESCAN_*`、`DIM_BOOST_*`、`MAX_MIDDLE_SUMMARY_CALLS`、`INITIAL_SCAN_*` 主要用于扫描阶段的补扫、增强策略和首扫并发调度，属于进阶调优项。

### 扫描阶段调优参数说明

下面这几项主要由 `novel_scan.py` 在扫描阶段使用，不是主流程里所有脚本都会依赖的参数。

它们的共同特点是：通常能提升召回率、补漏能力和复杂表达的识别效果，但也往往意味着更多额外调用、更长 prompt 和更高 token 消耗。

请特别注意：

- 如果把这些增强项开得更激进，扫描效果通常会上升，但 token 使用量也会显著增加。
- 作者建议：如果你使用的不是廉价 token，尽量不要盲目调高这些参数，优先保持默认值或保守值。
- 其中最容易明显拉高 token 消耗的，通常是 `DIM_BOOST_MAX_PER_CHUNK`、`MAX_MIDDLE_SUMMARY_CALLS`、`RESCAN_MAX_HITS`、`RESCAN_MAX_WINDOW` 和 `RESCAN_MAX_PROMPT_HEROINES`。

运行日志里的 `限流等待：... reason=rpm, scope=global/per_key` 是本程序的本地预限流日志，不是 API 服务端返回的报错。`reason=rpm` 代表最近 60 秒请求数达到 `RPM_LIMIT`；`scope=global` 代表所有线程和所有 key 共用这个计数桶，`scope=per_key` 代表每个 key 单独计数。默认 `RATE_LIMIT_SCOPE=auto` 会在多 key 时自动使用 `per_key`，单 key 时使用 `global`；如果供应商按账号或出口 IP 共享限速，可手动改回 `global`。

各参数作用如下：

- `DIM_BOOST_MAX_PER_CHUNK`：每个正文片段最多做多少次“按维度补抽”。数值越大，越容易把某个维度里漏掉的事实再补出来，但每个片段可能触发更多额外调用。
- `RESCAN_ROUNDS`：扫描完成后，针对遗漏片段或失败片段最多再补扫几轮。数值越大，整体更稳，但耗时和 token 成本都会继续增加。
- `MAX_MIDDLE_SUMMARY_CALLS`：扫描过程中最多生成多少次“中间上下文摘要”。它主要用来改善长文本跨片段承接，适合上下文依赖强的小说，但会直接增加额外模型调用。
- `INITIAL_SCAN_BLOCK_MULTIPLIER`：首扫时把正文切成约 `MAX_WORKERS` 多少倍的连续小段，线程池会动态领取小段，减少某个大段特别慢导致其他线程空等的问题。
- `INITIAL_SCAN_MIN_BLOCK_SIZE`：首扫动态小段的目标最小 chunk 数。值越小负载越均衡，但段边界补扫和上下文摘要开销会略增。
- `RESCAN_MAX_HITS`：全局补扫时，每个“女主 + 维度”最多保留多少个候选命中片段。越大越容易补到遗漏，但也意味着后续要处理更多候选片段；设为 `0` 可视为关闭这部分增强。
- `RESCAN_PRE_FILTER_THRESHOLD`：全局补扫前，对候选命中的最低预过滤分数。值越高越严格，进入后续补扫的片段越少；值越低则更激进，召回更高，但 token 消耗通常也会更高。
- `RESCAN_MAX_WINDOW`：全局补扫时，允许截取的最大上下文窗口长度。值越大，单次 prompt 的上下文更完整，但 prompt 本身也会更长、更费 token。
- `RESCAN_MAX_PROMPT_HEROINES`：单次全局补扫 prompt 最多携带多少名女主。值越大，单次覆盖的人物更多，但 prompt 更拥挤，token 成本也更高。

如果你的目标是“先稳定跑通、控制成本”，更推荐先用较保守的配置；只有在你明确发现漏扫比较严重、并且能接受成本上涨时，再逐步调高这些参数。

### `rules2.json`

如果你说的是 `rule.json`，代码里当前实际读取的文件名是 `rules2.json`。

这个文件不是普通备注文件，而是扫描和复核阶段都会用到的规则库，主要作用有三点：

- 定义项目到底要扫描哪些“雷点/郁闷点”类别与具体条目。
- 给每个条目提供文字说明，作为模型判断时的规则依据。
- 保持 `novel_scan.py` 与 `novel_reviewer.py` 使用同一套标准，避免初扫和二审口径不一致。

你可以把它理解成“项目的判定标准配置文件”。

在当前实现里：

- `novel_scan.py` 会读取 `rules2.json`，据此决定要按哪些类别和条目去扫正文。
- `toxic_reviewer.py` / `novel_reviewer.py` 会再次读取它，把条目说明作为二审时的规则描述。

如果你想调整项目对某类情节的敏感度、命名方式或说明口径，优先改的就是这个文件。

### `results/learned_keywords/`

这个目录是扫描阶段自动维护的“学习到的关键词快照目录”，主要服务于 `novel_scan.py` 的关键词增强和补扫逻辑。

它的作用不是保存最终结果，而是把扫描过程中逐步学到的新表达方式沉淀下来，供后续片段继续复用。这样做的好处是：当小说里用了比较特殊、比较隐晦的说法时，后续扫描更容易把同类表达补抓出来。

目录里通常会看到两类文件：

- `seed.json`：内置种子关键词，属于初始词表。
- `learned_<timestamp>_dim_boost.json` 或 `learned_<timestamp>_global_rescan_opt.json`：扫描过程中新增的关键词快照。

这些文件里的关键词按事实维度分类，常见维度包括：

- `sexual_relations`
- `children_info`
- `physical_contacts`
- `romantic_feelings`
- `partner_relations`

可以把它理解成“扫描器的增量经验库”。代码会把 `seed.json` 和最新的 learned 快照合并，形成当前生效的关键词集合，再用于后续扫描与补扫。

## 流程说明

主流程由 [main.py](./main.py) 串联四个阶段，对 `novels/` 下的每本 `.txt` 依次执行：

### 1. `protagonist.py`

负责识别男主、女主候选及其别名，并生成角色相关中间文件。

常见输出位于：

```text
results/<书名>_heroine_<timestamp>/
```

典型文件包括：

- `*_detailed_*.json`
- `*_detail_snapshot_*.json`
- `*_protagonists_*.json`
- `*_report_*.txt`
- `latest_checkpoint.json`

### 2. `novel_scan.py`

负责对正文进行分块扫描，提取问题点和结构化角色事实。该阶段会读取 `rules2.json` 作为扫描规则来源，还会生成 chunk manifest，并把部分事实回写到角色明细文件中。

常见输出位于：

```text
results/<书名>_scan_<timestamp>/
```

典型文件包括：

- `raw_data.json`
- `FULL_REPORT.txt`
- `chunk_manifest.json`
- `latest_checkpoint.json`
- `scan.log`

另外，扫描过程中如果模型提取到了新的稳定表达方式，还会把它们写入 `results/learned_keywords/`，用于增强后续扫描的命中率。

### 3. `novel_reviewer.py`

负责对扫描结果做二次复核，并输出更稳定的汇总结论。二审时会结合 `rules2.json` 中对应条目的说明，避免 reviewer 脱离项目既定规则单独发挥。

典型文件包括：

- `VERIFIED_SUMMARY_<timestamp>.json`
- `VERIFIED_REPORT_<timestamp>.txt`
- `reviewer.log`
- `reviewer3_checkpoint.json`

### 4. `report.py`

负责读取最新的 verified summary 与角色明细，生成最终给人阅读的扫书报告。

最终报告通常输出到：

```text
results/<书名>扫书报告_<timestamp>.txt
```

另外，`report.py` 也会在 `results/` 根目录维护如 `report_generation.log`、`report_checkpoint.json` 之类的报告生成状态文件。

## 输出结果概览

如果你只想快速找到最重要的结果文件，可以先看这些：

- `results/<书名>扫书报告_<timestamp>.txt`
- `results/<书名>_scan_<timestamp>/VERIFIED_SUMMARY_<timestamp>.json`
- `results/<书名>_scan_<timestamp>/raw_data.json`
- `results/<书名>_heroine_<timestamp>/*_detailed_*.json`
- `results/learned_keywords/seed.json`
- `results/learned_keywords/learned_*.json`
- `results/token_usage.json`

它们分别对应：

- 最终可读报告
- reviewer 阶段总结
- 扫描阶段原始结构化结果
- 角色与事实的详细中间产物
- 初始关键词种子库
- 扫描阶段学习到的增量关键词快照
- 当前运行批次的 token 使用汇总

## 单独运行某个阶段

如果你不想跑完整流程，也可以直接执行单个脚本：

```powershell
python protagonist.py
python novel_scan.py
python novel_reviewer.py --raw-data .\results\<某次扫描>\raw_data.json
python report.py --no-polish
```

其中：

- `novel_reviewer.py` 支持 `--raw-data` 和 `--results-dir`
- `report.py` 支持 `--polish`、`--no-polish`、`--skip-existing`、`--force-regenerate`

如果你是从 `main.py` 跑全流程，这些上下文参数会由主入口自动在各阶段之间传递。

## 适合什么场景

- 对整本中文小说或网文 `.txt` 做批量扫书
- 希望把“角色识别 + 正文扫描 + 复核 + 最终报告”串成一条流水线
- 需要保留中间 JSON、日志和断点产物，方便复盘或二次处理

## 使用前建议

- 先清理或归档 `results/`，避免历史样本和新结果混在一起。
- 如果 `results/learned_keywords/` 已经积累了很多旧快照，发布或复盘前可以先决定是否保留；它们更像过程资产，而不是必须公开的最终成果。

## 使用声明

- 禁止将本程序生成、汇总或润色后的报告，在未明确标注“AI 生成”或“AI 辅助生成”的情况下对外售卖。
- 如果基于本程序输出的内容进行商业发布、分发或售卖，必须进行清晰、显著、不可误解的 AI 生成标注。
- 不建议将本程序产出的报告包装成人工原创评测、人工精读结论或纯人工整理成果进行传播。
