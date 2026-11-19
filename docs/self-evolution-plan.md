# 自动自进化执行方案（v1）

> 目标：把当前“单题低分 → 改一条全局 prompt → 重启验证”的补丁式优化，升级为**可度量、可归因、可回滚、可持续**的进化系统。
>
> 结论先说：**不要先扩大 Agent 的改代码权限，先修评测尺子和知识飞轮。** 现有 9 次自优化中 5 次是 `knowledge_gap`、3 次是 `regression_failed`、0 次成功；瓶颈不是“优化器不够聪明”，而是“知识没补上 + 尺子抖动 + 变更不可归因”。

---

## 1. 现状审计

### 1.1 已有闭环

```text
/ask_smart 评估
  → analytics/evaluation.py 低分入队（total ≤ 6）
  → trigger --daemon 轮询 pending_queue.json
  → optimizer_agent 诊断（最多 2 次）
  → 改 overrides.json（prompt_rule / 参数）
  → 重启 8001 → 单题复测
  → run_regression（6 道核心题 + 2 道随机题）
  → sync_to_production → 重启 8000
  → daily_benchmark_guard 夜间全量回归 + 回滚
```

涉及文件：

| 能力 | 位置 |
|---|---|
| 低分入队 | `analytics/evaluation.py` |
| 队列 / 去重 / 巡检 / 同步 | `optimization/agent/trigger.py` |
| 受限 ReAct 优化器 | `optimization/agent/optimizer_agent.py` |
| 评分 | `optimization/utils/scorer.py` |
| 验证 / Benchmark | `optimization/agent/mcp_tools.py` |
| 夜间守护 | `optimization/agent/daily_benchmark_guard.py` |
| 变更白名单 / 审计 | `config/overrides.py` |
| 守护进程 | `cli/service.py` → `trigger --daemon` |

### 1.2 实测结果

`auto_optimization/logs/optimization_history.jsonl`：

| 状态 | 次数 | 说明 |
|---|---:|---|
| `knowledge_gap` | 5 | 知识库确实没有内容，调 prompt / 阈值无解 |
| `regression_failed` | 3 | 单题已到 8~9 分，但回归判定失败 |
| `give_up` | 1 | 两次尝试未达 6 分 |
| `success` | **0** | 没有一次稳定同步上线 |

三个典型信号：

1. **知识缺口是主因**：长征、抗战、平型关、红楼梦等缺口，任何检索参数都补不出来。
2. **尺子噪声大于优化幅度**：`regression_failed` 的 final_score 是 8~9 分，说明失败来自回归集抖动/单次 judge，而不是回答真的变差。
3. **全局 prompt_rule 不可归因**：一次优化把一条规则写给所有问题，下一次成功又会覆盖它，无法累积，也无法判断是哪条规则起了作用。

### 1.3 结构性问题（按优先级）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P0-1 | 评分单次采样，judge 抖动 | `scorer.score_answer()` 单次调用；回归 `times=1` | 8~9 分被误判回退，优化器学不到有效信号 |
| P0-2 | 回归集太小、门槛太松 | `lite_set.json` 6 题 + 2 随机；`worst > -2` 即通过 | 单题波动就能否决/放行 |
| P0-3 | 回滚可能回到很久以前 | `daily_benchmark_guard.py:48` 取全局排序第一个备份 | 当前工作区无 `backups/`，回滚实际无效 |
| P0-4 | `TOP_K` 是死参数 | `ALLOWED_PARAMS` 有 `TOP_K`，但 `bootstrap.py` 从未 `get_param("TOP_K")` | 优化器改了一个不影响行为的参数 |
| P0-5 | `prompt_rule` 单槽覆盖 | `config/overrides.py:167` 直接覆盖 | 规则无法累积、无法 A/B、无法归因 |
| P0-6 | 没有实验账本 | 只有 incident history，无 candidate / baseline / CI / decision | 无法回答“哪种进化有效” |
| P0-7 | 知识缺口没有执行者 | `daily_patrol` 只入队；`wiki/gaps/` 无自动闭环 | 5/9 失败重复发生 |
| P0-8 | 同步是全量重启 | `sync_to_production` 直接重启 8000 | 无灰度、无 canary、故障半径大 |
| P0-9 | 优化器直接写运行环境 | `apply_change` → 重启 8001 | 缺少 shadow / 候选隔离 |
| P0-10 | 索引新鲜度不在闭环内 | 设计文档已记录陈旧 93 篇、漏收 10 篇 | 召回基线不可信 |

