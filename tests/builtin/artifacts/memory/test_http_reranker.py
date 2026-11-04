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

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit


def _hits(count: int) -> tuple[MemoryHit, ...]:
    return tuple(
        MemoryHit(
            memory_ref=ArtifactRef(family="memory", artifact_id="original", revision=3),
            entry_id=f"entry-{rank}",
            entry_version_id=f"version-{rank}",
            text=f"Original 北京 text {rank}",
            score=1.0 / rank,
            matched_by=("fts",),
        )
        for rank in range(1, count + 1)
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not an object",
        {},
        {"scores": None},
        {"scores": {}},
        {"scores": [{"rank": 1}]},
        {"scores": [{"score": 1}]},
        {"scores": [{"rank": True, "score": 1}]},
        {"scores": [{"rank": 1.0, "score": 1}]},
        {"scores": [{"rank": "1", "score": 1}]},
        {"scores": [{"rank": 0, "score": 1}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": 1, "score": None}]},
        {"scores": [{"rank": 1, "score": -float("inf")}]},
    ],
)
def test_wire_schema_is_strict_and_does_not_coerce_scores_or_ranks(payload: object) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://offline/scores", client=client).rerank("q", _hits(1), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("content", [b'{"scores":[{"rank":1,"score":9}]}', b"{invalid JSON"])
def test_cross_batch_ranks_and_late_malformed_json_fail_without_a_partial_decision(content: bytes) -> None:
    async def scenario() -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            if len(calls) == 1:
                return httpx.Response(200, json={"scores": [{"rank": 1, "score": 1}]})
            return httpx.Response(200, content=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://offline/scores", client=client, batch_size=1).rerank("q", _hits(3), 1)
        assert [body["candidates"][0]["rank"] for body in calls] == [1, 2]

    asyncio.run(scenario())


@pytest.mark.parametrize("batch_size", [0, -1, True, False, 1.5, "2", None])
def test_invalid_batch_sizes_fail_before_any_request(batch_size: object) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))
        ) as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://offline/scores", client=client, batch_size=batch_size)  # ty: ignore[invalid-argument-type]

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, -1, 3, True, 1.5, "1", None])
def test_invalid_limits_fail_before_any_request(limit: object) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))
        ) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://offline/scores", client=client).rerank("q", _hits(2), limit)  # ty: ignore[invalid-argument-type]

    asyncio.run(scenario())


def test_twenty_queries_select_global_top_ten_from_173_candidates_in_25_batches() -> None:
    candidates = _hits(173)
    original = tuple(hit.model_dump() for hit in candidates)
    requests_by_query: dict[str, list[list[int]]] = {}

    def score(query: str, rank: int) -> int:
        return (rank * (int(query) + 3)) % 37 - 18

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests_by_query.setdefault(body["query"], []).append([item["rank"] for item in body["candidates"]])
        assert set(body) == {"query", "candidates"}
        assert 1 <= len(body["candidates"]) <= 7
        for item in body["candidates"]:
            assert set(item) == {"rank", "text"}
            assert item["text"] == candidates[item["rank"] - 1].text
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": score(body["query"], item["rank"])}
                    for item in reversed(body["candidates"])
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://offline/scores", client=client, batch_size=7)
            for query in map(str, range(20)):
                result = await reranker.rerank(query, candidates, 10)
                expected = tuple(sorted(range(1, 174), key=lambda rank: (-score(query, rank), rank))[:10])
                assert result.selected_ranks == expected
                assert result.usage.requests == len(requests_by_query[query]) == 25
                sent = [rank for batch in requests_by_query[query] for rank in batch]
                assert sorted(sent) == list(range(1, 174))
            assert not client.is_closed
        assert tuple(hit.model_dump() for hit in candidates) == original

    asyncio.run(scenario())


def test_finite_negative_and_large_integer_scores_remain_numeric() -> None:
    values = {1: -0.0, 2: 0, 3: -1.5, 4: 10**400}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            content=json.dumps({
                "scores": [{"rank": item["rank"], "score": values[item["rank"]]} for item in body["candidates"]]
            }).encode(),
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await HTTPMemoryReranker("https://offline/scores", client=client, batch_size=2).rerank(
                "q", _hits(4), 4
            )
            assert result.selected_ranks == (4, 1, 2, 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [302, 422, 503])
def test_non_2xx_preserves_http_errors_and_does_not_follow_redirects(status: int) -> None:
    async def scenario() -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(status, headers={"Location": "https://offline/redirected"}, content=b"error")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await HTTPMemoryReranker("https://offline/scores", client=client).rerank("q", _hits(1), 1)
            assert caught.value.response.status_code == status
            assert len(requests) == 1

    asyncio.run(scenario())


def test_timeout_error_is_preserved_without_fallback() -> None:
    async def scenario() -> None:
        timeout = httpx.ReadTimeout("offline timeout")

        def handler(request: httpx.Request) -> httpx.Response:
            raise timeout

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.ReadTimeout) as caught:
                await HTTPMemoryReranker("https://offline/scores", client=client).rerank("q", _hits(1), 1)
            assert caught.value is timeout

    asyncio.run(scenario())


def test_concurrent_calls_keep_separate_usage_and_preserve_client_settings() -> None:
    seen: dict[str, list[int]] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Caller"] == "retained"
        body = json.loads(request.content)
        ranks = [item["rank"] for item in body["candidates"]]
        seen.setdefault(body["query"], []).extend(ranks)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"scores": [{"rank": rank, "score": rank} for rank in reversed(ranks)]})

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://offline", headers={"X-Caller": "retained"}
        ) as client:
            reranker = HTTPMemoryReranker("/scores", client=client, batch_size=2)
            first, second = await asyncio.gather(
                reranker.rerank(" first 北京 ", _hits(5), 2), reranker.rerank("second", _hits(2), 1)
            )
            assert first.selected_ranks == (5, 4) and first.usage.requests == 3
            assert second.selected_ranks == (2,) and second.usage.requests == 1
            assert sorted(seen[" first 北京 "]) == [1, 2, 3, 4, 5]
            assert sorted(seen["second"]) == [1, 2]
            assert not client.is_closed

    asyncio.run(scenario())
