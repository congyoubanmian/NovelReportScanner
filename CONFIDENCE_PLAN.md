# 扫书软件：主观体验 → 定量分析 → 置信区间 建设计划

## 一、现状诊断

### 当前评分体系全景

项目已有**三层定量分析**，但置信度处理水平参差不齐：

#### 层1：规则统计层（纯文本规则，高客观性）
| 模块 | 指标 | 当前置信度处理 |
|------|------|---------------|
| reading_metrics | 张力/情绪均值+标准差、爽虐比、悬念率、投入度分布 | ✅ `confidence = min(1.0, n/30)` 基于样本量 |
| literary_metrics | 章节长度方差、句长标准差、对话比例、文笔规则分 | ✅ `"high"/"medium"/"low"` 三级 |
| sentiment_arcs | 情感极性曲线、波动量、升降趋势 | ✅ `"high"/"medium"/"low"` + 覆盖率说明 |
| readability_scorer | 平均句长、复杂句比例、可读性等级 | ✅ `"high"/"medium"/"low"` |

#### 层2：LLM 判断层（AI 语义分析，中主观性）
| 维度 | 评分方式 | 当前置信度处理 |
|------|---------|---------------|
| radar_scores（剧情/人物/世界观/节奏/文笔/情绪） | LLM 一次性打 0-10 分 | ❌ **无置信度**，仅靠 LLM 自报 |
| 纯洁度四维（身体/情感/精神/关系） | 规则+LLM 混合判断 | ⚠️ 部分：孩子来源有 `confidence`，伴侣判断有阈值门槛 |
| 男主/女主识别 | LLM + 规则交叉验证 | ⚠️ `"high"/"medium"/"low"` 但无区间 |

#### 层3：融合层（层1+层2 交叉验证）
| 模块 | 功能 | 当前缺陷 |
|------|------|---------|
| `fuse_scores_with_confidence()` | LLM radar × 规则 literary 交叉 | ⚠️ 只做了 writing/pacing/emotion 3维，plot/characters/worldbuilding 无规则对照 |
| `_compute_recommendation()` | 综合推荐等级 S/A/B/C/D | ❌ 无置信区间，一个点值定生死 |

### 核心问题

1. **LLM radar_scores 是"裸分"** — 一次调用出6个维度0-10分，无重复采样、无区间估计
2. **纯洁度判断缺乏不确定性度量** — 布尔判定（是/否），但"证据模糊"时只做了二选一，没说"有多大把握"
3. **融合层覆盖率低** — 只3/6维度有规则交叉验证
4. **推荐等级是点估计** — `composite = 7.2` → A 级，但 `[6.8, 7.6]` 可能跨越 A/B 边界

---

## 二、目标：建立统一置信区间体系

### 设计原则

**不是把主观变成客观，而是把"伪装确定的主观"变成"诚实的不确定性区间"**

- LLM 打了 8 分？我们说：`7.2 ± 1.3 (90% CI: 5.9–8.5)，置信度：中`
- 纯洁度判定"非处"？我们说：`判定：非处，置信度 0.82，反证风险：低`

### 新建模块：`confidence_engine.py`

负责所有评分的置信区间计算和不确定性传播。

---

## 三、写作手法 → 置信区间的映射方案

### 映射原理

每种"写作手法"可从三个维度量化其对评分的影响：

1. **可检测性（Detectability）** — 规则统计能多大程度捕捉到它
2. **LLM一致性（Agreement）** — 多次LLM采样之间的一致性
3. **证据强度（Evidence Strength）** — 文本中支持该判断的证据密度

### 写作手法映射表

| 写作手法 | 规则信号 | LLM信号 | 融合置信度公式 |
|---------|---------|---------|--------------|
| **伏笔工程** | foreshadowing 时间线密度 | LLM structural_function_tag | `CI = mean(密度×k, LLM_score) ± σ(碎片间距)` |
| **叙事节奏** | pacing_distribution 离散度 + 章节长度方差 | LLM pacing score | `CI = weighted_avg ± t×(s/√n)` |
| **人物弧光** | 角色提及频率曲线 + 互动密度变化 | LLM characters score | `CI = trend_slope ± margin` |
| **情绪调动** | sentiment polarity 曲线波动 + payoff_rate | LLM emotion score | `CI = weighted_avg ± z×σ_emotion` |
| **文笔水准** | 句长分布 + 对话/叙述比 + 修辞密度 | LLM writing score | `CI = rule_score ± tolerance` |
| **世界观一致性** | entity_prescan 实体稳定性 + continuity_audit | LLM worldbuilding score | `CI = 1 - error_rate ± Wilson` |
| **因果链强度** | causal_strength 分类分布 | LLM outline_architecture | `CI = weighted_score ± entropy` |
| **结构完整度** | structural_function_tag 分布 + arc_position 覆盖率 | LLM structure_execution_quality | `CI = coverage_rate ± margin` |

### 置信区间计算方法

