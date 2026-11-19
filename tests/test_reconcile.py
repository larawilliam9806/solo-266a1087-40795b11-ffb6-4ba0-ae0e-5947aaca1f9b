"""周报核对 API 与首报登记/旧库迁移测试。"""
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

os.environ["APP_DB_PATH"] = ":memory:"  # 避免模块级 app 落盘
os.environ["APP_RECONCILE_TOKEN"] = "test-token-123"

from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402
from app.models import SubmissionIn  # noqa: E402

TOKEN = "test-token-123"
MONDAY = "2026-11-16"          # 2026-11-16 是周一
SUNDAY = "2026-11-22"
PREV_WEEK = "2026-11-12"       # 上一周（周四）
THIS_WEEK_TUE = "2026-11-17"
THIS_WEEK_SUN = "2026-11-22"


def case(cid, age, zipc, disease, onset=None):
    body = {"case_id": cid, "age": age, "zip": zipc, "disease": disease}
    if onset is not None:
        body["onset_date"] = onset
    return body


GOOD = [
    case("C1", 21, "11111", "flu"),
    case("C2", 22, "11112", "flu"),
    case("C3", 23, "11113", "cold"),
    case("C4", 24, "11114", "cold"),
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def submit(client, cases, k=2):
    r = client.post("/api/submissions", json={"k": k, "cases": cases})
    assert r.status_code == 201, r.text
    return r.json()["version"]


def reconcile(client, week=MONDAY, initial=1, final=2, token=TOKEN):
    return client.get(
        "/api/weekly-reconciliation",
        params={
            "week_start": week,
            "initial_version": initial,
            "final_version": final,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


# --------------------------------------------------------------- 发病日期入库
def test_onset_date_optional_and_validated(client):
    v = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu"),
        case("C", 23, "11113", "cold"),
        case("D", 24, "11114", "cold", "2026-11-20"),
    ])
    rows = client.app.state.store._conn.execute(
        "SELECT case_id, onset_date FROM cases WHERE version = ? ORDER BY case_id", (v,)
    ).fetchall()
    got = {r["case_id"]: r["onset_date"] for r in rows}
    assert got == {"A": THIS_WEEK_TUE, "B": None, "C": None, "D": "2026-11-20"}


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "16-11-2026", "2026/11/17", "not-a-date"])
def test_invalid_onset_date_rejected(client, bad):
    r = client.post("/api/submissions", json={
        "k": 2, "cases": [case("A", 1, "11111", "flu", bad)] + GOOD[1:],
    })
    assert r.status_code == 422


# ----------------------------------------------------------------- 令牌保护
def test_reconcile_requires_token(client):
    submit(client, GOOD)
    submit(client, GOOD)

    assert reconcile(client, token="wrong-token").status_code == 401
    no_header = client.get("/api/weekly-reconciliation",
                           params={"week_start": MONDAY, "initial_version": 1, "final_version": 2})
    assert no_header.status_code == 401
    raw = client.get("/api/weekly-reconciliation",
                     params={"week_start": MONDAY, "initial_version": 1, "final_version": 2},
                     headers={"Authorization": "TOKEN test-token-123"})
    assert raw.status_code == 401
    assert reconcile(client).status_code == 200


def test_reconcile_disabled_when_env_unset(client, monkeypatch):
    monkeypatch.delenv("APP_RECONCILE_TOKEN", raising=False)
    r = reconcile(client)
    assert r.status_code == 401
    assert "未配置" in r.json()["detail"]


# --------------------------------------------------------------- 参数校验
def test_week_start_must_be_monday(client):
    submit(client, GOOD)
    submit(client, GOOD)
    r = reconcile(client, week="2026-11-18")  # 周三
    assert r.status_code == 422


def test_version_order_and_existence(client):
    submit(client, GOOD)
    submit(client, GOOD)
    assert reconcile(client, initial=2, final=1).status_code == 422
    assert reconcile(client, initial=1, final=1).status_code == 422
    assert reconcile(client, initial=1, final=99).status_code == 404
    assert reconcile(client, initial=98, final=99).status_code == 404


# --------------------------------------------------------------- 核对主口径
def test_basic_week_counts_and_moved_out(client):
    # v1: A、B 在本周，C 在上一周
    v1 = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu", SUNDAY),
        case("C", 23, "11113", "cold", PREV_WEEK),
        case("D", 24, "11114", "cold", THIS_WEEK_SUN),
    ])
    # v2: A、D 仍在本周；B 订正到上一周（移出本周）；C 订正进本周
    v2 = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu", PREV_WEEK),
        case("C", 23, "11113", "cold", THIS_WEEK_TUE),
        case("D", 24, "11114", "cold", THIS_WEEK_SUN),
    ])
    r = reconcile(client, initial=v1, final=v2)
    assert r.status_code == 200
    body = r.json()
    assert body["initial_week_cases"] == 3   # A B D
    assert body["final_week_cases"] == 3     # A C D
    assert body["moved_out"] == 1            # B
    assert body["newly_counted"] == 1        # C
    assert body["cross_week_corrected"] == 1
    assert body["newly_listed"] == 0
    assert body["late_reported"] == 0
    assert body["date_filled"] == 0
    # 守恒：两版周病例数差 = 新计入 - 移出；新计入 = 四分类之和
    assert (body["final_week_cases"] - body["initial_week_cases"]
            == body["newly_counted"] - body["moved_out"])
    assert body["newly_counted"] == (
        body["newly_listed"] + body["late_reported"]
        + body["date_filled"] + body["cross_week_corrected"]
    )


