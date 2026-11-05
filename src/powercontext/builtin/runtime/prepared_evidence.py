# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Query-aware, literal Memory passages measured in their complete wire envelope."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import TYPE_CHECKING

from powercontext.builtin.artifacts.search import analyze_fts_query, analyze_text
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

if TYPE_CHECKING:
    from powercontext.builtin.runtime.prepared_context import MemoryEntryAddress

_BOUNDARY = re.compile(r"[。！？]|[.!?]+[\"'”’\])]*(?=\s|$)|[\r\n]+")  # noqa: RUF001
_BEGIN = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"


@dataclass(frozen=True)
class PreparedContextFragment:
    """An unchanged, half-open character range in an exact Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Sentence:
    document: int
    start: int
    end: int
    terms: tuple[str, ...]


@dataclass(frozen=True)
class _Selection:
    indices: frozenset[int]
    fragments: tuple[PreparedContextFragment, ...]
    content: str
    size: int
    value: float


def select_memory_evidence(
    query: str, documents: Sequence[PreparedContextFragment], max_bytes: int
) -> tuple[tuple[PreparedContextFragment, ...], str | None, int]:
    """Pack query-bearing sentences and adjacent context without modifying source text.

    Sentence BM25, query coverage and adjacent query terms supply lexical relevance. A small
    beam compares whole passages against the *rendered* budget, including citation overhead.
    Joining adjacent ranges avoids paying for multiple citations to one contiguous passage.
    No model, corpus-specific identifiers, labels or external state participate in selection.
    """
    sentences = tuple(
        _Sentence(index, start, end, _terms(document.text[start:end]))
        for index, document in enumerate(documents)
        for start, end in _sentence_ranges(
            document.text,
            _payload_budget(document, max_bytes),
        )
    )
    if not sentences:
        return (), None, len(documents)
    weights = _relevance(query, sentences)
    # Identical repeated sentences within an entry do not supply additional evidence.
    seen: set[tuple[int, str]] = set()
    for index, sentence in enumerate(sentences):
        key = (sentence.document, " ".join(documents[sentence.document].text[sentence.start : sentence.end].split()))
        if key in seen:
            weights[index] = 0.0
        seen.add(key)
    anchors = sorted((index for index, value in enumerate(weights) if value > 0), key=lambda i: (-weights[i], i))
    if not anchors:
        # Retrieval has already admitted these candidates. With no lexical overlap, retain
        # its first candidate rather than inventing a semantic match to unrelated material.
        anchors = [0]
        weights[0] = 1.0

    fragments, content = _pack(
        documents, sentences, weights, _actions(documents, sentences, weights, anchors), max_bytes
    )
    eligible = {sentences[index].document for index in anchors}
    dropped = sum(all(fragment.address != documents[index].address for fragment in fragments) for index in eligible)
    return fragments, content, dropped


def _payload_budget(document: PreparedContextFragment, max_bytes: int) -> int:
    # The largest possible offsets bound metadata even for a passage at the end of a long entry.
    empty = replace(document, start=len(document.text), end=len(document.text), text="")
    return min(2048, max_bytes - len(_render((empty,)).encode("utf-8")))


def _actions(
    documents: Sequence[PreparedContextFragment],
    sentences: Sequence[_Sentence],
    weights: Sequence[float],
    anchors: Sequence[int],
) -> list[frozenset[int]]:
    actions: set[frozenset[int]] = set()
    for index in anchors[:48]:
        actions.add(frozenset((index,)))
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(sentences) and sentences[neighbor].document == sentences[index].document:
                actions.add(frozenset((index, neighbor)))
    # One action can deliver several useful sentences without imposing a fragment-count cap.
    for document_index in range(len(documents)):
        relevant = frozenset(index for index in anchors if sentences[index].document == document_index)
        if relevant:
            actions.add(relevant)
    return sorted(actions, key=lambda action: (-sum(weights[i] for i in action), tuple(sorted(action))))


def _pack(
    documents: Sequence[PreparedContextFragment],
    sentences: Sequence[_Sentence],
    weights: Sequence[float],
    actions: Sequence[frozenset[int]],
    max_bytes: int,
) -> tuple[tuple[PreparedContextFragment, ...], str | None]:

    measured: dict[frozenset[int], _Selection | None] = {}

    def measure(indices: frozenset[int]) -> _Selection | None:
        if indices not in measured:
            fragments = _fragments(documents, sentences, indices)
            content = _render(fragments)
            size = len(content.encode("utf-8"))
            measured[indices] = (
                _Selection(indices, fragments, content, size, sum(weights[index] for index in indices))
                if size <= max_bytes
                else None
            )
        return measured[indices]

    beam: list[_Selection] = []
    best: _Selection | None = None
    while True:
        trials = {item.indices: item for item in beam}
        bases = [item.indices for item in beam] or [frozenset()]
        for base in bases:
            for action in actions:
                indices = base | action
                selection = measure(indices)
                if selection is not None:
                    trials[indices] = selection
        if not trials:
            break
        ranked = sorted(trials.values(), key=lambda item: (-item.value, item.size, tuple(sorted(item.indices))))
        if best is not None and ranked[0].value <= best.value:
            break
        best = ranked[0]
        # Keep both strong and inexpensive alternatives: the latter can afford another
        # citation, and a neighboring sentence can be much cheaper than a new passage.
        economical = sorted(ranked, key=lambda item: (-item.value / item.size**0.65, item.size))
        beam = list({item.indices: item for item in (*ranked[:6], *economical[:6])}.values())
    if best is None:
        return (), None
    return best.fragments, best.content


def _relevance(query: str, sentences: Sequence[_Sentence]) -> list[float]:
    query_terms = tuple(_stem(term) for term in analyze_fts_query(query).split())
    query_set = set(query_terms)
    document_terms: dict[int, set[str]] = {}
    for sentence in sentences:
        document_terms.setdefault(sentence.document, set()).update(sentence.terms)
    # Chunks are not independent documents: repetition inside a long entry must not make
    # its identifying terms less informative than a stray word in a distractor entry.
    frequencies = Counter(term for terms in document_terms.values() for term in terms & query_set)
    idf = {
        term: math.log1p((len(document_terms) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
        for term in sorted(query_set)
    }
    total_weight = sum(idf.values()) or 1.0
    pairs = set(pairwise(query_terms))
    scores = []
    for sentence in sentences:
        counts = Counter(sentence.terms)
        matched = sorted(counts.keys() & query_set)
        coverage = sum(idf[term] for term in matched) / total_weight
        # The wire budget already charges passage length. Applying document-length
        # normalization again would prefer terse mentions over full explanatory sentences.
        score = sum(idf[term] * counts[term] * 2.2 / (counts[term] + 1.2) for term in matched)
        proximity = len(pairs & set(pairwise(sentence.terms))) / max(1, len(pairs))
        document_coverage = (
            sum(idf[term] for term in sorted(query_set & document_terms[sentence.document])) / total_weight
        )
        scores.append(score * (0.5 + coverage) * (1 + 0.25 * document_coverage + 0.25 * proximity))
    return scores


def _terms(text: str) -> tuple[str, ...]:
    return tuple(_stem(term) for term in analyze_text(text).split())


def _stem(term: str) -> str:
    """Light English morphology; keep numbers, identifiers and Unicode terms intact."""
    if len(term) < 4 or not term.isascii() or not term.isalpha():
        return term
    if term.endswith("ies") and len(term) > 4:
        term = term[:-3] + "y"
    elif term.endswith(("sses", "ches", "shes", "xes", "zes")):
        term = term[:-2]
    elif term.endswith("s") and not term.endswith(("ss", "us", "is")):
        term = term[:-1]
    term = _past_or_progressive(term)
    for suffix, replacement in (
        ("ational", "ate"),
        ("tional", "tion"),
        ("ation", "ate"),
        ("iveness", "ive"),
        ("fulness", "ful"),
        ("ousness", "ous"),
        ("ment", ""),
        ("ity", ""),
        ("ive", ""),
        ("ly", ""),
    ):
        if term.endswith(suffix) and len(term) > len(suffix) + 3:
            term = term[: -len(suffix)] + replacement
            break
    if term.endswith("e") and len(term) > 4:
        term = term[:-1]
    return term[:-1] + "i" if term.endswith("y") else term


def _past_or_progressive(term: str) -> str:
    for suffix in ("ingly", "edly", "ing", "ed"):
        if term.endswith(suffix) and len(term) > len(suffix) + 3:
            term = term[: -len(suffix)]
            if len(term) > 3 and term[-1] == term[-2] and term[-1] in "bdgmnprt":
                term = term[:-1]
            break
    return term


def _sentence_ranges(text: str, chunk_bytes: int) -> tuple[tuple[int, int], ...]:
    if chunk_bytes < 6:
        return ()
    sizes = [
        6 if char in "\u0085\u2028\u2029" else len(json.dumps(char, ensure_ascii=False).encode("utf-8")) - 2
        for char in text
    ]
    boundaries = [0, *(match.end() for match in _BOUNDARY.finditer(text)), len(text)]
    ranges: list[tuple[int, int]] = []
    for start, end in pairwise(boundaries):
        # Split exceptionally long sentences on words, retaining a character offset into
        # the original string. Adjacent pieces can subsequently be joined losslessly.
        while start < end:
            stop = start
            size = 0
            while stop < end and size + sizes[stop] <= chunk_bytes:
                size += sizes[stop]
                stop += 1
            if stop < end:
                space = text.rfind(" ", start, stop)
                if space > start:
                    stop = space + 1
            left, right = start, stop
            while left < right and text[left].isspace():
                left += 1
            while right > left and text[right - 1].isspace():
                right -= 1
            if left < right:
                ranges.append((left, right))
            start = stop
    return tuple(ranges)


def _fragments(
    documents: Sequence[PreparedContextFragment], sentences: Sequence[_Sentence], indices: frozenset[int]
) -> tuple[PreparedContextFragment, ...]:
    fragments: list[PreparedContextFragment] = []
    previous_document = -1
    for index in sorted(indices):
        sentence = sentences[index]
        document = documents[sentence.document]
        start, end = sentence.start, sentence.end
        if sentence.document == previous_document and not document.text[fragments[-1].end : start].strip():
            start = fragments.pop().start
        fragments.append(replace(document, start=start, end=end, text=document.text[start:end]))
        previous_document = sentence.document
    return tuple(fragments)


def _render(fragments: Sequence[PreparedContextFragment]) -> str:
    items = [
        {
            "citation": {
                "scope_id": fragment.address.memory.scope_id,
                "memory_ref": fragment.address.memory.artifact.model_dump(mode="json"),
                "entry_id": fragment.address.entry_id,
                "entry_version_id": fragment.address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }
        for fragment in fragments
    ]
    encoded = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    for separator in "\u0085\u2028\u2029":
        encoded = encoded.replace(separator, f"\\u{ord(separator):04x}")
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN}\n{encoded}\n{_END}"))
