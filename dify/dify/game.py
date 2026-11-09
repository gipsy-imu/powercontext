"""The HTTP service behind the Lunar Customs Dify workflow."""

import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
import re
from urllib.parse import parse_qs

import pylibseekdb
import pymysql


VERDICTS = ("放行", "隔离", "退运")
RULES = (
    ("P-01", "植物检疫", "休眠种子须先隔离检疫。密封与否不改变这条规则。", "隔离"),
    ("M-02", "月面样本", "经盖章的惰性仿制品可通关。真实月壤须退运。", "放行"),
    ("S-03", "自主设备", "能自行复制的设备一律退运。用途与复制速度不影响判定。", "退运"),
    ("A-04", "音频记录", "音频记录可以通关。会在午夜播放的内容仍属于音频记录。", "放行"),
    ("L-05", "发光液体", "温度变化会改变成分的液体须先隔离。货物颜色不是判定条件。", "隔离"),
)
CASES = (
    (1, "祖母的月桂", "玻璃罐密封完好。透过罐壁能看到一包仍可发芽的休眠种子。", "P-01"),
    (2, "一公斤月亮", "商品标签印着真实月壤，底部却有检验章和惰性树脂成分报告。", "M-02"),
    (3, "自动修伞蛛", "说明书写着可用废伞骨组装第二台同型号设备，无需外部控制。", "S-03"),
    (4, "午夜的掌声", "一份只读的音频文件。报关人保证它每天午夜才会播放。", "A-04"),
    (5, "极光汽水", "发光颜色很稳定，但降温后检测到瓶内成分发生变化。", "L-05"),
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS customs_rules (
        rule_code VARCHAR(8) PRIMARY KEY, topic VARCHAR(80) NOT NULL,
        rule_text VARCHAR(255) NOT NULL, verdict VARCHAR(12) NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS customs_cases (
        case_no INT PRIMARY KEY, title VARCHAR(80) NOT NULL,
        manifest VARCHAR(255) NOT NULL, rule_code VARCHAR(8) NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS customs_judgments (
        game_id VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, case_no INT NOT NULL,
        verdict VARCHAR(12) NOT NULL, correct TINYINT NOT NULL,
        PRIMARY KEY (game_id, case_no)
    )""",
)
CASE_QUERY = """
    SELECT c.title, c.manifest, r.rule_code, r.topic, r.rule_text, r.verdict
    FROM customs_cases c JOIN customs_rules r ON c.rule_code = r.rule_code
    WHERE c.case_no = %s
"""


class Customs:
    def __init__(self, options):
        self.options = options
        with self.connect() as connection, connection.cursor() as cursor:
            for statement in SCHEMA:
                cursor.execute(statement)
            cursor.executemany("INSERT IGNORE INTO customs_rules VALUES (%s,%s,%s,%s)", RULES)
            cursor.executemany("INSERT IGNORE INTO customs_cases VALUES (%s,%s,%s,%s)", CASES)
            cursor.execute("SELECT COUNT(*) FROM customs_cases")
            self.total = cursor.fetchone()[0]

    def connect(self):
        return pymysql.connect(database="test", charset="utf8mb4", autocommit=True, **self.options)

    @staticmethod
    def progress(cursor, game_id):
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(correct), 0) FROM customs_judgments WHERE game_id = %s",
            (game_id,),
        )
        return cursor.fetchone()

    def inspect(self, game_id):
        with self.connect() as connection, connection.cursor() as cursor:
            completed, score = self.progress(cursor, game_id)
            if completed == self.total:
                return f"值班结束。你的成绩是 {score}/{self.total}。换一个班次编号即可重新挑战。"
            case_no = completed + 1
            cursor.execute(CASE_QUERY, (case_no,))
            title, manifest, rule_code, topic, rule_text, _ = cursor.fetchone()
        return (
            f"第 {case_no}/{self.total} 案 · {title}\n\n"
            f"货物查验：{manifest}\n"
            f"数据库调出的规则 {rule_code} · {topic}：{rule_text}\n\n"
            f"已答对 {score} 案。选择「提交裁决」，填入案号 {case_no}，"
            "再选「放行」「隔离」或「退运」。"
        )

    def judge(self, game_id, case_number, verdict):
        if verdict not in VERDICTS or not re.fullmatch(r"[1-9][0-9]{0,3}", case_number):
            return "提交裁决需要填写查验结果里的案号，并选择放行、隔离或退运。"
        case_no = int(case_number)
        with self.connect() as connection, connection.cursor() as cursor:
            completed, _ = self.progress(cursor, game_id)
            if case_no > self.total or case_no > completed + 1:
                return "该案号尚未开放。请先用「查看线索」查询当前案号。"

            cursor.execute(CASE_QUERY, (case_no,))
            title, _, rule_code, _, rule_text, expected = cursor.fetchone()
            cursor.execute(
                """INSERT INTO customs_judgments (game_id, case_no, verdict, correct)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE correct = correct""",
                (game_id, case_no, verdict, int(verdict == expected)),
            )
            cursor.execute(
                "SELECT verdict, correct FROM customs_judgments WHERE game_id = %s AND case_no = %s",
                (game_id, case_no),
            )
            saved_verdict, correct = cursor.fetchone()
            completed, score = self.progress(cursor, game_id)
        outcome = "判定正确" if correct else "判定有误"
        next_step = "值班结束。" if completed == self.total else f"请选择「查看线索」进入第 {completed + 1} 案。"
        return (
            f"第 {case_no} 案 · {title}：{outcome}。\n"
            f"存档裁决：{saved_verdict}。规则裁决：{expected}。\n"
            f"{rule_code}：{rule_text}\n\n"
            f"已处理 {completed}/{self.total} 案，答对 {score} 案。{next_step}"
        )


class GameHandler(BaseHTTPRequestHandler):
    def send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/inspect", "/judge"):
            self.send_error(404)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 4 or int(length) > 4096:
            self.send_error(400, "Invalid form length")
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.send_error(415)
            return
        try:
            form = parse_qs(self.rfile.read(int(length)).decode("ascii"), errors="strict", max_num_fields=3)
        except ValueError:
            self.send_error(400, "Invalid form")
            return

        game_id = form.get("game_id", [""])[0]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", game_id):
            self.send_text(200, "班次编号须为 1 至 32 个英文字母、数字、下划线或连字符。")
            return
        game = self.server.game
        try:
            if self.path == "/inspect":
                result = game.inspect(game_id)
            else:
                result = game.judge(game_id, form.get("case_number", [""])[0], form.get("verdict", [""])[0])
        except pymysql.MySQLError:
            self.log_error("seekdb operation failed")
            self.send_text(503, "海关数据库暂时不可用，请重试。")
            return
        self.send_text(200, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default="./customs.db")
    args = parser.parse_args()
    with closing(pylibseekdb.open(args.data)) as database:
        with HTTPServer((args.host, args.port), GameHandler) as server:
            server.game = Customs(database.connection_options())
            print(f"Lunar Customs is ready on port {args.port}", flush=True)
            server.serve_forever()


if __name__ == "__main__":
    main()