def test_unknown_onset_counts_and_date_filled(client):
    v1 = submit(client, [
        case("A", 21, "11111", "flu"),                        # 未知
        case("B", 22, "11112", "flu"),                        # 未知
        case("C", 23, "11113", "cold", PREV_WEEK),
        case("D", 24, "11114", "cold", THIS_WEEK_TUE),
    ])
    v2 = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),         # 补填后计入本周
        case("B", 22, "11112", "flu"),                        # 仍未知
        case("C", 23, "11113", "cold", PREV_WEEK),
        case("D", 24, "11114", "cold", THIS_WEEK_TUE),
    ])
    body = reconcile(client, initial=v1, final=v2).json()
    assert body["initial_unknown_onset"] == 2
    assert body["final_unknown_onset"] == 1
    assert body["newly_counted"] == 1
    assert body["date_filled"] == 1
    assert body["cross_week_corrected"] == 0


def test_late_report_vs_newly_listed(client):
    # v1: 仅 A 在册（本周）
    v1 = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu"),
        case("C", 23, "11113", "cold"),
        case("D", 24, "11114", "cold"),
    ])
    # v2: B 曾在 v1 之前入库 -> 需要更早版本制造登记记录
    # 先造 v0：含 OLD 病例
    v0 = submit(client, [
        case("OLD", 31, "22221", "flu"),
        case("X2", 32, "22222", "flu"),
        case("X3", 33, "22223", "cold"),
        case("X4", 34, "22224", "cold"),
    ])
    # 重新提交 v1 的内容作为初报版（此时 OLD 已在更早的 v0 入库）
    v1b = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu"),
        case("C", 23, "11113", "cold"),
        case("D", 24, "11114", "cold"),
    ])
    # 定稿版：OLD 缺席于初报但初报前曾入库 -> 重新列入；
    # NEW 从未入库 -> 迟报
    v_final = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("OLD", 31, "22221", "flu", THIS_WEEK_TUE),
        case("NEW", 41, "33331", "cold", SUNDAY),
        case("X9", 42, "33332", "cold"),
    ])
    body = reconcile(client, initial=v1b, final=v_final).json()
    assert body["newly_counted"] == 2
    assert body["newly_listed"] == 1          # OLD
    assert body["late_reported"] == 1         # NEW
    assert body["date_filled"] == 0
    assert body["cross_week_corrected"] == 0
    # 登记表记录的首次入库版号
    reg = {r["case_id"]: r["first_seen_version"]
           for r in client.app.state.store._conn.execute("SELECT * FROM case_registry")}
    assert reg["OLD"] == v0
    assert reg["NEW"] == v_final
    assert reg["A"] == v1


def test_unpublished_withdrawn_versions_can_be_reconciled(client):
    # 制造一个撤下版本（3 条散点，任何参数都需抑制 3 条）
    scattered = [
        case("S1", 10, "11111", "flu", THIS_WEEK_TUE),
        case("S2", 50, "22222", "cold", THIS_WEEK_SUN),
        case("S3", 90, "33333", "flu", PREV_WEEK),
    ]
    v1 = submit(client, scattered)
    sub = client.get("/api/submissions").json()["submissions"]
    assert next(s for s in sub if s["version"] == v1)["status"] == "withdrawn"

    v2 = submit(client, GOOD)
    # 撤下版本同样可核对（核对只读病例明细，不依赖公开副本）
    r = reconcile(client, initial=v1, final=v2)
    assert r.status_code == 200
    assert r.json()["initial_week_cases"] == 2


def test_response_contains_only_versions_week_and_counts(client):
    v1 = submit(client, GOOD)
    v2 = submit(client, GOOD)
    body = reconcile(client, initial=v1, final=v2).json()
    assert set(body.keys()) == {
        "week_start", "initial_version", "final_version",
        "initial_week_cases", "final_week_cases",
        "initial_unknown_onset", "final_unknown_onset",
        "moved_out", "newly_counted",
        "newly_listed", "late_reported",
        "date_filled", "cross_week_corrected",
    }
    # 不泄露任何病例号或病例明细
    assert all(isinstance(v, int) or k == "week_start" for k, v in body.items())


def test_reconcile_reads_only_named_versions(client):
    # 三个版本，核对 v1/v3 时中间 v2 的病例不影响结果
    v1 = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu"),
        case("C", 23, "11113", "cold"),
        case("D", 24, "11114", "cold"),
    ])
    submit(client, [
        case("MID", 51, "44441", "flu", THIS_WEEK_TUE),
        case("M2", 52, "44442", "flu"),
        case("M3", 53, "44443", "cold"),
        case("M4", 54, "44444", "cold"),
    ])
    v3 = submit(client, [
        case("A", 21, "11111", "flu", THIS_WEEK_TUE),
        case("B", 22, "11112", "flu", SUNDAY),
        case("C", 23, "11113", "cold"),
        case("D", 24, "11114", "cold"),
    ])
    body = reconcile(client, initial=v1, final=v3).json()
    assert body["newly_counted"] == 1          # 仅 B，MID 不参与
    assert body["date_filled"] == 1


