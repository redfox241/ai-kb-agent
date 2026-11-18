# 知识库 Agent 技术设计（v0.1）

> 状态：草案 ｜ 日期：2026-11-18 ｜ 目标：在现有确定性 RAG 服务之上，增加**研究员 Agent**与**知识库管家 Agent**
> 配套：`docs/multi-turn-design.md`、`docs/agent-framework-comparison.html`、`README.md`
> 评测集：`auto_optimization/benchmark/agent_set.json` ｜ 评测脚本：`scripts/eval_agent.py`

---

## 0. TL;DR

1. **主链路不动。** `/ask`、`/ask_smart` 保持默认；Agent 是新入口，走独立路由。
2. **研究员 Agent = 有界状态机**：`PLAN → GATHER → CHECK → ANSWER`，最多 8 次工具调用、2 次计划修订，证据不足就 `ABSTAIN`。
3. **管家 Agent = 定时任务 + 人工审批**，不是聊天机器人；产出写入 vault 既有 schema（`wiki/gaps/`、`raw/inbox/`），而不是发明新目录。
4. **两个 Agent 的接口是 `coverage` + `gaps`**：研究员产出结构化缺口，管家消费缺口。这是飞轮，不是两个孤立功能。
5. **先修评测尺子，再写 runtime**。当前 LLM judge 单次打分 + 无 gold evidence，会把“常识答对但知识库没有”判成 10 分（实测：平型关大捷无独立条目，baseline 仍是 10.0）。
6. **不上 LangGraph、不做多 Agent、不做通用助理。**

---

## 1. 定位与非目标

### 1.1 定位

| 层 | 能力 | 入口 |
|---|---|---|
| L1 查得到 | 单跳事实问答（现状） | `POST /ask_smart` |
| L2 查得全 | 多跳、对比、聚合、核验、报告 | `POST /agent/run` |
| L3 补得上 | 缺口发现 → 人工批准 → 入库 → 复测 | `python -m agent.curator`（定时） |

### 1.2 非目标

- 不做通用个人助理、多渠道接入、插件市场。
- 不做多 Agent 角色编排（planner/executor/critic）。
- 不让模型直接执行 shell、任意写文件、访问网络（除非显式开启且白名单）。
- 不替代现有 RAG；简单问题必须继续走 `ask_smart`。
- 不在评测修好之前扩大自优化动作空间。

---

## 2. 为什么是这两个 Agent

来自本项目真实运行数据（`auto_optimization/logs/optimization_history.jsonl`，9 次触发）：

| 结果 | 次数 | 说明 |
|---|---|---|
| `knowledge_gap` | 5 | 系统自己判定“知识库没有” |
| `regression_failed` | 3 | 单题提分，但回归失败或 judge 抖动 |
| `give_up` | 1 | — |
| `success` | **0** | 没有一次同步上线 |

结论：**瓶颈已从“检索质量”转向“知识覆盖 + 证据组织”**。

- 继续调 prompt / 阈值 → 收益趋零（0/9 已经证明）。
- 多跳与聚合问题 → 单次检索生成结构性做不到（见 `agent_set.json` 的 `multi_hop` 组）。
- 缺口 → 已经有结构化诊断文本，但没人执行（`wiki/gaps/` 只有 2 页，而失败日志里已有 5 个缺口）。

---

## 3. 总体架构

```
                    ┌──────────────────────────────┐
   /ask_smart ─────▶│ 现有 RagPipeline（不动）      │
                    └──────────────────────────────┘
                              ▲
                    ┌─────────┴─────────┐
   /agent/run ─────▶│ Router（单跳/多跳）│
                    └─────────┬─────────┘
                              ▼
                    ┌──────────────────────────────┐
                    │ Research Agent Runtime        │
                    │  PLAN → GATHER → CHECK → ANSWER│
                    │  ToolRegistry / EvidenceLedger │
                    └───────┬───────────────┬───────┘
                            │               │
                   ┌────────▼──────┐  ┌─────▼─────────┐
                   │ KB Tools      │  │ Guardrails     │
                   │ search/read/  │  │ 沙箱/预算/契约  │
                   │ list/expand   │  └───────────────┘
                   └───────────────┘
                            │
                    ┌───────▼──────────────────────┐
                    │ coverage + gaps（共享 schema）│
                    └───────┬──────────────────────┘
                            ▼
                    ┌──────────────────────────────┐
                    │ Curator（定时 + 人工审批）     │
                    │ 聚类 → 需求单 → 起草 → 入库    │
                    └──────────────────────────────┘
```

