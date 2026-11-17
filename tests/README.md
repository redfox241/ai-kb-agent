# Tests

## 运行

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

测试默认不触网：`tests/conftest.py` 提供 `FakeLLM`、假 Embedding、假索引与临时 SQLite，
并替换评估/埋点/Disco 的网络调用。

## 快照

单轮输出的结构快照在 `tests/snapshots/single_turn.json`。
只有确认行为变更是有意的时候才更新：

```bash
UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest tests/test_single_turn_snapshot.py -q
```

更新后请 review diff，确认 `answer / sources / steps` 的变化符合预期。

## 覆盖范围

| 文件 | 覆盖 |
|---|---|
| `test_single_turn_snapshot.py` | 单轮输出快照、步骤结构、多轮契约、会话/实体/轨迹 |
| `test_api_contract.py` | 5 个问答入口、OpenAI 兼容、会话 CRUD、cookie、页面 |
| `test_domain_models.py` | 领域模型冻结、默认值、错误映射、端口表面、依赖纯度 |
| `test_config.py` | Profile、Settings 默认/覆盖/校验、Overrides 白名单/缓存/备份/回滚 |
| `test_ingestion_primitives.py` | frontmatter / 清洗 / 摘要 / 分块与旧实现逐字节等价 |
| `test_index_service.py` | vault 扫描等价、索引快照、原子持久化、重建失败恢复 |
| `test_retrieval_service.py` | BM25 / 向量 / 融合 / 会话加权 / 扩展 / 服务与旧实现等价 |
| `test_qa_context.py` | 上下文布局、token 预算、引用校验、追问与旧实现等价 |
| `test_qa_pipeline.py` | 单轮/多轮流水线与旧 retrieve + generate_answer 等价 |
| `test_trace_presenter.py` | Trace 事件 → steps 结构、状态图标、流水线步骤键 |
| `test_chat_service.py` | 新 ChatService 与旧 chat_service 对照、澄清、无证据、摘要任务 |
| `test_pipeline_wiring.py` | PIPELINE_V2 默认接线、旧路径回退、OpenAI 入口 |
| `test_sqlite_repository.py` | infra/sqlite_sessions：会话、轮次、实体、轨迹、统计 |
| `test_native_rewrite.py` | 原生 Rewrite 规则/失败回退/实体槽 |
| `test_mcp_tools.py` | RAG 测试、单题验证、benchmark 缓存、历史查询 |
| `test_optimizer_agent.py` | 受限 ReAct：成功、知识缺口、放弃、验证失败回滚 |
| `test_daily_guard.py` | benchmark 守护：只读、回退告警、基线刷新、回滚 |
| `test_analytics_stores.py` | analytics 评估/访问/归档读写、过滤、统计 |
| `test_disco_service.py` | Disco 主路径、空证据、LLM 失败、引用异常 |
| `test_jobs_json.py` | ThreadJobRunner 生命周期、parse_json 边界 |
| `test_chat_service_fallbacks.py` | fallback_raw / fallback_concat / citation flag / 摘要失败 |
| `test_eval_logger.py` | analytics/evaluation：同步评估、异步任务、低分入队 |
| `test_optimization_overrides.py` | overrides 同步生产、白名单、回滚 |
| `test_runtime_startup.py` | run_server 加载/重建/清理失败分支 |
| `test_concurrency.py` | SQLite 并发追加、Overrides 并发写 |
| `test_pages_contract.py` | 页面端口注入、HTML/Script 静态契约 |
| `test_service_manager.py` | 启停脚本：PID、端口、spawn、terminate、命令分发 |
| `test_trigger_logic.py` | 自优化队列、去重、聚合、优先级 |
| `test_architecture.py` | 四层依赖、shim、Prompt 资产、无 sys.path、Web 资产 |
| `test_prompts.py` | Prompt 版本、严格渲染、受控注入 |
| `test_bootstrap_config.py` | Profile / Settings / Overrides 组合根接线 |

线上基准（单轮 benchmark / 多轮集）需要服务启动后单独执行：

```bash
.venv/bin/python scripts/eval_multiturn.py \
  --base http://127.0.0.1:8002 --out eval_mt_base.json
```
