"""Retrieve a spec, make a MiniMax-guided edit, and retain task state."""

import argparse
import ast
import asyncio
import difflib
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import httpx
import pylibseekdb
import pymysql
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, open_builtin_runtime
from powercontext.builtin.scope import ScopeDraft
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parent
TASK = "SDD-001"
QUERY = "restock threshold"
TARGET = "demo/inventory.py"
FEATURE_TEST = "test_includes_an_active_sku_at_the_threshold"
ENDPOINTS = {
    "cn": "https://api.minimax.cn/v1/chat/completions",
    "global": "https://api.minimax.io/v1/chat/completions",
}


def as_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def project_sources():
    for kind, pattern in (
        ("spec", "demo/specs/*.md"),
        ("code", "demo/*.py"),
        ("test", "demo/tests/test_*.py"),
    ):
        for path in sorted(ROOT.glob(pattern)):
            yield path.relative_to(ROOT).as_posix(), kind, path.read_text(encoding="utf-8")


def retrieve(work, query):
    """Index repository files in seekdb and retrieve each SDD context category."""
    terms = list(dict.fromkeys(re.findall(r"[a-z0-9]+", query.casefold())))
    if not terms:
        raise ValueError("The seekdb query must contain a word or number.")
    rank = " + ".join("CASE WHEN LOCATE(%s, LOWER(body)) > 0 THEN 1 ELSE 0 END" for _ in terms)
    statement = f"""
        SELECT path, kind, body, {rank} AS matched_terms
        FROM sdd_sources
        WHERE kind = %s
        HAVING matched_terms > 0
        ORDER BY matched_terms DESC, path
        LIMIT %s
    """.strip()
    instance = pylibseekdb.open(str(work / "seekdb"))
    try:
        options = dict(instance.connection_options())
        options.update(database="test", charset="utf8mb4", autocommit=True, cursorclass=pymysql.cursors.DictCursor)
        with pymysql.connect(**options) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version")
            version = cursor.fetchone()["version"]
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS sdd_sources ("
                "path VARCHAR(255) PRIMARY KEY, kind VARCHAR(8) NOT NULL, body TEXT NOT NULL)"
            )
            connection.begin()
            cursor.execute("DELETE FROM sdd_sources")
            cursor.executemany("INSERT INTO sdd_sources (path, kind, body) VALUES (%s, %s, %s)", list(project_sources()))
            connection.commit()
            records = []
            for kind, limit in (("spec", 1), ("code", 1), ("test", 2)):
                cursor.execute(statement, (*terms, kind, limit))
                records.extend(cursor.fetchall())
    finally:
        instance.close()

    return records, {
        "database": "seekdb",
        "server_version": version,
        "query": query,
        "terms": terms,
        "sql": statement,
        "matches": [
            {k: value for k, value in row.items() if k != "body"} | {"sha256": digest(row["body"])}
            for row in records
        ],
    }


async def task_state(work, event=None):
    url = URL.create("sqlite+aiosqlite", database=str(work / "task-state.sqlite3"))
    config = BuiltinConfig(database=SQLiteConfig(url=url.render_as_string(hide_password=False)))
    async with open_builtin_runtime(config) as runtime:
        scope = await runtime.scopes.create(
            ScopeDraft(title=TASK, summary="Apply the inclusive restock spec", idempotency_key=TASK)
        )
        scoped = runtime.records.for_scope(scope.scope_id)
        if event is not None:
            payload = as_json(event)
            receipt = await runtime.sources.for_scope(scope.scope_id).capture(
                CaptureSource(source_id=f"{TASK}-{digest(payload)}", content=payload, metadata={"task": TASK})
            )
            return {"scope_id": scope.scope_id, "source_id": receipt.source_ref.source_id}

        latest = None
        cursor = None
        while True:
            page = await scoped.list_sources(limit=100, cursor=cursor)
            if page.items:
                latest = page.items[-1]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        if latest is None:
            return None
        return {"scope_id": scope.scope_id, "source_id": latest.source_id, "event": json.loads(latest.content)}


