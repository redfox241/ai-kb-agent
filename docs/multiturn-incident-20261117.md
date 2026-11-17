# 多轮会话退化故障诊断与整改方案

> 版本：v1.0 ｜ 日期：2026-11-17 ｜ 状态：P0 已实施；查询改写已切换本地 Qwen（2026-11-17） ｜ 关联会话：`ses_cd9927540497`（“长征”）
> 关联设计：[`docs/multi-turn-design.md`](./multi-turn-design.md)
> 适用代码：`qa/rewrite.py` / `qa/multiturn.py` / `conversations/summarizer.py` / `infra/llm.py` / `retrieval/`

---

## 0. 结论摘要（TL;DR）

“长征”会话表面上是“多轮回答把历史问题又答了一遍”，实际是三个确定性故障叠加，导致多轮链路退化为 **历史 `resolved_query` 的累积字符串拼接**：

1. **改写调用 100% 失败**：`qa/rewrite.py` 调用 `llm.chat_json(..., temperature=0.1)`，但 `LLMClient.chat_json()` 没有 `temperature` 参数，抛 `TypeError`；异常被 `except Exception: pass` 静默吞掉。
2. **失败回退是拼接，不是原文**：`_rule_resolve()` 返回“上一轮 `resolved_query` + 当前问题”。上一轮本身又包含再上一轮，于是 `resolved_query` 从 2 字累积到 42 字，T7 实际是 5 个历史问题的拼接。
3. **滚动摘要 100% 空输出**：摘要调用 `max_tokens=600`，带 thinking 的模型把 600 token 全部用于 thinking，`text_len=0`；异常同样被吞掉，`summary_upto_turn` 永远是 0。

结果：**改写不工作、摘要不工作、实体槽为空、话题切换被当成追问**，旧话题只能靠不断变长的 `resolved_query` 传递。检索和生成自然把 5 个话题一起答了。

修复优先级：

| 优先级 | 动作 | 目标 |
|---|---|---|
| P0 | 修 `chat_json` 签名；失败回退原文/单轮；摘要关闭 thinking + 重试 + 日志 | 立即停止拼接，恢复改写与摘要 |
| P1 | 话题边界、实体重置、检索隔离、上下文隔离、辅助快模型 | 解决跨话题污染与延迟 |
| P2 | trace/usage/指标/测试/评测集 | 防止同类问题再次静默发生 |

---

## 1. 背景与影响

### 1.1 现象

“长征”会话共 7 轮：

```text
T1 长征
T2 著名的事件
T3 抗日战争开始时间和结束时间
T4 主要领导人
T5 著名战役
T6 常用的武器型号
T7 解放战争著名的战役
```

用户预期：

- T2/T4/T5/T6 是同一话题下的追问，应改写为“长征的著名事件”“抗日战争的主要领导人”等独立问题；
- T7 是新话题，应切断“抗日战争”上下文，只回答解放战争。

实际结果：

- T4–T7 的 `resolved_query` 逐轮累积；
- T7 被标成 `follow_up`；
- T7 检索 Top10 没有“解放战争/国共内战”文档；
- T7 答案同时回答抗日战争的起止时间、领导人、百团大战、封神演义武器和解放战争，共 5 节。

### 1.2 影响面

| 影响 | 说明 |
|---|---|
| 答案质量 | 旧话题反复出现，答案越来越长，用户需要的信息被稀释 |
| 检索质量 | 语义检索被历史话题主导，新话题文档排不进来 |
| 成本/延迟 | prompt tokens 随拼接增长；T5/T6 延迟 66–70s |
| 可观测性 | 日志无应用错误，访问状态全部 `ok`，故障静默 |
| 评测 | 单轮指标无法发现多轮退化；现有多轮用例未覆盖“累积拼接” |

### 1.3 结论边界

本文档**不把 7 个问题拼成一个大问题**做分析；所有结论均按 `turn_id` 对齐 `messages` / `turn_traces` / `visit_records` / 服务日志，并用真实模型重放了关键 prompt。

---

## 2. 证据

### 2.1 会话轨迹（`var/db/sessions.db`）

| Turn | `raw_query` | `resolved_query` | `intent` | `confidence` | `rewrite_failed` | `prompt_tokens` | `latency_ms` |
|---|---|---|---|---|---|---|---|
| 1 | 长征 | 长征 | new_topic | 1.0 | 0 | 4485 | 9963 |
| 2 | 著名的事件 | 长征 著名的事件 | follow_up | 0.4 | **1** | 4264 | 10365 |
| 3 | 抗日战争开始时间和结束时间 | 抗日战争开始时间和结束时间 | new_topic | 1.0 | 0 | 5416 | 19899 |
| 4 | 主要领导人 | 抗日战争开始时间和结束时间 主要领导人 | follow_up | 0.4 | **1** | 6289 | 48245 |
| 5 | 著名战役 | 抗日战争开始时间和结束时间 主要领导人 著名战役 | follow_up | 0.4 | **1** | 5920 | 70273 |
| 6 | 常用的武器型号 | 抗日战争开始时间和结束时间 主要领导人 著名战役 常用的武器型号 | follow_up | 0.4 | **1** | 5497 | 66542 |
| 7 | 解放战争著名的战役 | 抗日战争开始时间和结束时间 主要领导人 著名战役 常用的武器型号 解放战争著名的战役 | follow_up | 0.4 | **1** | 5574 | 55640 |

关键事实：

1. 所有真正触发改写的轮次（T2、T4–T7）全部 `rewrite_failed=1`，`confidence` 固定 0.4。
2. `resolved_query` 长度单调增长：`2 → 8 → 13 → 19 → 24 → 32 → 42`。
3. T3 的 `confidence=1.0` 不是改写成功，而是 **长度 13 > 12，被 `needs_rewrite` 跳过**；trace 无法区分“跳过”和“成功”。
4. `sessions` 表 7 轮后仍为 `summary=''`、`summary_upto_turn=0`、`summary_tokens=0`。
5. `session_entities` 为空；`turn_traces.entities_json` 全为 `[]`，实体槽 7 轮从未写入。
6. T7 检索 Top10 主题分布：武器（《封神演义》）4 条、人物 4 条、抗日 2 条、**解放/国共 0 条**；Top1 为 `wiki/concepts/武器.md#detail4`，score 81.97。
7. 答案长度：`579 → 182 → 340 → 746 → 1089 → 1381 → 1509` 字；T7 答案 5 节，标题为“抗日战争与解放战争：起止时间、领导人、著名战役、武器型号”。

