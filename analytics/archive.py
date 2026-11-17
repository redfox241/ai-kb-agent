"""问答归档与评分存储（参考设计文档 §2.6）。"""
import hashlib
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA_ARCHIVE_DIR = os.environ.get("AIKB_QA_ARCHIVE_DIR") or str(ROOT / "qa_archive")
Path(QA_ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
(Path(QA_ARCHIVE_DIR) / "stats").mkdir(parents=True, exist_ok=True)
STATS_FILE = Path(QA_ARCHIVE_DIR) / "stats" / "qa_stats.jsonl"
SCORES_FILE = Path(QA_ARCHIVE_DIR) / "stats" / "qa_auto_scores.json"
LABELS_FILE = Path(QA_ARCHIVE_DIR) / "stats" / "qa_labels.json"


def question_hash(question):
    return hashlib.md5(question.strip().lower().encode("utf-8")).hexdigest()


def save_qa_record(question, answer, sources=None, user_id="local", username="local",
                   chat_type="direct", port=None):
    """追加统计 + 保存归档文件，返回归档路径。"""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    date = time.strftime("%Y-%m-%d")
    hhmmss = time.strftime("%H%M%S")
    date_dir = Path(QA_ARCHIVE_DIR) / chat_type / f"{user_id}_{username}" / date
    date_dir.mkdir(parents=True, exist_ok=True)
    seq = len(list(date_dir.glob(f"{hhmmss}_*.md"))) + 1
    filepath = date_dir / f"{hhmmss}_{seq:03d}.md"

    body = [f"# Q&A", "", f"**时间**: {ts}", f"**端口**: {port or '-'}", "",
            "## 问题", question, "", "## 回答", answer, ""]
    if sources:
        body += ["## 来源", *[f"- {s.get('title','')} (`{s.get('path','')}`)" for s in sources], ""]
    filepath.write_text("\n".join(body), encoding="utf-8")

    record = {"ts": ts, "question": question, "answer": answer, "sources": sources or [],
              "user_id": user_id, "username": username, "chat_type": chat_type,
              "port": port, "filepath": str(filepath)}
    with open(STATS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(filepath)


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_score(question, answer, score, reason="", strengths=None, weaknesses=None,
                 suggestion="", port=None):
    """写入评分结果（key=问题 md5）。"""
    h = question_hash(question)
    scores = _load_json(SCORES_FILE, {})
    scores[h] = {
        "question": question, "answer": answer, "score": score, "reason": reason,
        "strengths": strengths or [], "weaknesses": weaknesses or [],
        "suggestion": suggestion, "port": port,
        "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_json(SCORES_FILE, scores)
    # 标签（看板用）：<6 低分 / 6-7 中分 / >=8 高分
    label = "low" if score < 6 else ("mid" if score < 8 else "high")
    labels = _load_json(LABELS_FILE, {})
    labels[h] = {"question": question, "label": label, "score": score,
                 "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_json(LABELS_FILE, labels)
    return h


def query_scores(min_score=None, max_score=None, limit=50, port=None):
    scores = _load_json(SCORES_FILE, {})
    rows = []
    for h, s in scores.items():
        if min_score is not None and s.get("score", 0) < min_score:
            continue
        if max_score is not None and s.get("score", 0) > max_score:
            continue
        if port and s.get("port") != port:
            continue
        rows.append({"hash": h, **s})
    rows.sort(key=lambda r: r.get("scored_at", ""), reverse=True)
    return rows[:limit]