# ----------------------------------------------------- 首报登记与清单同事务
def test_failed_write_rolls_back_registry_and_keeps_publication(tmp_path, monkeypatch):
    db = str(tmp_path / "rollback.db")
    store = Store(db)
    payload = SubmissionIn.model_validate({"k": 2, "cases": GOOD})
    body, status = store.submit(payload)
    assert status == 201 and body["status"] == "published"
    published_before = store.publication()
    assert published_before[1] == 200

    # 注入写入失败：病例明细插入阶段抛错，验证整事务回滚
    def boom(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated disk/fk failure")

    monkeypatch.setattr(store, "_insert_cases", boom)
    with pytest.raises(sqlite3.IntegrityError):
        store.submit(payload)

    # 事务整体回滚：没有残留新版本/登记，公开副本原样保留
    assert store.publication() == published_before
    n_sub = store._conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
    n_reg = store._conn.execute("SELECT COUNT(*) c FROM case_registry").fetchone()["c"]
    assert n_sub == 1 and n_reg == 4
    store.close()


# --------------------------------------------------------------- 旧库迁移
LEGACY_SCHEMA = """
CREATE TABLE submissions (
    version INTEGER PRIMARY KEY AUTOINCREMENT, k INTEGER NOT NULL,
    status TEXT NOT NULL, reason TEXT, age_band INTEGER, zip_prefix INTEGER,
    retained INTEGER NOT NULL DEFAULT 0, suppressed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE cases (
    version INTEGER NOT NULL, case_id TEXT NOT NULL, age INTEGER NOT NULL,
    zip TEXT NOT NULL, disease TEXT NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version, case_id)
);
CREATE TABLE publication_rows (version INTEGER NOT NULL, age_band TEXT NOT NULL,
    zip TEXT NOT NULL, disease TEXT NOT NULL);
CREATE TABLE service_state (id INTEGER PRIMARY KEY CHECK (id = 1), published_version INTEGER);
INSERT INTO service_state (id, published_version) VALUES (1, 1);
"""


def test_legacy_db_migration_preserves_first_seen_versions(tmp_path):
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(LEGACY_SCHEMA)
    # 旧库两个版本：A/B 在 v1 首次入库；C 在 v2 首次入库；A 在 v2 仍在册
    conn.execute("INSERT INTO submissions VALUES (1,2,'published',NULL,5,3,2,0,'t1')")
    conn.execute("INSERT INTO submissions VALUES (2,2,'published',NULL,5,3,3,0,'t2')")
    conn.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?)", [
        (1, "A", 21, "11111", "flu", 0),
        (1, "B", 22, "11112", "flu", 0),
        (2, "A", 21, "11111", "flu", 0),
        (2, "C", 23, "11113", "cold", 0),
    ])
    conn.execute("INSERT INTO publication_rows VALUES (1,'20-24','111**','flu')")
    conn.commit()
    conn.close()

    store = Store(db)  # 触发迁移
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(cases)")}
    assert "onset_date" in cols
    # 旧病例发病日期未知
    assert all(r["onset_date"] is None for r in store._conn.execute("SELECT onset_date FROM cases"))
    reg = {r["case_id"]: r["first_seen_version"]
           for r in store._conn.execute("SELECT * FROM case_registry")}
    assert reg == {"A": 1, "B": 1, "C": 2}

    # 模拟初报在册且发病日期在别周：v2 的 A 置为上一周日期
    store._conn.execute(
        "UPDATE cases SET onset_date = ? WHERE version = 2 AND case_id = 'A'",
        (PREV_WEEK,),
    )

    # 迁移后仍可正常提交（新病例带发病日期）并核对
    with TestClient(create_app(db)) as client:
        v3 = submit(client, [
            case("A", 21, "11111", "flu", THIS_WEEK_TUE),
            case("B", 22, "11112", "flu", THIS_WEEK_SUN),
            case("C", 23, "11113", "cold", THIS_WEEK_TUE),
            case("Z", 24, "11114", "cold", SUNDAY),
        ])
        body = reconcile(client, initial=2, final=v3).json()
        # v2 在册：A（别周日期）、C（未知）；B 缺席于 v2 但 v1 已入库；Z 全新
        # A 跨周订正；C 补填日期；B 重新列入；Z 迟报
        assert body["cross_week_corrected"] == 1
        assert body["date_filled"] == 1
        assert body["newly_listed"] == 1
        assert body["late_reported"] == 1
        assert body["newly_counted"] == 4
        assert body["final_week_cases"] == 4
        assert body["initial_week_cases"] == 0
        assert body["initial_unknown_onset"] == 1
        assert body["final_unknown_onset"] == 0
    store.close()