def prepare(work, query, *, require_demo_sources):
    state = asyncio.run(task_state(work))
    rows, trace = retrieve(work, query)
    sources = {row["path"]: row["body"] for row in rows}
    expected = {
        TARGET, "demo/specs/restock.md", "demo/tests/test_feature.py", "demo/tests/test_regression.py",
    }
    if require_demo_sources and set(sources) != expected:
        raise RuntimeError(f"Required SDD context was missing or ambiguous: {sorted(sources)}")
    return {
        "task": TASK,
        "request": query,
        "sources": sources,
        "previous_state": state,
    }, trace


def check_suite(sandbox, pattern):
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"]
    env = os.environ.copy()
    env.pop("MINIMAX_API_KEY", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(command, cwd=sandbox, env=env, capture_output=True, text=True, timeout=30)
    output = (result.stdout + result.stderr).replace(str(sandbox), "demo")
    return {"command": ["python", *command[1:]], "exit_code": result.returncode, "output": output}


def baseline(sandbox):
    feature = check_suite(sandbox, "test_feature.py")
    regression = check_suite(sandbox, "test_regression.py")
    if feature["exit_code"] != 1 or f"FAIL: {FEATURE_TEST}" not in feature["output"]:
        raise RuntimeError(f"The baseline did not fail the intended assertion:\n{feature['output']}")
    if regression["exit_code"] != 0 or "Ran 2 tests" not in regression["output"]:
        raise RuntimeError(f"The baseline regression suite did not pass:\n{regression['output']}")
    return {"feature": feature, "regression": regression}


def request_edit(context, region, model, key):
    endpoint = ENDPOINTS[region]
    messages = [
        {
            "role": "system",
            "content": (
                "You edit one Python inventory function. Use the provided spec and tests as evidence. "
                'Return only a JSON object with keys "path", "original", and "replacement". '
                'Use the exact path "demo/inventory.py". Replace one existing source line with one line. '
                "Preserve the line indentation and change only the comparison operator against threshold."
            ),
        },
        {"role": "user", "content": as_json(context)},
    ]
    response = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "reasoning_split": True, "max_completion_tokens": 2048},
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    choice = payload["choices"][0]
    if not payload.get("id") or not payload.get("model") or choice.get("finish_reason") != "stop":
        raise RuntimeError("MiniMax did not return a complete edit with a response ID and model.")
    content = choice["message"]["content"].strip()
    if content.startswith("```") and content.endswith("```"):
        content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
    edit = json.loads(content)
    return edit, {
        "provider": "MiniMax",
        "endpoint": endpoint,
        "model": payload["model"],
        "response_id": payload["id"],
        "usage": payload.get("usage"),
        "request_messages_sha256": digest(as_json(messages)),
        "proposed_edit": edit,
    }


def normalized_comparison(source):
    tree = ast.parse(source)
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and len(node.ops) == 1
        and ast.unparse(node.left) == "item.quantity" and ast.unparse(node.comparators[0]) == "threshold"
    ]
    if len(matches) != 1:
        raise ValueError("Expected one quantity-to-threshold comparison.")
    comparison = matches[0]
    operator = type(comparison.ops[0])
    comparison.ops[0] = ast.Eq()
    return ast.dump(tree), operator


def apply_edit(source, edit):
    if not isinstance(edit, dict) or set(edit) != {"path", "original", "replacement"} or edit["path"] != TARGET:
        raise ValueError("MiniMax must return exactly one edit for the inventory module.")
    original, replacement = edit["original"], edit["replacement"]
    lines = source.splitlines()
    if (
        not isinstance(original, str) or not isinstance(replacement, str)
        or len(replacement.splitlines()) != 1 or lines.count(original) != 1
    ):
        raise ValueError("The edit must replace one exact source line.")
    lines[lines.index(original)] = replacement
    candidate = "\n".join(lines) + "\n"
    before_tree, before_op = normalized_comparison(source)
    after_tree, after_op = normalized_comparison(candidate)
    if before_tree != after_tree or before_op == after_op:
        raise ValueError("Only the operator comparing item quantity to the threshold may change.")
    return candidate


