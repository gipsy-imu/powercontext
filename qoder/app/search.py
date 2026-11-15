import html
import re
from pathlib import Path

from . import db
from .config import DbConfig
from .parser import parse_note

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples" / "notes"
TEXT_SUFFIXES = (".md", ".txt")


def upsert_records(cfg: DbConfig, records) -> dict:
    if not records:
        return {"total": 0, "inserted": 0, "unchanged": 0}
    sql = """
        INSERT INTO notes (note_uid, source, title, section, body, tags, seq)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE seq = VALUES(seq)
    """
    with db.get_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM notes")
            before = cur.fetchone()["c"]
            rows = [
                (r.note_uid, r.source, r.title, r.section, r.body,
                 "," + ",".join(r.tags) + ",", r.seq)
                for r in records
            ]
            cur.executemany(sql, rows)
            cur.execute("SELECT COUNT(*) AS c FROM notes")
            after = cur.fetchone()["c"]
    inserted = after - before
    return {
        "total": len(records),
        "inserted": inserted,
        "unchanged": len(rows) - inserted,
    }


def import_file(cfg: DbConfig, path: Path, source_name=None) -> dict:
    text = path.read_text(encoding="utf-8")
    records = parse_note(text, source_name or path.name)
    result = upsert_records(cfg, records)
    result["source"] = source_name or path.name
    return result


def import_directory(cfg: DbConfig, directory: Path) -> list:
    results = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            results.append(import_file(cfg, path))
    return results


def import_samples(cfg: DbConfig) -> list:
    return import_directory(cfg, SAMPLES_DIR)


CJK_RUN = re.compile(r"[㐀-鿿぀-ヿ가-힯]{2,}")


def query_tokens(query: str) -> list:
    """把查询拆成用于高亮的词：空白分词 + 中文串的二元组。"""
    tokens = [t for t in query.split() if t]
    for run in CJK_RUN.findall(query):
        if len(run) <= 4:
            tokens.append(run)
        tokens += [run[i:i + 2] for i in range(len(run) - 1)]
    seen, ordered = set(), []
    for t in sorted(tokens, key=len, reverse=True):
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def make_snippet(body: str, query: str, width: int = 90) -> str:
    """返回带 <mark> 高亮的匹配片段（HTML 已转义）。"""
    tokens = query_tokens(query)
    plain = re.sub(r"\s+", " ", body)
    lowered = plain.lower()
    hit = -1
    for t in tokens:
        pos = lowered.find(t.lower())
        if pos != -1 and (hit == -1 or pos < hit):
            hit = pos
    if hit == -1:
        start, end = 0, min(width, len(plain))
    else:
        start = max(0, hit - width // 3)
        end = min(len(plain), start + width)
    fragment = plain[start:end]
    if start > 0:
        fragment = "…" + fragment
    if end < len(plain):
        fragment += "…"
    escaped = html.escape(fragment)
    if tokens:
        combined = re.compile(
            "|".join(re.escape(html.escape(t)) for t in tokens), re.IGNORECASE)
        escaped = combined.sub(lambda m: "<mark>" + m.group(0) + "</mark>", escaped)
    return escaped


def search(cfg: DbConfig, query: str, report=None, tag=None, limit=20) -> list:
    if not query.strip():
        return []
    where = ["MATCH(title, section, body) AGAINST(%s)"]
    params = [query.strip(), query.strip()]
    if report:
        where.append("title = %s")
        params.append(report)
    if tag:
        where.append("tags LIKE %s")
        params.append("%," + tag + ",%")
    params.append(min(max(int(limit), 1), 100))
    sql = """
        SELECT id, note_uid, source, title, section, body, tags, seq,
               MATCH(title, section, body) AGAINST(%s) AS score
        FROM notes
        WHERE """ + " AND ".join(where) + """
        ORDER BY score DESC, id ASC
        LIMIT %s
    """
    with db.get_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "note_uid": row["note_uid"],
            "source": row["source"],
            "title": row["title"],
            "section": row["section"],
            "tags": [t for t in row["tags"].strip(",").split(",") if t],
            "score": round(float(row["score"]), 4),
            "snippet": make_snippet(row["body"], query),
            "body": row["body"],
        })
    return results


def filter_options(cfg: DbConfig) -> dict:
    with db.get_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT title FROM notes ORDER BY title")
            reports = [r["title"] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT tags FROM notes")
            tags = sorted({t for r in cur.fetchall()
                           for t in r["tags"].strip(",").split(",") if t})
            cur.execute("SELECT COUNT(*) AS c FROM notes")
            count = cur.fetchone()["c"]
    return {"reports": reports, "tags": tags, "count": count}


def get_section(cfg: DbConfig, note_uid: str):
    with db.get_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT note_uid, source, title, section, body, tags "
                "FROM notes WHERE note_uid=%s", (note_uid,))
            row = cur.fetchone()
    if not row:
        return None
    row["tags"] = [t for t in row["tags"].strip(",").split(",") if t]
    return row