### 2.2 日志与访问状态

- `logs/server_8000.log` 共 2249 行。
- `grep -Ei "error|exception|traceback|timeout|rewrite|chat_json|llm" logs/server_8000.log` **零命中**。
- `visit_records` 中 7 次 `/ask_smart` 全部 `status=ok`。
- 没有设计 §12.3 要求的 `[chat] session/turn/intent/resolved/ms` 日志。

结论：**改写失败、摘要失败在日志和访问状态中完全不可见**。

### 2.3 真实模型重放

使用仓库 `LLMClient` 与同一批 prompt 重放：

| 场景 | 配置 | 结果 |
|---|---|---|
| T4 改写 | `max_tokens=2000`，thinking 默认 | 20.8s；`output_tokens=774`（thinking 727 + text 158）；输出 `{"resolved_query":"抗日战争主要领导人","intent":"follow_up","confidence":0.85}` |
| T4 改写 | `max_tokens=2000`，`thinking={"type":"disabled"}` | **1.5s**；输出同上 |
| T7 改写 | `max_tokens=2000`，thinking 默认 | 18.2s；正确输出 `new_topic`、`confidence=0.95` |
| T7 改写 | `max_tokens=2000`，`thinking={"type":"disabled"}` | **2.0s**；输出 `{"resolved_query":"解放战争著名的战役","intent":"new_topic","entities":["解放战争"],"confidence":0.9}` |
| 摘要 | `max_tokens=600`，thinking 默认 | 11.9s；`output_tokens=600`，`thinking_tokens=600`，`stop_reason=max_tokens`，`text_len=0` |
| 摘要 | `max_tokens=600`，`thinking={"type":"disabled"}` | **5.6s**；`output_tokens=285`，输出 465 字有效摘要 |

两个结论：

1. 模型本身能正确改写和判断新话题，**只是线上从未被调用**。
2. 辅助任务必须关闭 thinking 或换快模型；否则修复改写后会每轮增加 18–21s。

---

## 3. 根因分析

### 3.1 P0-1：`chat_json` 契约不匹配，改写 100% 抛异常

**代码位置**

```python
# infra/llm.py:98
def chat_json(self, prompt, system=None, max_tokens=None):
```

```python
# qa/rewrite.py:151-154
chat_json = getattr(llm, "chat_json", None)
if callable(chat_json):
    data = chat_json(prompt, system=REWRITE_SYSTEM,
                     max_tokens=2000, temperature=0.1)
```

**实际异常**

```text
TypeError: LLMClient.chat_json() got an unexpected keyword argument 'temperature'
```

**为什么没被发现**

- `resolve_native()` 外层是 `except Exception: pass`，异常被吞掉；
- 重试循环第二次调用仍是同一个错误，立即进入规则回退；
- `tests/conftest.py` 的 `FakeLLM` 只实现 `chat()`，没有 `chat_json()`，所以测试走的是 `llm.chat(..., temperature=...)` 分支，永远覆盖不到真实客户端的坏分支。

**实测**

```text
direct call -> TypeError: LLMClient.chat_json() got an unexpected keyword argument 'temperature'
resolve_native -> failed=True, resolved='抗日战争开始时间和结束时间 主要领导人', elapsed=0.000s
```

### 3.2 P0-2：失败回退使用“上一轮 resolved + 当前问题”

**代码位置**

```python
# qa/rewrite.py:77
def _rule_resolve(question, window, entities):
    ...
    last = 上一轮的 resolved_query
    if last and last.strip() != q:
        return f"{last.strip()} {q}"
    return q
```

更关键的是 `ChatService` 即使知道 `rewrite.failed`，仍然使用回退结果：

```python
# qa/multiturn.py:151
resolved = (rewrite.resolved_query or question).strip() or question
```

**累积过程**

```text
T2: 长征 + 著名的事件
T4: 抗日战争起止 + 主要领导人
T5: 抗日战争起止 + 主要领导人 + 著名战役
T6: 抗日战争起止 + 主要领导人 + 著名战役 + 武器型号
T7: 抗日战争起止 + 主要领导人 + 著名战役 + 武器型号 + 解放战争
```

这与设计 §5.6 的“改写失败 → 直接用 `raw_question` 检索（等价单轮）”相反。**实现把“规则兜底”当成了“语义改写结果”继续使用**，这是用户看到“历史问题拼接”的直接原因。

### 3.3 P0-3：摘要 `max_tokens=600` 被 thinking 吃满，摘要永远为空

**代码位置**

```python
# conversations/summarizer.py:45
summary = llm.chat(
    [{"role": "user", "content": prompt}],
    system=SUMMARY_SYSTEM,
    max_tokens=600,
    temperature=0.2,
)
```

真实模型重放：

```text
output_tokens: 600
thinking_tokens: 600
stop_reason: max_tokens
block types: ['thinking']
text_len: 0
```

外层：

```python
except Exception:
    return
```

于是：

- `summary` 永远为空；
- `summary_upto_turn` 永远为 0；
- 超过最近 3 轮的历史无法进入 prompt；
- 旧话题只能靠 `resolved_query` 拼接传递，进一步放大 P0-2。

**附带的数据库问题**：`SqliteSessionRepository.update_summary()` 只更新 `summary` / `summary_upto_turn` / `updated_at`，没有写入 `summary_tokens`。

### 3.4 P1-1：话题切换没有独立判定，失败时一律 `follow_up`

- `needs_rewrite` 对长度 ≤ 12 的输入一律触发改写；
- intent 完全依赖 LLM；
- LLM 调用坏掉后，回退固定为 `Intent.FOLLOW_UP`、`confidence=0.4`、`failed=True`；
- T7“解放战争著名的战役”只有 9 个字，因此被当成追问；
- `update_entity_slots()` 只在 `correction` 时删除旧实体，`new_topic` 不会清空。

即使改写成功后正确返回 `new_topic`，旧实体、`doc_boost`、`repeat_ids` 仍会带入下一轮，话题边界没有真正建立。

### 3.5 P1-2：检索权重放大被污染的 query

```python
# retrieval/fusion.py
WEIGHTS = (1.0, 2.0, 1.0)  # BM25 : semantic : rewrite-semantic
```

语义检索对 `resolved_query` 权重 2.0，BM25 原文只有 1.0。当 `resolved_query` 是 5 个话题的拼接时，语义检索自然被旧话题主导。