---

## 4. 路由：不是所有问题都走 Agent

### 4.1 规则优先

```python
# agent/router.py（示意）
def route(question: str, *, force_agent: bool = False) -> str:
    if force_agent:
        return "agent"
    q = question.strip()
    if len(q) <= 15 and not _has_multi_hop_marker(q):
        return "rag"
    if any(k in q for k in ("对比", "区别", "差异", "时间线", "归纳", "整理成", "报告", "哪些", "分别")):
        return "agent"
    return "rag"   # 默认走便宜的路径
```

### 4.2 产品入口

- Web 聊天页：默认 RAG；提供「深度研究」开关（`force_agent=True`）。
- API：`/agent/run` 显式调用，不隐式升级。
- 任何时候都允许用户手动切换，并把当前路由结果写进 trace。

### 4.3 预期分流

| 问题类型 | 路由 | 依据 |
|---|---|---|
| “平型关大捷发生在哪一年？” | RAG | 单跳 |
| “长征和百团大战相隔几年？” | Agent | 跨文档计算 |
| “我笔记里有哪些 RAG 评估方法？” | Agent | 聚合 |
| “知识库有没有长征省份数？” | Agent | 核验 + 可能拒答 |

---

## 5. 研究员 Agent

### 5.1 状态机

```
        ┌────────┐
        │  PLAN  │ 产出 2~5 个子问题 + 检索策略
        └───┬────┘
            ▼
        ┌────────┐   tool_call    ┌──────────────┐
        │ GATHER │◀──────────────▶│ ToolRegistry │
        └───┬────┘   observation  └──────────────┘
            ▼
        ┌────────┐  不足且修订<2
        │ CHECK  │──────────────▶ 回 PLAN
        └───┬────┘
            │ 足够 / 预算耗尽
            ▼
        ┌────────┐
        │ ANSWER │──▶ 证据充足 → 带引用作答
        └────────┘──▶ 证据不足 → ABSTAIN + gaps
```

**为什么不用自由 ReAct**：自由循环会产生不可预测的工具调用序列，难以评测、难以回放、容易烧 token。四状态显式机足够覆盖研究型问答，且每一步都能写进 trace。

### 5.2 工具契约（只读）

| 名称 | 参数 | 返回 | 权限 | 超时 |
|---|---|---|---|---|
| `search_kb` | `query: str, top_k: int = 8, filters: {type?, path_prefix?}` | `[{chunk_id, doc_path, title, snippet, score}]` | 只读 | 3s |
| `read_doc` | `path: str, section?: str, max_chars: int = 6000` | `{title, frontmatter, text, truncated}` | 只读，沙箱 | 1s |
| `list_docs` | `prefix?: str, tag?: str, limit: int = 50` | `[{path, title, type, updated_at}]` | 只读 | 1s |
| `expand` | `chunk_id: str` | `{parent_text, neighbors[]}` | 只读 | 1s |

**设计要点**

- `search_kb` 复用 `RetrievalService.search()`，不重写检索。
- `read_doc` 是 Agent 与 RAG 的关键差异：允许读全文/章节，而不是只吃 Top-K 片段。
- `list_docs` 解决“先知道有什么，再决定读什么”，降低无效检索。
- 所有工具返回结构化数据 + 一份紧凑文本渲染；模型只看到文本渲染，结构化数据进证据账本。
- 工具注册表带 `schema / permission / timeout / idempotent` 四个字段（不做 import 副作用注册）。

