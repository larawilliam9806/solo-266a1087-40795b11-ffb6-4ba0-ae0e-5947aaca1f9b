# 哨点病例脱敏副本发布服务

研究方按年龄段和邮区分析哨点病例。病例清单以整批方式提交，平台按 k-匿名
规则生成**逐行脱敏副本**对外供数；清单一旦修订，旧副本立即撤下，不再供数。

周报定稿时，数据员还需要分清每个病例是**迟报、重新列入，还是因发病日期
订正才计入本周**。服务在提交中接收可选发病日期，并提供令牌保护的**周报
核对 API**：按病例号对照初报版与定稿版，输出两版的该周病例数、日期未知数，
以及移出/新计入例数与四类新计入原因的计数。

技术栈：FastAPI + SQLite（WAL），支持 `docker compose` 一键启动。

## 快速开始

```bash
docker compose up --build        # 服务监听 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_DB_PATH=./app.db APP_RECONCILE_TOKEN=dev-token \
  .venv/bin/uvicorn app.main:app --reload
```

运行测试：

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/submissions` | 整批提交新清单（≤40 条，可带发病日期） |
| GET  | `/api/publication` | 公开下载当前发布的脱敏副本与参数 |
| GET  | `/api/status`      | 当前发布状态（published / withdrawn / empty） |
| GET  | `/api/submissions` | 提交历史（仅元数据，不含原始病例） |
| GET  | `/api/weekly-reconciliation` | 周报核对（令牌保护，未发布版本也可核对） |
| GET  | `/health`          | 健康检查 |

### POST /api/submissions

```json
{
  "k": 3,
  "cases": [
    {"case_id": "A1", "age": 34, "zip": "10001", "disease": "流感", "onset_date": "2026-11-17"},
    {"case_id": "A2", "age": 36, "zip": "10002", "disease": "哮喘"}
  ]
}
```

- `onset_date` 可选，格式严格为 `YYYY-MM-DD`（非法日期返回 422）；
  未填（或缺省/null）按**发病日期未知**入库，核对时计入该版日期未知数。
- 首报登记与清单写入在**同一事务**内生效：每个病例首次入库的版号登记到
  `case_registry`；任一步写入失败整体回滚，原清单与公开副本保持不变。

**合法性校验**（任一不满足即 HTTP 422，且不影响已发布的旧版本）：

- 整批 1～40 条；`k` 为 2～5 的整数；
- `case_id` 批内唯一、非空；`age` 为 0～100 的整数；
- `zip` 为五位数字；`disease` 非空；`onset_date` 为合法 `YYYY-MM-DD` 或 null。

**有效提交的处理**（在单个事务内完成）：

1. 立即撤下旧脱敏副本（删除旧副本行、清空发布指针）；
2. 在 9 种参数组合（年龄段 5/10/20 岁 × 邮区保留前 3/2/0 位）中寻找可行发布：
   - 每个可见（年龄段 × 邮区）组合须 ≥ k 条且含 ≥ 2 种病种；
   - 不达标组合只能整组抑制，抑制总数 ≤ 2 条，且至少保留一个组合；
   - 优先保留更多病例；并列时依次选更细的年龄段、更长的邮区前缀；
3. 可行则发布新版本（HTTP 201，`status=published`，含参数与被抑制病例号）；
   不可行则保持撤下（HTTP 201，`status=withdrawn`，附原因与逐组合诊断）。

### GET /api/weekly-reconciliation

令牌保护接口，请求头须带 `Authorization: Bearer <APP_RECONCILE_TOKEN>`
（恒定时间比较；未配置令牌或令牌错误均返回 401）。查询参数：

| 参数 | 说明 |
|---|---|
| `week_start` | 周一日期 `YYYY-MM-DD`（非周一返回 422） |
| `initial_version` | 初报提交版号（须 < 定稿版号，否则 422） |
| `final_version` | 定稿提交版号（版号不存在返回 404） |

核对**只读取指定两个版本的病例明细**（外加 `case_registry` 的首次入库版号），
脱敏副本未发布（withdrawn）的版本同样可以核对。发病日期落在
`[week_start, week_start+7天)` 内即属于该周；日期未知的病例不归属任何一周。

响应只含版号、周起日与计数（不含任何病例号或病例明细）：

```json
{
  "week_start": "2026-11-16",
  "initial_version": 2,
  "final_version": 3,
  "initial_week_cases": 8,
  "final_week_cases": 10,
  "initial_unknown_onset": 3,
  "final_unknown_onset": 1,
  "moved_out": 1,
  "newly_counted": 3,
  "newly_listed": 1,
  "late_reported": 1,
  "date_filled": 0,
  "cross_week_corrected": 1
}
```

计数口径：

- `initial_week_cases` / `final_week_cases`：各版发病日期落在该周的病例数；
- `initial_unknown_onset` / `final_unknown_onset`：各版**整版**发病日期未知的例数；
- `moved_out`：初报版在该周、定稿版不在该周的病例数；
- `newly_counted`：定稿版新计入该周的病例数，等于下面四类之和，并按病例号细分：
  - `late_reported`（迟报）：初报版缺席，且首次入库版号晚于初报版；
  - `newly_listed`（重新列入）：初报版缺席，但初报版之前曾入库（首次入库版号 ≤ 初报版）；
  - `date_filled`（补填日期）：初报版在册但发病日期未知，定稿版补填后计入本周；
  - `cross_week_corrected`（跨周订正）：初报版在册且发病日期在别周，定稿版订正到本周。

守恒关系：`final_week_cases - initial_week_cases = newly_counted - moved_out`。

### GET /api/publication

- 已发布：HTTP 200，返回同一完整版本的 `version`、`k`、`params`
  （`age_band_years`、`zip_prefix_digits`）、计数与逐行副本；
  副本每行只含泛化字段与病种，如 `{"age_band": "30-39", "zip": "100**", "disease": "流感"}`；
- 已撤下/无数据：HTTP 404，附撤下原因。

## 一致性设计

- **不过期发布**：发布在提交请求内同步完成，写路径持全局可重入锁并以
  `BEGIN IMMEDIATE` 单事务提交，发布与再次提交交错时不会出现过期清单；
- **不撕裂下载**：读路径持同一把锁，副本行与参数始终来自同一完整版本；
- **首报登记同事务**：`case_registry` 的首次入库版号与清单、发布指针同事务
  写入，写入失败回滚后原清单与公开副本保留；
- **重启不恢复**：发布指针持久化于 SQLite（`service_state.published_version`），
  旧副本行在撤下时即物理删除，重启后已撤下副本不可恢复；
- **非法不影响旧版**：所有合法性校验在写库之前完成（Pydantic，HTTP 422）。

## 旧库升级

旧库的 `cases` 表没有 `onset_date`、也没有首次入库登记表。服务启动时自动迁移：

1. `ALTER TABLE` 补 `onset_date` 列（旧病例一律为未知）；
2. 按病例在 `cases` 中出现过的最小版号回填 `case_registry`，
   **保留每个病例首次入库的版号**，迟报/重新列入的区分对历史数据同样成立。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DB_PATH` | `./app.db`（容器内 `/data/app.db`） | SQLite 数据库路径 |
| `APP_RECONCILE_TOKEN` | 空（核对接口返回 401） | 周报核对接口的 Bearer 令牌；docker compose 下默认 `change-me`，生产必须覆盖 |

## 目录结构

```
app/
  main.py        FastAPI 入口、路由与核对令牌校验
  models.py      请求模型与合法性校验（含发病日期）
  generalize.py  k-匿名泛化与最优参数选择
  store.py       SQLite 持久化、发布状态机、首报登记、旧库迁移与周报核对
tests/
  test_api.py          原有发布/撤下/并发一致性测试
  test_reconcile.py    发病日期、周报核对、令牌、事务回滚与旧库迁移测试
Dockerfile / docker-compose.yml / requirements.txt
```