T7 的 Top10 没有任何“解放战争/国共内战”文档，Top1 是《封神演义》武器，就是直接结果。

### 3.6 P1-3：生成 prompt 明确要求模型回答整个拼接后的问题

`ChatService` 把拼接后的 `resolved` 作为当前问题，并同时传入最近 3 轮 Q/A。生成阶段看到的是：

```text
## 当前问题
抗日战争开始时间和结束时间 主要领导人 著名战役 常用的武器型号 解放战争著名的战役
```

system prompt 中“不要重复上一轮内容”的规则，敌不过当前问题本身就把所有子问题列了出来。

### 3.7 P2：可观测性与测试缺口

| 缺口 | 后果 |
|---|---|
| 无 `[chat]` 结构化日志 | 改写/摘要失败不可见 |
| `except Exception: pass/return` | 异常无堆栈、无计数 |
| trace 只有 `rewrite_failed` | 无法区分“跳过”和“成功” |
| `prompt_tokens/completion_tokens` 为估算值 | 无法分析真实成本/延迟 |
| `summary_tokens` 未写入 | 摘要状态不可观测 |
| 测试替身无 `chat_json` | 真实客户端契约未被覆盖 |
| 无“累积拼接”回归用例 | 同类问题可再次上线 |

---

## 4. 整改方案

### 4.1 设计原则

1. **改写结果必须是独立可检索问题**：不允许把上一轮 `resolved_query` 直接前缀拼接。
2. **改写失败按单轮处理**：使用 `raw_question`，不继承历史、实体、doc boost；与设计 §5.6 一致。
3. **历史不是证据**：历史只用于理解指代；生成阶段只引用本轮 `[E1]..[En]`。
4. **话题边界必须显式**：`new_topic` 清空旧实体、旧历史、旧 boost。
5. **辅助任务不参与深度思考**：改写/摘要默认关闭 thinking，或使用独立快模型。
6. **失败必须可观测**：任何降级都要落 trace + 日志，不允许静默 `pass`。

### 4.2 P0：立即修复

#### 4.2.1 修复 `LLMClient.chat_json` 契约，并支持关闭 thinking

```python
# infra/llm.py

def chat(self, messages, system=None, max_tokens=None, temperature=None,
         thinking=None):
    if self.style == "openai":
        # OpenAI 兼容路径暂不透传 thinking；如供应商支持再用扩展字段
        return self._call_openai(
            messages, system=system, max_tokens=max_tokens,
            temperature=temperature,
        )
    return self._call_anthropic(
        messages, system=system, max_tokens=max_tokens,
        temperature=temperature, thinking=thinking,
    )


def _call_anthropic(self, messages, system=None, max_tokens=None,
                    temperature=None, thinking=None):
    ...
    if thinking is not None:
        body["thinking"] = thinking
    ...


def chat_json(self, prompt, system=None, max_tokens=None,
              temperature=None, thinking=None):
    text = self.chat(
        [{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens or 2000,
        temperature=0.2 if temperature is None else temperature,
        thinking=thinking,
    )
    return parse_json(text)
```

改写与摘要调用：

```python
# qa/rewrite.py
data = chat_json(
    prompt, system=REWRITE_SYSTEM,
    max_tokens=2000, temperature=0.1,
    thinking={"type": "disabled"},
)

# conversations/summarizer.py
summary = llm.chat(
    [{"role": "user", "content": prompt}],
    system=SUMMARY_SYSTEM,
    max_tokens=1200,
    temperature=0.2,
    thinking={"type": "disabled"},
)
```

> 兼容性：如果供应商不支持 `thinking` 字段，捕获 400 后回退到不带该字段的请求，并记录一次 `aux_thinking_unsupported` 标志。

#### 4.2.2 失败回退改为“原文单轮”，禁止拼接

```python
# qa/rewrite.py

def _rule_resolve(question, window, entities):
    q = (question or "").strip()
    # 只在“单实体 + 明确代词”时做最小替换；其余一律原文
    if len(entities) == 1 and PRONOUN_RE.search(q):
        return PRONOUN_RE.sub(entities[0], q, count=1)
    return q
```

```python
# qa/multiturn.py

failed = bool(rewrite.failed)
new_topic = rewrite.intent is Intent.NEW_TOPIC
isolate = failed or new_topic

resolved = (
    (question if failed else rewrite.resolved_query) or question
).strip() or question
```

失败时按“等价单轮”：

```python
if failed:
    history_for_answer = ()
    merged_entities = []
    doc_boost = {}
    recent_ids = frozenset()
    penalty = 1.0
    summary_for_answer = ""
elif new_topic:
    history_for_answer = ()
    merged_entities = list(dict.fromkeys(rewrite.entities))[:8]
    doc_boost = {}
    recent_ids = frozenset()
    penalty = 1.0
    summary_for_answer = summary
else:
    history_for_answer = window
    merged_entities = list(dict.fromkeys(
        list(rewrite.entities) + list(entities)
    ))[:8]
    doc_boost = {path: 1.1 for path in recent_docs[:3]}
    # recent_ids 保持 ChatService 开头读取的值
    penalty = (
        1.0
        if rewrite.intent is Intent.FOLLOW_UP and CHAT_CONT_RE.search(question or "")
        else self._repeat_penalty
    )
    summary_for_answer = summary

options = AnswerOptions(
    rewrite=False,
    raw_question=question,
    entities=tuple(merged_entities),
    doc_boost=doc_boost,
    repeat_ids=frozenset(recent_ids),
    repeat_penalty=penalty,
    history=history_for_answer,
    summary=summary_for_answer,
    intent_hint=None if failed else rewrite.intent,
    multiturn=True,
)
```

#### 4.2.3 摘要预算、重试与状态写入

```python
# conversations/summarizer.py

def work() -> None:
    try:
        ...
        prompt = summarize_prompt(current.summary, turns)
        summary = llm.chat(
            [{"role": "user", "content": prompt}],
            system=SUMMARY_SYSTEM,
            max_tokens=1200,
            temperature=0.2,
            thinking={"type": "disabled"},
        )
        if not (summary or "").strip():
            # 第一次空输出，放宽预算重试一次
            summary = llm.chat(
                [{"role": "user", "content": prompt}],
                system=SUMMARY_SYSTEM,
                max_tokens=2000,
                temperature=0.2,
                thinking={"type": "disabled"},
            )
        summary = (summary or "").strip()[:800]
        if summary:
            self._sessions.update_summary(session_id, summary, turns[-1].turn_id)
        else:
            print(
                f"[summary] empty session={session_id} upto={turns[-1].turn_id}",
                flush=True,
            )
    except Exception as exc:
        print(
            f"[summary] failed session={session_id}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
```

