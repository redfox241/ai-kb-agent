"""PR-08：Trace 事件、Presenter 与流水线追踪。"""
from __future__ import annotations

from domain import StepStatus, TraceEvent
from infra.trace_presenter import TracePresenter
from qa.trace import TraceBuilder, context_io, generation_io, io_item
from qa.pipeline import RagPipeline
from retrieval.bm25 import BM25Index
from retrieval.service import RetrievalService
from infra.index_files import chunk_from_dict
from ingestion.index_service import IndexSnapshot


class FakeEmbedder:
    model = "fake"

    def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    def embed_many(self, texts):
        return [self.embed(text) for text in texts]


def _chunks(state):
    return tuple(chunk_from_dict(chunk) for chunk in state["chunks"])


def _snapshot(state):
    chunks = _chunks(state)
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    return IndexSnapshot(
        version="test",
        chunks=chunks,
        embeddings=state["emb_matrix"],
        bm25=BM25Index.build([chunk.text for chunk in chunks]),
        summary_by_doc={
            doc: by_id[chunk["chunk_id"]]
            for doc, chunk in state["summary_by_doc"].items()
        },
        built_at="test",
        meta={},
    )


def _pipeline(state, llm):
    retrieval = RetrievalService(lambda: _snapshot(state), FakeEmbedder())
    return RagPipeline(retrieval, llm, system_prompt="SYS", max_tokens=8000)


def test_trace_builder_and_presenter_shape():
    builder = TraceBuilder(trace_id="tr_test")
    builder.add(
        "bm25",
        StepStatus.OK,
        summary="命中 2 条",
        detail="查询：测试",
        items=[{"title": "A"}],
        ms=8,
        ops=["分词", "打分"],
        io={"input": [io_item("q", "测试")]},
    )
    presenter = TracePresenter()
    steps = presenter.render(builder.trace)

    assert len(steps) == 1
    step = steps[0]
    assert set(step) == {
        "key", "title", "status", "icon", "summary", "detail",
        "items", "ms", "ops", "io",
    }
    assert step["key"] == "bm25"
    assert step["title"] == "BM25 关键词检索"
    assert step["status"] == "ok"
    assert step["icon"] == "✅"
    assert step["ms"] == 8
    assert step["io"]["input"][0]["label"] == "q"


def test_trace_presenter_status_icons():
    presenter = TracePresenter()
    for status, icon in (
        (StepStatus.OK, "✅"),
        (StepStatus.EMPTY, "⚠️"),
        (StepStatus.SKIP, "⏭️"),
        (StepStatus.ERROR, "❌"),
    ):
        step = presenter.render_event(TraceEvent(
            trace_id="tr",
            step="generate",
            status=status,
            started_at=0.0,
            duration_ms=1,
        ))
        assert step["icon"] == icon
        assert step["status"] == status.value


def test_pipeline_emits_legacy_step_keys(fake_index, fake_llm):
    pipeline = _pipeline(fake_index, fake_llm)
    answer = pipeline.answer("孙悟空是谁？")
    new_keys = [event.step for event in answer.trace]

    assert new_keys[0] == "accept"
    assert {"bm25", "semantic", "merge", "expand", "context", "generate", "cite"} <= set(new_keys)

    rendered = TracePresenter().render(answer.trace)
    assert [step["key"] for step in rendered] == new_keys
    for step in rendered:
        assert {"key", "title", "status", "icon", "summary", "detail",
                "items", "ms", "ops", "io"} <= set(step)


def test_context_and_generation_io_shape(fake_index):
    chunks = _chunks(fake_index)
    context = "[来源: 孙悟空 | summary | wiki/sources/孙悟空.md]"
    c_io = context_io("问题", chunks[:2], context)
    g_io = generation_io(
        "问题", context, "SYS", "USER", "fake", 8000,
        answer="答案", elapsed_ms=12, sources=[],
    )

    for io in (c_io, g_io):
        assert {"input", "pending", "result", "output"} <= set(io)
        assert all(isinstance(row, dict) for row in io["input"])
        assert all({"label", "value", "meta"} <= set(row) for row in io["output"])
