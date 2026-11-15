#!/usr/bin/env python3
"""发酵罐温度日志质控采集服务.

设备行为约定:
  - 向挂载目录持续追加 NDJSON 温度日志到 active.ndjson;
  - 轮转时把 active.ndjson 重命名为 active.ndjson.1, 再新建同名 active.ndjson;
  - 归档不会被删除或覆盖, 目录最多保留这两份文件;
  - 轮转后旧文件(active.ndjson.1)仍可能有缓冲数据续写落盘.

采集语义:
  - 每个文件按字节偏移续采, 只处理上次成功后的新增字节;
  - 只纳入以换行结束的完整记录, 末尾半行留待下次;
  - 记录合法条件: JSON 对象、seq 为非负整数且同一文件内严格递增、
    ts 为 UTC 时刻(ISO8601, Z 或 +00:00)、temp 为有限数值;
  - 序号已存在且内容相同 -> 幂等跳过; 同序号异内容或任何非法行 -> 整轮拒绝,
    已入库记录与采集进度(偏移)全部不变(单事务回滚);
  - 进度存于 SQLite, 与记录写入同一事务提交, 服务重启后接续采集.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

from flask import (
    Flask,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    url_for,
)

# 采集顺序: 先旧归档后当前文件, 使序号大体按时间进入库中
LOG_FILES = ("active.ndjson.1", "active.ndjson")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq     INTEGER PRIMARY KEY,          -- 设备记录序号
    ts      TEXT NOT NULL,                -- 归一化 UTC ISO8601 时刻
    temp    REAL NOT NULL,                -- 温度(有限浮点)
    source  TEXT NOT NULL                 -- 来源文件名
);
CREATE TABLE IF NOT EXISTS files (
    name     TEXT PRIMARY KEY,
    offset   INTEGER NOT NULL DEFAULT 0,  -- 已成功入库的字节偏移
    last_seq INTEGER                      -- 该文件已入库的最大序号
);
"""

PAGE_SIZE = 200


class RejectRound(Exception):
    """整轮拒绝: 非法行或同序号异内容. 事务回滚, 记录与进度均不变."""


# ---------------------------------------------------------------- 应用工厂

def create_app(log_dir: str | None = None, db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret")
    app.config["LOG_DIR"] = log_dir or os.environ.get("LOG_DIR", "/data")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "qc.db")
    )

    @app.teardown_appcontext
    def _close_db(exc):  # noqa: ANN001
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.get("/")
    def index():
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"]
        bounds = conn.execute(
            "SELECT MIN(seq) AS lo, MAX(seq) AS hi FROM records"
        ).fetchone()
        rows = conn.execute(
            "SELECT seq, ts, temp, source FROM records ORDER BY seq DESC LIMIT ?",
            (PAGE_SIZE,),
        ).fetchall()
        files = []
        for name in LOG_FILES:
            path = os.path.join(app.config["LOG_DIR"], name)
            row = conn.execute(
                "SELECT offset, last_seq FROM files WHERE name = ?", (name,)
            ).fetchone()
            files.append(
                {
                    "name": name,
                    "exists": os.path.isfile(path),
                    "size": os.path.getsize(path) if os.path.isfile(path) else 0,
                    "offset": row["offset"] if row else 0,
                    "last_seq": row["last_seq"] if row else None,
                }
            )
        return render_template(
            "index.html",
            total=total,
            lo=bounds["lo"],
            hi=bounds["hi"],
            rows=rows,
            files=files,
            page_size=PAGE_SIZE,
        )

    @app.post("/collect")
    def collect():
        try:
            stats = collect_round(get_db(), current_app.config["LOG_DIR"])
            flash(
                "采集完成: 新增 {added} 条, 重复忽略 {dup} 条, 半行待续 {pending} 字节".format(
                    added=stats["added"], dup=stats["duplicates"], pending=stats["pending_bytes"]
                ),
                "ok",
            )
        except RejectRound as exc:
            flash(f"整轮拒绝(记录与进度均未变更): {exc}", "err")
        except Exception as exc:  # noqa: BLE001
            flash(f"采集失败(记录与进度均未变更): {exc}", "err")
        return redirect(url_for("index"))

    @app.post("/api/collect")
    def api_collect():
        try:
            stats = collect_round(get_db(), current_app.config["LOG_DIR"])
            return jsonify({"ok": True, **stats})
        except RejectRound as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.get("/api/stats")
    def api_stats():
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"]
        bounds = conn.execute("SELECT MIN(seq) AS lo, MAX(seq) AS hi FROM records").fetchone()
        files = {
            r["name"]: {"offset": r["offset"], "last_seq": r["last_seq"]}
            for r in conn.execute("SELECT name, offset, last_seq FROM files")
        }
        return jsonify({"total": total, "min_seq": bounds["lo"], "max_seq": bounds["hi"], "files": files})

    @app.get("/api/records")
    def api_records():
        limit = min(int(request.args.get("limit", 100)), 1000)
        offset = max(int(request.args.get("offset", 0)), 0)
        rows = get_db().execute(
            "SELECT seq, ts, temp, source FROM records ORDER BY seq LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.get("/download.csv")
    def download_csv():
        rows = get_db().execute(
            "SELECT seq, ts, temp, source FROM records ORDER BY seq"
        )
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["seq", "ts", "temp", "source"])
        for r in rows:
            writer.writerow([r["seq"], r["ts"], r["temp"], r["source"]])
        return Response(
            buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=records.csv"},
        )

    return app