### 5.3 证据账本（Evidence Ledger）

```python
@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str      # E1..En，全局唯一
    chunk_id: str
    doc_path: str
    title: str
    quote: str            # 支撑结论的最小片段
    source_tool: str      # search_kb / read_doc / expand
```

规则：

1. 按 `chunk_id` 去重；同一块被多次命中只登记一次。
2. `read_doc` 的整段内容登记为一条证据（quote 截断到 500 字）。
3. 最终答案只能引用账本里的 `evidence_id`；`validate_citations()` 复用现有实现。
4. 账本与答案一起持久化，支持“点引用跳原文”。

### 5.4 输出契约

```json
{
  "answer": "……[E3][E7]……",
  "citations": {"ok": true, "invalid": [], "missing": 0},
  "coverage": [
    {"subquestion": "长征起止时间", "answered": true,  "evidence": ["E1"]},
    {"subquestion": "途经省份数量", "answered": false, "evidence": []}
  ],
  "gaps": [
    {"topic": "长征途经省份", "reason": "知识库仅有里程，无省份数", "priority": "medium"}
  ],
  "confidence": "high|medium|low",
  "usage": {"steps": 4, "tool_calls": 5, "prompt_tokens": 18234, "latency_ms": 21870}
}
```

`coverage` 与 `gaps` 是**对外契约**，不是调试字段。管家 Agent 直接消费它们。

### 5.5 预算与终止

| 约束 | 默认值 | 触发行为 |
|---|---|---|
| 工具调用 | 8 | 停止收集，进入 CHECK |
| 计划修订 | 2 | 强制 ANSWER/ABSTAIN |
| prompt tokens | 60k | 截断历史观察，保留证据摘要 |
| 墙钟 | 90s | 返回“已知部分 + 缺口” |
| 连续无新证据 | 2 步 | 提前停止，避免空转 |

### 5.6 失败与降级

| 情况 | 行为 |
|---|---|
| 索引未就绪 | 返回 503，不启动 Agent |
| 某工具超时 | 记录 observation，继续；超过 2 次则该路禁用 |
| LLM 计划解析失败 | 回退到“单子问题 + search_kb”最小计划 |
| 最终引用校验失败 | 移除非法引用并降 confidence；仍失败则 ABSTAIN |
| 总预算耗尽 | 输出已收集证据 + 明确 gaps |

### 5.7 Prompt 资产

```
agent/prompts/
├── plan.md       # 子问题拆解
├── check.md      # 证据充分性判定
├── answer.md     # 带引用作答
└── abstain.md    # 拒答 + 缺口描述
```

沿用 `qa/prompts/*.md` 的约定：frontmatter 带 `version`，严格变量渲染，版本号进 trace。

---

## 6. 管家 Agent

### 6.1 关键判断：它是任务，不是聊天

管家面向的是“每天/每周跑一次”的批处理，输入是缺口集合，输出是待审批的变更。**不要做成对话式 Agent**，否则会把写权限暴露在交互链路里，风险和复杂度都陡增。

### 6.2 输入来源（按可信度排序）

| 来源 | 位置 | 用途 |
|---|---|---|
| Agent 运行缺口 | `agent_runs.gaps_json` | 一手、结构化、带 coverage |
| 自优化失败记录 | `auto_optimization/logs/optimization_failures.jsonl` | 已有 5 条 knowledge_gap，含高质量诊断 |
| 低分评估 | `var/db/analytics.db` → `eval_records` | 广度补充 |
| 现有 Gap 页 | `wiki/gaps/*.md` | 人工已确认的开放问题 |

### 6.3 缺口聚类与排序

```
缺口文本 → jieba 关键词（复用 trigger._extract_keywords）
        → embedding 相似度（复用 nomic-embed-text）
        → 按主题聚类（阈值 0.75）
        → 排序 = 频次 × 用户价值 × 可补性
```

输出「知识需求单」：

