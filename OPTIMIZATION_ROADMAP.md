# 扫书优化路线图

> 生成时间：2026-06-10
> 核心理念：让扫书器像"写作大师/阅读大师"一样，一目十行、先粗后细、语义跳跃式聚焦
> 状态：全部实现完毕 ✅

---

## 总体思路

一个真正的阅读大师不是逐字读到最后一页的。他/她的阅读策略是：

1. **先翻目录** → 建立全局心智模型
2. **先粗后细** → 关键段落慢读，过渡段落快扫
3. **不拆叙事** → 不会在战斗高潮处翻页中断
4. **脑中划去已知** → 不重复提取已确认的事实
5. **带着疑问验证** → 对矛盾信息交叉比对
6. **聚焦当前维度** → 不会同时做 10 件事

---

## 已实现

### ✅ P0-1: 语义边界对齐切分

**文件**: `text_anchor.py`（新增 `build_semantic_chunk_manifest`）

**策略**: 章节优先
- 检测章节标题（正则），以章节为第一级分割单位
- 每章尽量作为一个 chunk；超过 chunk_size 的章节内部按段落分割
- 超大章节（> chunk_size × 1.5）内部检测场景切换断点作为辅助分割
- chunk 的 window 扩展不跨越章节边界

**配置**:
- `SEMANTIC_CHUNK_ENABLED=1`（默认开启，设为 0 回退原始逻辑）

**效果**:
- 锦衣夜行（3.9M 字）：原始 609 chunks → 语义 1091 chunks（97% 从章节边界开始）
- 修真四万年（10.9M 字）：原始 1688 chunks → 语义 3297 chunks，0.63s 生成

### ✅ P0-2: 大纲预扫描模块

**文件**: `outline_prescan.py`（新建）

**策略**: 规则提取 + 可选 LLM 增强
- 从每章提取标题 + 首段 + 尾段（零 LLM 成本）
- 为每章打信号标签（战斗/突破/情感/悬疑/转折/日常）
- `outline_to_compact_text()` 压缩为可注入 prompt 的紧凑文本（≤4000字）
- 可选 `enhance_outline_with_llm()` 用一次 LLM 调用生成章节摘要

**配置**:
- `OUTLINE_PRESCAN_ENABLED=1`（默认开启）
- `OUTLINE_PRESCAN_LLM_ENABLED=0`（默认关闭 LLM 增强）
- `OUTLINE_PRESCAN_SAMPLE_CHARS=300`
- `OUTLINE_PRESCAN_MAX_CHAPTERS=500`
- `OUTLINE_PRESCAN_COMPACT_MAX_CHARS=4000`

### ✅ P0-2b: 大纲注入 chunk prompt

**改动文件**: `novel_scan.py`、`general_scan.py`

**策略**: 精准定位注入
- 每个 chunk 的 prompt 注入"当前章节及前后 N 章"的大纲上下文
- 大纲标注为"仅供参考，不能替代原文证据"
- 注入量控制在 `OUTLINE_INJECT_MAX_CHARS`（默认 2000 字）以内

**配置**:
- `OUTLINE_INJECT_ENABLED=1`
- `OUTLINE_INJECT_CONTEXT_CHAPTERS=5`
- `OUTLINE_INJECT_MAX_CHARS=2000`

### ✅ P1-1: 结构化记忆体

**文件**: `scan_memory.py`（新建）

**策略**: 结构化记忆替代纯文本摘要
- `ScanMemory` 类维护：active_threads、character_states、timeline、confirmed_fact_hashes
- 每个 chunk 扫完后自动更新记忆体
- 记忆衰减：超过 50 个 chunk 未引用的线索降权
- 注入 prompt 时压缩到 800 字以内

**配置**:
- `SCAN_MEMORY_ENABLED=1`
- `SCAN_MEMORY_MAX_THREADS=30`
- `SCAN_MEMORY_MAX_TIMELINE=50`
- `SCAN_MEMORY_MAX_CHARS=800`
- `SCAN_MEMORY_DECAY_CHUNKS=50`

### ✅ P1-2: 分层 prompt / 维度聚焦扫

**改动文件**: `novel_scan.py`

**策略**: 首 chunk 完整 prompt，后续 chunk 压缩 prompt
- `_compact_system_prompt()` 截断超长示例，保留核心规则
- `_compact_system_prompt()` 在第 N 个 chunk 后自动启用
- 第一个 chunk 发送完整规则 + checklist，后续沿用精简版

**配置**:
- `LAYERED_PROMPT_ENABLED=1`
- `LAYERED_PROMPT_COMPACT_THRESHOLD=3`（前 3 个 chunk 用完整 prompt）

### ✅ P1-3: 增量去重 + 已知事实注入

**改动文件**: `novel_scan.py`

**策略**: 已确认事实注入 prompt，告诉 LLM 无需重复提取
- `_build_known_facts_block()` 从 ScanMemory 提取关键已确认事实
- 注入格式："【已确认事实（无需重复提取，只关注新事实）】"
- 只注入关键维度（sexual_relations、children_info、partner_relations）
- 控制注入量在 600 字以内

**配置**:
- `INCREMENTAL_DEDUP_ENABLED=1`
- `INCREMENTAL_DEDUP_INJECT_MAX_FACTS=15`
- `INCREMENTAL_DEDUP_INJECT_MAX_CHARS=600`

### ✅ P2-1: 矛盾检测 + 置信度分级

**文件**: `contradiction_detector.py`（新建）

**策略**: 规则驱动的矛盾检测 + 多维度置信度评分
- 预定义矛盾规则（如"未破处 vs 有孩子"、"无性关系 vs 有孩子"）
- 置信度评分基于：evidence_level、证据长度、evidence_strength、speech_act
- 输出矛盾报告和置信度标注
- 集成到 novel_reviewer.py，在二审前自动运行

