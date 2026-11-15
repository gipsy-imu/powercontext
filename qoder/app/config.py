import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def load_config() -> DbConfig:
    """优先读 SEEKDB_* 环境变量，其次读 YANJI_CREDENTIALS_FILE 指向的 JSON。"""
    creds = {}
    path = os.environ.get("YANJI_CREDENTIALS_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            creds = json.load(f)

    def pick(key: str, default: str) -> str:
        env = os.environ.get("SEEKDB_" + key.upper())
        if env is not None and env != "":
            return env
        return str(creds.get(key, default))

    return DbConfig(
        host=pick("host", "127.0.0.1"),
        port=int(pick("port", "2881")),
        user=pick("user", "root"),
        password=pick("password", ""),
        database=pick("database", "yanji"),
    )