```json
{
  "topic": "长征途经省份",
  "occurrences": 3,
  "questions": ["长征经过了几个省？"],
  "why_missing": "现有长征页只有里程与会议，无省份维度",
  "suggested_action": "补充党史/军史资料，或标注为不覆盖",
  "priority": "medium"
}
```

### 6.4 与 vault schema 对接（重要）

vault 已有九类知识对象规范（`schema/CLAUDE.md`），**不要另造目录**：

| 产出 | 写入位置 | 必填小节 |
|---|---|---|
| 开放问题 | `wiki/gaps/<主题>.md` | 已知的不知道 / 为何重要 / 现有线索 / 下一步 / 状态 |
| 原始资料 | `raw/inbox/<文件>` | 只读归档，等待 ingest |
| 单篇摘要 | `wiki/sources/<名>.md` | 核心观点 / 关键术语 / 能力边界 / 原文定位 |
| 问答沉淀 | `wiki/questions/<名>.md` | 问题 / 答案 / 依据 / 相关问题 |
| 导航更新 | `wiki/index.md`、`wiki/log.md` | 追加，不覆盖 |

流程：

```
需求单（dry-run）
  → 人工批准
  → 资料写入 raw/inbox/（或由用户提供 URL/PDF）
  → 复用「知识库整理技能」/ extract_source.py 做 ingest
  → 生成 wiki/sources + 派生 concepts/entities
  → 更新 index.md / log.md / checkpoint
  → 重建索引
  → 重跑原失败问题，比较分数与 gold 证据
  → 提升则把 gap 状态置 closed
```

### 6.5 安全边界

| 规则 | 说明 |
|---|---|
| 默认 dry-run | 只生成需求单，不写任何文件 |
| 写入限于 `raw/inbox/` 与 `wiki/gaps/` | 永不覆盖既有页面 |
| `wiki/` 变更必须走 ingest 流程 | 不直接批量生成低质摘要 |
| 变更前备份 + 审计 | 复用 `OverridesStore` 的备份/审计/回滚模式 |
| 所有写入带 provenance | `source`、`retrieved_at`、`confidence` |
| 一次最多 3 个主题 | 防止批量污染 |

### 6.6 缺口关闭判定

不是“写完就算关闭”，而是：

```
原始失败问题重跑 → 分数 ≥ 阈值
且 gold_docs 召回 ≥ 0.8
且 引用校验通过
→ gap.status = closed
```

否则保持 `partial`，并记录原因。

---

## 7. 工程落位

### 7.1 目录

```
agent/                       # 新增：application 层
├── runtime.py               # 有界循环 + 预算 + 终止
├── state.py                 # AgentState / 状态转移
├── router.py                # 单跳 vs 多跳
├── ledger.py                # EvidenceLedger
├── guardrails.py            # 沙箱 / 成本 / 输出契约
├── curator.py               # 缺口聚类 + 需求单 + 审批流
├── tools/
│   ├── registry.py          # schema / permission / timeout
│   └── kb.py                # search / read / list / expand
└── prompts/                 # plan / check / answer / abstain
```

依赖约束（与 `tests/test_architecture.py` 一致）：

- `agent` 加入 `APPLICATION_DIRS`，只允许依赖 `domain` 与其他 application 包。
- **禁止 `from infra import ...`**；文件读取、索引、持久化都通过端口注入。
- `bootstrap.py` 是唯一装配点；`config/profiles.py` 的 `flags.agent` 控制启用（当前已定义但未使用）。

### 7.2 端口扩展（`domain/ports.py`）

```python
class DocumentReader(Protocol):
    def read(self, path: str, *, section: str | None = None, max_chars: int = 6000) -> dict: ...
    def list(self, *, prefix: str = "", tag: str = "", limit: int = 50) -> list[dict]: ...

class AgentRunStore(Protocol):
    def create(self, run: "AgentRun") -> None: ...
    def append_event(self, run_id: str, event: "AgentEvent") -> None: ...
    def finish(self, run_id: str, result: dict) -> None: ...
    def get(self, run_id: str) -> dict | None: ...
```

实现放在 `infra/vault_reader.py`、`infra/sqlite_agent.py`。

