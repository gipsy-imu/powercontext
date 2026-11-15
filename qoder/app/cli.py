import argparse
import json
import sys
from pathlib import Path

from . import db, search
from .config import load_config
from .expectations import EXPECTED_QUERIES, FILTERED_QUERIES
from .db import DatabaseUnavailable


def _cfg(args):
    return load_config()


def cmd_init(args):
    cfg = _cfg(args)
    observe = db.initialize(cfg)
    print("初始化完成：")
    for step in observe["steps"]:
        print("  -", step)
    print("  - seekdb 版本:", observe["version"])


def cmd_import(args):
    cfg = _cfg(args)
    path = Path(args.path) if args.path else search.SAMPLES_DIR
    if not path.exists():
        sys.exit("路径不存在：%s" % path)
    results = (search.import_directory(cfg, path) if path.is_dir()
               else [search.import_file(cfg, path)])
    total = sum(r["total"] for r in results)
    inserted = sum(r["inserted"] for r in results)
    for r in results:
        print("  %s：段落 %d，新增 %d，已存在 %d" %
              (r["source"], r["total"], r["inserted"], r["unchanged"]))
    print("导入完成：共 %d 段，新增 %d 段，重复导入未新增 %d 段" %
          (total, inserted, total - inserted))


def cmd_search(args):
    cfg = _cfg(args)
    hits = search.search(cfg, args.query, report=args.report, tag=args.tag,
                         limit=args.limit)
    for h in hits:
        location = " / ".join(x for x in [h["title"], h["section"]] if x)
        print("[%.4f] %s\n        来源：%s\n        %s\n" %
              (h["score"], location, h["source"],
               h["snippet"].replace("<mark>", "«").replace("</mark>", "»")))
    print("命中 %d 条" % len(hits))


def cmd_status(args):
    cfg = _cfg(args)
    options = search.filter_options(cfg)
    print(json.dumps(options, ensure_ascii=False, indent=2))


def _check_query(cfg, case, label):
    hits = search.search(cfg, case["query"], report=case.get("report"),
                         tag=case.get("tag"))
    sections_by_title = {}
    for h in hits:
        sections_by_title.setdefault(h["title"], []).append(h["section"])
    problems = []
    if case.get("expect_empty"):
        if hits:
            problems.append("期望无命中，实际命中 %d 条" % len(hits))
    for title, sections in (case.get("must_have") or {}).items():
        got = sections_by_title.get(title, [])
        for sec in sections:
            if sec not in got:
                problems.append("缺少命中：%s / %s" % (title, sec))
    for title in case.get("must_have_titles") or []:
        if title not in sections_by_title:
            problems.append("缺少命中报告：%s" % title)
    for title in case.get("forbid_titles") or []:
        if title in sections_by_title:
            problems.append("不应命中却出现：%s" % title)
    status = "PASS" if not problems else "FAIL"
    print("%s [%s] 查询 %r -> 命中 %d 条（%s）" %
          (status, label, case["query"], len(hits), case.get("note", "")))
    for p in problems:
        print("      ·", p)
    return not problems


def cmd_verify(args):
    cfg = _cfg(args)
    ok = True
    for case in EXPECTED_QUERIES:
        ok = _check_query(cfg, case, "全文检索") and ok
    for case in FILTERED_QUERIES:
        ok = _check_query(cfg, case, "筛选") and ok
    rerun = search.import_samples(cfg)
    extra = sum(r["inserted"] for r in rerun)
    if extra != 0:
        print("FAIL [重复导入] 期望新增 0，实际新增 %d" % extra)
        ok = False
    else:
        print("PASS [重复导入] 再次导入全部示例，新增 0、已存在 %d 段" %
              sum(r["total"] for r in rerun))
    print("\n验证%s" % ("全部通过" if ok else "存在失败项"))
    sys.exit(0 if ok else 1)


def cmd_serve(args):
    from .web import create_app
    app = create_app()
    print("研迹界面：http://%s:%d" % (args.host, args.port))
    app.run(host=args.host, port=args.port, debug=False)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="yanji", description="研迹：科研笔记检索")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "verify", "demo"):
        sub.add_parser(name).set_defaults(func=None)
    p_import = sub.add_parser("import", help="导入 Markdown/TXT 笔记或示例")
    p_import.add_argument("path", nargs="?", help="文件或目录，缺省为内置示例")
    p_search = sub.add_parser("search", help="全文检索")
    p_search.add_argument("query")
    p_search.add_argument("--report")
    p_search.add_argument("--tag")
    p_search.add_argument("--limit", type=int, default=10)
    p_serve = sub.add_parser("serve", help="启动 Web 界面")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8030)

    args = parser.parse_args(argv)
    funcs = {"init": cmd_init, "import": cmd_import, "search": cmd_search,
             "status": cmd_status, "verify": cmd_verify, "serve": cmd_serve}
    if args.command == "demo":
        args.func = None
        cmd_init(args)
        cmd_import(argparse.Namespace(path=None))
        cmd_verify(args)
        return
    try:
        funcs[args.command](args)
    except DatabaseUnavailable as exc:
        print("数据库不可用：%s" % exc, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
