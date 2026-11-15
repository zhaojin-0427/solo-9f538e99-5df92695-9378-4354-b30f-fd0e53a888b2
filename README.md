# 发酵罐温度日志质控采集系统

Flask + SQLite 实现的质控记录网页: 读取设备持续追加的 NDJSON 温度日志,
点击“立即采集”把上次成功后的新增记录整理入库, 可在线查看并下载 CSV。

## 设备行为约定

- 设备向挂载目录中的 `active.ndjson` 持续追加 NDJSON 温度日志;
- 轮转时把 `active.ndjson` 重命名为 `active.ndjson.1`, 再新建同名 `active.ndjson`;
- 归档不会被删除或覆盖, 目录最多保留这两份文件;
- 轮转后旧文件(`active.ndjson.1`)仍可能有缓冲数据续写落盘。

## 采集语义(不漏、不重、可拒绝)

- **增量字节**: 每个文件按字节偏移续采, 只处理上次成功后的新增内容;
  偏移存于 SQLite, 服务重启后接续采集。
- **半行留待下次**: 只纳入以 `\n` 结束的完整行, 末尾未换行的半行不入库、
  不推进偏移, 待设备补全后下轮再采。
- **合法记录**: JSON 对象, `seq` 为非负整数且同一文件内严格递增,
  `ts` 为 UTC 时刻(ISO8601, `Z` 或 `+00:00`), `temp` 为有限数值
  (拒绝 `NaN`/`Infinity`/字符串)。
- **幂等去重**: 序号已存在且内容相同 → 跳过(重复点击、轮转重读、重启均不重记)。
- **整轮拒绝**: 同序号异内容, 或任何非法行 → 整轮回滚,
  已入库记录与采集进度(偏移)全部不变; 修复后下一轮正常接续。
- **轮转透明**: 两个文件名各自跟踪偏移; 同名文件变小(轮转新建)则从头续采;
  旧文件续写按自身偏移继续追加, 与采集任意交错都不漏不重。

## 快速开始(Docker Compose)

```bash
docker compose up --build
# 浏览器打开 http://localhost:8080
```

- 宿主机 `./data` 挂载为容器 `/data`(只读), 设备/模拟器往这里写日志;
- SQLite 库在命名卷 `qc-state` 中, 独立于日志目录持久化;
- 点击页面“立即采集”或 `curl -X POST http://localhost:8080/api/collect`。

## 演示(半行 + 轮转 + 拒绝 + 幂等)

服务启动后, 一键跑完整演示:

```bash
bash demo/demo.sh                 # 默认打 http://localhost:8080
DEMO_RESTART=1 bash demo/demo.sh  # 含 docker compose restart 重启接续演示
```

演示流程: 正常采集 → 半行挂起 → 半行补全 → 轮转 → 新文件写入 +
旧文件缓冲落盘续写 → 重复点击幂等 → NaN 非法行整轮拒绝 →
同序号异内容整轮拒绝 → 恢复后续采 → CSV 导出。

也可以手动逐步操作设备模拟器(仅标准库, 直接写 `./data`):

```bash
python3 demo/device_sim.py write 5      # 追加 5 条合法记录
python3 demo/device_sim.py partial      # 写半行(无换行)
python3 demo/device_sim.py complete     # 补全半行
python3 demo/device_sim.py buffer 2     # 生成 2 条进设备缓冲(不落盘)
python3 demo/device_sim.py rotate       # 轮转: active.ndjson -> active.ndjson.1
python3 demo/device_sim.py flush-old    # 缓冲落盘, 续写旧文件
python3 demo/device_sim.py invalid      # 追加非法记录(NaN)
python3 demo/device_sim.py conflict     # 追加同序号异内容记录
python3 demo/device_sim.py undo         # 撤销上一次追加
python3 demo/device_sim.py reset        # 清空日志与设备状态
python3 demo/device_sim.py status       # 查看设备状态
```

## 记录格式

```json
{"seq":1,"ts":"2026-11-15T08:00:00Z","temp":36.55}
```

| 字段   | 要求                                                        |
| ------ | ----------------------------------------------------------- |
| `seq`  | 非负整数, 同一文件内严格递增; 全局唯一(同序号同内容幂等去重) |
| `ts`   | ISO8601 UTC 时刻(`Z` 或 `+00:00`)                            |
| `temp` | JSON 数值且有限(拒绝 `NaN`、`Infinity`、字符串)              |

## 网页与 API

| 入口                | 说明                                             |
| ------------------- | ------------------------------------------------ |
| `GET /`             | 记录表格(最新 200 条)、文件偏移进度、采集按钮     |
| `POST /collect`     | 表单采集(网页按钮), 结果以闪现消息展示            |
| `GET /download.csv` | 下载全部记录 CSV                                  |
| `POST /api/collect` | 采集, 返回 JSON; 整轮拒绝时 HTTP 409              |
| `GET /api/stats`    | 总数、序号范围、各文件偏移与最大序号              |
| `GET /api/records`  | 记录 JSON(`limit`/`offset` 分页)                  |

## 本地开发与测试

```bash
pip install -r app/requirements.txt
python3 -m unittest discover -s tests -v     # 16 个用例
LOG_DIR=./data DB_PATH=./state/qc.db PORT=8080 python3 app/app.py
```

测试覆盖: 半行挂起/补全、轮转前后采集、旧文件续写、归档重读去重、
非法行与同序号异内容整轮拒绝(进度不变)、序号未递增拒绝、
服务重启接续、同名小文件重建、CSV 导出、16 种非法行矩阵。

## 目录结构

```
├── docker-compose.yml      # Compose 入口(日志目录只读挂载 + SQLite 卷)
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py              # Flask 应用与采集主逻辑(单事务)
│   └── templates/index.html
├── demo/
│   ├── device_sim.py       # 设备模拟器(半行/轮转/续写/非法行/冲突)
│   └── demo.sh             # 端到端演示脚本
├── tests/test_app.py       # unittest 测试套件
└── data/                   # 设备日志目录(挂载点)
```

## 配置(环境变量)

| 变量         | 默认值            | 说明                 |
| ------------ | ----------------- | -------------------- |
| `LOG_DIR`    | `/data`(容器内)   | 设备日志目录         |
| `DB_PATH`    | `/state/qc.db`    | SQLite 库路径        |
| `PORT`       | `5000`            | 服务端口(容器内)     |
| `SECRET_KEY` | `dev-only-secret` | Flask 会话密钥       |