### 7.3 数据模型（SQLite）

```sql
CREATE TABLE agent_runs (
    run_id           TEXT PRIMARY KEY,
    session_id       TEXT,
    goal             TEXT NOT NULL,
    route            TEXT NOT NULL,          -- rag | agent
    status           TEXT NOT NULL,          -- running | done | abstain | failed | cancelled
    plan_json        TEXT,
    coverage_json    TEXT,
    gaps_json        TEXT,
    evidence_json    TEXT,
    answer           TEXT,
    steps            INTEGER,
    tool_calls       INTEGER,
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,
    latency_ms       INTEGER,
    model            TEXT,
    created_at       TEXT NOT NULL,
    finished_at      TEXT
);

CREATE TABLE agent_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- plan | tool_call | observation | check | token | done
    payload_json TEXT
);

CREATE INDEX idx_agent_runs_created ON agent_runs(created_at);
CREATE INDEX idx_agent_events_run ON agent_events(run_id, seq);
```

### 7.4 API / SSE

```
POST /agent/run                 # SSE 流式
GET  /agent/runs?limit=20       # 运行列表
GET  /agent/runs/{run_id}       # 回放
POST /agent/runs/{run_id}/cancel
```

SSE 事件：

```
event: plan          data: {"subquestions": [...]}
event: tool_call     data: {"name": "search_kb", "args": {...}}
event: observation   data: {"tool": "search_kb", "summary": "...", "n_hits": 8}
event: check         data: {"sufficient": false, "missing": ["省份数量"]}
event: token         data: {"text": "根据"}
event: done          data: {"answer": "...", "citations": {...}, "coverage": [...], "gaps": [...], "usage": {...}}
```

事件形状直接复用 `TraceEvent`，前端可沿用现有 trace 渲染。

### 7.5 与现有模块的复用表

| 能力 | 直接复用 | 说明 |
|---|---|---|
| 混合检索 | `retrieval/service.py` | `search_kb` 包装 |
| 父子扩展 | `retrieval/expansion.py` | `expand` 工具 |
| 引用校验 | `qa/citation.py` | 答案后处理 |
| 上下文预算 | `qa/context.py` | 截断与历史裁剪 |
| 轨迹模型 | `qa/trace.py` | SSE + 回放 |
| 会话/实体 | `infra/sqlite_sessions.py` | Agent 运行挂到 session |
| 评分 | `optimization/utils/scorer.py` | 评测复用，但需多次采样 |
| 缺口队列 | `optimization/agent/trigger.py` | 聚类/抑制/限流思路 |
| 变更审计 | `config/overrides.py` | Curator 写入审计照抄 |

---

## 8. 评测

### 8.1 评测集

`auto_optimization/benchmark/agent_set.json`，27 题，六类：

| 类型 | 数量 | 目的 |
|---|---|---|
| multi_hop | 6 | 跨文档串联 |
| comparison | 5 | 分别取证再对齐 |
| aggregation | 5 | 遍历同类页面归纳 |
| verification | 4 | 覆盖核验、冲突检测 |
| report | 4 | 结构化长输出 |
| abstention | 3 | 必须拒答/标注缺口 |

每题带 `gold_docs`（vault 相对路径），用于**证据召回**，而不是只看答案分数。

### 8.2 指标

| 指标 | 定义 | 目标 |
|---|---|---|
| Evidence Recall | 命中的 `gold_docs` / `gold_docs` 总数 | ≥ 0.80 |
| Full Recall Rate | 全部 gold 文档都被命中的题占比 | ≥ 0.60 |
| Citation Precision | 引用块真实存在于账本且支撑结论 | ≥ 0.95 |
| Abstention Accuracy | 该拒答时拒答、不该拒答时不拒答 | ≥ 0.90 |
| Constraint Accuracy | `expect_contains` 命中 | ≥ 0.80 |
| Avg Steps / Tokens | 预算遵守 | ≤ 8 / ≤ 60k |
| P90 Latency | 尾延迟 | ≤ 90s |
| **Δ vs RAG** | 同一题走 `/ask_smart` 的对比 | 多跳组显著为正，单跳组不为负 |

