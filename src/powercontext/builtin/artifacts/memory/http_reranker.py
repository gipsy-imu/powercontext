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

"""HTTP scoring over bounded batches of identity-preserving Memory candidates."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

__all__ = ["HTTPMemoryReranker"]


class _ScoredRank(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    rank: int = Field(ge=1)
    score: int | FiniteFloat


class _BatchScores(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    scores: list[_ScoredRank]


def _read_scores(response: httpx.Response, expected: set[int]) -> dict[int, int | float]:
    payload = _BatchScores.model_validate(response.json())
    ranks = {item.rank for item in payload.scores}
    if ranks != expected or len(payload.scores) != len(expected):
        raise ValueError("scores must cover every rank in this batch exactly once")  # noqa: TRY003
    return {item.rank: item.score for item in payload.scores}


class HTTPMemoryReranker:
    """Select original candidate ranks using one global ordering of HTTP scores.

    The caller owns the supplied client. Each successful run sends every candidate
    once and reports the number of HTTP requests it issued.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Validate each batch, then choose top scores across the complete pool."""
        if not isinstance(query, str):
            raise ValueError("query must be a string")  # noqa: TRY003, TRY004
        if (
            not isinstance(candidates, Sequence)
            or not candidates
            or any(not isinstance(candidate, MemoryHit) for candidate in candidates)
        ):
            raise ValueError("rerank requires a nonempty sequence of MemoryHit candidates")  # noqa: TRY003
        if type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("limit must be an integer between one and the candidate count")  # noqa: TRY003

        scores: dict[int, int | float] = {}
        requests = 0
        for offset in range(0, len(candidates), self._batch_size):
            batch = [
                {"rank": offset + index, "text": candidate.text}
                for index, candidate in enumerate(candidates[offset : offset + self._batch_size], start=1)
            ]
            requests += 1
            response = await self._client.post(
                self._endpoint,
                json={"query": query, "candidates": batch},
                # Redirects would add requests and bypass the non-2xx contract.
                follow_redirects=False,
            )
            response.raise_for_status()
            expected = set(range(offset + 1, offset + len(batch) + 1))
            scores.update(_read_scores(response, expected))

        selected = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(selected_ranks=selected, usage=InferenceUsage(requests=requests))
