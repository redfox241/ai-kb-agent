# AI-KB Agent

本地知识库 RAG 服务：Markdown vault → 混合检索 → 多轮问答 → 引用校验 → 评估与自优化。

本仓库是独立可运行项目，按《详细设计》落地，分层目录直接位于仓库根：

- **四层单向依赖**：api → application(qa/retrieval/ingestion/conversations/analytics) → domain ← infra；
- **单一组合根**：`bootstrap.py`；
- **配置三层**：`Settings` / `Profile` / `OverridesStore`；
- **Prompt 资产化**：`qa/prompts/*.md` + 严格渲染 + 版本号进 trace；
- **前端分层**：`web/templates` + `web/static`，Python 只注册路由；
- **Agent 旁路**：主链路确定性 workflow，`optimization/` 作为独立工具边界。

## 技术栈

- Python 3.14
- FastAPI + uvicorn
- SQLite WAL
- Ollama Embedding
- Anthropic / OpenAI 兼容 LLM

## 目录

```
bootstrap.py              # 唯一组合根：Profile/Settings/Overrides + 依赖装配
version.py                # 版本号
config/                   # settings / profiles / overrides
domain/                   # models / ports / errors / 纯工具
ingestion/                # vault / frontmatter / chunking / index_service
retrieval/                # bm25 / vector / fusion / expansion / service
qa/                       # pipeline / multiturn / assistant / context / citation / rewrite / disco
├── prompts/              # answer / rewrite / summarize / disco（版本化 Markdown）
├── prompts.py            # PromptRegistry：严格渲染
└── trace.py              # TraceBuilder（应用层事件）
conversations/            # summarizer（会话摘要用例）
analytics/                # repository / visit_repository / archive / evaluation
optimization/             # 触发、队列、评分、Overrides 应用（Agent 旁路）
api/                      # app / middleware / routes / schemas / presenters / runtime
web/                      # templates + static（4 个页面）
infra/                    # llm / embedding / disco_client / sqlite_* / index_files / jobs / trace_presenter / paths
cli/                      # service（start/stop/status/health）+ kbtool

server.py                 # 8000 线上薄 shim
server_auto.py            # 8001 自迭代试验
server_v3.py              # 8002 人工测试
scripts/                  # service.sh / kbtool / restart_*.sh / 评测 / 门禁 / E2E
auto_optimization/        # 数据目录：benchmark / overrides
tests/                    # pytest 全量测试 + 架构门禁
docs/                     # 设计文档
```

依赖门禁见 `tests/test_architecture.py`：

```
domain      仅标准库
application 只依赖 domain（禁止 fastapi / httpx / sqlite3 / api / infra）
infra       只实现端口（禁止 api / application / web）
api         禁止直接依赖 infra（组合根除外）
bootstrap   唯一允许同时 import 四层
```

## 启动

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env      # 按需修改
./scripts/service.sh start
```

也可以用原生命令：

```bash
./scripts/service.sh start|stop|restart|status|health [--skip-daemon] [--wait N]
# 等价于：
.venv/bin/python -m cli.service start|stop|restart|status|health
```

端口：

| 端口 | 用途 | Profile |
|---|---|---|
| 8000 | 线上 | prod |
| 8001 | 自迭代试验 | experiment |
| 8002 | 人工测试 | test |

单独启动：

```bash
RAG_PORT=8002 .venv/bin/python server_v3.py
```

`scripts/` 下提供统一服务入口 `service.sh`（start/stop/restart/status/health）、`kbtool`、单端口重启脚本 `restart_8000.sh` / `restart_8001.sh` / `restart_8002.sh`（PID 文件 + health，不用 lsof/pkill），以及评测、发布门禁与 E2E 脚本。

## 前置依赖

1. **Ollama**：`ollama serve`，准备 `nomic-embed-text`（向量）与 `qwen3:4B-Instruct`（查询改写，`OLLAMA_CHAT_MODEL` 可覆盖）。
2. **LLM**：`.env` 或环境变量配置 `LLM_API_STYLE` / `LLM_BASE_URL` / `LLM_API_KEY`（或 `ANTHROPIC_AUTH_TOKEN`）。
3. **知识库**：默认 `vault/`（本仓库为外部 Markdown 库软链接），可用 `RAG_VAULT_PATH` 覆盖。

## 数据目录

```
var/
├── db/sessions.db        # 会话
├── db/analytics.db       # 评估 + 访问
└── index/                # 索引快照
logs/                     # 服务与守护进程日志
qa_archive/               # 问答归档
auto_optimization/logs/   # 优化队列、历史、备份、审计
```

均被 git 忽略；可用 `RAG_SESSION_DB` / `RAG_EVAL_DB` / `RAG_INDEX_PATH` 覆盖。

## 配置与 Prompt

- `config/settings.py`：环境变量 → frozen `Settings`；
- `config/profiles.py`：prod / experiment / test；
- `config/overrides.py`：`OverridesStore`，白名单参数 + `prompt_rule`，带缓存 / 备份 / 审计 / 回滚；
- 运行期唯一读取点：`bootstrap.OVERRIDES`；`get_param()` 热读取，`effective_prompt()` 只追加规则。
- `qa/prompts/*.md` 带 `version` frontmatter；`PromptRegistry.render()` 缺失/未知变量直接报错；生成步骤 trace 中带 `prompt answer@N`。

## 测试

```bash
make test          # 全量测试（181 项）
make arch          # 架构门禁
make snapshot      # 更新单轮快照
make gate          # 测试 + 架构 + 启动健康检查
make e2e           # Chrome headless 页面冒烟
```

CI：`.github/workflows/test.yml`。

## 主要接口

| 接口 | 说明 |
|---|---|
| `POST /ask` | 单轮问答 |
| `POST /ask_smart` | 多轮问答 |
| `POST /ask_eval` | 问答 + 评估 |
| `POST /ask_smart_disco` | 检索 + 话语分析 |
| `POST /v1/chat/completions` | OpenAI 兼容 |
| `GET/POST/PATCH/DELETE /api/sessions/*` | 会话管理 |
| `GET /api/eval/*`、`/api/visits` | 评估与访问 |
| `GET /health`、`/reindex*` | 运维 |

页面：`/`、`/chat`、`/single`、`/eval`、`/analysis`。

## 工具

```bash
./scripts/kbtool --help          # 等价于 python -m cli.kbtool
.venv/bin/python -m cli.kbtool --help
.venv/bin/python -m optimization.agent.trigger --list-failures
.venv/bin/python -m optimization.agent.daily_benchmark_guard --check
```

会写队列、改 overrides 或重启服务的命令（`--process-queue`、`--incident`、
`--daily-patrol`、`daily_benchmark_guard`）请先确认环境与授权。

## 文档

- `docs/architecture-review.html`
- `docs/detailed-design.html`
- `docs/agent-framework-comparison.html`
- `docs/multi-turn-design.md`
- `docs/multiturn-incident-20261117.md`（“长征”会话多轮退化诊断与整改方案）
