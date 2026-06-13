# 超长文本（200万-800万字+）适配性诊断与改进计划

## 一、现状架构总结

### 分模块扫描策略

| 模块 | chunk大小 | 采样上限 | 覆盖率(800万字) | API调用估算 |
|------|----------|---------|----------------|-------------|
| **novel_scan**（女主/纯洁度） | 6000字 | ❌ 无上限 | **100%**（全量扫） | ~31,500次 |
| **general_scan**（写作质量） | 12000字 | 400 chunk | **18.3%** | ~2,400次 |
| **protagonist**（男主角色） | 7000字 | 80 chunk（共享） | **~5%** | ~480次 |
| **novel_reviewer**（后处理） | — | 依赖前述结果 | N/A | ~500次 |

### 关键发现

**novel_scan 是瓶颈**：它全量扫描每个 chunk（无 MAX_CHUNKS 限制），每 chunk 触发 fact_boost(×2) + dim_boost(×3) + 3轮补扫，800万字需要约 31,500 次 API 调用。

---

## 二、超长文本的6大核心问题

### 问题1：伏笔追踪在超长文中失效 🔴 严重

```
伏笔生命周期：埋设(chunk #50) → ... 5000 chunks ... → 回收(chunk #4500)
```

**滚动上下文的限制：**
- `active_foreshadowing` 列表上限 = **50条**
- `CONTEXT_MAX_CHARS` = **1600字符**（注入到每个chunk的LLM prompt中）
- `_rolling_context_snapshot()` 传入LLM的上下文只保留最近6条 progress + 10条 active_foreshadowing

**后果：** chunk #50 的伏笔在 chunk #200 时已经从 active 列表被挤掉。当 chunk #4500 回收这个伏笔时，LLM 完全不知道它存在。**跨百章伏笔无法被检测到。**

### 问题2：novel_scan 全量扫描——API成本和时间爆炸 🔴 严重

| 规模 | 总chunks | API调用 | 估算时间（6线程） |
|------|---------|---------|-----------------|
| 200万字 | 1,250 | ~7,875 | **2.9小时** |
| 500万字 | 3,125 | ~19,687 | **7.3小时** |
| 800万字 | 5,000 | ~31,500 | **11.7小时** |

即使不计 token 成本，**单次扫描时间过长**，中途任何 API 超时/网络抖动都可能导致大量重试。

### 问题3：内存累积——chunk_results 全部驻留内存 🟡 中等

```python
# general_scan.py: chunk_results 列表线性增长
chunk_results.append(result)  # 每个 ~12KB JSON
```

400 chunks × 12KB = ~4.7MB（可控），但 novel_scan 全量 5000 chunks 时：
- `all_issues` 列表：可能累积数万条
- `all_heroine_facts` 列表：可能累积上万条
- checkpoint JSON 序列化：可能达到 **50-100MB**
- `json.loads(json.dumps(state))` 深拷贝在 `_update_rolling_context_state` 中每次调用都复制整个状态

### 问题4：采样覆盖率过低——大部分原文从未被LLM看到 🟡 中等

800万字 general_scan 覆盖率仅 **18.3%**：
- 81.7% 的原文从未被 LLM 分析
- 写作质量评分基于不到 1/5 的内容
- 情感曲线、节奏分析只能反映采样片段

### 问题5：弱模型能力限制 🟡 中等

使用低价弱模型带来的影响：
- **结构化输出不稳定**：复杂 JSON schema 更容易截断/格式错误
- **上下文理解弱**：1600字符滚动上下文对弱模型来说信息量太少
- **伏笔/因果识别弱**：弱模型难以理解隐含的因果和伏笔
- **角色一致性差**：同一角色在不同 chunk 可能被识别为不同人

### 问题6：checkpoint 磁盘 I/O 瓶颈 🟢 低

全量 checkpoint JSON 在超长文时可能达到 50-100MB，每次保存都全量写入：
- `_write_full_checkpoint_data()` 会阻塞线程（有 CHECKPOINT_LOCK）
- 增量 delta 机制存在但触发条件严格

---

## 三、改进计划

### Phase 1：伏笔/线索全局注册表（解决跨超长文本追踪）

**新建 `foreshadowing_registry.py`**

核心思想：**不依赖滚动上下文传递伏笔，而是维护一个全局注册表，对每个 chunk 的扫描结果做双向匹配。**

```python
@dataclass
class ForeshadowingEntry:
    id: str               # 基于描述文本的 hash
    description: str       # 伏笔描述
    planted_chunk: int     # 埋设位置
    resolved_chunk: int | None  # 回收位置
    status: str            # active / resolved / false / orphaned
    resolution_distance: int | None  # 埋设→回收的 chunk 距离
```