---

## 2. 目标：三层进化闭环

### 2.1 从“单题补丁”到“策略学习”

```text
快循环（请求级，秒~分钟）
  低分 / 零命中 / 引用失败 / 用户负反馈
    → 只做“采集与归因”，不直接改生产

中循环（天级）
  失败聚类 → 知识需求单 / 候选变更
    → 离线评测 → 门禁 → shadow
    → 人工批准 or 自动灰度
    → 监控 → 保留或回滚

慢循环（周/月级）
  实验账本 → 动作成功率 → 更新优化策略
    → 调整动作空间、预算、提示词
    → 淘汰无效动作，补充新动作
```

核心原则：

1. **一次只进化一个变量**：一条规则、一个参数、一个知识主题、一个路由策略。
2. **先离线、再 shadow、再 canary、最后全量**：生产永远不是试验场。
3. **没有对照就不算进化**：每个候选必须有 baseline、样本量、置信区间、回滚点。
4. **知识进化优先于提示词进化**：知识库没有的内容，prompt 写花也变不出来。
5. **代码进化走 PR，不走热改**：LLM 可以在 worktree 里改代码，但必须过测试、门禁、人工合并。

### 2.2 分层进化矩阵

| 层 | 进化对象 | 自动化程度 | 频率 | 主要门禁 |
|---|---|---|---|---|
| L0 知识 | vault 文档、gap 关闭、索引 | 自动发现 + 人工批准写入 | 每日 | gold 召回 ≥ 0.8、原题复测通过 |
| L1 检索策略 | 阈值、top_k、融合权重、chunk、上下文预算 | 离线搜索 + 自动灰度 | 每周 | 全量 benchmark 不退化、成本不升 |
| L2 提示词/回答策略 | prompt 规则库、回答模板、拒答策略 | 候选生成 + A/B | 每周 | 分域不退化、引用精度 ≥ 0.95 |
| L3 工作流 | 单跳 vs 研究 Agent、路由、预算 | 配对实验 + 人工决策 | 每两周 | 多跳组提升、单跳组不退 |
| L4 代码 | 检索/评分/工具实现 | Agent 改 worktree + PR | 每月/按需 | `make test` + `make arch` + 人工 review |

**不建议**让优化器直接修改 `server.py` / `server_auto.py` 并重启。现有 `run_agent_claude_cli()` 只能作为实验，不能进生产闭环。

---

## 3. 目标架构与模块落位

新增旁路包（不破坏四层依赖门禁）：

```text
optimization/evolution/
├── signals.py       # 采集：低分、零命中、引用失败、延迟、成本、索引漂移
├── taxonomy.py      # 失败分类：knowledge_gap / retrieval_miss / ranking / context / prompt / routing / stale_index
├── candidates.py    # 候选生成：知识、参数、规则、工作流
├── evaluate.py      # 离线评测：固定集 + holdout + 多次采样 + paired bootstrap
├── gate.py          # 门禁：提升显著性、回退上限、成本/延迟约束
├── ledger.py        # 实验账本：candidate / baseline / metrics / decision / rollback
├── promote.py       # 版本化、shadow、canary、自动回滚
├── curator.py       # 知识缺口聚类、需求单、审批、ingest、复测
└── policy.py        # 元学习：动作成功率、预算、动作空间
```

建议新增数据表（`var/db/evolution.db`，或复用 `analytics.db`）：

