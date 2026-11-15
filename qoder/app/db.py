import re
from contextlib import contextmanager

import pymysql
import pymysql.cursors

from .config import DbConfig

DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class DatabaseUnavailable(Exception):
    """数据库连接或初始化失败，界面需要原样展示该信息。"""


def _connect(cfg: DbConfig, database=None):
    if not DB_NAME_RE.match(cfg.database):
        raise DatabaseUnavailable(
            "数据库名 %r 不合法，仅允许字母、数字和下划线" % cfg.database
        )
    try:
        return pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        raise DatabaseUnavailable(
            "无法连接 seekdb（%s@%s:%s）：%s" % (cfg.user, cfg.host, cfg.port, exc)
        ) from exc


@contextmanager
def get_connection(cfg: DbConfig, database=None):
    conn = _connect(cfg, database or cfg.database)
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id         BIGINT PRIMARY KEY AUTO_INCREMENT,
    note_uid   VARCHAR(80)  NOT NULL,
    source     VARCHAR(512) NOT NULL,
    title      VARCHAR(512) NOT NULL,
    section    VARCHAR(512) NOT NULL DEFAULT '',
    body       MEDIUMTEXT   NOT NULL,
    tags       VARCHAR(512) NOT NULL DEFAULT '',
    seq        INT          NOT NULL DEFAULT 0,
    created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_note_uid (note_uid),
    KEY idx_source (source(191)),
    FULLTEXT KEY ft_search (title, section, body) WITH PARSER IK
) DEFAULT CHARSET=utf8mb4
"""


def initialize(cfg: DbConfig) -> dict:
    """建库建表（含 IK 分词的全文索引），返回观察信息供 CLI 与界面展示。"""
    conn = _connect(cfg)
    observe = {"database": cfg.database, "steps": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARSET utf8mb4"
                % cfg.database
            )
            observe["steps"].append("数据库 %s 就绪" % cfg.database)
            cur.execute("USE `%s`" % cfg.database)
            cur.execute(SCHEMA_SQL)
            observe["steps"].append("表 notes 就绪（全文索引 ft_search，IK 分词）")
            cur.execute("SELECT VERSION() AS v")
            observe["version"] = cur.fetchone()["v"]
    except pymysql.MySQLError as exc:
        raise DatabaseUnavailable("seekdb 初始化失败：%s" % exc) from exc
    finally:
        conn.close()
    return observe
