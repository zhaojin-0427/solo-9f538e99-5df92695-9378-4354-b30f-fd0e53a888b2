#!/usr/bin/env python3
"""发酵罐温度记录仪模拟器(本地示例).

向数据目录写入 NDJSON 温度日志, 复现设备真实行为:

  write N       向 active.ndjson 追加 N 条合法记录
  partial       写入半行(无换行符), 模拟一条记录写到一半
  complete      把之前的半行补全(追加剩余字节和换行符)
  buffer N      生成 N 条记录放进设备缓冲(暂不落盘)
  flush-old     缓冲落盘: 追加到 active.ndjson.1(轮转后旧文件续写)
  rotate        轮转: active.ndjson -> active.ndjson.1, 新建空 active.ndjson
  invalid       向 active.ndjson 追加一条非法记录(NaN 温度)
  conflict      向 active.ndjson.1 追加一条"同序号异内容"记录
  undo          撤销上一次追加(把文件截断回追加前的大小)
  reset         清空数据目录与设备状态, 可重新演示
  status        查看设备状态与文件大小

记录格式: {"seq": 1, "ts": "2026-11-15T08:00:00Z", "temp": 36.55}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.environ.get("LOG_DIR", os.path.join(os.path.dirname(HERE), "data"))
STATE_PATH = os.path.join(HERE, "device_state.json")

ACTIVE = "active.ndjson"
ARCHIVE = "active.ndjson.1"

STEP = timedelta(seconds=10)


def load_state() -> dict:
    if os.path.isfile(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {
        "next_seq": 1,
        "next_ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "pending_partial": None,   # {"file":..., "rest":...}
        "buffer": [],              # 已生成未落盘的完整行
        "undo": None,              # {"file":..., "size":...}
    }


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def make_line(state: dict) -> str:
    """按状态生成下一条合法记录, 推进序号与时刻."""
    seq = state["next_seq"]
    ts = datetime.fromisoformat(state["next_ts"])
    temp = round(36.5 + 0.05 * ((seq * 7) % 11 - 5), 2)
    line = json.dumps(
        {"seq": seq, "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "temp": temp},
        separators=(",", ":"),
    )
    state["next_seq"] = seq + 1
    state["next_ts"] = (ts + STEP).isoformat()
    return line


def remember_undo(state: dict, path: str) -> None:
    size = os.path.getsize(path) if os.path.exists(path) else 0
    state["undo"] = {"file": path, "size": size}


def append_bytes(state: dict, path: str, payload: bytes) -> None:
    remember_undo(state, path)
    with open(path, "ab") as fh:
        fh.write(payload)


def cmd_write(state, args):
    path = os.path.join(args.dir, ACTIVE)
    lines = [make_line(state) + "\n" for _ in range(args.n)]
    append_bytes(state, path, "".join(lines).encode())
    print(f"已向 {ACTIVE} 追加 {args.n} 条 (seq {state['next_seq'] - args.n}..{state['next_seq'] - 1})")


def cmd_buffer(state, args):
    for _ in range(args.n):
        state["buffer"].append(make_line(state) + "\n")
    print(f"已生成 {args.n} 条放入设备缓冲(未落盘), 缓冲共 {len(state['buffer'])} 条")


def cmd_flush_old(state, args):
    if not state["buffer"]:
        sys.exit("设备缓冲为空, 先执行 buffer N")
    path = os.path.join(args.dir, ARCHIVE)
    if not os.path.exists(path):
        sys.exit(f"{ARCHIVE} 不存在, 先执行 rotate")
    append_bytes(state, path, "".join(state["buffer"]).encode())
    print(f"缓冲落盘: 已向 {ARCHIVE} 续写 {len(state['buffer'])} 条(轮转后旧文件续写)")
    state["buffer"] = []


def cmd_partial(state, args):
    if state["pending_partial"]:
        sys.exit("已有未补全的半行, 先执行 complete")
    line = make_line(state) + "\n"
    cut = len(line) // 2
    path = os.path.join(args.dir, ACTIVE)
    append_bytes(state, path, line[:cut].encode())
    state["pending_partial"] = {"file": path, "rest": line[cut:]}
    print(f"已向 {ACTIVE} 写入半行({cut} 字节, 无换行符): {line[:cut]!r}")


def cmd_complete(state, args):
    pending = state["pending_partial"]
    if not pending:
        sys.exit("没有待补全的半行, 先执行 partial")
    append_bytes(state, pending["file"], pending["rest"].encode())
    print(f"已补全半行(追加 {len(pending['rest'])} 字节, 含换行符)")
    state["pending_partial"] = None


def cmd_rotate(state, args):
    if state["pending_partial"]:
        sys.exit("存在未补全的半行, 为避免演示混乱请先 complete")
    src = os.path.join(args.dir, ACTIVE)
    dst = os.path.join(args.dir, ARCHIVE)
    if not os.path.exists(src):
        sys.exit(f"{ACTIVE} 不存在, 先执行 write")
    if os.path.exists(dst):
        sys.exit(f"{ARCHIVE} 已存在(归档不会被覆盖), 如需重新演示请先 reset")
    os.rename(src, dst)
    open(src, "wb").close()  # 设备新建同名空文件
    state["undo"] = None
    print(f"已轮转: {ACTIVE} -> {ARCHIVE}, 并新建空 {ACTIVE}")


def cmd_invalid(state, args):
    seq = state["next_seq"]  # 故障行, 不消耗序号
    ts = datetime.fromisoformat(state["next_ts"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f'{{"seq":{seq},"ts":"{ts}","temp":NaN}}\n'  # NaN: 非有限温度
    path = os.path.join(args.dir, ACTIVE)
    append_bytes(state, path, line.encode())
    print(f"已向 {ACTIVE} 追加非法记录(NaN 温度): {line.strip()}")


def cmd_conflict(state, args):
    seq = state["next_seq"] - 1
    if seq < 1:
        sys.exit("还没有已写入的记录, 无法制造冲突")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = json.dumps({"seq": seq, "ts": ts, "temp": 99.9}, separators=(",", ":")) + "\n"
    # 写到旧归档: 模拟故障固件把缓冲里被篡改的记录续写进旧文件
    name = ARCHIVE if os.path.exists(os.path.join(args.dir, ARCHIVE)) else ACTIVE
    path = os.path.join(args.dir, name)
    append_bytes(state, path, line.encode())
    print(f"已向 {name} 追加同序号异内容记录(seq={seq}, temp=99.9)")


def cmd_undo(state, args):
    undo = state["undo"]
    if not undo:
        sys.exit("没有可撤销的追加")
    with open(undo["file"], "r+b") as fh:
        fh.truncate(undo["size"])
    print(f"已撤销: {os.path.basename(undo['file'])} 截断回 {undo['size']} 字节")
    state["undo"] = None


def cmd_reset(state, args):
    for name in (ACTIVE, ARCHIVE):
        path = os.path.join(args.dir, name)
        if os.path.exists(path):
            os.remove(path)
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
    print(f"已清空 {args.dir} 下的日志与设备状态(数据库记录不受影响, 如需清空请删除 qc.db)")


def cmd_status(state, args):
    print(f"next_seq={state['next_seq']} next_ts={state['next_ts']}")
    print(f"缓冲 {len(state['buffer'])} 条, 半行 {'有' if state['pending_partial'] else '无'}")
    for name in (ACTIVE, ARCHIVE):
        path = os.path.join(args.dir, name)
        size = os.path.getsize(path) if os.path.exists(path) else "不存在"
        print(f"  {name}: {size} 字节" if size != "不存在" else f"  {name}: 不存在")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=DEFAULT_DIR, help="数据目录(默认 ../data 或 $LOG_DIR)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("write", "buffer"):
        p = sub.add_parser(name)
        p.add_argument("n", type=int)
    for name in ("flush-old", "partial", "complete", "rotate", "invalid", "conflict", "undo", "reset", "status"):
        sub.add_parser(name)
    args = parser.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    state = load_state()
    handler = {
        "write": cmd_write, "buffer": cmd_buffer, "flush-old": cmd_flush_old,
        "partial": cmd_partial, "complete": cmd_complete, "rotate": cmd_rotate,
        "invalid": cmd_invalid, "conflict": cmd_conflict, "undo": cmd_undo,
        "reset": cmd_reset, "status": cmd_status,
    }[args.cmd]
    handler(state, args)
    if args.cmd != "reset":
        save_state(state)


if __name__ == "__main__":
    main()