#### 方法1：Bootstrap 重采样（适用于多chunk数据）
```python
def bootstrap_ci(scores: list, confidence=0.90, n_resample=1000):
    """从 chunk 级评分做 Bootstrap 重采样，计算均值的置信区间"""
    means = []
    for _ in range(n_resample):
        sample = random.choices(scores, k=len(scores))
        means.append(statistics.mean(sample))
    means.sort()
    lower = means[int((1 - confidence) / 2 * n_resample)]
    upper = means[int((1 + confidence) / 2 * n_resample)]
    return statistics.mean(scores), lower, upper
```

#### 方法2：Wilson 区间（适用于比率指标）
```python
def wilson_ci(successes: int, total: int, z=1.645):
    """适用于 payoff_rate, cliffhanger_rate 等比率指标的置信区间"""
    if total == 0:
        return 0.0, 0.0, 0.0
    p = successes / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    spread = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return p, max(0, center - spread), min(1, center + spread)
```

#### 方法3：LLM 多次采样一致性（适用于裸 LLM 分）
```python
def llm_consensus_ci(scores: list, threshold=1.5):
    """对同一维度的多次 LLM 采样，用一致性计算置信度"""
    if len(scores) < 2:
        return scores[0] if scores else 5.0, 5.0, 5.0, "low"
    mean = statistics.mean(scores)
    std = statistics.stdev(scores) if len(scores) > 1 else 0
    # 一致性 → 高置信度；分歧大 → 区间宽
    lower = mean - 1.645 * std  # 90% CI
    upper = mean + 1.645 * std
    conf = "high" if std < 0.8 else "medium" if std < 1.5 else "low"
    return mean, lower, upper, conf
```

#### 方法4：Dempster-Shafer 证据融合（适用于多源冲突）
```python
def dempster_combine(belief_a: dict, belief_b: dict):
    """当规则证据和LLM证据冲突时，用D-S理论融合不确定性"""
    combined = {}
    conflict = 0.0
    for ka, va in belief_a.items():
        for kb, vb in belief_b.items():
            if ka == kb:
                combined[ka] = combined.get(ka, 0) + va * vb
            elif ka != kb:
                conflict += va * vb
    if conflict >= 1.0:
        return {}  # 完全冲突
    for k in combined:
        combined[k] /= (1 - conflict)
    return combined, conflict
```

---

## 四、实施计划（分4个阶段）

### Phase A：新建 `confidence_engine.py`（核心引擎）
**工作量：~400行新代码，不改动现有逻辑**

- `bootstrap_ci()` — 多chunk评分重采样
- `wilson_ci()` — 比率指标区间
- `llm_consensus_ci()` — LLM 采样一致性
- `dempster_combine()` — 多源证据融合
- `propagate_uncertainty()` — 不确定性传播（加减运算时区间如何合并）
- `ScoreInterval` dataclass — 统一的评分区间容器

### Phase B：扩展规则覆盖（补齐交叉验证短板）
**工作量：~200行新代码 + 少量修改**

- 为 `plot/characters/worldbuilding` 3维度补充规则评分
  - `rule_plot_score`：基于 causal_strength 分布 + turning_point 密度
  - `rule_characters_score`：基于角色稳定性 + 互动密度变化
  - `rule_worldbuilding_score`：基于 entity_prescan 稳定性 + continuity error 率
- 扩展 `fuse_scores_with_confidence()` 覆盖全部6维度

### Phase C：接入现有评分流程
**工作量：修改 general_scan.py + report.py + novel_reviewer.py**

- general_scan `_summarize_book()`：输出 summary 时附加 CI
- report.py `_compute_recommendation()`：推荐等级改为区间（如 `A (6.8–7.6) 稳定` vs `A/B 边界 (6.2–7.4) 不稳定`）
- report.py 雷达图渲染：分数旁标注 `±x.x`
- novel_reviewer 纯洁度：布尔判定附加置信概率

### Phase D：报告呈现优化
**工作量：~修改 report.py 渲染逻辑**

- 雷达图分数改为区间条：`剧情 7.2 [6.0–8.4] 🟡中置信`
- 推荐等级改为分级+稳定性标注：`A级推荐（稳定）` vs `A/B 边界（不确定）`
- 纯洁度判定附加概率：`身体：❌ 非初（置信度 0.92，证据：3条/强）`
- 新增"分析局限性"段落：说明采样覆盖率、置信度边界

---

## 五、优先级排序

| 优先级 | 任务 | ROI | 风险 |
|--------|------|-----|------|
| **P0** | Phase A: confidence_engine.py | 高 | 低（纯新增） |
| **P1** | Phase C-1: 接入 radar_scores CI | 高 | 中（改summary结构） |
| **P1** | Phase C-2: 接入纯洁度置信概率 | 高 | 中（改判定输出） |
| **P2** | Phase B: 补齐规则覆盖 | 中 | 低（纯新增） |
| **P2** | Phase C-3: 推荐等级区间化 | 中 | 低（改渲染） |
| **P3** | Phase D: 报告呈现优化 | 中 | 低（改渲染） |

### 先做 Phase A + Phase C-1（radar_scores CI）

这是投入产出比最高的路径：
1. 新建一个独立模块（不碰现有代码）
2. 只在 `_summarize_book` 输出时附加 CI 字段
3. 在 report.py 渲染时读取并显示
4. 全程向后兼容——不读 CI 的代码不受影响