```sql
CREATE TABLE experiments (
    experiment_id   TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    source_type     TEXT NOT NULL,        -- incident | patrol | scheduled | manual
    layer           TEXT NOT NULL,        -- knowledge | retrieval | prompt | workflow | code
    hypothesis      TEXT NOT NULL,
    candidate_json  TEXT NOT NULL,
    baseline_json   TEXT NOT NULL,
    metrics_json    TEXT,
    decision        TEXT,                 -- pending | shadow | canary | promoted | rejected | rolled_back
    version         TEXT,
    decided_at      TEXT,
    rollback_reason TEXT
);

CREATE TABLE metric_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id   TEXT NOT NULL,
    stage           TEXT NOT NULL,        -- offline | shadow | canary | prod
    metric          TEXT NOT NULL,
    value           REAL NOT NULL,
    sample_size     INTEGER,
    ts              TEXT NOT NULL
);

CREATE TABLE gaps (
    gap_id          TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    occurrences     INTEGER NOT NULL,
    questions_json  TEXT NOT NULL,
    why_missing     TEXT,
    status          TEXT NOT NULL,        -- open | approved | ingesting | closed | wontfix
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);
```

配置扩展（`auto_optimization/config.yaml`）：

```yaml
evolution:
  enabled: true
  mode: shadow                 # shadow | canary | auto；上线初期必须是 shadow
  max_experiments_per_day: 4
  max_changed_rules: 3
  judge_samples: 3             # 取中位数
  min_improvement: 0.3         # 主指标至少提升
  max_regression: 0.2          # 单题/分域最大允许回退
  canary_stages: [0.05, 0.25, 1.0]
  soak_hours: 24
  daily_budget_usd: 5
  freeze_windows: ["00:00-06:00"]
  kill_switch_file: "auto_optimization/PAUSED"
```

---

## 4. 分阶段执行计划

### Phase 0：修尺子与安全底座（2~3 天）

**目标：让“变好/变坏”可复现，再谈自动进化。**

任务：

1. **切换 shadow 模式**：`evolution.mode=shadow`，停止任何自动 `sync_to_production`；候选只写报告和 8001。
2. **修复夜间回滚**：`rollback_to_first_stable()` 只允许回滚“今天 00:00 之后”的第一个 `overrides.prod.before_sync.*`；无备份则明确失败，不静默跳过。
3. **修复死参数**：把 `TOP_K` 接到 `RetrievalService` 的 `semantic_k/rewrite_k`，或从 `ALLOWED_PARAMS` 删除。二选一，不能留“假旋钮”。
4. **评分多次采样**：`score_answer()` 增加 `samples=3` 取中位数；回归、guard、单题验证统一走 `score_answer_median()`。
5. **回归集扩容**：从 18 题扩到 ≥60 题；固定 holdout 20%；每次回归至少 20 题，单题阈值改为“中位数回退 > 1.0 才失败”。
6. **索引新鲜度门禁**：优化前比较磁盘文档数 / mtime 与索引快照；不新鲜先 `POST /reindex` 或直接拒绝进化。
7. **实验账本最小版**：先 JSONL，字段包含 `experiment_id / layer / hypothesis / candidate / baseline / metrics / decision / version`。
8. **Kill switch**：daemon 每轮检查 `auto_optimization/PAUSED`，存在即退出；增加 `evolution pause/resume` 命令。

验收：

- 同一批 10 题连续跑 3 次，平均分极差 ≤ 0.5；单题中位数极差 ≤ 1.0。
- `make test`、`make arch`、`make gate` 全绿。
- 手动触发一次“假回滚”演练，5 分钟内恢复到指定版本。
- 账本里能看到一条完整实验记录（即使 decision=rejected）。

### Phase 1：知识飞轮（1 周，最高 ROI）

**目标：把 5/9 的 `knowledge_gap` 变成可执行的补库流程。**

任务：

