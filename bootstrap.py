"""AI-KB 组合根与启动实现。

- API 路由、页面、异常处理：api/
- 领域/配置/摄入/检索/问答/会话/评估：各同名模块
- 自优化子系统：optimization/
- 本文件只负责：环境配置、依赖装配、运行时注入、uvicorn 启动

server.py (8000) / server_auto.py (8001) / server_v3.py (8002) 共用本文件，仅 profile/端口不同。
"""
import os

from config.overrides import OverridesStore
from config.profiles import PROFILES, Profile
from config.settings import Settings
from infra.paths import load_env
from infra.embedding import get_embedding, get_embeddings
from infra.llm import LLMClient
from infra.ollama_chat import OllamaChatClient
from infra.disco_client import DISCO_TOP_N, disco_analyze

from api.app import create_app
from api.presenters import chat_result_to_legacy
from api.routes.admin import AdminRuntime
from api.routes.disco import DiscoRuntime
from api.runtime import AskRuntime
from infra.sqlite_sessions import SqliteSessionRepository
from domain.models import AnswerOptions
from analytics import evaluation as evaluation_service
from analytics import repository as analytics_repository
from analytics.service import AnalyticsService
from infra.index_files import FileIndexStore
from infra.jobs import ThreadJobRunner
from infra.sqlite_analytics import SqliteAnalyticsRepository
from infra.trace_presenter import TracePresenter
from ingestion.index_service import IndexService
from qa.assistant import AssistantService
from qa.disco import DiscoService
from qa.multiturn import ChatService
from qa.prompts import default_registry
from qa.pipeline import RagPipeline
from qa.rewrite import NativeRewriter
from retrieval.bm25 import BM25Index as NewBM25Index, tokenize
from retrieval.service import RetrievalService

load_env()

_PROFILE_BY_PORT = {profile.port: profile for profile in PROFILES.values()}


def _profile_for(port: int) -> Profile:
    return _PROFILE_BY_PORT.get(int(port), PROFILES["prod"])


def _initial_profile() -> Profile:
    name = (os.environ.get("RAG_PROFILE") or "").strip()
    if name in PROFILES:
        return PROFILES[name]
    raw_port = (os.environ.get("RAG_PORT") or "").strip()
    return _profile_for(int(raw_port)) if raw_port.isdigit() else PROFILES["prod"]


PROFILE = _initial_profile()
SETTINGS = Settings.from_env(PROFILE)
PORT = int(os.environ.get("RAG_PORT") or PROFILE.port)

# ---------- 多轮开关（默认开启；session_id 缺省时仍走单轮路径） ----------
MULTITURN_ENABLED = SETTINGS.multiturn.enabled
REWRITE_MODE = SETTINGS.multiturn.rewrite_mode
MAX_HISTORY_TURNS = SETTINGS.multiturn.max_history_turns

# ---------- 自动优化注入点（overrides） ----------
# 唯一读取路径：config.OverridesStore（白名单 / 缓存 / 审计 / 回滚）
OVERRIDES_PATH = os.environ.get("RAG_OVERRIDES") or str(PROFILE.overrides_path)
OVERRIDES = OverridesStore(OVERRIDES_PATH)


def get_param(name, default):
    return OVERRIDES.param(name, default)


def effective_prompt():
    rule = OVERRIDES.prompt_rule()
    return SYSTEM_PROMPT + (f"\n\n## 补充规则（自动注入）\n{rule}" if rule else "")


# ---------- 新链路适配 ----------

class _ServerEmbedder:
    model = SETTINGS.embed.model

    def embed(self, text):
        return get_embedding(text)

    def embed_many(self, texts):
        return get_embeddings(texts)


_JOB_RUNNER = ThreadJobRunner(max_workers=SETTINGS.jobs.max_workers)