# ---------------------------------------------------------------- 数据库

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DB_PATH"]
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        g.db = conn
    return g.db


# ---------------------------------------------------------------- 行解析与校验

def _reject_constant(value: str):
    # json 模块默认接受 NaN/Infinity, 这里直接判非法
    raise RejectRound(f"非法数值常量 {value!r}(温度须为有限值)")


def _parse_ts(value, where: str) -> str:
    if not isinstance(value, str):
        raise RejectRound(f"{where}: ts 须为 ISO8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise RejectRound(f"{where}: ts {value!r} 不是合法 ISO8601 时刻") from None
    if dt.tzinfo is None:
        raise RejectRound(f"{where}: ts {value!r} 缺少时区, 须为 UTC 时刻")
    if dt.utcoffset().total_seconds() != 0:
        raise RejectRound(f"{where}: ts {value!r} 非 UTC 时刻")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_line(raw: bytes, where: str):
    """解析并校验一行, 返回 (seq, ts, temp); 任何不合法即抛 RejectRound."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RejectRound(f"{where}: 非 UTF-8 内容") from None
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise RejectRound(f"{where}: JSON 解析失败({exc.msg})") from None
    if not isinstance(obj, dict):
        raise RejectRound(f"{where}: 记录须为 JSON 对象")

    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise RejectRound(f"{where}: seq 须为非负整数, 实际 {seq!r}")

    ts = _parse_ts(obj.get("ts"), where)

    temp = obj.get("temp")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        raise RejectRound(f"{where}: temp 须为数值, 实际 {temp!r}")
    temp = float(temp)
    if not math.isfinite(temp):
        raise RejectRound(f"{where}: temp 须为有限值, 实际 {temp!r}")

    return seq, ts, temp


# ---------------------------------------------------------------- 采集主逻辑

def collect_round(conn: sqlite3.Connection, log_dir: str) -> dict:
    """执行一轮采集.

    单事务处理两个日志文件: 任一非法行或序号冲突 -> 回滚,
    已入库记录与 files 表中的偏移全部保持不变.
    """
    stats = {"added": 0, "duplicates": 0, "pending_bytes": 0, "files": {}}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name in LOG_FILES:
            path = os.path.join(log_dir, name)
            if not os.path.isfile(path):
                continue
            row = conn.execute(
                "SELECT offset, last_seq FROM files WHERE name = ?", (name,)
            ).fetchone()
            offset = row["offset"] if row else 0
            last_seq = row["last_seq"] if row else None

            size = os.path.getsize(path)
            if size < offset:
                # 同名文件被重建(轮转后新建 active.ndjson): 从头续采
                offset = 0

            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()

            nl = data.rfind(b"\n")
            if nl < 0:
                # 没有完整行: 全部是半行, 留待下次
                stats["pending_bytes"] += len(data)
                continue
            chunk = data[: nl + 1]
            stats["pending_bytes"] += len(data) - len(chunk)

            lines = chunk.split(b"\n")[:-1]
            for idx, raw in enumerate(lines):
                where = f"{name} 第{idx + 1}行(偏移{offset}起)"
                seq, ts, temp = parse_line(raw, where)
                if last_seq is not None and seq <= last_seq:
                    raise RejectRound(
                        f"{where}: 序号 {seq} 未递增(该文件已入库至 {last_seq})"
                    )
                last_seq = seq
                existing = conn.execute(
                    "SELECT ts, temp FROM records WHERE seq = ?", (seq,)
                ).fetchone()
                if existing is not None:
                    if existing["ts"] != ts or existing["temp"] != temp:
                        raise RejectRound(
                            f"{where}: 序号 {seq} 内容冲突, 库中({existing['ts']}, "
                            f"{existing['temp']}) != 新行({ts}, {temp})"
                        )
                    stats["duplicates"] += 1  # 同序号同内容: 幂等跳过
                else:
                    conn.execute(
                        "INSERT INTO records(seq, ts, temp, source) VALUES (?,?,?,?)",
                        (seq, ts, temp, name),
                    )
                    stats["added"] += 1

            new_offset = offset + len(chunk)
            conn.execute(
                "INSERT INTO files(name, offset, last_seq) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET offset = excluded.offset, "
                "last_seq = excluded.last_seq",
                (name, new_offset, last_seq),
            )
            stats["files"][name] = {"from": offset, "to": new_offset, "lines": len(lines)}
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return stats


# ---------------------------------------------------------------- 入口

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