### 8.3 配对实验

`scripts/eval_agent.py` 支持：

```bash
# 1) 校验评测集的 gold_docs 是否都在索引里
.venv/bin/python scripts/eval_agent.py --verify-gold

# 2) 只跑基线（当前就能用）
.venv/bin/python scripts/eval_agent.py --mode baseline \
    --base http://127.0.0.1:8002 --out eval/agent_baseline.json

# 3) Agent 上线后跑实验组
.venv/bin/python scripts/eval_agent.py --mode agent \
    --base http://127.0.0.1:8002 --out eval/agent_v1.json

# 4) 配对对比
.venv/bin/python scripts/eval_agent.py --compare \
    eval/agent_baseline.json eval/agent_v1.json
```

### 8.4 发布门禁

只有同时满足才允许 `flags.agent = True` 上线：

1. 多跳组 Evidence Recall ≥ baseline + 0.15；
2. 单跳组（从 `benchmark_set.json` 抽 10 题）分数不下降超过 0.3；
3. Abstention Accuracy ≥ 0.9；
4. `make test` + `make arch` 全绿；
5. 人工抽检 10 条引用，全部可跳转到原文。

### 8.5 冒烟结果（2026-11-18，8002 基线）

先跑 3 道代表题验证脚手架可用：

| 题 | 类型 | Evidence Recall | 是否拒答 | 观察 |
|---|---|---|---|---|
| `mh-02` 长征会议距百团大战几年 | multi_hop | **0.50** | 否 | 答案算对了“5 年”，但只召回 `长征.md`，`百团大战.md` 未进 sources |
| `vf-02` 平型关大捷是否有独立条目 | verification | 1.00 | 否 | 正确回答“没有独立条目、无法支撑年份”，并引用 `wiki/log.md` |
| `ab-01` 长征经过几个省 | abstention | — | ✅ | 正确拒答，并说明知识库只有里程 |

**读法**：单跳与核验已经可用；多跳的失败点不是答案，而是**证据召回不全**。这正是 Agent 要解决的问题，也说明“只看 LLM 分数”会漏掉它——`mh-02` 的答案看起来完全正确。

### 8.6 索引新鲜度（P0 必须先处理）

`--verify-gold` 实测：

- 索引：1,937 篇（`var/index`，2026-11-17 构建）
- 磁盘：1,882 篇 Markdown
- **索引陈旧 93 篇**（磁盘已删/改名，索引仍在）
- **应索引未收录 10 篇**（含 `wiki/decisions/知识库类型归并规则.md`）
- 8002 进程内索引更旧：27,249 chunks @ 2026-11-11（当前快照 27,898）

在这些数字修好之前，任何 gold 召回都会被陈旧快照污染。P0 的第一条操作必须是**重建索引并复跑 `--verify-gold`**。

---

## 9. 分期计划

| 阶段 | 时间 | 交付 | 验收 |
|---|---|---|---|
| P0 修尺子 | 3~5 天 | `agent_set.json` + `eval_agent.py` + 多次采样评分 | `--verify-gold` 全绿；基线可复现 |
| P1 只读研究员 | 2 周 | 4 工具、4 状态、SSE、trace UI、预算 | 27 题跑通；多跳组优于 RAG |
| P2 证据与拒答 | 1 周 | 账本、coverage、gaps、引用聚合 | 引用精度 ≥ 0.95；拒答 ≥ 0.9 |
| P3 管家 dry-run | 1 周 | 缺口聚类、需求单、审批列表 | 能从历史缺口聚出 2~3 个主题 |
| P4 人工批准闭环 | 1 周 | 审批 UI、`raw/inbox` 写入、ingest、复测 | 至少 1 个 gap 从 open → closed |
| P5 可选 | — | 联网、定时简报、Agent 自优化 | 前四阶段指标稳定后再做 |