```sql
-- infra/sqlite_sessions.py
UPDATE sessions
SET summary=?, summary_upto_turn=?, summary_tokens=?, updated_at=?
WHERE session_id=?
```

`summary_tokens` 使用 `estimate_tokens(summary)` 或 API 真实 usage。

#### 4.2.4 最小可观测性

```python
# qa/multiturn.py（落库后）
print(
    f"[chat] sid={sid} turn={turn_id} "
    f"intent={rewrite.intent.value} "
    f"rewrite={'fail' if rewrite.failed else 'ok'} "
    f"isolate={int(isolate)} "
    f"resolved_len={len(resolved)} "
    f"prompt_tokens={answer.memory.prompt_tokens} "
    f"ms={latency_ms}",
    flush=True,
)
```

```python
# qa/rewrite.py
except Exception as exc:
    print(
        f"[rewrite] failed: {type(exc).__name__}: {exc}",
        flush=True,
    )
```

### 4.3 P1：结构性修复

#### 4.3.1 话题边界独立判定

LLM intent 为主，规则为辅：

```python
TOPIC_SHIFT_RE = re.compile(
    r"(换个话题|另外|另一个|新问题|再问一个|说说|谈谈|聊一下)"
)

def is_topic_shift(question, window, entities, rewrite):
    if rewrite.intent is Intent.NEW_TOPIC:
        return True
    if TOPIC_SHIFT_RE.search(question or ""):
        return True
    if not window or not entities:
        return False
    # 当前问题与所有 active entity 无交集，且与上一轮独立问题重合度很低
    if all(entity not in question for entity in entities):
        last = window[-1].resolved_query or window[-1].question
        if _char_overlap(question, last) < 0.1:
            return True
    return False
```

规则：

- `new_topic`：清空 `session_entities`、`doc_boost`、`repeat_ids`，生成阶段不传历史原文，只保留摘要；
- `follow_up`：保留实体与最近历史；
- `correction`：删除被纠正实体后 upsert；
- 改写失败：直接按 `new_topic` 单轮处理。

#### 4.3.2 实体槽按话题重置

```python
def update_entity_slots(prev_entities, result, turn_id):
    upsert = list(dict.fromkeys(result.entities))
    delete = []
    if result.intent in (Intent.CORRECTION, Intent.NEW_TOPIC):
        delete = [e for e in prev_entities if e not in upsert]
    return upsert, delete
```

#### 4.3.3 检索隔离

| 场景 | 主查询 | 实体/文档 boost | 说明 |
|---|---|---|---|
| `follow_up` | `resolved_query` | 保留 | 双路 BM25（raw + resolved）+ 语义 |
| `new_topic` | `raw_question` | 清空 | 不继承旧话题 |
| `rewrite.failed` | `raw_question` | 清空 | 等价单轮 |
| `correction` | 纠正后的 `resolved_query` | 删除旧实体 | 不混用旧实体 |

补充保护：

- `resolved_query` 长度上限 120 字；
- 超过 2 个问句标记（`？` / `?`）时拒绝使用改写结果；
- 改写结果包含 3 个以上历史话题实体时拒绝使用；
- 被拒绝时记 `flags.resolved_rejected=1`，回退原文。

#### 4.3.4 上下文构建

- 历史 user 行优先使用 `resolved_query`，原始输入可放在括号中；
- `new_topic` 时 `history=()`，只保留摘要；
- system 中增加“当前话题”标记：
  ```text
  ## 当前话题
  解放战争
  ```
- 摘要 prompt 明确“摘要只作背景，不是证据；跨话题内容必须分节”。

#### 4.3.5 辅助模型与缓存