def _build_rewrite_llm():
    """查询改写默认走本地 Qwen；LLM_REWRITE_PROVIDER=main 可回退主模型。"""
    provider = (os.environ.get("LLM_REWRITE_PROVIDER") or "ollama").strip().lower()
    if provider in {"main", "off", "none", "disabled", "deepseek"}:
        return None
    try:
        client = OllamaChatClient()
        print(
            f"[startup] rewrite llm: ollama {client.model} @ {client.url}",
            flush=True,
        )
        return client
    except Exception as exc:
        print(f"[startup] rewrite llm disabled: {exc}", flush=True)
        return None


REWRITE_LLM = _build_rewrite_llm()
print(
    f"[startup] main llm: {SETTINGS.llm.model} @ {SETTINGS.llm.base_url}",
    flush=True,
)

# 分析仓储：唯一实例，评估与访问共享。
_ANALYTICS_REPO = SqliteAnalyticsRepository(
    os.environ.get("RAG_EVAL_DB") or str(SETTINGS.db.analytics)
)
_ANALYTICS_REPO.migrate()
analytics_repository.configure(_ANALYTICS_REPO)
evaluation_service.configure(llm_factory=lambda: LLMClient(), jobs=_JOB_RUNNER)


def _build_pipeline(llm):
    retrieval = RetrievalService(
        lambda: _index_service().current(),
        _ServerEmbedder(),
        similarity_threshold=get_param("SIMILARITY_THRESHOLD", SIMILARITY_THRESHOLD),
    )
    return RagPipeline(
        retrieval,
        llm,
        system_prompt=effective_prompt,
        max_tokens=SETTINGS.llm.max_tokens,
        max_context_chunks=int(get_param("MAX_CONTEXT_CHUNKS", MAX_CONTEXT_CHUNKS)),
        max_sources=int(get_param("MAX_SOURCES", MAX_SOURCES)),
        max_prompt_tokens=SETTINGS.multiturn.max_prompt_tokens,
        max_history_turns=int(get_param("MAX_HISTORY_TURNS", MAX_HISTORY_TURNS)),
        followup_suggest=os.environ.get("FOLLOWUP_SUGGEST", "1") != "0",
        prompt_version=f"answer@{default_registry().version('answer')}",
    )


def _session_repo():
    db_path = os.environ.get("RAG_SESSION_DB") or str(SETTINGS.db.sessions)
    repo = SqliteSessionRepository(db_path)
    repo.migrate()
    return repo


def _build_chat_service(llm):
    pipeline = _build_pipeline(llm)
    sessions = _session_repo()

    return ChatService(
        pipeline,
        sessions,
        NativeRewriter(),
        jobs=_JOB_RUNNER,
        max_history_turns=int(get_param("MAX_HISTORY_TURNS", MAX_HISTORY_TURNS)),
        repeat_penalty=float(get_param("REPEAT_PENALTY", SETTINGS.multiturn.repeat_penalty)),
        summary_every=int(get_param("SUMMARY_EVERY_TURNS", SETTINGS.multiturn.summary_every_turns)),
        rewrite_llm=REWRITE_LLM,
    )


def _chat_v2(question, llm, session_id=None, history=None,
             use_rewrite=True, channel=None):
    result = chat_result_to_legacy(
        _build_chat_service(llm).chat(
            question,
            llm,
            session_id=session_id,
            history=history,
            use_rewrite=use_rewrite,
            channel=PORT if channel is None else channel,
            rewrite_mode=REWRITE_MODE,
        ),
        TracePresenter().render,
    )
    result["port"] = PORT
    return result


# ---------- 检索参数 ----------
CHUNK_SIZE = SETTINGS.ingestion.chunk_size
CHUNK_OVERLAP = SETTINGS.ingestion.chunk_overlap
MAX_CONTEXT_CHUNKS = get_param("MAX_CONTEXT_CHUNKS", SETTINGS.retrieval.max_context_chunks)
SIMILARITY_THRESHOLD = float(
    get_param("SIMILARITY_THRESHOLD", SETTINGS.retrieval.similarity_threshold)
)
MAX_SOURCES = int(get_param("MAX_SOURCES", SETTINGS.retrieval.max_sources))
VAULT_PATH = str(SETTINGS.vault_path)
INDEX_PATH = str(SETTINGS.index_path)

