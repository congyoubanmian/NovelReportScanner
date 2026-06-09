# AI-Reader-V2 借鉴归纳

分析来源：`/home/ctyun/workspace/AI-Reader-V2`，远程为 `https://github.com/mouseart2025/AI-Reader-V2`。

本文记录从 AI-Reader-V2 中可以借鉴的设计，以及已经应用到 NovelReportScanner 的改动。

## 已落地

### 1. 全书实体预扫描

AI-Reader-V2 的实体抽取管线先用程序规则做高召回预扫描，再把实体词典作为后续章节抽取的上下文提示。这个思路适合 NovelReportScanner 的通用/专项剧情扫描，但不适合直接搬它的数据库实体词典和 LLM 别名分组。

已在 `general_scan.py` 落地轻量版：

- 新增 `GENERAL_SCAN_ENTITY_PRESCAN`，默认开启。
- 从显式命名句式、对话说话人、章节标题、后缀类型和高频 n-gram 提取人物/地点/组织候选。
- 将候选注入 chunk prompt 的“全书预扫描实体候选”区块。
- 明确提示候选只用于提高召回，不能当成已确认事实或别名。
- 在输出 JSON 中记录 `entity_prescan`、`entity_prescan_count` 和 `entity_prescan_type_counts`，便于排查扫描召回问题。

对应测试覆盖：

- 实体候选提取不会把“张三说道”误提成“张三说”。
- prompt 中保留“以当前片段原文为准”的安全约束。
- 主流程会把预扫描候选传给每个 chunk，包括上下文溢出拆分后的重试路径。
- 新旧 summary 新鲜度校验会感知实体预扫描 schema，避免旧结果绕过新逻辑。

### 2. 任务稳定性与降载配置

AI-Reader-V2 的任务管线强调可恢复、失败分类、进度持久化和成本控制。NovelReportScanner 当前仍是文件结果 + Web 子进程队列，不适合整体迁移 asyncio/SQLite 任务服务，但可以先把线上最明显的 504 卡住问题收敛。

已落地：

- `API_SERVER_ERROR_MAX_RETRIES` 默认 `2`，同一请求遇到 500/502/503/504 时短重试后快速失败，避免一个大请求长期占住队列。
- `HAREM_SCAN_CHUNK_SIZE` 默认 `7000`，降低单次首扫输入长度。
- `HAREM_SCAN_MAX_TOKENS` 默认 `3000`，降低单次首扫输出压力。
- `HAREM_SCAN_RETRY_WORKERS` 默认 `1`，网关不稳定或单 Key 场景下减少补漏并发冲击。
- Web 配置页、`.env.sample`、`setting.txt.sample`、`docker-compose.yml`、README 均已同步这些配置。

## 可继续借鉴

### 1. 结构化 run_state

AI-Reader-V2 会记录任务阶段、章节状态、失败原因、耗时和 token 等运行状态。NovelReportScanner 后续可在现有 `web_manager_state.json` 或每本书结果目录中增加结构化字段：

- `stage`
- `current_chunk`
- `total_chunks`
- `failed_chunks`
- `error_type`
- `last_progress_at`
- `token_usage`
- `can_resume`

收益是重启后更容易判断“可恢复、需重试、已失败、已卡住”，也能减少只靠日志判断状态的问题。

### 2. 失败片段重试入口

AI-Reader-V2 支持失败章节查询和重试。NovelReportScanner 已经记录 `failed_chunks` 和 checkpoint，后续可以在 Web 端增加“只重试失败片段/失败阶段”的入口，避免整本书重扫。

建议失败类型至少拆分：

- API 5xx/504
- 超时
- JSON 截断/解析失败
- 上下文超长
- 权限或文件写入失败

### 3. 名称权威层

AI-Reader-V2 把泛称、亲属称谓、职衔、代词过滤和 canonical 选择集中在 name authority 规则中。NovelReportScanner 后续可抽一个轻量 `name_authority.py`，服务于：

- 女主别名合并
- 女主事实对象归一
- 关系对象去泛称
- 报告中同一角色多名碎片化合并

原则是泛称/职衔降权或需要上下文确认，不直接硬删“公主、队长、夫人”等可能有效的网文称呼。

### 4. 前端结构化报告视图

AI-Reader-V2 的阅读页、实体抽屉、图谱、时间线和 overrides 机制更完整。NovelReportScanner 可借鉴交互形态，但不应直接搬 React/Zustand 代码。

更贴合本项目的方向：

- 把报告问题做成可筛选结构化列表。
- 每个问题关联证据片段和来源文件。
- 增加“问题时间线”，按 chunk/章节展示质量波动、伏笔、连续性风险。
- 增加用户修正 overrides，保留“原始模型结果 + 用户修正 + 最终视图”的审计链。

## 不建议直接迁移

- 不直接搬 AI-Reader-V2 的 SQLite 章节事实表、WebSocket 分析服务和完整实体词典流程；这会和当前文件式扫描架构冲突。
- 不把 LLM 预扫描分类或别名分组当强事实；本项目更适合低成本提示增强和后置验证。
- 不直接替换现有后宫别名合并为全自动 Union-Find；`other_names` 污染风险仍需要现有的共现、证据和辈分冲突校验。
- 不迁移古典名著热修补或地图层级规则，除非后续产品目标明确转向世界结构可视化。

## 当前结论

本轮最适合立即应用的是“实体预扫描作为提示而非事实”和“504 降载配置”。这两项已经落地并有测试覆盖。其余能力更偏架构和产品化，应作为后续分支逐项推进。