**工作流程：**
1. 所有 chunk 扫描完成后，收集所有 `new_foreshadowing` 和 `foreshadowing_resolutions`
2. 用**模糊匹配**（关键词重合度 + embedding 相似度）连接埋设和回收
3. 标注 `orphaned`（埋了没回收）和 `unmatched_resolution`（回收了但找不到来源）
4. 计算 `resolution_distance` 分布——伏笔回收跨度统计

**优势：** 不依赖 LLM 的上下文记忆，而是用事后匹配重建伏笔网络。

### Phase 2：novel_scan 分级扫描（解决全量扫描瓶颈）

**三级扫描架构：**

```
Level 1: 快速预扫（规则过滤）→ 100% 覆盖，零 API 成本
    ↓ 筛选出高价值 chunk（含角色名/关系词/敏感词）
Level 2: 轻量 LLM 扫描（300 token）→ 高价值 chunk，~30% 覆盖
    ↓ 筛选出含关键事件的 chunk
Level 3: 深度 LLM 扫描（3000 token）→ 关键 chunk，~10% 覆盖
```

**实现方式：**
- Level 1：纯规则，基于 `_entity_prescan_candidates()` 的实体密度 + 敏感词词典
- Level 2：简化 prompt（只提取角色名+关系标签，不做深度判断）
- Level 3：完整 prompt（纯洁度判断、详细事实提取）

**预期效果（800万字）：**
- Level 1：5000 chunks × 0秒 = 0 分钟
- Level 2：~1500 chunks × 2秒 / 6线程 = 8 分钟
- Level 3：~500 chunks × 8秒 / 6线程 = 11 分钟
- **总计：~20分钟**（vs 当前 11.7小时，提速 35×）

### Phase 3：分段汇总架构（解决信息丢失）

**当前问题：** `_summarize_book()` 把所有 chunk_results 压缩成一个材料包给 LLM 做 summary，超长文时信息大量丢失。

**改进：分段汇总→层级聚合**

```
chunk_results (500个)
    ↓ 每50个一组，LLM 生成 segment_summary
segment_summaries (10个)
    ↓ LLM 融合
book_summary (1个)
```

这样每层 LLM 的输入都是可控大小，信息密度高。

### Phase 4：弱模型适配优化

**prompt 简化策略：**
- 拆分大 schema 为多个小 schema（弱模型处理小 JSON 更稳定）
- 使用 few-shot 示例代替复杂指令描述
- 降低 `max_tokens` 以减少截断概率
- 对弱模型启用 `response_format: json_schema`（如 API 支持）

**结构化输出增强：**
- 增加后验校验：检查必填字段是否存在、枚举值是否合法
- 弱模型输出失败时自动 downshift 到更简单的 prompt
- 关键判定（纯洁度）使用多轮验证而非单次判定

### Phase 5：磁盘/内存优化

- checkpoint 改用 NDJSON（每行一个 JSON 对象，可增量追加）
- chunk_results 分批写入磁盘而非全量驻留内存
- `_update_rolling_context_state` 避免深拷贝（改为原地更新+快照引用）
- 大列表（all_issues, all_heroine_facts）改用 SQLite 临时表

---

## 四、优先级排序

| 优先级 | 任务 | 效果 | 风险 | 工作量 |
|--------|------|------|------|--------|
| **P0** | Phase 2: novel_scan 分级扫描 | 扫描时间 35× 提速 | 高（改核心扫描逻辑） | ~800行 |
| **P0** | Phase 1: 伏笔全局注册表 | 伏笔追踪可用 | 低（纯新增模块） | ~400行 |
| **P1** | Phase 3: 分段汇总架构 | 超长文 summary 质量 | 中（改 _summarize_book） | ~300行 |
| **P1** | Phase 4: 弱模型适配 | 输出稳定性提升 | 中（改 prompt + 校验） | ~200行 |
| **P2** | Phase 5: 磁盘/内存优化 | 稳定性提升 | 低（改存储层） | ~300行 |

---

## 五、建议执行顺序

```
第1步：新建 foreshadowing_registry.py（纯新增，不改现有逻辑）
第2步：给 novel_scan 加分级扫描（Level 1 规则预扫 + Level 2/3 LLM 扫描）
第3步：改造 _summarize_book 为分段汇总
第4步：弱模型 prompt 适配
第5步：内存/磁盘优化
```

每步都保持向后兼容——新功能用 env var 开关控制，默认行为不变。