_INDEX_SERVICE = None


def _index_service():
    global _INDEX_SERVICE
    if _INDEX_SERVICE is None:
        _INDEX_SERVICE = IndexService(
            VAULT_PATH,
            INDEX_PATH,
            _ServerEmbedder(),
            store=FileIndexStore(),
            bm25_factory=NewBM25Index.build,
            batch_size=SETTINGS.ingestion.batch_size,
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )
    return _INDEX_SERVICE


class _AdminState:
    """把 IndexService 适配成 admin 路由需要的只读 state。"""

    @property
    def status(self):
        return _index_service().status.value

    @property
    def chunks(self):
        try:
            return _index_service().current().chunks
        except Exception:
            return ()

    @property
    def built_at(self):
        try:
            return _index_service().current().built_at
        except Exception:
            return None

    @property
    def error(self):
        return _index_service().error

    @property
    def reindex(self):
        progress = _index_service().progress
        return {
            "status": progress.status,
            "total": progress.total,
            "done": progress.done,
            "current_file": progress.current_file,
            "level": progress.level,
            "failed": progress.failed,
            "failed_chunks": list(progress.failed_chunks),
        }

EXCLUDE_DIRS = {
    ".obsidian", ".rag", ".claude", ".git", "assets", "copilot", "schema",
    "raw/archive", "__pycache__", ".trash", "templates", "_导入报告", "attachments",
}
EXCLUDE_FILE_PREFIXES = ("session-", "article-", "handoff")
EXCLUDE_FILES = {"opencode.json"}

BOOST_PREFIXES = ("wiki/", "knowledge/")  # 整理后的知识优先级更高

SYSTEM_PROMPT = default_registry().body("answer")



def _answer_question_v2(question, llm, use_rewrite=True):
    answer = AssistantService(_build_pipeline(llm)).answer(
        question, AnswerOptions(rewrite=use_rewrite)
    )
    sources = [
        {"title": source.title, "path": source.path, "type": source.kind}
        for source in answer.sources
    ]
    return answer.text, sources, TracePresenter().render(answer.trace)


def answer_question(question, llm, use_rewrite=True):
    """单轮问答：统一走新流水线。"""
    return _answer_question_v2(question, llm, use_rewrite=use_rewrite)


def record_query_visit(question, endpoint, sources, steps, latency_ms, status="ok"):
    """问答访问落库（失败不影响主流程）。"""
    try:
        from analytics.visit_repository import record_visit
        record_visit(kind="query", question=question, endpoint=endpoint,
                     n_sources=len(sources or []), n_steps=len(steps or []),
                     latency_ms=latency_ms, status=status, port=PORT)
    except Exception as e:
        print(f"[visit] 记录失败: {e}", flush=True)


# ---------- FastAPI ----------

app = create_app(
    PROFILE,
    port=PORT,
    start=lambda: _startup(),
    stop=lambda: _JOB_RUNNER.shutdown(wait=False),
)
app.state.session_repo = _session_repo
app.state.analytics = AnalyticsService(_ANALYTICS_REPO)


def _run_multiturn(question, llm, session_id, use_rewrite=True, reset=False):
    """有会话的多轮问答：重置 → 编排 → 补端口字段。"""
    if reset:
        _session_repo().reset(session_id)

    return _chat_v2(question, llm, session_id=session_id,
                    use_rewrite=use_rewrite, channel=PORT)


# ---------- 会话 cookie 兜底（客户端漏传 session_id 时仍能续上） ----------
# cookie 名带端口：同一主机的 8000/8002/8001 各自隔离，不串会话。
SESSION_COOKIE = f"kb_session_{PORT}"
SESSION_COOKIE_MAX_AGE = 180 * 24 * 3600


