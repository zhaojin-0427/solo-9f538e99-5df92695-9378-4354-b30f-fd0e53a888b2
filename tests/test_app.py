#!/usr/bin/env python3
"""采集服务测试: 半行、轮转、旧文件续写、重启接续、整轮拒绝、幂等。

运行: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import app as qc  # noqa: E402

ACTIVE = "active.ndjson"
ARCHIVE = "active.ndjson.1"
BASE_TS = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)


def rec(seq: int, temp: float = 36.5, ts: str | None = None) -> bytes:
    ts = ts or (BASE_TS + timedelta(seconds=10 * seq)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"seq": seq, "ts": ts, "temp": temp}, separators=(",", ":")).encode()


class CollectTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qc-test-")
        self.log_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.log_dir)
        self.db_path = os.path.join(self.tmp, "state", "qc.db")
        self.app = qc.create_app(log_dir=self.log_dir, db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 工具

    def path(self, name=ACTIVE):
        return os.path.join(self.log_dir, name)

    def write(self, chunks: bytes, name=ACTIVE, append=False):
        mode = "ab" if append else "wb"
        with open(self.path(name), mode) as fh:
            fh.write(chunks)

    def lines(self, *seqs, name=ACTIVE, append=False, temp=36.5):
        self.write(b"".join(rec(s, temp=temp) + b"\n" for s in seqs), name=name, append=append)

    def rotate(self):
        os.rename(self.path(ACTIVE), self.path(ARCHIVE))
        open(self.path(ACTIVE), "wb").close()

    def collect(self):
        resp = self.client.post("/api/collect")
        return resp.status_code, resp.get_json()

    def total(self):
        return self.client.get("/api/stats").get_json()["total"]

    def seqs(self):
        return [r["seq"] for r in self.client.get("/api/records?limit=1000").get_json()]

    def offset(self, name=ACTIVE):
        files = self.client.get("/api/stats").get_json()["files"]
        return files.get(name, {}).get("offset")

    # ------------------------------------------------------------- 用例

    def test_basic_collect_then_idempotent(self):
        self.lines(1, 2, 3)
        code, stats = self.collect()
        self.assertEqual(code, 200)
        self.assertEqual(stats["added"], 3)
        self.assertEqual(self.seqs(), [1, 2, 3])
        # 重复点击: 不漏不重
        code, stats = self.collect()
        self.assertEqual(stats["added"], 0)
        self.assertEqual(self.total(), 3)

    def test_partial_line_held_until_completed(self):
        full = rec(1) + b"\n" + rec(2) + b"\n"
        half = rec(3) + b"\n"
        cut = len(half) // 2
        self.write(full + half[:cut])  # 末尾半行无换行
        code, stats = self.collect()
        self.assertEqual(stats["added"], 2)
        self.assertGreater(stats["pending_bytes"], 0)
        self.assertEqual(self.seqs(), [1, 2])
        # 补全半行 + 又一条完整记录
        self.write(half[cut:] + rec(4) + b"\n", append=True)
        code, stats = self.collect()
        self.assertEqual(stats["added"], 2)
        self.assertEqual(self.seqs(), [1, 2, 3, 4])

    def test_partial_only_file(self):
        self.write(rec(1)[:20])  # 整个文件只有半行
        code, stats = self.collect()
        self.assertEqual(code, 200)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(self.total(), 0)

    def test_rotation_then_late_old_file_writes(self):
        self.lines(1, 2, 3, 4, 5)
        self.collect()
        self.rotate()
        # 新文件写 8,9,10; 旧文件续写缓冲的 6,7(轮转与采集交错)
        self.lines(8, 9, 10)
        self.lines(6, 7, name=ARCHIVE, append=True)
        code, stats = self.collect()
        self.assertEqual(stats["added"], 5)
        self.assertEqual(self.seqs(), list(range(1, 11)))
        # 再点一次: 幂等
        _, stats = self.collect()
        self.assertEqual(stats["added"], 0)
        self.assertEqual(self.total(), 10)

    def test_rotation_before_any_collect(self):
        self.lines(1, 2, 3)
        self.rotate()
        self.lines(4, 5)
        code, stats = self.collect()
        self.assertEqual(stats["added"], 5)
        self.assertEqual(self.seqs(), [1, 2, 3, 4, 5])

    def test_rotation_after_collect_dedupes_archive(self):
        self.lines(1, 2, 3)
        self.collect()
        self.rotate()  # 归档内容与已采的 active 完全相同
        self.lines(4, 5)
        code, stats = self.collect()
        self.assertEqual(stats["added"], 2)          # 只有新文件的 2 条入库
        self.assertEqual(stats["duplicates"], 3)     # 归档重读的 3 条幂等跳过
        self.assertEqual(self.seqs(), [1, 2, 3, 4, 5])

    def test_invalid_line_rejects_whole_round(self):
        self.lines(1, 2)
        self.collect()
        offset_before = self.offset()
        bad = b'{"seq":3,"ts":"2026-11-15T08:00:30Z","temp":NaN}\n'
        self.write(rec(3) + b"\n" + bad + rec(4) + b"\n", append=True)
        code, stats = self.collect()
        self.assertEqual(code, 409)
        self.assertFalse(stats["ok"])
        # 整轮拒绝: 记录与进度都不变
        self.assertEqual(self.total(), 2)
        self.assertEqual(self.offset(), offset_before)
        # 修掉坏行(重写为 4 条合法记录)后接续采集
        self.lines(1, 2, 3, 4)
        code, stats = self.collect()
        self.assertEqual(stats["added"], 2)
        self.assertEqual(self.seqs(), [1, 2, 3, 4])

    def test_conflict_same_seq_different_content_rejected(self):
        self.lines(1, 2, 3)
        self.collect()
        # 旧归档中出现同序号异内容(故障固件)
        self.write(rec(2, temp=99.9) + b"\n", name=ARCHIVE)
        code, stats = self.collect()
        self.assertEqual(code, 409)
        self.assertIn("冲突", stats["error"])
        self.assertEqual(self.total(), 3)

    def test_same_seq_same_content_dedupes(self):
        self.lines(1, 2, 3)
        self.collect()
        self.write(rec(2) + b"\n", name=ARCHIVE)  # 同序号同内容
        code, stats = self.collect()
        self.assertEqual(code, 200)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(self.total(), 3)

    def test_non_increasing_seq_rejected(self):
        self.write(rec(1) + b"\n" + rec(2) + b"\n" + rec(2) + b"\n")
        code, stats = self.collect()
        self.assertEqual(code, 409)
        self.assertIn("未递增", stats["error"])
        self.assertEqual(self.total(), 0)

    def test_restart_continues_from_offsets(self):
        self.lines(1, 2, 3)
        self.collect()
        # 模拟服务重启: 同一数据库与日志目录, 全新应用实例
        app2 = qc.create_app(log_dir=self.log_dir, db_path=self.db_path)
        client2 = app2.test_client()
        stats = client2.post("/api/collect").get_json()
        self.assertEqual(stats["added"], 0)
        self.lines(4, 5, append=True)
        stats = client2.post("/api/collect").get_json()
        self.assertEqual(stats["added"], 2)
        self.assertEqual(self.seqs(), [1, 2, 3, 4, 5])

    def test_no_files_and_empty_file(self):
        code, stats = self.collect()  # 目录为空
        self.assertEqual(code, 200)
        self.assertEqual(stats["added"], 0)
        self.write(b"")  # 空文件
        code, stats = self.collect()
        self.assertEqual(code, 200)
        self.assertEqual(stats["added"], 0)

    def test_validation_matrix(self):
        bad_lines = [
            b'{"seq":1,"ts":"2026-11-15T08:00:00Z","temp":NaN}',          # NaN
            b'{"seq":1,"ts":"2026-11-15T08:00:00Z","temp":Infinity}',    # Infinity
            b'{"seq":1,"ts":"2026-11-15T08:00:00Z","temp":1e999}',       # 溢出 inf
            b'{"seq":1,"ts":"2026-11-15T08:00:00Z","temp":"36.5"}',      # 字符串温度
            b'{"seq":1,"ts":"2026-11-15T08:00:00Z"}',                    # 缺 temp
            b'{"seq":1,"temp":36.5}',                                    # 缺 ts
            b'{"seq":1,"ts":"2026-11-15T08:00:00","temp":36.5}',         # 无时区
            b'{"seq":1,"ts":"2026-11-15T16:00:00+08:00","temp":36.5}',   # 非 UTC
            b'{"seq":1,"ts":"not-a-time","temp":36.5}',                  # 非法时刻
            b'{"seq":-1,"ts":"2026-11-15T08:00:00Z","temp":36.5}',       # 负序号
            b'{"seq":true,"ts":"2026-11-15T08:00:00Z","temp":36.5}',     # 布尔序号
            b'{"seq":1.5,"ts":"2026-11-15T08:00:00Z","temp":36.5}',      # 浮点序号
            b'[1,2,3]',                                                  # 非对象
            b'{broken json',                                             # 坏 JSON
            b'',                                                         # 空行
        ]
        for i, bad in enumerate(bad_lines):
            with self.subTest(case=i, line=bad):
                shutil.rmtree(self.log_dir)
                os.makedirs(self.log_dir)
                for suffix in ("", "-wal", "-shm"):
                    p = self.db_path + suffix
                    if os.path.exists(p):
                        os.remove(p)
                app = qc.create_app(log_dir=self.log_dir, db_path=self.db_path)
                client = app.test_client()
                self.write(bad + b"\n")
                resp = client.post("/api/collect")
                self.assertEqual(resp.status_code, 409, f"用例{i}应被拒绝: {bad!r}")
                self.assertEqual(client.get("/api/stats").get_json()["total"], 0)

    def test_csv_download(self):
        self.lines(1, 2, 3)
        self.collect()
        resp = self.client.get("/download.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)
        rows = resp.get_data(as_text=True).strip().split("\n")
        self.assertEqual(rows[0], "seq,ts,temp,source")
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[1].startswith("1,"))

    def test_web_pages(self):
        self.lines(1, 2)
        self.client.post("/collect")  # 表单入口
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("已入库记录", body)
        self.assertIn("active.ndjson", body)

    def test_file_recreated_smaller_resets_offset(self):
        self.lines(1, 2, 3, 4, 5)
        self.collect()
        # 设备新建同名小文件(轮转), 序号接续递增
        self.lines(6, 7)
        code, stats = self.collect()
        self.assertEqual(stats["added"], 2)
        self.assertEqual(self.seqs(), [1, 2, 3, 4, 5, 6, 7])


if __name__ == "__main__":
    unittest.main()
