
# NovelReportScanner

一个用于扫描男性向小说的程序
============================

# 小说扫书分析工具

一个面向长篇小说 `.txt` 文本的多阶段分析流水线。项目会按配置的分析模式完成角色识别、正文扫描、结果复核和最终报告生成，并把中间产物与可读报告统一输出到 `results/` 目录。

默认模式仍然保留原有“男性向/后宫扫书、排雷、女主事实提取、最终汇总”能力；同时新增 `general` 通用小说分析入口，用于后续扩展剧情、主题、设定、历史、硬科幻等专项分析。

## 核心能力

- 批量扫描 `novels/` 目录下的所有小说 `.txt` 文件。
- 自动识别核心角色、后宫模式下的男主/女主候选及其别名，并输出角色中间结果。
- `harem` 模式按分块方式扫描全书正文，提取雷点、郁闷点和女主相关事实。
- `harem` 模式在 reviewer 阶段对扫描结果做二次复核，生成更稳定的洁度和毒点结论。
- `general` 模式跳过后宫毒点二审，基于角色识别产物生成通用小说报告。
- 自动生成最终可读的报告文本。
- 记录阶段性中间文件、断点信息、日志和 token 使用情况。
- Windows 下可直接通过 `main.bat` 启动，并在首次运行时自动创建 `.venv`。

## 项目结构

```text
.
├─ main.bat                # Windows 启动入口，负责调用 bootstrap_venv.py
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
├─ setting.txt             # 运行配置
├─ api.txt                 # API Key 列表，每行一个
├─ novels/                 # 输入小说文本目录
├─ results/                # 输出目录
   └─ learned_keywords/    # 扫描阶段生成的增量关键词快照

```

## 快速开始

### 方式一：Windows 下直接运行

这是当前仓库最直接的启动方式。

1. 安装 Python 3.10 或更高版本。
2. 把待分析的小说 `.txt` 放进 `novels/` 目录。
3. 在项目根目录创建 `api.txt`，每行写一个可用的 API Key。
4. 按需修改 `setting.txt`。
5. 双击运行 `main.bat`。

`main.bat` 会优先使用本地 `.venv\Scripts\python.exe`。如果 `.venv` 还不存在，它会调用 `bootstrap_venv.py` 自动完成以下动作：

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

### 方式三：本地 Web 管理端

如果你需要管理多本书、上传后手动调整分类，可以启动本地 Web 管理端：

```powershell
python web_manager.py
```

默认访问：

```text
http://127.0.0.1:8765
```

Web 管理端能力：

- 上传 `.txt` 小说到 `novels/`。
- 为每本书选择 `auto`、`harem`、`general`、`history`、`hard_sci_fi`。
- 单 worker 串行扫描：后台一次只扫一本书，未轮到的显示“排队中”。
- 状态持久化到 `results/web_manager_state.json`。

也可以通过环境变量改监听地址：

```powershell
set WEB_HOST=0.0.0.0
set WEB_PORT=8765
python web_manager.py
```

## 配置说明

### `api.txt`

项目会读取根目录下的 `api.txt`，每行一个 key，并自动组装成 `API_KEY_POOL`。

示例：

```text
sk-your-key-1
sk-your-key-2
```

请只在本地保存真实 key，不要把真实 `api.txt` 提交到公开仓库。

### `setting.txt`

`main.py` 会从 `setting.txt` 中读取常用配置，并写入环境变量。下面是一组可参考的示例：

```ini
BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
MAX_WORKERS=6
ANALYSIS_PROFILE=harem
RPM_LIMIT=10
TPM_LIMIT=100000
DIM_BOOST_MAX_PER_CHUNK=3
RESCAN_ROUNDS=3
MAX_MIDDLE_SUMMARY_CALLS=10
RESCAN_MAX_HITS=4
RESCAN_PRE_FILTER_THRESHOLD=1.0
RESCAN_MAX_WINDOW=2000
RESCAN_MAX_PROMPT_HEROINES=4
```

最常用的几个配置是：

- `BASE_URL`：OpenAI 兼容接口地址。
- `MODEL_NAME`：调用的模型名称。
- `MAX_WORKERS`：并发规模基线。
- `ANALYSIS_PROFILE`：分析模式。`harem` 为默认后宫/男性向排雷模式；`auto` 可按每本书自动识别；`general`、`history`、`hard_sci_fi` 为通用和类型专长入口。
- `RPM_LIMIT` / `TPM_LIMIT`：限流相关配置。

### 分析模式

项目现在通过 `ANALYSIS_PROFILE` 区分不同分析模式：

- `harem`：默认模式，保留原有男主、女主、初处、漏女、毒点/雷点分析流程。
- `auto`：自动识别模式，会根据书名和正文前段启发式选择 `harem`、`history`、`hard_sci_fi` 或 `general`。
- `general`：通用小说分析入口，会运行角色识别、剧情/冲突/主题/设定扫描并生成通用小说报告，不执行初处、漏女、后宫毒点二审。
- `history`：历史小说专长分析，在通用流程上额外关注时代制度、战争权谋、派系逻辑、人物立场和历史氛围。
- `hard_sci_fi`：硬科幻专长分析，在通用流程上额外关注科学假设、技术链、工程约束、因果推演和设定自洽。

当前 `general` 模式会运行通用角色识别，并继续执行 `general_scan.py` 抽取剧情主线、核心冲突、世界观设定、主题表达、伏笔回收、优点和问题。角色明细 JSON 中会输出通用 `characters` 列表。

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

其余几个 `RESCAN_*`、`DIM_BOOST_*`、`MAX_MIDDLE_SUMMARY_CALLS` 主要用于扫描阶段的补扫和增强策略，属于进阶调优项。

### 扫描阶段调优参数说明

下面这几项主要由 `novel_scan.py` 在扫描阶段使用，不是主流程里所有脚本都会依赖的参数。

它们的共同特点是：通常能提升召回率、补漏能力和复杂表达的识别效果，但也往往意味着更多额外调用、更长 prompt 和更高 token 消耗。

请特别注意：

- 如果把这些增强项开得更激进，扫描效果通常会上升，但 token 使用量也会显著增加。
- 作者建议：如果你使用的不是廉价 token，尽量不要盲目调高这些参数，优先保持默认值或保守值。
- 其中最容易明显拉高 token 消耗的，通常是 `DIM_BOOST_MAX_PER_CHUNK`、`MAX_MIDDLE_SUMMARY_CALLS`、`RESCAN_MAX_HITS`、`RESCAN_MAX_WINDOW` 和 `RESCAN_MAX_PROMPT_HEROINES`。

各参数作用如下：

- `DIM_BOOST_MAX_PER_CHUNK`：每个正文片段最多做多少次“按维度补抽”。数值越大，越容易把某个维度里漏掉的事实再补出来，但每个片段可能触发更多额外调用。
- `RESCAN_ROUNDS`：扫描完成后，针对遗漏片段或失败片段最多再补扫几轮。数值越大，整体更稳，但耗时和 token 成本都会继续增加。
- `MAX_MIDDLE_SUMMARY_CALLS`：扫描过程中最多生成多少次“中间上下文摘要”。它主要用来改善长文本跨片段承接，适合上下文依赖强的小说，但会直接增加额外模型调用。
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
