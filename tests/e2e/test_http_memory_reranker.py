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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx

from powercontext.builtin.artifacts.memory import MemoryCitation, MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft


def test_loopback_http_scores_preserve_memory_text_identity_and_exact_citations(tmp_path: Path) -> None:
    requests = []
    server_scores = {1: 0.1, 2: 0.9, 3: -0.2, 4: 0.9, 5: 0.99, 6: 0.8}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            response = json.dumps({
                "scores": [
                    {"rank": item["rank"], "score": server_scores[item["rank"]]}
                    for item in reversed(body["candidates"])
                ]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario() -> None:
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"))
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            scope = await runtime.scopes.create(
                ScopeDraft(title="HTTP rerank", summary="Evidence", idempotency_key="http")
            )
            memory = runtime.memory.for_scope(scope.scope_id)
            await memory.remember(
                RememberMemoryRequest(
                    entries=tuple(
                        MemoryEntryInput(kind="fact", text=f"Calibration 北京 original record {number}.")
                        for number in range(1, 7)
                    )
                )
            )
            before = await memory.search(SearchMemoryRequest(query="calibration", mode="fts", limit=10))
            assert len(before.hits) == 6
            originals = {hit.entry_version_id: hit.model_dump() for hit in before.hits}

        endpoint = f"http://127.0.0.1:{server.server_port}/scores"
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            reranker = HTTPMemoryReranker(endpoint, client=client, batch_size=2, policy_id="test-http-policy")
            async with open_builtin_runtime(config, memory_reranker=reranker) as runtime:
                memory = runtime.memory.for_scope(scope.scope_id)
                after = await memory.search(SearchMemoryRequest(query="calibration", mode="fts", limit=3))
                trace = after.rerank
                assert trace is not None
                assert trace.policy_id == "test-http-policy"
                assert trace.selected_ranks == (5, 2, 4)
                assert trace.usage.requests == len(requests) == 3
                assert after.hits == tuple(trace.candidate_hits[rank - 1] for rank in (5, 2, 4))
                assert after.memory_ref == before.memory_ref
                citations = []
                entries = []
                for hit in after.hits:
                    assert hit.model_dump() == originals[hit.entry_version_id]
                    citation = MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    )
                    entry = await memory.get(GetMemoryEntryRequest(citation=citation))
                    assert entry.entry.text == hit.text
                    citations.append(citation)
                    entries.append(entry)
                sent_ranks = []
                for request in requests:
                    assert request["query"] == "calibration" and len(request["candidates"]) <= 2
                    for item in request["candidates"]:
                        sent_ranks.append(item["rank"])
                        assert item["text"] == trace.candidate_hits[item["rank"] - 1].text
                assert sorted(sent_ranks) == [1, 2, 3, 4, 5, 6]
            assert not client.is_closed

        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for citation, expected in zip(citations, entries, strict=True):
                assert await memory.get(GetMemoryEntryRequest(citation=citation)) == expected

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