def _resolve_session(request, explicit=None):
    """显式 session_id > cookie > 新建。返回可用的 session_id。"""
    repo = _session_repo()
    if explicit and repo.valid_session_id(explicit):
        return explicit
    sid = request.cookies.get(SESSION_COOKIE)
    if sid and repo.valid_session_id(sid) and repo.get(sid):
        return sid
    return repo.create(channel=PORT).session_id


def _bind_session_cookie(response, session_id):
    response.set_cookie(SESSION_COOKIE, session_id, max_age=SESSION_COOKIE_MAX_AGE,
                        path="/", samesite="lax", httponly=True)


# ---------- API 运行时 ----------


def _log_eval(question, answer, latency_ms):
    from analytics.evaluation import log_eval_async
    log_eval_async(question, answer, port=PORT, latency_ms=latency_ms)


def _evaluate(question, answer, latency_ms):
    from analytics.evaluation import evaluate_and_record
    return evaluate_and_record(question, answer, port=PORT, latency_ms=latency_ms)


def _build_ask_runtime():
    return AskRuntime(
        port=PORT,
        multiturn_enabled=MULTITURN_ENABLED,
        run_multiturn=_run_multiturn,
        chat_v2=_chat_v2,
        answer_question=answer_question,
        max_history_turns=MAX_HISTORY_TURNS,
        resolve_session=_resolve_session,
        bind_cookie=_bind_session_cookie,
        record_visit=record_query_visit,
        log_eval=_log_eval,
        evaluate=_evaluate,
        llm_factory=lambda: LLMClient(),
    )


app.state.ask_runtime = _build_ask_runtime()
app.state.admin_runtime = AdminRuntime(
    port=PORT,
    state=_AdminState(),
    vault_path=VAULT_PATH,
    index_path=INDEX_PATH,
    rebuild=lambda: _index_service().rebuild(background=True),
)
def _build_disco_service():
    llm = LLMClient()
    return DiscoService(
        _build_pipeline(llm),
        llm,
        lambda question, chunks: disco_analyze(question, chunks),
        system_prompt=effective_prompt,
        top_n=DISCO_TOP_N,
        max_sources=int(get_param("MAX_SOURCES", MAX_SOURCES)),
        max_tokens=SETTINGS.llm.max_tokens,
    )


app.state.disco_runtime = DiscoRuntime(
    port=PORT,
    service_factory=_build_disco_service,
    render_trace=TracePresenter().render,
    record_visit=record_query_visit,
    log_eval=_log_eval,
)


# ---------- 启动 ----------


_STARTED = False


def _startup(force: bool = False) -> None:
    """生命周期启动：migrate / purge / index load / jobs start（幂等）。"""
    global _STARTED
    if _STARTED and not force:
        return
    _STARTED = True
    try:
        _ANALYTICS_REPO.migrate()
    except Exception as exc:
        print(f"[startup] 分析库迁移失败: {exc}", flush=True)

    try:
        purged = _session_repo().purge()
        if purged:
            print(f"[startup] 清理过期会话 {purged} 个", flush=True)
    except Exception as exc:
        print(f"[startup] 会话清理失败: {exc}", flush=True)

    service = _index_service()
    if not service.load():
        service.rebuild(background=True)
        print("[startup] 索引后台构建中，/reindex_status 查看进度", flush=True)
    else:
        snapshot = service.current()
        print(f"[startup] 索引已加载: {snapshot.built_at} {len(snapshot.chunks)} chunks", flush=True)

    _JOB_RUNNER.start()


def run_server(port=None):
    """统一启动入口：server.py / server_v3.py / server_auto.py 共用。"""
    import uvicorn

    _startup(force=True)
    uvicorn.run(app, host="127.0.0.1", port=int(port or PORT), log_level="info")


if __name__ == "__main__":
    run_server()
