import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class NoteRecord:
    note_uid: str
    source: str
    title: str
    section: str
    body: str
    tags: list = field(default_factory=list)
    seq: int = 0


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SPLIT_TAGS = re.compile(r"[,，;；]")


def _make_uid(source: str, section: str, body: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join([source, section, body]).encode("utf-8")
    ).hexdigest()
    return digest


def _parse_front_matter(lines):
    meta = {}
    if lines and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            stripped = lines[idx].strip()
            if stripped == "---":
                rest = lines[idx + 1:]
                for line in lines[1:idx]:
                    if ":" in line:
                        key, _, value = line.partition(":")
                        meta[key.strip().lower()] = value.strip()
                return meta, rest
    return meta, lines


def parse_note(text: str, source: str) -> list:
    """把 UTF-8 Markdown/TXT 笔记解析为段落记录，保留标题、段落标题、来源和原文。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    meta, lines = _parse_front_matter(lines)

    tags = [t.strip() for t in SPLIT_TAGS.split(meta.get("标签", meta.get("tags", "")))
            if t.strip()]
    file_title = meta.get("标题") or meta.get("title") or ""
    source_name = meta.get("来源") or meta.get("source") or source

    title = file_title
    section = ""
    paragraphs = []
    buffer = []

    def flush():
        body = "\n".join(buffer).strip()
        buffer.clear()
        if body:
            paragraphs.append((title, section, body))

    for line in lines:
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            text_of_heading = heading.group(2)
            if level == 1 and not file_title:
                title = text_of_heading
            elif level == 1:
                title = text_of_heading
            else:
                section = text_of_heading
            continue
        buffer.append(line)
    flush()

    if not title:
        title = re.sub(r"\.(md|txt)$", "", source, flags=re.I)

    if not paragraphs:
        paragraphs = [(title, "", text.strip())] if text.strip() else []

    records = []
    for seq, (t, sec, body) in enumerate(paragraphs):
        records.append(NoteRecord(
            note_uid=_make_uid(source_name, sec, body),
            source=source_name,
            title=t,
            section=sec,
            body=body,
            tags=tags,
            seq=seq,
        ))
    return records
