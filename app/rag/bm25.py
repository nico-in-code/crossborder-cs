from __future__ import annotations

import math
import re
from collections import Counter

_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens: list[str] = []
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_WORD.findall(lowered))
    return tokens


class BM25:
    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.tokens = [tokenize(document) for document in documents]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.average_length = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.term_frequencies = [Counter(tokens) for tokens in self.tokens]
        self.document_frequencies: Counter[str] = Counter()
        for term_frequency in self.term_frequencies:
            self.document_frequencies.update(term_frequency.keys())
        self.total_documents = len(documents)

    def _idf(self, term: str) -> float:
        frequency = self.document_frequencies.get(term, 0)
        return math.log(
            1 + (self.total_documents - frequency + 0.5) / (frequency + 0.5)
        )

    def scores(self, query: str) -> list[float]:
        query_terms = tokenize(query)
        results: list[float] = []
        for index, term_frequency in enumerate(self.term_frequencies):
            score = 0.0
            length = self.lengths[index] or 1
            for term in query_terms:
                frequency = term_frequency.get(term, 0)
                if frequency == 0:
                    continue
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length / (self.average_length or 1.0)
                )
                score += self._idf(term) * frequency * (self.k1 + 1) / denominator
            results.append(score)
        return results

    def ranked_indices(self, query: str) -> list[int]:
        scored = list(enumerate(self.scores(query)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [index for index, score in scored if score > 0]