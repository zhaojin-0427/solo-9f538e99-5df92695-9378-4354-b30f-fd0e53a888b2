#!/usr/bin/env bash
# 端到端演示: 半行、轮转、旧文件续写、整轮拒绝、重复点击幂等、CSV 导出。
# 前提: 服务已启动( docker compose up --build 或本地 python3 app/app.py )。
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${BASE:-http://localhost:8080}"
SIM="python3 demo/device_sim.py"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
collect() { curl -sS -X POST "$BASE/api/collect" | python3 -m json.tool; }
stats() { curl -sS "$BASE/api/stats" | python3 -m json.tool; }

step "0. 重置设备日志(数据库不动)"
$SIM reset

step "1. 设备写入 5 条完整记录"
$SIM write 5
echo "-- 采集(预期 added=5)"
collect

step "2. 设备写入半行(无换行符)"
$SIM partial
echo "-- 采集(预期 added=0, pending_bytes>0, 半行留待下次)"
collect

step "3. 半行补全, 再写 2 条"
$SIM complete
$SIM write 2
echo "-- 采集(预期 added=3: 补采半行+2条新记录)"
collect

step "4. 轮转前设备缓冲 2 条(未落盘), 然后轮转"
$SIM buffer 2
$SIM rotate
$SIM status

step "5. 新 active.ndjson 写 4 条; 随后缓冲落盘续写旧文件 active.ndjson.1"
$SIM write 4
$SIM flush-old
echo "-- 采集(预期 added=6: 新文件4条+旧文件续写2条, 序号连续不漏不重)"
collect

step "6. 重复点击采集(幂等, 预期 added=0)"
collect

step "7. 设备写入非法记录(NaN 温度)"
$SIM invalid
echo "-- 采集(预期整轮拒绝, HTTP 409, 进度不变)"
collect || true
stats
echo "-- 撤销非法行"
$SIM undo

step "8. 设备写入同序号异内容记录(故障固件)"
$SIM conflict
echo "-- 采集(预期整轮拒绝, HTTP 409)"
collect || true
echo "-- 撤销冲突行"
$SIM undo

step "9. 恢复正常: 再写 2 条"
$SIM write 2
echo "-- 采集(预期 added=2, 序号接续)"
collect

if [ "${DEMO_RESTART:-0}" = "1" ] && command -v docker >/dev/null 2>&1; then
  step "10. 重启服务(docker compose restart), 验证进度持久化"
  docker compose restart web
  sleep 3
  echo "-- 重启后采集(预期 added=0, 不漏不重)"
  collect
fi

step "11. CSV 导出(前 6 行)"
curl -sS "$BASE/download.csv" | head -6

step "完成, 最终统计"
stats
echo
echo "浏览器打开 $BASE 可查看记录表格并下载 CSV。"