**配置**:
- `CONTRADICTION_DETECTION_ENABLED=1`
- `CONFIDENCE_SCORING_ENABLED=1`

### ✅ P2-2: 快慢双通道

**改动文件**: `novel_scan.py`

**策略**: 低密度 chunk 用精简 max_tokens，高密度用完整 max_tokens
- `_chunk_density_level()` 快速判断 chunk 密度
- `_effective_max_tokens_for_chunk()` 根据密度返回不同 max_tokens
- 快通道：max_tokens=2000（低密度日常段落）
- 慢通道：max_tokens=6000（高密度关键段落）

**配置**:
- `FAST_SLOW_CHANNEL_ENABLED=1`
- `FAST_CHANNEL_MAX_TOKENS=2000`
- `FAST_CHANNEL_DENSITY_THRESHOLD=low`
- `SLOW_CHANNEL_MAX_TOKENS=6000`

### ✅ P2-3: 两阶段自适应采样

**改动文件**: `general_scan.py`

**策略**: 先用极低 max_tokens 快速扫判断 chunk 价值，再筛选
- `_quick_scan_for_value()` 用 300 max_tokens 输出 `has_high_value` + `value_score`
- 保留高价值 chunk + 均匀采样保底（每 20 个 chunk 至少保留 1 个）
- 仅当 chunk 数 > 30 时启用（小书无需两阶段）

**配置**:
- `GENERAL_SCAN_TWO_STAGE_SAMPLING=1`
- `GENERAL_SCAN_TWO_STAGE_QUICK_MAX_TOKENS=300`
- `GENERAL_SCAN_TWO_STAGE_HIGH_VALUE_THRESHOLD=0.5`

---

## 新增文件清单

| 文件 | 功能 |
|------|------|
| `outline_prescan.py` | 大纲预扫描：章节结构提取、标签标注、紧凑文本生成 |
| `scan_memory.py` | 结构化记忆体：活跃线索、角色状态、时间线、事实指纹 |
| `contradiction_detector.py` | 矛盾检测 + 置信度分级 |

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `text_anchor.py` | 新增 `build_semantic_chunk_manifest`、章节检测、场景断点 |
| `novel_scan.py` | 集成语义切分、大纲注入、记忆体、分层 prompt、去重、快慢通道 |
| `general_scan.py` | 集成语义切分、大纲注入、记忆体、两阶段采样 |
| `novel_reviewer.py` | 集成矛盾检测和置信度标注 |

## 配置变量汇总

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEMANTIC_CHUNK_ENABLED` | `1` | 语义边界感知切分 |
| `OUTLINE_PRESCAN_ENABLED` | `1` | 大纲预扫描 |
| `OUTLINE_PRESCAN_LLM_ENABLED` | `0` | 大纲 LLM 增强 |
| `OUTLINE_PRESCAN_SAMPLE_CHARS` | `300` | 每章首/尾段采样字符数 |
| `OUTLINE_PRESCAN_MAX_CHAPTERS` | `500` | 大纲最大章节数 |
| `OUTLINE_PRESCAN_COMPACT_MAX_CHARS` | `4000` | 压缩大纲最大字符数 |
| `OUTLINE_INJECT_ENABLED` | `1` | 大纲注入 chunk prompt |
| `OUTLINE_INJECT_CONTEXT_CHAPTERS` | `5` | 注入当前章节前后几章 |
| `OUTLINE_INJECT_MAX_CHARS` | `2000` | 大纲注入最大字符数 |
| `SCAN_MEMORY_ENABLED` | `1` | 结构化记忆体 |
| `SCAN_MEMORY_MAX_CHARS` | `800` | 记忆体注入最大字符数 |
| `SCAN_MEMORY_DECAY_CHUNKS` | `50` | 记忆衰减阈值 |
| `LAYERED_PROMPT_ENABLED` | `1` | 分层 prompt |
| `LAYERED_PROMPT_COMPACT_THRESHOLD` | `3` | 前几个 chunk 用完整 prompt |
| `INCREMENTAL_DEDUP_ENABLED` | `1` | 增量去重 |
| `INCREMENTAL_DEDUP_INJECT_MAX_FACTS` | `15` | 已知事实注入最大条数 |
| `INCREMENTAL_DEDUP_INJECT_MAX_CHARS` | `600` | 已知事实注入最大字符数 |
| `CONTRADICTION_DETECTION_ENABLED` | `1` | 矛盾检测 |
| `CONFIDENCE_SCORING_ENABLED` | `1` | 置信度评分 |
| `FAST_SLOW_CHANNEL_ENABLED` | `1` | 快慢双通道 |
| `FAST_CHANNEL_MAX_TOKENS` | `2000` | 快通道 max_tokens |
| `SLOW_CHANNEL_MAX_TOKENS` | `6000` | 慢通道 max_tokens |
| `GENERAL_SCAN_TWO_STAGE_SAMPLING` | `1` | 两阶段自适应采样 |
| `GENERAL_SCAN_TWO_STAGE_QUICK_MAX_TOKENS` | `300` | 快速扫 max_tokens |

## 回退方案

所有优化都有配置开关，可独立关闭回退到原始行为：

```bash
# 关闭全部优化（回退原始行为）
SEMANTIC_CHUNK_ENABLED=0
OUTLINE_PRESCAN_ENABLED=0
OUTLINE_INJECT_ENABLED=0
SCAN_MEMORY_ENABLED=0
LAYERED_PROMPT_ENABLED=0
INCREMENTAL_DEDUP_ENABLED=0
CONTRADICTION_DETECTION_ENABLED=0
FAST_SLOW_CHANNEL_ENABLED=0
GENERAL_SCAN_TWO_STAGE_SAMPLING=0
```
