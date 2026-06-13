# 准确性优先改进计划（token/时间无约束，1M上下文模型）

## 核心原则

**准确性 > 一切。** 扫3遍得对 > 扫1遍得错。多花10倍token换5%准确率提升 = 值得。

---

## 一、立即可做：解除人为限制（改配置即可，零代码）

既然有1M上下文，以下限制纯属浪费：

| 配置 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| `GENERAL_SCAN_MAX_CHUNKS` | 80 | `0`（无限制） | 全量扫描，覆盖率100% |
| `GENERAL_SCAN_TWO_STAGE_SAMPLING` | `1` | `0` | 关闭两阶段过滤，不做价值预筛 |
| `GENERAL_SCAN_CONTEXT_MAX_CHARS` | 1600 | `16000+` | 大幅扩大滚动上下文窗口 |
| `GENERAL_SCAN_CONTENT_AWARE_SAMPLING` | `1` | `0` | 关闭内容感知采样，全量扫 |
| `max_tokens`(summary) | 5200 | `16000+` | 给LLM足够空间输出详细分析 |
| `max_tokens`(chunk scan) | 3800 | `8000+` | 每个chunk输出更详细 |

这些都是env var，**不需要改代码**，在docker-compose或启动命令中设置即可。

---

## 二、结构性准确性瓶颈（需要改代码）

### 🔴 A1：伏笔全局注册表 — 事后全量匹配

**问题本质：** 当前伏笔追踪依赖LLM的上下文记忆（滚动上下文列表），列表有上限、且LLM不一定会主动做跨chunk匹配。

**即使1M上下文也解决不了：** 因为伏笔信息是分散在每个chunk的独立LLM调用中产生的，不是一次调用看全文。

**方案：** 新建 `foreshadowing_registry.py`

扫描完成后，收集所有chunk的伏笔数据，用确定性算法做双向匹配：

```
chunk_results 中每个 chunk 都有:
  - new_foreshadowing: ["描述A", "描述B", ...]
  - foreshadowing_resolutions: [{"resolved_item": "描述A", ...}]
  - false_foreshadowing: ["描述C"]

事后处理:
  1. 收集所有 new_foreshadowing → 全局伏笔池
  2. 收集所有 resolutions → 在全局池中模糊匹配来源
  3. 匹配算法: 关键词重合度 + TF-IDF + 可选 embedding
  4. 输出: 每个伏笔的完整生命周期
     - planted_chunk → resolved_chunk
     - resolution_distance (跨度)
     - status: resolved / orphaned / false_lead
```

**准确性提升点：** 
- `orphaned`（埋了没收）能被发现
- `resolution_distance` 分布是客观统计
- 不受滚动上下文列表上限影响

### 🔴 A2：分段汇总架构 — 替代单次大prompt

**问题本质：** `_summarize_book()` 把所有chunk材料压缩到一个prompt里发给LLM。即使1M上下文能放下，LLM在超长prompt中的注意力会退化（"lost in the middle"问题）。

**方案：层级聚合**

```
5000 chunk_results
    ↓ 每50个一组，LLM生成 segment_summary（~20个字段）
100 segment_summaries
    ↓ 每10个一组，LLM生成 arc_summary
10 arc_summaries
    ↓ LLM融合
1 book_summary + radar_scores
```

每层LLM调用：
- 输入可控（50个chunk的材料 ≈ 50K字符，远在有效注意力范围）
- 输出完整（不需要 _compact_*_for_summary 截断）
- 可以用高 max_tokens

**关键改进：** 每层summary都保留完整的伏笔/角色/线索信息，不会因截断丢失。

### 🔴 A3：radar_scores 多次采样 + 交叉验证

**问题本质：** 6个维度的0-10分来自一次LLM调用，是点估计。

**方案：**

```
radar_scores 三路独立来源:
  来源1: LLM chunk级评分（每个chunk的writing_quality中已有评分）
         → 聚合为 rule_plot_score, rule_writing_score 等
  来源2: 文学指标规则评分（literary_metrics 已有）
  来源3: book_summary LLM 评分（当前方式）

融合: fuse_scores_with_confidence() 扩展到全部6维度
     差异 ≤1.5 → high confidence，取均值
     差异 1.5-3.0 → medium confidence，偏向LLM
     差异 >3.0 → medium confidence，标注矛盾
```

### 🟡 A4：纯洁度/关键判定多次验证

**问题本质：** 单次LLM判定 + 固定阈值0.75，复杂场景容易误判。

**方案：** 对低置信度判定做二轮验证

```
第一轮: 正常判定 → confidence >= 0.85 → 确认
                      confidence 0.65-0.85 → 进入第二轮
                      confidence < 0.65 → 标注"无法确定"

第二轮: 换prompt角度重新判定（不告知第一轮结果）
        两轮一致 → 提升置信度到0.85+
        两轮不一致 → 标注"证据不足，结果不确定"
```

### 🟡 A5：滚动上下文扩大 + 列表上限解除

**方案：**

```python
# general_scan.py 修改
# 当前:
state["active_foreshadowing"] = _dedupe_extend(..., limit=50)
state["progress_summaries"] = _dedupe_extend(..., limit=10)

# 改为根据文本规模动态调整:
text_len = total_text_length
foreshadow_limit = max(50, text_len // 100_000)  # 10M文本 → 100条
progress_limit = max(10, text_len // 500_000)     # 10M文本 → 20条
char_limit = max(1600, text_len // 100)           # 10M文本 → 100K字符

# 同步修改 _rolling_context_snapshot 的 max_chars
# 同步修改 _trim_context_snapshot 的截断逻辑
```

### 🟡 A6：chunk overlap 和扫描增强

**当前：** chunk_size=12000, overlap=1000

**问题：** 伏笔/角色信息可能被截断在chunk边界

**方案：**
- overlap 增加到 2000-3000（减少边界截断）
- 或者改为"语义边界分块"（在章节末尾分块，不在章节中间断开）

---

## 三、优先级排序（准确性导向）

| 优先级 | 任务 | 准确性影响 | 代码量 | 风险 |
|--------|------|-----------|--------|------|
| **P0** | 解除人为限制（env var配置） | 🔴高 | 0行 | 无 |
| **P0** | A2: 分段汇总架构 | 🔴高 | ~400行 | 中 |
| **P0** | A1: 伏笔全局注册表 | 🔴高 | ~350行 | 低 |
| **P1** | A3: radar多采样+交叉验证 | 🟡中 | ~200行 | 中 |
| **P1** | A5: 滚动上下文扩大 | 🟡中 | ~100行 | 低 |
| **P2** | A4: 纯洁度多次验证 | 🟡中 | ~150行 | 中 |
| **P2** | A6: chunk overlap增强 | 🟢低 | ~50行 | 低 |

## 四、建议执行顺序

```
第0步: 解除env var限制（立即生效，零风险）
第1步: A2 分段汇总架构（最大准确性提升）
第2步: A1 伏笔全局注册表（解决跨超长文本追踪）
第3步: A5 滚动上下文扩大（配合A1/A2）
第4步: A3 radar多采样验证
第5步: A4 纯洁度多次验证
```