新增配置：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LLM_AUX_MODEL` | 空（复用主模型） | 改写/摘要/评估使用的快模型 |
| `LLM_AUX_THINKING` | `off` | `off` 时传 `thinking={"type":"disabled"}` |
| `REWRITE_MAX_TOKENS` | `2000` | 改写预算 |
| `SUMMARY_MAX_TOKENS` | `1200` | 摘要预算 |
| `REWRITE_CACHE_TTL` | `600` | 同会话同问题改写缓存秒数 |

改写缓存键：`(session_id, question_hash, mode)`；命中时跳过 LLM，trace 记 `rewrite_cached=1`。

### 4.4 P2：可观测性、数据与测试

#### 4.4.1 `turn_traces` 字段扩展

```sql
ALTER TABLE turn_traces ADD COLUMN rewrite_skipped INTEGER DEFAULT 0;
ALTER TABLE turn_traces ADD COLUMN rewrite_ms INTEGER;
ALTER TABLE turn_traces ADD COLUMN rewrite_error TEXT;
ALTER TABLE turn_traces ADD COLUMN summary_failed INTEGER DEFAULT 0;
ALTER TABLE turn_traces ADD COLUMN resolved_rejected INTEGER DEFAULT 0;
```

对应 `schema_version` 升级到 2，迁移脚本需兼容已有 `version=1` 数据库。

#### 4.4.2 看板指标

| 指标 | 定义 | 目标 |
|---|---|---|
| `rewrite_failed_rate` | `rewrite_failed=1 / 触发改写的轮次` | < 2% |
| `rewrite_skip_rate` | `rewrite_skipped=1 / 多轮轮次` | 观察，不设硬线 |
| `resolved_accumulation_rate` | `resolved_query` 包含上一轮完整 resolved 的轮次占比 | 0 |
| `topic_switch_accuracy` | 新话题被正确判定 `new_topic` 的比例 | ≥ 95% |
| `summary_coverage` | 达到阈值轮次后存在有效摘要的会话占比 | ≥ 95% |
| `rewrite_p95_ms` | 改写调用 P95 | ≤ 2000ms |
| `multiturn_p95_ms` | 端到端 P95 | ≤ 12000ms |
| `citation_precision` | 有效引用 / 总引用 | ≥ 95% |

#### 4.4.3 测试矩阵

| 用例 | 断言 |
|---|---|
| `chat_json` 真实签名 | 接受 `temperature` / `thinking`，不再 `TypeError` |
| 改写失败回退 | `resolved_query == raw_question`，历史/实体为空，trace `rewrite_failed=1` |
| 7 轮失败注入 | `resolved_query` 长度不累积，始终等于原文 |
| 话题切换 | 抗日 → 解放战争，intent=`new_topic`，检索命中 `国共内战`，答案无抗日章节 |
| 短追问 | 长征 → 著名的事件，resolved 含“长征”，不累积其他话题 |
| 指代 | 长征 → 它持续了多久，resolved 含“长征” |
| 纠正 | A → 不是 A，是 B，旧实体被删除 |
| 摘要空输出重试 | 第一次返回空、第二次返回文本，`summary` 落库且 `summary_tokens>0` |
| 摘要预算耗尽 | `stop_reason=max_tokens` 且无 text 时，记录 `summary_failed=1` 并重试 |
| 单轮回归 | `session_id=None` 时响应与改造前一致 |

#### 4.4.4 多轮评测集补充

在 `auto_optimization/benchmark/multiturn_set.json` 增加：

```json
{
  "id": "mt-topic-switch-001",
  "category": "switch",
  "turns": [
    {"q": "抗日战争开始时间和结束时间"},
    {"q": "主要领导人"},
    {"q": "著名战役"},
    {"q": "常用的武器型号"},
    {"q": "解放战争著名的战役",
     "expect_intent": "new_topic",
     "expect_resolved_not_contains": ["抗日战争", "主要领导人", "武器型号"],
     "expect_docs": ["wiki/concepts/国共内战.md"]}
  ]
}
```

---

## 5. 数据与配置变更

### 5.1 配置

| 环境变量 | 默认 | 变更 |
|---|---|---|
| `LLM_AUX_MODEL` | 空 | 新增；为空复用主模型 |
| `LLM_AUX_THINKING` | `off` | 新增；改写/摘要默认关闭 thinking |
| `REWRITE_MAX_TOKENS` | `2000` | 新增 |
| `SUMMARY_MAX_TOKENS` | `1200` | 新增 |
| `REWRITE_CACHE_TTL` | `600` | 新增 |
| `TOPIC_GUARD` | `1` | 新增；话题边界规则开关 |
| `SUMMARY_RETRY_MAX_TOKENS` | `2000` | 新增；摘要空输出重试预算 |

### 5.2 数据库

- `sessions.summary_tokens` 开始写入；
- `turn_traces` 增加 5 个诊断字段（见 4.4.1）；
- `schema_version` 从 1 升到 2；
- 不修改历史 `messages.resolved_query`，保留故障样本。

### 5.3 兼容性

- 单轮路径（`session_id=None`）行为不变；
- `MULTITURN_ENABLED=0` 时完全回退单轮；
- `LLM_AUX_THINKING=on` 时恢复旧行为；
- 旧数据库无需重建，`ALTER TABLE ADD COLUMN` 即可。

---

## 6. 验收标准

### 6.1 功能验收

1. “长征”7 轮重放：
   - T2 resolved = “长征 著名的事件”（只含一个话题）；
   - T4 resolved = “抗日战争主要领导人”；
   - T5 resolved = “抗日战争著名战役”；
   - T6 resolved = “抗日战争常用武器型号”；
   - T7 resolved = “解放战争著名的战役”，intent=`new_topic`。
2. T7 检索 Top10 至少 2 条 `国共内战` / `解放战争` 相关文档；
3. T7 答案不包含“抗日起止时间”“百团大战”“封神演义武器”章节；
4. 7 轮后 `summary_upto_turn >= 6`，`summary_tokens > 0`；
5. `session_entities` 在 T4 后包含“抗日战争”，T7 后只包含“解放战争”；
6. 日志出现 `[chat]` 行，包含 `intent/rewrite/isolate/resolved_len/ms`；
7. 任意改写失败时 trace `rewrite_failed=1`，且 `resolved_query == raw_query`。

### 6.2 非功能验收

| 指标 | 目标 |
|---|---|
| 改写 P95 | ≤ 2s（关闭 thinking） |
| 摘要 P95 | ≤ 8s |
| 多轮端到端 P95 | ≤ 12s |
| `rewrite_failed_rate` | < 2% |
| `resolved_accumulation_rate` | 0 |
| 单轮 benchmark 均分下降 | ≤ 2% |
| 多轮评测核心指标下降 | ≤ 5% |

### 6.3 回归验收

```bash
make test          # 全量测试
make arch          # 架构门禁
make snapshot      # 单轮快照
make gate          # 测试 + 架构 + 健康检查
make e2e           # 页面冒烟
```

多轮专项：

```bash
.venv/bin/python scripts/eval_multiturn.py \
  --base http://127.0.0.1:8002 \
  --set auto_optimization/benchmark/multiturn_set.json \
  --out eval_mt_after.json

.venv/bin/python scripts/eval_multiturn.py \
  --compare eval_mt_before.json eval_mt_after.json --max-drop 5
