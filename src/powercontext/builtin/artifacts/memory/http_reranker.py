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

"""Cross-encoder reranking delegated to an HTTP scoring service.

The scoring service has a hard batch limit, so one query is split into several
requests. Splitting is the easy part; what makes the result trustworthy is that
the split never leaks into the ranking:

* a candidate keeps the rank it had in the **complete** pool, so a score that
  comes back in a later batch still maps onto the original Memory hit;
* every response must cover its own batch exactly once, which turns a lazy or
  buggy service into a loud ``ValueError`` instead of a silently short list;
* the final order is computed once, over all batches, which is what lets a
  high-scoring candidate from the last batch overtake the first batch.

Reranking only selects and orders. Text, Memory artifact, entry id and entry
version id travel through untouched, so a caller can still resolve every
citation back to the exact record the score was computed for.
"""

from __future__ import annotations

import json
import math
from typing import Any, Sequence

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"
DEFAULT_HTTP_RERANK_BATCH_SIZE = 32


class HTTPMemoryReranker:
    """Score Memory candidates with a batched HTTP cross-encoder.

    The client is supplied by the caller and never opened, closed or reconfigured
    here: retries, timeouts, proxies and transports stay a deployment decision.
    """

    def __init__(
        self,
        endpoint: str,
        /,
        *,
        client: httpx.AsyncClient,
        batch_size: int = DEFAULT_HTTP_RERANK_BATCH_SIZE,
        policy_id: str = DEFAULT_HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("reranker endpoint must be a non-empty string")  # noqa: TRY003
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("reranker batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("reranker policy_id must be a non-empty string")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: Sequence[MemoryHit],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return validated original ranks ordered by the service scores.

        ``limit`` bounds the selection, not the work: every candidate is scored,
        because a candidate that looks weak in the coarse ranking can be the best
        answer once the cross-encoder has read the query alongside it.
        """

        if not isinstance(query, str):
            raise ValueError("reranker query must be a string")  # noqa: TRY003
        hits = tuple(candidates)
        _validate_bounds(len(hits), limit)

        scores: dict[int, float] = {}
        requests = 0
        for batch in _batches(len(hits), self._batch_size):
            payload = {
                "query": query,
                "candidates": [{"rank": rank, "text": hits[rank - 1].text} for rank in batch],
            }
            response = await self._client.post(self._endpoint, json=payload)
            requests += 1
            response.raise_for_status()
            scores.update(_parse_scores(response, expected_ranks=batch))

        # Descending score with the original rank as the tie-break, so two equally
        # scored candidates keep the order the coarse search produced for them.
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for rank, _score in ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _batches(candidate_count: int, batch_size: int) -> tuple[tuple[int, ...], ...]:
    """Split 1-based global ranks into contiguous service-sized batches."""

    return tuple(
        tuple(range(start, min(start + batch_size, candidate_count + 1)))
        for start in range(1, candidate_count + 1, batch_size)
    )


def _parse_scores(
    response: httpx.Response,
    *,
    expected_ranks: tuple[int, ...],
) -> dict[int, float]:
    """Validate one response and return its ranks mapped to finite scores.

    A response is only usable when it answers exactly the ranks that were asked
    for. Missing ranks would silently shrink the pool, a repeated rank would let
    one candidate vote twice, and a rank from another batch would map a score onto
    a candidate the service never read.
    """

    try:
        payload: Any = json.loads(response.text)
    except ValueError as error:
        raise ValueError("reranker response is not valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError('reranker response must contain exactly one "scores" field')  # noqa: TRY003
    records = payload["scores"]
    if not isinstance(records, list):
        raise ValueError('reranker "scores" must be a list')  # noqa: TRY003

    expected = set(expected_ranks)
    scores: dict[int, float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rank", "score"}:
            raise ValueError('each score record must contain exactly "rank" and "score"')  # noqa: TRY003
        rank = record["rank"]
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError("score rank must be an integer")  # noqa: TRY003
        if rank not in expected:
            raise ValueError(f"score rank {rank} was not requested in this batch")  # noqa: TRY003
        if rank in scores:
            raise ValueError(f"score rank {rank} appears more than once")  # noqa: TRY003
        score = record["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("score must be a number")  # noqa: TRY003
        numeric = float(score)
        # NaN compares false against everything and Infinity wins every sort, so
        # both would corrupt the global order without ever looking like an error.
        if not math.isfinite(numeric):
            raise ValueError("score must be a finite number")  # noqa: TRY003
        scores[rank] = numeric

    if set(scores) != expected:
        raise ValueError("reranker response must score every candidate in the batch exactly once")  # noqa: TRY003
    return scores


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("rerank limit must be an integer")  # noqa: TRY003
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


__all__ = [
    "DEFAULT_HTTP_RERANK_BATCH_SIZE",
    "DEFAULT_HTTP_RERANK_POLICY_ID",
    "HTTPMemoryReranker",
]