1. `curator.py` 汇总四个来源：
   - `optimization_failures.jsonl` 的 `knowledge_gap`
   - `daily_patrol` 的低分题
   - Agent 运行 `gaps_json`（后续接入）
   - `wiki/gaps/*.md` 已有开放问题
2. 用 jieba + embedding 聚类（阈值 0.75），输出「知识需求单」：
   ```json
   {
     "topic": "长征途经省份",
     "occurrences": 3,
     "questions": ["长征经过了几个省？"],
     "why_missing": "现有长征页只有里程与会议，无省份维度",
     "suggested_action": "补充党史/军史资料，或标注为不覆盖",
     "priority": "high"
   }
   ```
3. 默认 dry-run：只写 `auto_optimization/gaps/YYYY-MM-DD.json` 和 `wiki/gaps/<主题>.md`，不碰正文。
4. 增加审批命令：
   ```bash
   .venv/bin/python -m optimization.evolution.curator --report
   .venv/bin/python -m optimization.evolution.curator --approve <gap_id>
   .venv/bin/python -m optimization.evolution.curator --reject <gap_id> --reason ...
   ```
5. 批准后：资料进 `raw/inbox/` → 复用现有 ingest → 重建索引 → 重跑原失败题 3 次取中位数。
6. 关闭条件（三个都满足）：
   - 原题中位数 ≥ 6；
   - `gold_docs` 召回 ≥ 0.8（有 gold 时）；
   - 引用校验通过。
7. 每日 09:30 定时执行；一次最多 3 个主题，防止批量污染。

验收：

- 从历史缺口聚出 2~3 个主题；
- 至少关闭 1 个 gap（open → closed），或明确标记 `wontfix` 并写明原因；
- 关闭过程中生产 benchmark 无回退。

### Phase 2：离线策略进化（1~1.5 周）

**目标：让 L1/L2 的变更从“单题拍脑袋”变成“候选池 + 统计门禁”。**

任务：

1. **候选空间**
   - L1：`SIMILARITY_THRESHOLD`、`TOP_K`、`MAX_SOURCES`、`MAX_CONTEXT_CHUNKS`、BM25/向量融合权重、chunk size（离线）。
   - L2：分域 prompt 规则（`scope=intent|domain`），不再用一个全局 `prompt_rule`。
2. **规则库改造**：`overrides.json` 从 `prompt_rule: str` 升级为：
   ```json
   {
     "rules": [
       {"id": "ambiguity-001", "scope": "ambiguity", "text": "...", "version": 1, "status": "active"}
     ],
     "params": {}
   }
   ```
   同时保留只读兼容层，最多组合 3 条规则；每条规则独立审计、独立回滚、独立归因。
3. **评测器**：`evaluate.py`
   - 全量 benchmark + hard set + holdout；
   - 每题 3 次采样取中位数；
   - paired bootstrap 2000 次，输出均值差与 95% CI；
   - 成本、P95 延迟一起记录。
4. **门禁**：`gate.py`
   - 主指标提升 ≥ `min_improvement`；
   - CI 下界 > `-max_regression`；
   - 任一 tier 回退不超过阈值；
   - 成本/延迟不超预算。
5. **夜间进化任务**（02:00）：
   - 从失败聚类生成 2~4 个候选；
   - 每个候选跑离线评测；
   - 通过者进入 shadow，不直接改 8000；
   - 结果写账本 + 次日审批清单。
6. **元学习**：按 `failure_type × action` 统计成功率，自动降低长期无效动作的采样概率（例如连续 10 次 `prompt_rule` 对 `knowledge_gap` 无效 → 禁用该组合）。

验收：

- 至少 1 个候选通过统计门禁并进入 shadow；
- 连续 7 天无自动回滚；
- 账本能回答“哪个动作对哪类失败有效”。

### Phase 3：安全发布（1 周）

**目标：把“重启即上线”改成“版本化 + 灰度 + 自动回滚”。**

任务：

