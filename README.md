# 电表累计读数 -> 区间用电量 API

把电表的**累计读数**（Wh）增量转成相邻读数之间的**区间用电量**，供负荷分析使用。
FastAPI + PostgreSQL，区间由数据库函数做持久化的增量维护。

## 1. 业务规则

- 读数字段：`id`、`meter_no`（电表号）、`read_at`（UTC 时刻）、`reading`（0～999999 整数 Wh）、
  `replaced`（换表标记）；同表同时刻唯一（数据库唯一约束保证）。
- 同一电表按时刻排序，相邻两条读数 `(a, b)` 形成**左开右闭**区间 `(a.read_at, b.read_at]`：
  - 首条读数之前**无区间**；
  - 若右端读数 `b.replaced = true`（换表后首读），区间电量**缺测** `energy_wh = NULL`；
  - 否则 `energy_wh = b.reading - a.reading`；差值为负时按 **1,000,000 单次回绕**
    （即 `+1000000`）。
- 补录 / 更正 / 删除读数都会**增量**修正相邻区间，不残留旧的跨越段；
  更改电表号会同时修正**旧表与新表**两只电表。

## 2. 快速启动（Docker Compose）

```bash
docker compose up --build
```

- API： http://localhost:8000  ，交互式文档： http://localhost:8000/docs
- PostgreSQL： localhost:55438（容器内 5432），库 `meterdb`，用户/密码 `postgres/postgres`
- 启动时 `api` 容器会等待数据库就绪，再幂等执行 `sql/` 下迁移，然后启动 uvicorn。
- 数据存放在命名卷 `pgdata`，**重启保留**。

健康检查：

```bash
curl http://localhost:8000/health
```

## 3. HTTP 接口（详见 /docs）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/readings` | 新增读数（补录亦可，自动拆分区间） |
| PUT | `/readings/{id}` | 更正读数（含改电表号，修正两只表） |
| DELETE | `/readings/{id}` | 删除读数（相邻区间自动合并） |
| GET | `/readings?meter_no=...` | 查读数 |
| GET | `/intervals?meter_no=...` | 查区间电量 |
| GET | `/supply?meter_no=...` | **同一提交快照**返回读数 + 区间 |
| GET | `/meters` | 电表清单 |

写入示例：

```bash
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","read_at":"2026-01-01T00:00:00Z","reading":999900}'
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","read_at":"2026-01-01T01:00:00Z","reading":100}'   # 回绕 -> 200 Wh
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"meter_no":"M-001","read_at":"2026-01-01T02:00:00Z","reading":500,"replaced":true}' # 缺测
```

## 4. 独立 SQL 示例（READ COMMITTED，psql 直连即可）

API 与手工 SQL 走的是同一套数据库函数，行为完全一致：

- [`docs/examples/01_backfill.sql`](docs/examples/01_backfill.sql)：补录历史读数，旧区间自动拆分。
- [`docs/examples/02_meter_replace.sql`](docs/examples/02_meter_replace.sql)：换表缺测 + 改电表号修正两只表。
- [`docs/examples/03_concurrency.sql`](docs/examples/03_concurrency.sql)：同表并发串行、异表并发不阻塞。

## 5. 并发与一致性设计（要点）

- 每个写事务先在 `meters` 表上按 `meter_no` **排序加行锁**（`SELECT ... FOR UPDATE`），
  之后才操作该表的读数/区间：
  - 同表并发写入被**串行化**，后到者在 READ COMMITTED 下看到已提交的最新前驱/后继，
    因此不漏段、不重叠；
  - 不同电表锁不同行，**互不阻塞**；
  - 固定加锁顺序使跨两只电表的改号事务**不会死锁**。
- 区间是**持久化增量维护**：每次写入只读取前驱、后继两条读数，改动至多三条区间行，
  走索引（见下文验证），不重扫整只电表；查询直接读区间表，不临时全量计算。
- 写操作在单事务内完成，任一步失败**整体回滚**。
- `/supply`（及读接口）使用 **REPEATABLE READ** 单事务，读数与区间取自同一个完整提交快照。

## 6. 目录结构

```
sql/01_schema.sql          表结构、约束、电量纯函数
sql/02_functions.sql       电表行锁 + 增量维护函数 + 一致性体检
app/                       FastAPI 应用（连接池、模型、路由、错误映射）
scripts/migrate.py         幂等迁移
scripts/entrypoint.sh      容器入口（等库 -> 迁移 -> 启动）
scripts/concurrency_stress.py  真实并发压测（在容器/正常环境运行）
docs/examples/*.sql        补录 / 换表 / 并发独立 SQL 示例
tests/                     pytest 自测（22 项）
docker-compose.yml / Dockerfile / requirements.txt
```

## 7. 自测

```bash
# 需要一个可连的 PostgreSQL（通过 DATABASE_URL 指定，库需已存在）
export DATABASE_URL=postgresql://postgres:postgres@localhost:55438/meterdb
python scripts/migrate.py            # 或由 compose 自动完成
PYTHONPATH=. pytest tests/
```

真实阻塞/唤醒并发压测（依赖 PostgreSQL 进程信号，需在容器或正常主机运行）：

```bash
docker compose exec api python scripts/concurrency_stress.py
```

一致性巡检（无行即完全一致，正常写入路径不依赖它、不做全表重算）：

```sql
SELECT * FROM interval_consistency_report();
```