def execute(work, context, trace, args):
    source = context["sources"][TARGET]
    with tempfile.TemporaryDirectory(prefix="sdd-", dir=work) as directory:
        sandbox = Path(directory) / "demo"
        shutil.copytree(ROOT / "demo", sandbox, ignore=shutil.ignore_patterns("__pycache__"))
        before = baseline(sandbox)
        if args.command == "verify":
            event = {
                "phase": "prepared",
                "completed": ["retrieve spec, implementation and tests", "reproduce boundary failure"],
                "pending": ["request MiniMax edit", "run feature and regression tests after the edit"],
                "baseline_sha256": digest(source),
                "tests": before,
            }
            receipt = asyncio.run(task_state(work, event))
            return {"seekdb": trace, "baseline": before, "state": receipt}

        key = os.environ.get("MINIMAX_API_KEY")
        if not key and not sys.stdin.isatty():
            raise ValueError("Set MINIMAX_API_KEY or run from a terminal for a hidden key prompt.")
        key = key or getpass.getpass("MiniMax API key: ")
        if not key.strip():
            raise ValueError("A MiniMax API key is required to generate an edit.")
        edit, model_trace = request_edit(context, args.region, args.model, key.strip())
        candidate = apply_edit(source, edit)
        (sandbox / "inventory.py").write_text(candidate, encoding="utf-8")
        after = check_suite(sandbox, "test_*.py")
        if after["exit_code"] != 0 or "Ran 3 tests" not in after["output"]:
            raise RuntimeError(f"The MiniMax edit failed validation:\n{after['output']}")

        patch = "".join(
            difflib.unified_diff(source.splitlines(True), candidate.splitlines(True), "a/" + TARGET, "b/" + TARGET)
        )
        run_id = digest(model_trace["response_id"])[:16]
        report = {
            "task": TASK,
            "phase": "verified",
            "completed": ["retrieve SDD context", "apply MiniMax edit", "pass feature and regression tests"],
            "pending": ["export evidence and submit the Git commit"],
            "run_id": run_id,
            "utc_time": datetime.now(timezone.utc).isoformat(),
            "before_sha256": digest(source),
            "after_sha256": digest(candidate),
            "seekdb": trace,
            "minimax": model_trace,
            "tests": {"before": before, "after": after},
        }
        evidence = work / "evidence" / run_id
        evidence.parent.mkdir(exist_ok=True)
        staged = Path(directory) / "evidence"
        staged.mkdir()
        (staged / "context.json").write_text(as_json(context) + "\n", encoding="utf-8")
        (staged / "report.json").write_text(as_json(report) + "\n", encoding="utf-8")
        (staged / "change.patch").write_text(patch, encoding="utf-8")
        (staged / "inventory.py").write_text(candidate, encoding="utf-8")
        staged.rename(evidence)
        receipt = asyncio.run(task_state(work, report))
        return {"evidence": str(evidence), "response_id": model_trace["response_id"], "state": receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("context", "verify", "run", "resume"))
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".work")
    parser.add_argument("--region", choices=ENDPOINTS, default="cn")
    parser.add_argument("--model", default="MiniMax-M3")
    parser.add_argument("--query", help="Custom seekdb lookup for the context command")
    args = parser.parse_args()
    if args.query is not None and args.command != "context":
        parser.error("--query is only available with context")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if args.command == "resume":
        result = asyncio.run(task_state(work))
    else:
        context, trace = prepare(work, args.query or QUERY, require_demo_sources=args.command != "context")
        result = {"context": context, "seekdb": trace} if args.command == "context" else execute(work, context, trace, args)
    print(as_json(result))


if __name__ == "__main__":
    main()