1. 版本化所有进化产物：`versions/<version>/` 包含 overrides、规则、索引 manifest、config。
2. `promote.py` 状态机：
   ```text
   candidate → shadow(8001) → canary 5% → canary 25% → 100%
   ```
   本地可用按 `question_hash` 分流；有反向代理时按请求比例分流。
3. 自动回滚指标：
   - 固定探针题中位数下降 > 1；
   - P95 延迟上升 > 30%；
   - 5xx / 超时率 > 1%；
   - 引用非法率 > 5%；
   - 零命中率上升 > 10%。
4. 每次晋级前检查 soak 时间（默认 24h）和最小样本量。
5. 新增 `GET /evolution/status`、`GET /evolution/experiments`（只读）。

验收：

- 做一次灰度回滚演练：5% 阶段故意注入退化候选，系统在 5 分钟内自动回滚；
- 生产 8000 无感，用户请求不中断；
- 所有变更可在账本中按版本追溯。

### Phase 4：工作流与代码进化（持续，需前置门禁）

**目标：在评测稳定后，才让系统进化“流程”和“代码”。**

任务：

1. 按 `docs/agent-design.md` 落地研究员 Agent P1~P3：四状态机、证据账本、coverage/gaps、SSE。先只读，不写库。
2. 管家 Agent 消费 gaps，走 Phase 1 的审批与 ingest 流程。
3. 代码进化流程：
   ```text
   失败聚类 → 生成 issue
     → git worktree 创建沙箱
     → coding agent 改代码 + 补测试
     → make test / make arch / make gate
     → 人工 review
     → 合并 → 灰度
   ```
   **禁止**直接改 `server.py` 或自动重启生产。
4. 每周把生产失败题脱敏后加入 hard set；每月评审一次 benchmark 是否过拟合。

验收：

- 多跳组证据召回 ≥ baseline + 0.15；
- 单跳组不下降超过 0.3；
- 代码变更 100% 经 PR + 测试；
- 月度 benchmark 净提升 > 0，且成本不升。

---

## 5. 调度与预算

| 时间 | 任务 | 是否允许改生产 |
|---|---|---|
| 实时 | 采集低分/负反馈/零命中，写入 incident | 否 |
| 每 10 分钟 | daemon 消费队列，生成候选到 shadow | 否 |
| 每日 09:00 | `daily_patrol` 低分扫描 + 失败清单 | 否 |
| 每日 09:30 | `curator` 缺口聚类 + 需求单 | 否（需审批） |
| 每日 02:00 | 全量 benchmark + 离线进化 + 夜间 guard | 仅 canary/自动参数 |
| 每周一 03:00 | holdout 复测 + benchmark 刷新 + 规则淘汰 | 需审批 |
| 每月 | 架构/代码进化评审 | 人工 |

预算建议：

- 每日最多 4 个实验、8 个 incident；
- 单实验最多 2 个变量、3 条规则、30 分钟；
- LLM 成本上限 $5/天；
- 任一日触发回滚 ≥ 2 次 → 自动降级为 shadow-only，等人工复盘。

macOS 常驻可用 `launchd`；已有 `./scripts/service.sh start` 会拉起优化 daemon。定时任务建议新增 `scripts/evolve_daily.sh` / `scripts/evolve_weekly.sh`，由 `launchd` 调用，日志写 `logs/evolution.log`。

---

## 6. 安全边界（必须遵守）

1. **默认 shadow**：没有通过门禁的候选永远不进 8000。
2. **生产隔离**：优化器只允许操作 8001/8002；8000 只接受 promote。
3. **知识写入白名单**：只允许 `raw/inbox/` 和 `wiki/gaps/`；正文必须走 ingest，不覆盖既有页面。
4. **代码不热改**：worktree + PR + 测试 + 人工合并。
5. **不可变审计**：每次变更记录 who/what/why/version/rollback point。
6. **Kill switch**：`auto_optimization/PAUSED` 或 `evolution.enabled=false` 立即停止所有自动动作。
7. **故障半径**：一次只动一个变量；一次最多 3 个知识主题；canary 失败自动回滚。
8. **人工闸门**：知识写入、代码合并、跨域 prompt 规则必须人工批准。

