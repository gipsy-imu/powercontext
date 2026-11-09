"""Run the exported workflow with Graphon and a temporary real seekdb."""

from contextlib import contextmanager
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import socket
import subprocess
import sys

import yaml

from graphon.entities import GraphInitParams
from graphon.graph import Graph
from graphon.graph_engine import GraphEngine
from graphon.graph_engine.command_channels import InMemoryChannel
from graphon.graph_events import GraphRunSucceededEvent
from graphon.nodes.end.end_node import EndNode
from graphon.nodes.http_request import HttpRequestNode, HttpRequestNodeConfig
from graphon.nodes.if_else.if_else_node import IfElseNode
from graphon.nodes.start.start_node import StartNode
from graphon.runtime import GraphRuntimeState, VariablePool

ROOT = Path(__file__).resolve().parent
NODES = {
    "start": StartNode,
    "if-else": IfElseNode,
    "end": EndNode,
    "http-request": HttpRequestNode,
}


def unexpected_file_operation(*args, **kwargs):
    raise AssertionError("Text workflow must not need file services")


def run(graph_config, game_id, action, case_number="", verdict=""):
    pool = VariablePool()
    inputs = {"game_id": game_id, "action": action, "case_number": case_number, "verdict": verdict}
    for key, value in inputs.items():
        pool.add(["1001", key], value)
    runtime = GraphRuntimeState(variable_pool=pool, start_at=monotonic())
    params = GraphInitParams(workflow_id="lunar-customs", graph_config=graph_config, run_context={}, call_depth=0)

    class Factory:
        def create_node(self, conf):
            kind = conf["data"].type
            cls = NODES[kind]
            deps = {}
            if kind == "http-request":
                deps = dict(
                    http_request_config=HttpRequestNodeConfig(10, 20, 10, 1024, 65536, True, 0),
                    tool_file_manager_factory=unexpected_file_operation,
                    file_manager=object(),
                    file_reference_factory=unexpected_file_operation,
                )
            return cls(
                conf["id"], cls.validate_node_data(conf["data"]),
                graph_init_params=params, graph_runtime_state=runtime, **deps,
            )

    graph = Graph.init(graph_config=graph_config, root_node_id="1001", node_factory=Factory())
    events = list(GraphEngine("lunar-customs", graph, runtime, InMemoryChannel()).run())
    assert isinstance(events[-1], GraphRunSucceededEvent), events[-1]
    return events[-1].outputs["report"]


@contextmanager
def backend(port, data_path):
    command = [sys.executable, str(ROOT / "game.py"), "--data", str(data_path), "--port", str(port)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        try:
            assert "ready" in process.stdout.readline()
            yield
        finally:
            process.terminate()
            process.wait(timeout=20)


def expect(label, text, *snippets):
    for snippet in snippets:
        assert snippet in text, (label, snippet, text)
    print(f"{label}: PASS")
    print(text.replace("\n\n", "\n"))


def main():
    dsl = yaml.safe_load((ROOT / "lunar-customs.yml").read_text(encoding="utf-8"))
    assert dsl["version"] == "0.6.0" and dsl["app"]["mode"] == "workflow"
    graph_config = dsl["workflow"]["graph"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    for node in graph_config["nodes"]:
        if node["data"]["type"] == "http-request":
            node["data"]["url"] = node["data"]["url"].replace("lunar-customs:8080", f"127.0.0.1:{port}")
    play = partial(run, graph_config)
    with TemporaryDirectory(prefix="lunar-customs-") as data:
        data_path = Path(data) / "seekdb"
        with backend(port, data_path):
            expect("workflow inspection", play("demo-01", "查看线索"), "第 1/5 案", "P-01", "休眠种子")
            expect("invalid future case", play("demo-01", "提交裁决", "3", "退运"), "尚未开放")
            expect("workflow judgment", play("demo-01", "提交裁决", "1", "隔离"), "判定正确", "已处理 1/5 案")
            expect("duplicate judgment", play("demo-01", "提交裁决", "1", "放行"), "存档裁决：隔离", "已处理 1/5 案")
            expect("independent shift", play("demo-02", "查看线索"), "第 1/5 案")
        with backend(port, data_path):
            expect("resume after restart", play("demo-01", "查看线索"), "第 2/5 案", "已答对 1 案")
            expect("wrong answer", play("demo-01", "提交裁决", "2", "隔离"), "判定有误", "答对 1 案")
            for number, verdict in [(3, "退运"), (4, "放行"), (5, "隔离")]:
                expect(f"case {number}", play("demo-01", "提交裁决", str(number), verdict), "判定正确")
            expect("final score", play("demo-01", "查看线索"), "值班结束", "4/5")


if __name__ == "__main__":
    main()
