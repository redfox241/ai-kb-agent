"""轻量 BM25：与 server.py 的 tokenize / BM25Index 保持等价。"""
from __future__ import annotations

import re
from typing import Sequence

import numpy as np


_CJK_RE = re.compile(r"([一-鿿]+|[A-Za-z]+|\d{3,})")


def tokenize(text: str) -> list[str]:
    """连续中文（含 2-3 字 ngram）/ 英文单词 / 长数字。"""
    tokens: list[str] = []
    for match in _CJK_RE.finditer((text or "").lower()):
        token = match.group(1)
        if re.fullmatch(r"[一-鿿]+", token):
            length = len(token)
            tokens.append(token)
            for n in (2, 3):
                if length >= n:
                    tokens.extend(token[i:i + n] for i in range(length - n + 1))
        else:
            tokens.append(token)
    return tokens


class BM25Index:
    """轻量 BM25，k1=1.5, b=0.75。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_terms: list[list[str]] = []
        self.doc_lens: list[int] = []
        self.df: dict[str, int] = {}
        self.idf: dict[str, float] = {}
        self.avg_len = 0.0

    @classmethod
    def build(cls, texts: Sequence[str]) -> "BM25Index":
        index = cls()
        for text in texts:
            terms = tokenize(text)
            index.doc_terms.append(terms)
            index.doc_lens.append(len(terms))
            for term in set(terms):
                index.df[term] = index.df.get(term, 0) + 1

        count = len(texts)
        index.avg_len = (sum(index.doc_lens) / count) if count else 0.0
        index.idf = {
            term: np.log(1 + (count - freq + 0.5) / (freq + 0.5))
            for term, freq in index.df.items()
        }
        return index

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        query_terms = tokenize(query)
        if not query_terms or not self.doc_terms:
            return []

        query_freq: dict[str, int] = {}
        for term in query_terms:
            query_freq[term] = query_freq.get(term, 0) + 1

        scores: list[tuple[int, float]] = []
        for index, terms in enumerate(self.doc_terms):
            if not terms:
                continue
            doc_freq: dict[str, int] = {}
            for term in terms:
                doc_freq[term] = doc_freq.get(term, 0) + 1

            score = 0.0
            for term, qf in query_freq.items():
                freq = doc_freq.get(term, 0)
                if freq == 0:
                    continue
                idf = self.idf.get(term, 0.0)
                denominator = freq + self.k1 * (
                    1 - self.b + self.b * self.doc_lens[index] / self.avg_len
                )
                score += idf * qf * (freq * (self.k1 + 1)) / denominator
            scores.append((index, score))

        scores.sort(key=lambda item: -item[1])
        return [(index, score) for index, score in scores[:top_k] if score > 0]
