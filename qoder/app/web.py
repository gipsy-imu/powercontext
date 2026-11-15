from flask import Flask, jsonify, render_template, request

from . import db, search
from .config import load_config
from .db import DatabaseUnavailable


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    def cfg():
        return load_config()

    def status_payload():
        conf = cfg()
        payload = {"host": conf.host, "port": conf.port,
                   "database": conf.database, "ready": False, "count": 0,
                   "error": None}
        try:
            payload.update(search.filter_options(conf))
            payload["ready"] = True
        except DatabaseUnavailable as exc:
            payload["error"] = str(exc)
        return payload

    @app.route("/")
    def index():
        return render_template("index.html", status=status_payload())

    @app.route("/api/status")
    def api_status():
        return jsonify(status_payload())

    @app.route("/api/demo", methods=["POST"])
    def api_demo():
        try:
            observe = db.initialize(cfg())
            imported = search.import_samples(cfg())
            return jsonify({
                "ok": True,
                "steps": observe["steps"],
                "files": imported,
                "total": sum(r["total"] for r in imported),
                "inserted": sum(r["inserted"] for r in imported),
                "status": status_payload(),
            })
        except DatabaseUnavailable as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

    @app.route("/api/search")
    def api_search():
        query = request.args.get("q", "").strip()
        report = request.args.get("report") or None
        tag = request.args.get("tag") or None
        limit = request.args.get("limit", 20, type=int)
        try:
            hits = search.search(cfg(), query, report=report, tag=tag, limit=limit)
            return jsonify({"ok": True, "query": query, "hits": hits})
        except DatabaseUnavailable as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

    @app.route("/api/filters")
    def api_filters():
        try:
            return jsonify({"ok": True, **search.filter_options(cfg())})
        except DatabaseUnavailable as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

    @app.route("/api/section/<uid>")
    def api_section(uid):
        try:
            row = search.get_section(cfg(), uid)
        except DatabaseUnavailable as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503
        if not row:
            return jsonify({"ok": False, "error": "段落不存在"}), 404
        return jsonify({"ok": True, "section": row})

    @app.route("/api/import", methods=["POST"])
    def api_import():
        files = request.files.getlist("notes")
        if not files:
            return jsonify({"ok": False, "error": "请选择 UTF-8 的 .md 或 .txt 笔记"}), 400
        results = []
        try:
            for upload in files:
                name = upload.filename or "未命名"
                if not name.lower().endswith(search.TEXT_SUFFIXES):
                    results.append({"source": name, "total": 0, "inserted": 0,
                                    "unchanged": 0, "skipped": "仅支持 Markdown/TXT"})
                    continue
                text = upload.read().decode("utf-8", errors="strict")
                from .parser import parse_note
                records = parse_note(text, name)
                outcome = search.upsert_records(cfg(), records)
                outcome["source"] = name
                results.append(outcome)
            return jsonify({"ok": True, "files": results,
                            "status": status_payload()})
        except UnicodeDecodeError:
            return jsonify({"ok": False,
                            "error": "存在非 UTF-8 编码的文件，请先转为 UTF-8"}), 400
        except DatabaseUnavailable as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8030)