```

---

## 7. 发布与回滚

### 7.1 灰度顺序

1. **8002（人工测试）**：跑多轮用例集 + “长征”7 轮重放；
2. **8001（自迭代）**：接入多轮回归，观察自动优化是否退化；
3. **8000（线上）**：先开 `TOPIC_GUARD=1`，观察 1 天；
4. 稳定后开启 `LLM_AUX_THINKING=off` 的默认值。

### 7.2 开关

| 开关 | 作用 |
|---|---|
| `MULTITURN_ENABLED=0` | 完全回退单轮 |
| `REWRITE_MODE=off` | 关闭改写，全部按原文检索 |
| `LLM_AUX_THINKING=on` | 恢复辅助任务 thinking |
| `TOPIC_GUARD=0` | 关闭话题边界规则 |
| `SUMMARY_MAX_TOKENS` | 调整摘要预算 |

### 7.3 回滚

- 代码回滚：恢复 `qa/rewrite.py` / `qa/multiturn.py` / `conversations/summarizer.py` 上一版本；
- 配置回滚：`MULTITURN_ENABLED=0` 立即回单轮；
- 数据回滚：无需迁移历史消息；新增 trace 字段可为空；
- 索引回滚：本次不改索引结构。

---

## 8. 风险登记

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| 关闭 thinking 后复杂指代改写质量下降 | 中 | 中 | 多轮评测集覆盖指代/省略/纠正；低置信度时回退 thinking |
| 辅助模型供应商不支持 `thinking` 字段 | 低 | 低 | 捕获 400 后重试无该字段，记 `aux_thinking_unsupported` |
| 话题规则误判，把真实追问切成新话题 | 中 | 中 | 规则只做高置信判断；LLM intent 为主；误判可关 `TOPIC_GUARD` |
| 改写结果仍被拼接污染 | 低 | 高 | 长度/子句/实体数保护 + `resolved_accumulation_rate` 监控 |
| 摘要重试增加延迟 | 低 | 低 | 摘要异步执行，不阻塞主响应 |
| 旧会话数据继续污染 UI | 高 | 低 | 建议新会话验证；旧数据保留为样本，不做不可靠修复 |
| 数据库迁移失败 | 低 | 中 | 迁移前备份 `var/db/sessions.db`；`ALTER TABLE` 幂等检查 |

---

## 9. 实施清单

### 9.1 代码改动

| 文件 | 动作 | 内容 |
|---|---|---|
| `infra/llm.py` | 改 | `chat` / `chat_json` 增加 `temperature`、`thinking`；记录真实 usage |
| `qa/rewrite.py` | 改 | 修复调用参数；失败回退原文；异常日志；历史使用 resolved |
| `qa/multiturn.py` | 改 | 失败/新话题单轮隔离；历史/实体/boost 清空；`[chat]` 日志 |
| `conversations/summarizer.py` | 改 | thinking off、预算提升、空输出重试、失败日志 |
| `infra/sqlite_sessions.py` | 改 | `summary_tokens` 写入；trace 新字段；schema v2 |
| `retrieval/service.py` | 改 | `new_topic` / 失败时只用原文；可选 `semantic_query` |
| `qa/context.py` | 改 | 历史使用 resolved；话题边界标记 |
| `api/routes/ask.py` | 改 | 透传 `stateless` / 新开关（如需要） |
| `tests/*` | 增 | 契约、失败回退、话题切换、累积保护、摘要重试 |
| `auto_optimization/benchmark/multiturn_set.json` | 增 | 话题切换与累积拼接用例 |

### 9.2 实施顺序

```text
D0  P0-1 chat_json 签名 + thinking 支持
D0  P0-2 失败回退原文 / 单轮隔离
D0  P0-3 摘要 thinking off + 重试 + 日志
D1  回归测试 + 8002 “长征”7 轮重放
D2  P1-1 话题边界 + 实体重置
D3  P1-2 检索隔离 + resolved 保护
D4  P1-3 上下文隔离 + 辅助模型/缓存
D5  P2 trace/usage/看板/评测集
D6  8002 → 8001 → 8000 灰度
```

---

## 10. 附录

### 10.1 证据文件索引

| 数据 | 位置 |
|---|---|
| 会话/消息/实体/轨迹 | `var/db/sessions.db` → `sessions` / `messages` / `session_entities` / `turn_traces` |
| 访问/评测 | `var/db/analytics.db` → `visit_records` / `eval_records` |
| 服务日志 | `logs/server_8000.log` |
| 改写实现 | `qa/rewrite.py` |
| 多轮编排 | `qa/multiturn.py` |
| 摘要实现 | `conversations/summarizer.py` |
| LLM 客户端 | `infra/llm.py` |
| 融合权重 | `retrieval/fusion.py` |
| 测试替身 | `tests/conftest.py` |

### 10.2 关键 SQL 复核

```sql
-- 改写失败与累积拼接
SELECT turn_id, raw_query, resolved_query, intent, confidence,
       rewrite_failed, length(resolved_query) AS resolved_len,
       prompt_tokens, latency_ms, flags_json
FROM turn_traces
WHERE session_id = 'ses_cd9927540497'
ORDER BY turn_id;

-- 摘要状态
SELECT session_id, title, turn_count, summary_upto_turn,
       summary_tokens, length(summary) AS summary_len
FROM sessions
WHERE session_id = 'ses_cd9927540497';

-- 实体槽
SELECT * FROM session_entities
WHERE session_id = 'ses_cd9927540497';

-- T7 检索主题
SELECT turn_id, retrieved_json
FROM turn_traces
WHERE session_id = 'ses_cd9927540497' AND turn_id = 7;
```

### 10.3 真实模型重放记录

**T4 改写（thinking disabled，1.5s）**

```json
{
  "resolved_query": "抗日战争主要领导人",
  "intent": "follow_up",
  "entities": ["抗日战争"],
  "filters": {},
  "needs_clarification": false,
  "clarification_question": "",
  "confidence": 0.85
}
```

**T7 改写（thinking disabled，2.0s）**

```json
{
  "resolved_query": "解放战争著名的战役",
  "intent": "new_topic",
  "entities": ["解放战争"],
  "filters": {},
  "needs_clarification": false,
  "clarification_question": "",
  "confidence": 0.9
}
```

**摘要（thinking disabled，5.6s，465 字）**

```text
主题：长征与抗日战争相关史实查询

已确认内容：
1. 长征：1934—1935年红军主力从中央苏区向陕北的战略撤退，全程约12,500公里；
   核心著名事件为遵义会议（1935年1月），确立毛泽东实际领导地位。
2. 抗日战争起止：知识库未收录精确日期，仅确认1937年全面爆发、1945年结束（间接证据）。
3. 抗日战争主要领导人：中共方面有毛泽东、彭德怀；国民党方面领导人未收录。
4. 抗日战争著名战役：仅收录百团大战（1940年8月—12月）。

用户明确关注但知识库缺失的信息：
- 抗日战争精确起止日期
- 抗日战争常用武器型号（现有武器条目均为《封神演义》小说兵器）

尚未解决的问题：
- 抗日战争国民党方面领导人
- 除百团大战外的其他抗战著名战役
```

### 10.4 相关文档

- [`docs/multi-turn-design.md`](./multi-turn-design.md)：多轮对话详细设计（§5.2 改写契约、§5.6 失败回退、§7.5 滚动摘要、§12 可观测性）
- [`docs/detailed-design.html`](./detailed-design.html)：整体详细设计
- [`docs/architecture-review.html`](./architecture-review.html)：架构评审

---

## 11. 决策记录

| 编号 | 决策 | 理由 |
|---|---|---|
| D-001 | 改写失败一律按单轮 `raw_question` 处理 | 与设计 §5.6 一致，避免累积拼接；失败时宁可少继承，不可污染 |
| D-002 | 改写/摘要默认关闭 thinking | 实测 18–21s → 1.5–2.0s；辅助任务不需要深度推理 |
| D-003 | `new_topic` 清空历史原文、实体、doc boost | 话题边界必须硬隔离；摘要只作背景 |
| D-004 | 不修复旧会话 `resolved_query` | 数据已被污染，拆分不可靠；保留为故障样本 |
| D-005 | trace 增加 `rewrite_skipped` / `rewrite_ms` / `rewrite_error` | 区分“跳过/成功/失败”，避免 T3 的 1.0 被误读为成功 |
| D-006 | 新增 `resolved_query` 长度/子句/实体数保护 | 防止未来改写模型再次输出累积拼接 |

---

## 12. 实施记录（2026-11-17）

### 12.1 已完成

| 文件 | 变更 | 状态 |
|---|---|---|
| `infra/llm.py` | `chat` / `chat_json` 增加 `temperature`、`thinking`；Anthropic 路径 thinking 400 时自动回退重试 | ✅ |
| `infra/ollama_chat.py` | 新增本地 Qwen 客户端：`/api/chat` + `format=json`，默认 `qwen3:4B-Instruct` | ✅ |
| `bootstrap.py` | 新增 `REWRITE_LLM`（`LLM_REWRITE_PROVIDER=ollama|main`）；`ChatService` 注入改写模型 | ✅ |
| `qa/prompts/rewrite.md` | 升级 v3：当前话题锚点、最近一轮优先、new_topic 判定、禁止拼接 | ✅ |
| `qa/context.py` | 新增 `aux_thinking()`，默认 `{"type":"disabled"}`，可用 `LLM_AUX_THINKING=on` 恢复 | ✅ |
| `qa/rewrite.py` | 修复 `chat_json` 调用；失败回退原文；新增 `_guard_resolved`；新增 `[rewrite]` 日志；`new_topic` 清空旧实体 | ✅ |
| `qa/multiturn.py` | 改写失败/新话题时隔离历史、实体、doc boost、repeat；新增 `rewrite_failed` / `topic_isolated` flags 与 `[chat]` 日志 | ✅ |
| `conversations/summarizer.py` | 摘要默认关闭 thinking；预算 1200；空输出自动重试；失败日志；写入 `summary_tokens`；延迟导入避免循环依赖 | ✅ |
| `domain/ports.py` | `update_summary` 增加可选 `summary_tokens` | ✅ |
| `infra/sqlite_sessions.py` | `update_summary` 写入 `summary_tokens` | ✅ |
| `tests/test_native_rewrite.py` | 新增“失败不拼接”“新话题清空实体”用例 | ✅ |
| `tests/test_multiturn_isolation.py` | 新增失败隔离、新话题隔离、摘要重试、`chat_json` 契约用例 | ✅ |
| `tests/conftest.py` 等 | 测试替身补充 `thinking` 参数 | ✅ |

### 12.2 验证结果

```text
.venv/bin/python -m pytest -q
198 passed, 1 warning in 2.80s
```

真实模型重放（关闭 thinking）：

```text
{"q": "主要领导人", "resolved": "抗日战争主要领导人",
 "intent": "follow_up", "failed": false, "skipped": false,
 "confidence": 0.85, "entities": ("抗日战争",)}

{"q": "解放战争著名的战役", "resolved": "解放战争著名的战役",
 "intent": "new_topic", "failed": false, "skipped": false,
 "confidence": 0.9, "entities": ("解放战争",)}
```

### 12.3 尚未实施（P1/P2）

| 项 | 说明 |
|---|---|
| 检索层强隔离 | 当前在 `ChatService` 清空 `entities` / `doc_boost` / `repeat_ids`，`RetrievalService` 本身未新增 `semantic_query` 参数 |
| 话题规则兜底 | 已加入 `TOPIC_SHIFT_RE` + `_post_check_intent`；仍建议用评测集覆盖边界用例 |
| 上下文构建 | 改写 prompt 已使用 `resolved_query`；生成侧历史仍使用 `question`，话题边界标记待加 |
| trace 扩展 | `rewrite_skipped` / `rewrite_ms` / `rewrite_error` / `summary_failed` 字段与 schema v2 未落 |
| 辅助模型/缓存 | 查询改写已切本地 Qwen；摘要仍走主模型；改写缓存尚未接入 |
| 真实 usage | `prompt_tokens` 仍为估算值 |
| 数据修复 | 未修改旧会话 `resolved_query`；建议新会话验证 |

### 12.4 部署提示

- 代码已改，但**运行中的 8000 进程仍是旧代码**，需要重启后生效：
  ```bash
  ./scripts/restart_8000.sh
  ```
- 重启后先用新会话重放“长征”7 轮，确认：
  - `resolved_query` 不再累积；
  - T7 `intent=new_topic`；
  - 日志出现 `[chat]` 行；
  - 第 6 轮后 `summary_upto_turn >= 6`、`summary_tokens > 0`。

---

## 13. 查询改写本地化（Qwen）与逻辑修正（2026-11-17）

### 13.1 为什么“改写逻辑有问题”

修复 `chat_json` 之后，改写虽然不再抛 `TypeError`，但仍然存在三个问题：

1. **改写仍走主模型 DeepSeek**：每轮多一次主模型调用，成本和延迟都高；README 虽提到 `OLLAMA_CHAT_MODEL`，但代码没有本地 chat 实现。
2. **旧 prompt 没有“当前话题”锚点**：`_format_window()` 只放原始短句（“长征”“著名的事件”“抗日战争开始时间和结束时间”），小模型会被最早出现的“长征”吸引。
   - 实测 Qwen3-4B 在旧 prompt 下把“主要领导人”改写为 `长征的主要领导人`，`intent=new_topic`；
   - 最近一轮明明是“抗日战争开始时间和结束时间”，正确答案应为 `抗日战争的主要领导人` / `follow_up`。
3. **小模型的 `new_topic` 判定不稳定**：Qwen3-4B 对“解放战争著名的战役”可能返回 `follow_up`；仅依赖 LLM intent，`ChatService` 不会触发话题隔离。

另外，`needs_rewrite` 仍是“长度 ≤ 12 或含代词/省略”的粗粒度门控；它只能决定“是否调用改写”，不能决定“是否新话题”。因此必须在小模型输出后加确定性校验。

### 13.2 实现

#### 13.2.1 本地 Qwen 客户端

新增 `infra/ollama_chat.py`：

```python
class OllamaChatClient:
    # POST http://localhost:11434/api/chat
    # chat_json(...) 强制 format="json"
    # 默认模型 qwen3:4B-Instruct，超时 120s
```

选择 `/api/chat` 而不是 `/v1/chat/completions` 的原因：

- `format="json"` 由 Ollama 约束输出，避免小模型加 Markdown 围栏；
- 响应结构简单，直接取 `message.content`；
- 实测 `/api/chat` 首次加载后约 7–12s，`/v1` 首次调用可能 27s+。

#### 13.2.2 路由

`bootstrap.py`：

```python
REWRITE_LLM = _build_rewrite_llm()   # 默认 OllamaChatClient(qwen3:4B-Instruct)

def _build_chat_service(llm):
    return ChatService(..., rewrite_llm=REWRITE_LLM)
```

`qa/multiturn.py`：

```python
rewrite = self._rewriter.resolve(
    question, window, summary, entities,
    self._rewrite_llm or llm,      # 改写走 Qwen；生成仍走主模型
    mode=rewrite_mode,
)
```

配置：

```env
LLM_REWRITE_PROVIDER=ollama          # main 可回退主模型
OLLAMA_CHAT_URL=http://localhost:11434/api/chat
OLLAMA_CHAT_MODEL=qwen3:4B-Instruct
OLLAMA_CHAT_TIMEOUT=120
```

#### 13.2.3 Prompt v3

`qa/prompts/rewrite.md` 升级到 v3，新增：

- `## 当前话题（最近一轮独立问题）`；
- 历史 user 行使用上一轮 `resolved_query`，并保留原话括号；
- “省略/指代必须延续最近一轮，不能跳到更早话题”；
- “只有明确出现新主题才判 `new_topic`”；
- “resolved_query 不得包含上一轮完整问题，也不得拼接多个历史问题”。

`_format_window()` 对改写只保留 **最近 1 轮、assistant 截断 80 字**，降低小模型 prompt 和延迟。

#### 13.2.4 确定性意图兜底

`qa/rewrite.py` 新增 `_post_check_intent()`：

```text
if 模型已判 new_topic: 保持
if 当前输入含明确话题切换词: new_topic
if 当前输入含代词/省略/继续词: 保持模型 intent
if 会话实体非空，且 resolved_query 不含任何 active entity: new_topic
if 会话实体为空且输入长度 > 6，用“当前话题”做最长公共子串：
    与当前话题 LCS < 4 且无指代/省略: new_topic
```

效果：

- “主要领导人” + active entity `抗日战争` + resolved `抗日战争的主要领导人` → 保持 `follow_up`；
- “解放战争著名的战役” + active entity `抗日战争` + resolved `解放战争中著名的战役` → 强制 `new_topic`；
- 即使实体槽为空，也能用上一轮话题锚点兜底。

### 13.3 实测结果

| 场景 | 结果 |
|---|---|
| 旧 prompt + Qwen，T4“主要领导人” | `长征的主要领导人` / `new_topic` / 错误 |
| 新 prompt + Qwen，T4“主要领导人” | `抗日战争的主要领导人` / `follow_up` / 0.98 |
| 新 prompt + Qwen，T7“解放战争著名的战役” | `解放战争中著名的战役` / 模型给 `follow_up`，兜底改为 `new_topic` / 0.98 |
| 8000 端到端 T2“著名的事件” | `长征中有哪些著名的事件` / `follow_up` / 27.7s |
| 8000 端到端 T3“解放战争著名的战役” | `解放战争中有哪些著名的战役` / `new_topic` / `topic_isolated=1` / 20.9s |
| 全量测试 | `198 passed` |

日志示例：

```text
[startup] rewrite llm: ollama qwen3:4B-Instruct @ http://localhost:11434/api/chat
[chat] sid=ses_qwen_check_001 turn=2 intent=follow_up rewrite=ok rewrite_model=qwen3:4B-Instruct isolate=0 resolved_len=11 prompt_tokens=4091 ms=27657
[chat] sid=ses_qwen_check_001 turn=3 intent=new_topic rewrite=ok rewrite_model=qwen3:4B-Instruct isolate=1 resolved_len=13 prompt_tokens=4931 ms=20876
```

### 13.4 仍存在的问题与调优建议

| 问题 | 建议 |
|---|---|
| Qwen3-4B 改写约 9–12s，端到端 20–28s | 缩短 prompt（已做 1 轮）；降低 `num_predict`；Ollama `keep_alive`；GPU；或换更小 Qwen（需评测质量） |
| Ollama 单实例并发会排队 | 增加改写并发上限/超时；高并发时降级主模型 |
| 小模型 JSON 字段完整性和实体质量不稳定 | 评测集增加 `expect_resolved_contains` / `expect_intent`；必要时只要求输出 4 个关键字段 |
| Ollama 不可用时改写直接失败回退原文 | 可选 `LLM_REWRITE_FALLBACK=main`：本地失败后再用主模型重试一次 |
| 旧“长征”会话实体槽为空 | 用新会话验证；旧会话不建议修复历史 `resolved_query` |
| 没有 `rewrite_model` / `rewrite_ms` trace 字段 | 后续加 schema v2，便于按模型统计失败率与延迟 |

### 13.5 主模型切换：`deepseek-v4-flash`（2026-11-17）

根据部署要求，主模型从 `deepseek-v4-pro` 切换为 `deepseek-v4-flash`：

| 位置 | 值 |
|---|---|
| `.env` | `LLM_MODEL=deepseek-v4-flash` |
| `.env.example` | `LLM_MODEL=deepseek-v4-flash` |
| `config/settings.py` | 默认值改为 `deepseek-v4-flash` |
| `infra/llm.py` | 环境变量缺省值改为 `deepseek-v4-flash` |

当前分工：

```text
最终答案生成 / 评估 / 摘要：deepseek-v4-flash @ https://api.deepseek.com/anthropic
查询改写：qwen3:4B-Instruct @ http://localhost:11434/api/chat
向量：nomic-embed-text
```

启动日志：

```text
[startup] main llm: deepseek-v4-flash @ https://api.deepseek.com/anthropic
[startup] rewrite llm: ollama qwen3:4B-Instruct @ http://localhost:11434/api/chat
```

验证：

- `deepseek-v4-flash` 普通调用与 `thinking=disabled` 调用均返回 200；
- `/ask`（stateless）“长征” 生成 1485 字答案，耗时 6.9s；
- 全量测试 `198 passed`；
- 三端口 8000/8001/8002 与守护进程均已重启并通过健康检查。
