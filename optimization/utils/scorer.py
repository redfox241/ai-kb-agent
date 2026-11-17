"""评分器（v2 标准：准确40% + 相关40% + 完整20%）。"""
import re

from infra.llm import LLMClient, parse_json

MULTI_DIMENSIONS = [
    {"key": "accuracy", "name": "准确性", "weight": 0.4, "desc": "数据、年份、事实是否正确，来源是否可靠"},
    {"key": "relevance", "name": "相关性", "weight": 0.4, "desc": "是否回答了用户实际想问的问题"},
    {"key": "completeness", "name": "完整性", "weight": 0.2, "desc": "关键信息是否遗漏"},
    {"key": "clarity", "name": "表达清晰度", "weight": 0.0, "desc": "结构是否清晰、是否易于阅读（辅助维度）"},
    {"key": "sources", "name": "来源支撑", "weight": 0.0, "desc": "是否引用了知识库来源（辅助维度）"},
]

MULTI_SCORE_PROMPT = """你是知识库回答质量评估专家。基于用户问题与参考答案，从 5 个维度打分（每项 1-10）。

## 评分标准
- 准确性：数据正确性、年份正确性、事实一致性
  - 知识库中无此内容（回答明示"未找到"且确属知识盲区）→ 准确性上限 2 分
  - 数据/年份错误 → 准确性上限 4 分
- 相关性：是否针对用户问题作答，而非答非所问
- 完整性：关键信息是否覆盖（多要点问题是否有遗漏）
- 表达清晰度（辅助）：结构、条理、易读性
- 来源支撑（辅助）：是否标注了知识库来源

## 输出
严格输出 JSON，不要任何其他内容：
{"dimensions": {"accuracy": {"score": 8, "comment": "一句话"}, "relevance": {"score": 8, "comment": "一句话"}, "completeness": {"score": 7, "comment": "一句话"}, "clarity": {"score": 9, "comment": "一句话"}, "sources": {"score": 9, "comment": "一句话"}}, "total": 8.0, "reason": "一句话总结", "strengths": ["优点1"], "weaknesses": ["不足1"], "suggestion": "改进建议"}"""

SCORE_PROMPT = """你是知识库回答质量评估专家。基于用户问题与参考答案，对参考答案打分（1-10）。

## 评分标准（以信息交付为核心，不为格式、话术单独加分）
- 准确性（权重40%）：数据正确性、年份正确性、来源可靠性
  - 知识库中无此内容（回答明示"未找到"且确属知识盲区）→ 上限2分
  - 数据/年份错误 → 上限4分
- 相关性（权重40%）：是否回答了用户实际想问的问题
- 完整性（权重20%）：关键信息是否遗漏
- 格式不单独加分，仅同档微调（±0.5）

## 输出
严格输出 JSON，不要任何其他内容：
{"score": 8, "reason": "一句话说明给分理由", "strengths": ["优点1"], "weaknesses": ["不足1"], "suggestion": "改进建议"}"""


def _coerce_score(v):
    """接受 int/float/数字字符串，非法返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def score_answer(question, answer, llm=None, retries=3):
    """对问答对评分，返回结构化结果；失败重试后返回 None。"""
    llm = llm or LLMClient()
    prompt = (f"请对下面的【参考答案】评分。这是评分任务，不要回答用户问题本身，只输出评分 JSON。\n"
              f"## 用户问题\n{question}\n\n## 参考答案\n{answer[:3000]}\n")
    for _ in range(retries + 1):
        data = llm.chat_json(prompt, system=SCORE_PROMPT, max_tokens=1000)
        score = _coerce_score(data.get("score")) if isinstance(data, dict) else None
        if score is not None:
            return {
                "question": question,
                "answer": answer,
                "score": round(score, 1),
                "reason": str(data.get("reason", "")),
                "strengths": data.get("strengths") or [],
                "weaknesses": data.get("weaknesses") or [],
                "suggestion": str(data.get("suggestion", "")),
            }
    return None


def _norm_dim(dim, key):
    """规范化单个维度的评分结果，非法返回 None。"""
    if not isinstance(dim, dict):
        return None
    score = _coerce_score(dim.get("score"))
    if score is None:
        return None
    return {"key": key, "score": round(score, 1), "comment": str(dim.get("comment", "") or "")}


def score_answer_multi(question, answer, llm=None, retries=3):
    """多维评分：返回总分 + 各维度分数/评语。失败重试后返回 None。"""
    llm = llm or LLMClient()
    prompt = (f"请对下面的【参考答案】进行多维度评分。这是评分任务，不要回答用户问题本身，只输出评分 JSON。\n"
              f"## 用户问题\n{question}\n\n## 参考答案\n{answer[:3000]}\n")
    for _ in range(retries + 1):
        data = llm.chat_json(prompt, system=MULTI_SCORE_PROMPT, max_tokens=1200)
        if not isinstance(data, dict):
            continue
        raw_dims = data.get("dimensions")
        if not isinstance(raw_dims, dict):
            continue
        dims = {}
        for meta in MULTI_DIMENSIONS:
            d = _norm_dim(raw_dims.get(meta["key"]), meta["key"])
            if d is None:
                d = {"key": meta["key"], "score": 0, "comment": "未给出"}
            d["name"] = meta["name"]
            d["weight"] = meta["weight"]
            d["desc"] = meta["desc"]
            dims[meta["key"]] = d
        # 加权总分（仅统计 weight>0 的维度）
        w_sum = sum(d["weight"] for d in dims.values())
        total = sum(d["score"] * d["weight"] for d in dims.values()) / w_sum if w_sum else 0
        total = round(max(0, min(10, total)), 1)
        return {
            "question": question,
            "answer": answer,
            "total": total,
            "dimensions": dims,
            "reason": str(data.get("reason", "") or ""),
            "strengths": data.get("strengths") or [],
            "weaknesses": data.get("weaknesses") or [],
            "suggestion": str(data.get("suggestion", "") or ""),
        }
    return None


if __name__ == "__main__":
    import json
    q = sys.argv[1]
    a = sys.stdin.read() if len(sys.argv) < 3 else sys.argv[2]
    r = score_answer_multi(q, a)
    print(json.dumps(r, ensure_ascii=False, indent=2))
