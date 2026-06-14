# 📚 NovelReportScanner

> **Fork 自** [linglingpp2/NovelReportScanner](https://github.com/linglingpp2/NovelReportScanner) — 原项目是一个男性向长篇小说扫书程序，支持扫描女主、输出毒点雷点、女主四维纯洁度。
>
> 本 Fork 在原项目基础上进行了大量重构和功能增强：新增 Web 管理端（Vue3 前端 + Python HTTP 后端）、24 种小说类型专项分析、大纲预扫描、伏笔追踪、矛盾检测、事实验证器、Token 追踪、Docker/CI 自动化部署等。代码从原始单文件逐步重构为 27 个模块、42,000+ 行结构化代码。

一个面向长篇小说 `.txt` 文本的多阶段分析流水线。项目会按配置的分析模式完成角色识别、正文扫描、结果复核和最终报告生成，并把中间产物与可读报告统一输出到 `results/` 目录。

**项目地址**：<https://github.com/congyoubanmian/NovelReportScanner>

---

## ✨ 核心能力

- **多阶段流水线**：角色识别 → 分块扫描 → 二次复核 → 报告生成，支持断点续跑和多轮补扫
- **后宫排雷模式**：男主/女主识别、别称合并、女主四维纯洁度判定、毒点/雷点检测
- **24 种专项分析**：后宫、游戏/系统、仙侠、硬科幻、历史、悬疑、克苏鲁、末世、西幻等类型专长分析
- **自动分类**：基于关键词匹配 + 置信度评分，自动推荐小说类型，支持多分类联合扫描
- **Web 管理端**：上传、分类、排队、实时进度、日志查看、报告下载，SSE 实时推送
- **准确率保障**：大纲预扫描、滚动上下文、伏笔工程、矛盾检测、事实验证器、名称归一化
- **超长文本支持**：完整支持 200万-800万字长篇，断点续跑 + 降载切半 + 智能补扫
- **Docker 一键部署**：CI/CD 自动构建，GHCR 镜像拉取即用

## 📦 项目结构

```
NovelReportScanner/
├── 🔧 核心引擎
│   ├── novel_reviewer.py    9,477行  核心审查（女主四维/排雷/矛盾）
│   ├── protagonist.py       5,273行  主角识别（男主+女主+别称合并）
│   ├── novel_scan.py        5,176行  深度扫描（分块/断点/补扫）
│   └── general_scan.py      3,744行  专项分析（按类型扫描）
│
├── 📊 报告与前端
│   ├── report.py            5,546行  报告渲染（raw_data → 文字报告/JSON）
│   ├── web_manager.py       2,992行  Web后台（HTTP API + 管理界面）
│   └── frontend/                     Vue3 前端（App.vue / api.js / components/）
│
├── ⚙️ 基础设施
│   ├── text_anchor.py       2,000行  文本锚点（章节定位/证据引用）
│   ├── Timerror.py          1,007行  LLM调用引擎（限流/Key轮换/重试/降级）
│   ├── shared_utils.py        938行  公共工具库
│   └── main.py                883行  CLI入口/任务分发
│
├── 📈 分析指标（17个模块）
│   ├── analysis_profiles.py    867行  类型识别 + 自动分类
│   ├── literary_metrics.py     522行  文学性指标
│   ├── foreshadowing_registry  442行  伏笔追踪
│   ├── outline_prescan.py      408行  大纲预扫描
│   ├── reading_metrics.py      358行  可读性指标
│   ├── toxic_reviewer.py       330行  排雷审查
│   ├── sentiment_arcs.py       316行  情感弧线
│   ├── token_tracker.py        301行  Token追踪
│   ├── scan_memory.py          299行  跨chunk扫描记忆
│   ├── contradiction_detector  281行  矛盾检测
│   ├── fact_validator.py       249行  事实验证器
│   ├── readability_scorer.py   227行  可读性评分
│   ├── name_authority.py       210行  名称权威表
│   ├── name_normalizer.py      157行  名称归一化
│   ├── rv_llm_payload.py       112行  LLM负载控制
│   ├── bootstrap_venv.py        99行  环境引导
│   └── prompt_templates.py      42行  Prompt模板
│
├── 📁 profiles/          24种类型规则（每种 rules.json + profile.json）
│   ├── harem/            后宫/男性向排雷
│   ├── game_system/      游戏/系统/无限流
│   ├── xianxia_fantasy/  仙侠/玄幻
│   ├── hard_sci_fi/      硬科幻
│   ├── history/          历史
│   ├── mystery_detective/ 悬疑/推理
│   ├── cosmic_horror/    克苏鲁/诡秘
│   ├── apocalypse_survival/ 末世/生存
│   ├── steampunk_fantasy/   西幻/蒸汽朋克
│   └── ... (共24个分类)
│
├── 🧪 tests/             448个测试 / 18,103行
├── 📦 results/           扫描结果 + 任务日志 + 运行状态
├── 🚀 Dockerfile + docker-compose.yml + .github/workflows/ (CI/CD)
├── novels/               上传的小说文件
└── requirements.txt
```

**统计**：27个 Python 模块，42,256行业务代码 + 18,103行测试，448个测试方法全绿 ✅

## 🚀 快速开始

### Docker 部署（推荐）

```bash
# 1. 准备目录
mkdir -p novels results

# 2. 拉取镜像
docker pull ghcr.io/congyoubanmian/novelreportscanner:latest

# 3. 启动
docker run -d \
  --name novel-report-scanner \
  --restart unless-stopped \
  -p 8765:8765 \
  --env-file .env \
  -v "$PWD/novels:/app/novels" \
  -v "$PWD/results:/app/results" \
  ghcr.io/congyoubanmian/novelreportscanner:latest
```

访问 `http://服务器IP:8765`

### 本地运行

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .\.venv\Scripts\activate
pip install openai tqdm httpx

# Web 管理端
python web_manager.py

# 或 CLI 批量扫描
python main.py
```

**最简配置 `.env`**：

```ini
BASE_URL=https://your-openai-compatible-endpoint/v1
MODEL_NAME=your-model-name
API_KEY=sk-your-key
MAX_WORKERS=2
RPM_LIMIT=100
TPM_LIMIT=10000000
```

配置优先级：进程环境变量 / `.env` > `setting.txt` > 默认值。仓库中只有 `.env.sample` 模板，真实 `.env` 已在 `.gitignore` 中忽略。

## 🔄 分析流程

主流程由 `main.py` 串联四个阶段，对 `novels/` 下的每本 `.txt` 依次执行：

1. **`protagonist.py`** — 角色识别：并行扫描全书，识别男主、女主候选及别称
2. **`novel_scan.py`** — 深度扫描：分块扫描正文，提取角色/事件/关系，支持断点续跑+多轮补扫
3. **`novel_reviewer.py`** — 二次复核：女主四维纯洁度判定、毒点/雷点检测、矛盾检测
4. **`report.py`** — 报告生成：渲染最终可读的文字报告和 JSON 摘要

通用/专项分析（`general`、`history`、`hard_sci_fi` 等）则跳过排雷二审，执行 `general_scan.py` 抽取剧情主线、核心冲突、世界观设定、伏笔工程、写作质量等维度。

## 📖 分析模式

通过 `ANALYSIS_PROFILE` 环境变量或在 Web 页面选择：

- `harem`：后宫/男性向排雷（男主/女主/初处/漏女/毒点）
- `auto`：自动识别，按内容推荐分类，最多同时执行3个profile
- `general`：通用小说分析（剧情/主题/设定/伏笔）
- `history`、`hard_sci_fi`、`game_system`、`xianxia_fantasy`、`mystery_detective`、`cosmic_horror`、`apocalypse_survival`、`steampunk_fantasy` 等24种专项分析

每种 profile 位于 `profiles/<name>/`，包含 `profile.json`（元数据）和 `rules.json`（扫描规则）。

## 🌐 Web 管理端

```bash
python web_manager.py    # 默认 http://127.0.0.1:8765
```

- 上传 `.txt` 小说，自动推荐分类
- 单本或批量加入扫描队列，支持优先级调整
- 实时进度（SSE 推送）、任务日志、Token 用量
- 查看和下载输出报告
- 状态持久化到 `results/web_manager_state.json`
- 服务重启后，queued 任务恢复排队；running 任务标记为 interrupted，需手动重新入队

## ⚙️ 关键配置项

### 扫描调优

| 参数 | 默认值 | 说明 |
|---|---|---|
| `MAX_WORKERS` | 2 | 并发线程数 |
| `HAREM_SCAN_CHUNK_SIZE` | 7000 | 后宫分块字符数 |
| `HAREM_SCAN_MAX_TOKENS` | 3000 | 后宫单块最大输出tokens |
| `RESCAN_ROUNDS` | 3 | 补扫轮数 |
| `DIM_BOOST_MAX_PER_CHUNK` | 3 | 每片段维度补抽次数 |
| `GENERAL_SCAN_MAX_CHUNKS` | 80 | 通用扫描片段预算（按字数自动提高） |
| `API_SERVER_ERROR_MAX_RETRIES` | 2 | 5xx最大重试 |

### 限流

| 参数 | 默认值 | 说明 |
|---|---|---|
| `RPM_LIMIT` | 100 | 每分钟最大请求数 |
| `TPM_LIMIT` | 10000000 | 每分钟最大token数 |
| `RATE_LIMIT_SCOPE` | auto | 限流域：auto/global/per_key |

### Web 管理

| 参数 | 默认值 | 说明 |
|---|---|---|
| `WEB_HOST` | 0.0.0.0 | 监听地址 |
| `WEB_PORT` | 8765 | 监听端口 |
| `WEB_ACCESS_TOKEN` | - | 访问令牌（Docker默认必填） |
| `SCAN_STALL_TIMEOUT_SECONDS` | 1200 | 扫描卡死保护超时（秒） |

完整配置项参见 `.env.sample`。

## 🐳 Docker Compose 部署

```bash
# 使用预构建镜像
export NOVEL_REPORT_SCANNER_IMAGE=ghcr.io/congyoubanmian/novelreportscanner:latest
docker compose pull
docker compose up -d

# 或本地源码构建
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Compose 会把 `.env` 中的运行参数传入容器，支持 `PUID`/`PGID` 权限映射、内存限制、端口映射等。

生产部署建议通过反向代理（Caddy/Nginx）暴露 HTTPS 入口，容器只绑定 `127.0.0.1:8765`。

## 📁 输出文件

最重要的结果文件：

- `results/<书名>扫书报告_<timestamp>.txt` — 最终可读报告
- `results/<书名>_<profile>_<timestamp>/GENERAL_SUMMARY.json` — 通用/专项扫描摘要
- `results/<书名>_scan_<timestamp>/VERIFIED_SUMMARY_<timestamp>.json` — reviewer 总结
- `results/<书名>_scan_<timestamp>/raw_data.json` — 扫描原始数据
- `results/token_usage.json` — token 消耗汇总

## 🧪 测试

```bash
python -m pytest tests/ -v
# 或
python -m unittest discover -s tests -v
```

448 个测试覆盖核心扫描逻辑、配置加载、名称归一化、文本锚点等。

## 📜 使用声明

- 禁止将本程序生成、汇总或润色后的报告，在未明确标注"AI 生成"或"AI 辅助生成"的情况下对外售卖。
- 如果基于本程序输出的内容进行商业发布、分发或售卖，必须进行清晰、显著、不可误解的 AI 生成标注。
- 不建议将本程序产出的报告包装成人工原创评测、人工精读结论或纯人工整理成果进行传播。

## 📄 License

GPL-3.0