---

## 7. 指标看板

北极星：

- benchmark 平均分；
- hard set 通过率；
- gold evidence recall；
- 用户负反馈率。

护栏：

- 单题/分域回退数；
- 自动回滚率；
- P95 延迟；
- 单请求成本；
- 引用非法率；
- 零命中率；
- 索引陈旧文档数；
- gap 关闭率；
- 实验采纳率。

建议目标（上线 2 周后）：

| 指标 | 目标 |
|---|---|
| benchmark 平均分 | 相对当前 +0.5 |
| 单题回退 > 1 的比例 | < 5% |
| 自动回滚率 | < 10% |
| gap 关闭数 | ≥ 3/月 |
| P95 延迟 | 不高于当前 +20% |
| 引用非法率 | < 5% |

---

## 8. 里程碑

| 周 | 交付 | 验收 |
|---|---|---|
| W1 | Phase 0 + Phase 1 dry-run | 尺子稳定；首份缺口报告 |
| W2 | 知识闭环 + 第一个 gap 关闭 | 原题复测通过、无回退 |
| W3 | Phase 2 评测器 + 规则库 | 1 个候选进 shadow |
| W4 | 门禁 + 夜间进化 | 7 天无自动回滚 |
| W5 | Phase 3 灰度 + 回滚演练 | 5 分钟回滚 |
| W6+ | Research Agent / 代码进化 | 多跳提升、PR 流程跑通 |

---

## 9. 本周立即可做的 6 件事

1. 把 `evolution.mode` 设为 `shadow`，停止自动同步。
2. 修 `daily_benchmark_guard.py` 的回滚选择逻辑，并补一个“无备份不静默”的测试。
3. 统一评分中位数：`score_answer_median(question, answer, samples=3)`，替换回归/guard/验证里的单次调用。
4. 处理 `TOP_K`：接线或删除，二选一。
5. 建 `optimization/evolution/` 骨架 + `experiments.jsonl`，先把现有 9 条历史补成实验记录。
6. 跑一次 `curator --report`，把 5 条 `knowledge_gap` 聚成需求单。

对应命令：

```bash
make test
make arch
.venv/bin/python -m optimization.agent.daily_benchmark_guard --check
.venv/bin/python scripts/eval_agent.py --verify-gold
.venv/bin/python -m optimization.evolution.curator --report
```

---

## 10. 需要拍板的 3 个决策

| 决策 | 建议 | 理由 |
|---|---|---|
| 自动上线范围 | 先只允许低风险参数；prompt 规则先 shadow/人工批准 | 规则是全局的，误伤面大 |
| 知识写入 | 必须人工批准；只写 `raw/inbox/` + `wiki/gaps/` | 防止低质内容污染知识库 |
| 代码进化 | 只允许 worktree + PR，不允许热改 | 可测试、可 review、可回滚 |

---

## 11. 风险

| 风险 | 对策 |
|---|---|
| judge 噪声导致误判 | 3 次中位数 + paired bootstrap + 固定 holdout |
| 过拟合 benchmark | 固定 holdout、每月换题、hard case mining |
| 全局规则互相打架 | 分域规则 + 最多 3 条 + 逐条 A/B |
| 知识库被批量污染 | dry-run、人工批准、一次 3 主题、provenance |
| 自动回滚失效 | 回滚演练 + 备份存在性检查 + 版本 manifest |
| 成本失控 | 日预算、实验数上限、单实验 token/时间上限 |
| 代码自改引入隐蔽 bug | 测试门禁 + 架构门禁 + PR review + canary |

---

## 12. 一句话总结

**先让系统“看得准”（评测），再让系统“补得上”（知识），最后才让系统“改得动”（策略/代码）。**  
当前最值得做的不是更强的 Agent，而是把 5 个知识缺口变成 5 个可关闭的 gap，把 8~9 分的“假回退”从尺子里去掉。
