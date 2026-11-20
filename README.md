# 水质平台 · 报文契约发布

固件升级前确认新报文能被分析程序读取、程序升级后仍能读旧归档的契约发布网页。
按设备型号登记**历代固件写入格式**（只追加）与**现用程序读取格式**（按程序号替换），
各至多 8 份。每次发布前用 **z3** 对「全部历代写入格式 × 全部现用程序」做**全量**兼容判定
（逻辑证明，非抽样试样例）：任一写入格式允许的全部报文必须被每个现用程序接受，
否则拒绝发布，并给出**合法反例报文**与**拒收程序**。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `pgdata`（PostgreSQL），服务重启后已发布格式与试跑记录保留。

## 网页操作

1. 首页创建设备型号（如 `WQ-5000`），进入型号管理页。
2. 在「发布新格式」粘贴受限 JSON Schema：
   - 左栏**追加固件写入格式**（版本号自动递增，只追加）；
   - 右栏**发布/替换程序读取格式**（同一程序号覆盖旧版）。
3. 发布结果即时展示：成功，或拒绝 + 每个不兼容组合的反例报文与拒收程序；
   页面下方「发布日志」保留最近 50 条成功/拒绝/无效记录（含拒绝依据）。
4. 页面内置三个示例，点按钮即可填入编辑器：
   - **温度扩范围**：固件 v2 扩到 -40~100℃，直接发布会被旧程序（-10~50℃）拒收，
     反例如 `{"temp": -40, ...}`；先升级程序再发固件则通过。
   - **质控码缺省**：固件缺省 `qc`（或写 `null`），把 `qc` 列为 `required` 的程序会被拒，
     反例为不含 `qc` 的报文；程序改为可选并允许 `null` 后通过。
   - **顶层 anyOf**：程序同时接受多种报文形态。

## 现场试跑报文生成

程序升级后仍拒收水质报文时，数据工程师需要确认分析程序实际用了哪版读取契约。
型号页「生成现场试跑报文」按同一程序选 2～6 条**成功发布记录**作为候选契约，
在**同一已提交视图**（固件格式 + 候选契约快照）上生成一组可拿到现场试跑的报文：

- 每条试跑报文都至少符合一种已发布的固件写入格式；
- 凡能用合法报文区分收拒结果的两版，报文组中至少有一条报文能区分它们；
- 报文总数最少（枚举全部可达收拒向量 + 精确集合覆盖）；
- 全部合法报文上收拒相同的版本归为一组，网页逐条列出各版本「接收 / 拒收」；
- 无法证明报文数最少（z3 unknown、超时、复核失败、求解器繁忙）时提示**本次未能生成**，
  不输出报文组；生成记录（含失败原因）留存于 `trial_runs`。

「同一已提交视图」在 PostgreSQL 用 `REPEATABLE READ` 事务固定，SQLite 用显式
`BEGIN` 取共享读锁；生成期间其他发布不会混入快照。

## 受限 JSON Schema 语法（其余一律拒绝）

- 平面对象：`{"type":"object","properties":{...},"required":[...],"additionalProperties":false}`
  （`required` 可省略，默认空；`additionalProperties` 必须显式为 `false`）
- 顶层 `anyOf`：`{"anyOf":[<平面对象>, ...]}`，且只能单独出现在顶层
- 整数上下界：`{"type":"integer","minimum":m,"maximum":M}`（上下界均可省略）
- 字符串/整数/空值枚举：`{"enum":[...]}`，可配 `"type"` 限定；枚举值只允许字符串、整数、`null`
- 空值类型：`{"type":"null"}`

嵌套对象、数组、`pattern`、`oneOf`/`allOf`/`not`、浮点/布尔值、`string` 无枚举等语法均被拒绝。

## 校验原理

对每对（写入格式 W，读取格式 R），把报文编码为 z3 变量（每个字段：是否出现、
值类型 null/int/string、整数值、字符串枚举下标），求解 `W 合法 ∧ R 拒收`：

- **UNSAT** ⇒ 不存在反例，`L(W) ⊆ L(R)` 成立——这是对全部报文的证明，不是抽样；
- **SAT** ⇒ 从模型还原一个反例报文，并用独立的纯 Python 判定器复核
  （确实满足 W、确实被 R 拒收）后随拒绝结果返回。

试跑报文生成复用同一编码：对每个候选版本用 `mask` 指定接收/拒收，求解
「满足某固件格式 ∧ 各版本收拒恰为 mask」的报文，穷举全部非零 mask；
任何一步 unknown 或超时都视为「未能证明」，不会输出结果。z3 默认上下文非线程安全，
所有 z3 操作经进程内锁串行化。

## 并发与事务

- 每次发布在事务内先对型号行加锁（PostgreSQL `SELECT ... FOR UPDATE`），
  再基于最新已提交状态做**全量交叉检查**后落库——并发升级被串行化，
  不会各自通过却留下不兼容组合；
- 检查失败整体回滚，**已发布格式不变**；拒绝/无效尝试另写发布日志留痕；
- 固件格式只追加（无修改/删除入口），程序格式按程序号替换；各至多 8 份。

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/models` | 创建型号 `{"name": "WQ-5000"}` |
| GET | `/api/models/{name}` | 查看当前固件/程序格式 |
| GET | `/api/models/{name}/log` | 发布日志（含拒绝依据） |
| POST | `/api/models/{name}/firmware` | 追加固件格式 `{"schema": {...}}` |
| POST | `/api/models/{name}/programs` | 发布/替换程序格式 `{"program_id": "...", "schema": {...}}` |
| GET | `/api/models/{name}/programs/{program_id}/publishes` | 同一程序的成功发布记录（候选契约） |
| POST | `/api/models/{name}/trials` | 生成试跑报文 `{"program_id": "...", "record_ids": [..2~6..]}` |
| GET | `/api/models/{name}/trials/{id}` | 取试跑报文组（含快照、分组、最少性证明） |

响应：成功 `200 {"ok": true, ...}`；不兼容 `409 {"ok": false, "problems": [...]}`；语法/上限错误 `422`。
试跑生成成功 `200`（含 `messages/groups/proof`），未能证明最少时为 `409 {"ok": false, "error": ...}`。

## 本地开发与自测

```bash
pip install -r app/requirements.txt httpx
python3 tests/test_units.py                 # 语法子集 + z3 判定单元自测
python3 tests/test_trial.py                 # 试跑生成：分组/最少性/穷举对拍/未证明路径
cd app && DATABASE_URL=sqlite:////tmp/contracts.db uvicorn main:app --port 8000 &
python3 tests/selftest.py                   # 集成自测（示例/上限/并发/试跑/页面）
```

`DATABASE_URL` 缺省为 SQLite（便于本地自测）；Compose 中注入 PostgreSQL 连接串。

## 目录结构

```
app/
  main.py         FastAPI 入口（HTML 页面 + JSON API）
  service.py      发布事务：加锁 → 全量检查 → 落库/回滚；试跑生成与快照
  solver.py       z3 编码与全量兼容判定（含反例复核）
  trial.py        试跑报文：可达收拒向量枚举 + 分组 + 精确最小覆盖
  schema_spec.py  受限 JSON Schema 子集解析与纯 Python 报文判定
  db.py           SQLAlchemy 模型（PostgreSQL JSONB / SQLite JSON）
  examples.py     温度扩范围、质控码缺省、anyOf 示例
  templates/      页面模板（型号页 / 试跑报文页）
tests/
  test_units.py   单元自测
  test_trial.py   试跑生成自测（含穷举对拍）
  selftest.py     集成自测
docker-compose.yml
```