**P0 完成前不写 runtime。** 否则无法回答“Agent 到底比 RAG 好在哪里”。

---

## 10. 风险与前置修复

| 风险 | 证据 | 对策 |
|---|---|---|
| judge 噪声导致误判 | 同一题 10.0 → 8.0 的“回归” | 多次采样取中位数；引入 gold evidence |
| 常识答对被当成知识库答对 | 平型关大捷无独立条目，baseline 10.0 | 增加 grounding 指标；无 gold 文档时按拒答题评分 |
| 索引污染 | `.agents/skills/**` 被索引（`EXCLUDE_DIRS` 缺 `.agents`） | 补排除；重建索引后复测 |
| 多轮改写脆弱 | `docs/multiturn-incident-20261117.md` | Agent 只做指代消解，不复用整段历史拼接 |
| 成本/延迟 | Agent 一次 3~8 次 LLM 调用 | 路由 + 工具缓存 + 硬预算 |
| 写权限滥用 | 管家要写 vault | dry-run 默认、限 3 主题、审批、审计 |
| 过度工程 | 易上 LangGraph/多 Agent | 四状态 + SQLite，单进程足够 |

### 10.1 立即可做的四个修复

1. **重建索引并复核 gold**：当前索引陈旧 93 篇、漏收 10 篇；先 `POST /reindex`，再跑 `make agent-gold`，确保基线可复现。
2. **`TOP_K` 死参数**：在 `ALLOWED_PARAMS` 里但 `bootstrap.py` 从未读取。要么接线，要么移除，避免优化器做无效变更。
3. **索引排除 `.agents/`**：`bootstrap.EXCLUDE_DIRS` / `ingestion/vault.py` 补充 `.agents`；同时确认 `wiki/log.md` 这类日志页是否需要参与检索。
4. **评分多次采样**：`scorer.score_answer_multi()` 增加 3 次调用取中位数，或在回归路径上做 2 次。

---

## 11. 决策记录（ADR）

| 决策 | 选择 | 理由 | 放弃的方案 |
|---|---|---|---|
| 编排方式 | 四状态显式机 | 可评测、可回放、成本可控 | 自由 ReAct、LangGraph 图 |
| Agent 数量 | 单 Agent + 工具 | 调试成本低 | planner/executor/critic 三角色 |
| 管家形态 | 定时任务 + 审批 | 写权限不进交互链路 | 对话式写库 Agent |
| 知识写入 | 走 vault 既有 schema | 与九类对象、Lint、dashboard 兼容 | 新建 `vault/inbox/` 平行体系 |
| 工具权限 | 只读默认，写操作白名单 + HITL | 安全边界清晰 | 给模型 shell/写权限 |
| 评测 | 配对 + gold evidence + 多次采样 | 避免 judge 噪声和“常识蒙对” | 只看 LLM 分数 |
| 依赖 | 不引入框架 | 现有四层门禁已足够 | LangChain / LangGraph |

---

## 12. 附录：最小原型骨架（P1 目标形态）

```python
# agent/runtime.py（示意，不是最终实现）
def run(goal: str, ctx: AgentContext) -> AgentResult:
    state = AgentState.new(goal=goal, budget=ctx.budget)
    state = plan(state, ctx)                      # 2~5 个子问题

    while state.status == "gather":
        if state.tool_calls >= ctx.budget.max_tool_calls:
            state.status = "check"; break
        action = ctx.llm.next_action(state)       # 结构化输出：tool + args
        if action.kind == "answer":
            state.status = "check"; break
        obs = ctx.tools.invoke(action.tool, action.args)
        state.observe(obs)
        state = check(state, ctx)                 # 证据足够？
        if state.status == "answer" and state.revisions < ctx.budget.max_revisions:
            state = plan(state, ctx, revision=True)

    return answer_or_abstain(state, ctx)          # 引用校验 + coverage + gaps
```

真正的约束不在循环代码，而在四个数字：**最大工具调用、最大修订、token 预算、墙钟预算**。没有这四个数，Agent 不可运维。
