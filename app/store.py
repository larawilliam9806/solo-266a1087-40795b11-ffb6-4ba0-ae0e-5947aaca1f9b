"""SQLite 持久化与发布状态机。

一致性保证：
- 每个有效提交在单个事务（BEGIN IMMEDIATE）内完成：写入新版本、
  首报登记（case_registry 记录每个病例首次入库的版号）、
  删除旧脱敏副本、原子切换发布指针 —— 旧副本随提交立即撤下；
  事务中任一步失败则整体回滚，原清单与公开副本保持不变；
- 全部读写经过同一把可重入锁，发布与再次提交交错时不会发布过期清单，
  公开下载也不会读到跨版本的撕裂数据；
- 状态全部落盘（service_state.published_version 是唯一发布指针），
  重启后已撤下的副本不会被恢复（旧副本行在撤下时即被物理删除）。

旧库升级：
- 旧库 cases 表没有 onset_date，也没有首次入库登记。启动时自动迁移：
  补列并以各病例最早出现的版本回填 case_registry，保留其首次入库版号。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from .generalize import (
    CaseRecord,
    age_band_label,
    evaluate,
    infeasible_reason,
    zip_label,
)
from .models import SubmissionIn

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    k            INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'withdrawn')),
    reason       TEXT,
    age_band     INTEGER,
    zip_prefix   INTEGER,
    retained     INTEGER NOT NULL DEFAULT 0,
    suppressed   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
-- 病例明细（按版本）：onset_date 为 NULL 表示发病日期未知（提交时未填）
CREATE TABLE IF NOT EXISTS cases (
    version     INTEGER NOT NULL REFERENCES submissions(version),
    case_id     TEXT NOT NULL,
    age         INTEGER NOT NULL,
    zip         TEXT NOT NULL,
    disease     TEXT NOT NULL,
    suppressed  INTEGER NOT NULL DEFAULT 0,
    onset_date  TEXT,
    PRIMARY KEY (version, case_id)
);
-- 脱敏副本：只保存当前发布版本的逐行副本，撤下即删除
CREATE TABLE IF NOT EXISTS publication_rows (
    version  INTEGER NOT NULL,
    age_band TEXT NOT NULL,
    zip      TEXT NOT NULL,
    disease  TEXT NOT NULL
);
-- 单例状态行：published_version 为 NULL 表示当前无已发布副本（撤下状态）
CREATE TABLE IF NOT EXISTS service_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    published_version INTEGER
);
INSERT OR IGNORE INTO service_state (id, published_version) VALUES (1, NULL);
-- 每个病例首次入库的版号登记表（首报登记），与清单提交在同一事务内维护
CREATE TABLE IF NOT EXISTS case_registry (
    case_id            TEXT PRIMARY KEY,
    first_seen_version INTEGER NOT NULL
);
"""

SUBMISSION_COLS = "version, k, status, reason, age_band, zip_prefix, retained, suppressed, created_at"


class VersionNotFound(LookupError):
    """核对时指定的提交版号不存在。"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_legacy_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- migration
    def _migrate_legacy_db(self) -> None:
        """升级旧库：补 onset_date 列，并回填每个病例首次入库的版号。

        新建库的 cases 表已含该列、case_registry 已随 SCHEMA 创建，本方法为空操作。
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(cases)")}
        if "onset_date" not in cols:
            # 旧库：旧提交的发病日期一律未知
            self._conn.execute("ALTER TABLE cases ADD COLUMN onset_date TEXT")
        # 回填登记表：以病例在 cases 中出现过的最小版号作为首次入库版号
        self._conn.execute(
            """
            INSERT OR IGNORE INTO case_registry (case_id, first_seen_version)
            SELECT case_id, MIN(version)
            FROM cases
            GROUP BY case_id
            """
        )

    # ------------------------------------------------------------------ submit
    def submit(self, payload: SubmissionIn) -> tuple[dict, int]:
        """处理一个（已通过合法性校验的）新清单。

        返回 (响应体, HTTP 状态码)。无论新版能否发布，旧副本都在本事务内撤下。
        首报登记与清单写入同事务：任一写入失败则整体回滚，原清单与公开副本保留。
        """
        records = [
            CaseRecord(c.case_id, c.age, c.zip, c.disease) for c in payload.cases
        ]
        onset_dates = {c.case_id: c.onset_date for c in payload.cases}
        best, diagnostics = evaluate(records, payload.k)
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 立即撤下旧副本：删除旧脱敏行并清空发布指针
                self._conn.execute("DELETE FROM publication_rows")
                self._conn.execute(
                    "UPDATE service_state SET published_version = NULL WHERE id = 1"
                )

                if best is None:
                    reason = infeasible_reason(payload.k, diagnostics)
                    cur = self._conn.execute(
                        f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'withdrawn', ?, NULL, NULL, 0, 0, ?)",
                        (payload.k, reason, now),
                    )
                    version = cur.lastrowid
                    self._insert_cases(version, records, set(), onset_dates)
                    self._register_first_seen(version, records)
                    self._conn.execute("COMMIT")
                    body = {
                        "version": version,
                        "status": "withdrawn",
                        "k": payload.k,
                        "message": "旧脱敏副本已撤下；新版无法达标，保持撤下状态。",
                        "reason": reason,
                        "details": diagnostics,
                    }
                    return body, 201

                suppressed_ids = set(best.suppressed_ids)
                retained = len(records) - best.suppressed_count
                cur = self._conn.execute(
                    f"INSERT INTO submissions ({SUBMISSION_COLS}) VALUES (NULL, ?, 'published', NULL, ?, ?, ?, ?, ?)",
                    (payload.k, best.age_band, best.zip_prefix, retained,
                     best.suppressed_count, now),
                )
                version = cur.lastrowid
                self._insert_cases(version, records, suppressed_ids, onset_dates)
                self._register_first_seen(version, records)
                for r in records:
                    if r.case_id in suppressed_ids:
                        continue
                    self._conn.execute(
                        "INSERT INTO publication_rows (version, age_band, zip, disease) VALUES (?, ?, ?, ?)",
                        (version, age_band_label(r.age, best.age_band),
                         zip_label(r.zip, best.zip_prefix), r.disease),
                    )
                self._conn.execute(
                    "UPDATE service_state SET published_version = ? WHERE id = 1",
                    (version,),
                )
                self._conn.execute("COMMIT")
                body = {
                    "version": version,
                    "status": "published",
                    "k": payload.k,
                    "params": {
                        "age_band_years": best.age_band,
                        "zip_prefix_digits": best.zip_prefix,
                    },
                    "retained": retained,
                    "suppressed": best.suppressed_count,
                    "suppressed_case_ids": sorted(suppressed_ids),
                    "download": "/api/publication",
                }
                return body, 201
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _insert_cases(self, version: int, records, suppressed_ids: set[str],
                      onset_dates: dict[str, str | None]) -> None:
        for r in records:
            self._conn.execute(
                "INSERT INTO cases (version, case_id, age, zip, disease, suppressed, onset_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version, r.case_id, r.age, r.zip, r.disease,
                 1 if r.case_id in suppressed_ids else 0,
                 onset_dates.get(r.case_id)),
            )

    def _register_first_seen(self, version: int, records) -> None:
        """首报登记：仅登记此前从未入库的病例，保留每个病例的首次入库版号。"""
        for r in records:
            self._conn.execute(
                "INSERT OR IGNORE INTO case_registry (case_id, first_seen_version) VALUES (?, ?)",
                (r.case_id, version),
            )

    # --------------------------------------------------------------- reconcile
    def reconcile(self, week_start: date, initial_version: int,
                  final_version: int) -> dict:
        """周报核对：按病例号对照初报版与定稿版在指定一周的病例差异。

        只读取指定两个版本的病例明细（及登记表的首次入库版号），
        不读取其他版本、也不依赖脱敏副本是否发布（撤下/未发布版本同样可核对）。

        分类口径（新计入定稿版该周的病例）：
        - 初报版缺席：首次入库版号 > 初报版 -> 迟报；<= 初报版 -> 重新列入；
        - 初报版在册：初报日期未知 -> 补填日期；初报日期在别周 -> 跨周订正。
        """
        with self._lock:
            found = {
                r["version"]
                for r in self._conn.execute(
                    "SELECT version FROM submissions WHERE version IN (?, ?)",
                    (initial_version, final_version),
                )
            }
            if initial_version not in found or final_version not in found:
                raise VersionNotFound("指定的提交版号不存在")

            initial_rows = self._conn.execute(
                "SELECT case_id, onset_date FROM cases WHERE version = ?",
                (initial_version,),
            ).fetchall()
            final_rows = self._conn.execute(
                "SELECT case_id, onset_date FROM cases WHERE version = ?",
                (final_version,),
            ).fetchall()

        week_end = week_start + timedelta(days=7)

        def in_week(onset: str | None) -> bool:
            if onset is None:
                return False
            d = date.fromisoformat(onset)
            return week_start <= d < week_end

        initial_map = {r["case_id"]: r["onset_date"] for r in initial_rows}
        final_map = {r["case_id"]: r["onset_date"] for r in final_rows}

        initial_week = {cid for cid, od in initial_map.items() if in_week(od)}
        final_week = {cid for cid, od in final_map.items() if in_week(od)}

        moved_out = initial_week - final_week
        new_in_week = final_week - initial_week

        newly_listed = 0       # 重新列入：初报前曾入库
        late_reported = 0      # 迟报：初报之后才首次入库
        date_filled = 0        # 补填日期：初报在册但发病日期未知
        cross_week_corrected = 0  # 跨周订正：初报在册且发病日期在别周
        absent_ids: list[str] = []

        for cid in sorted(new_in_week):
            if cid not in initial_map:
                absent_ids.append(cid)
            else:
                if initial_map[cid] is None:
                    date_filled += 1
                else:
                    cross_week_corrected += 1

        if absent_ids:
            placeholders = ",".join("?" for _ in absent_ids)
            with self._lock:
                registry = {
                    r["case_id"]: r["first_seen_version"]
                    for r in self._conn.execute(
                        f"SELECT case_id, first_seen_version FROM case_registry "
                        f"WHERE case_id IN ({placeholders})",
                        absent_ids,
                    )
                }
            for cid in absent_ids:
                first_seen = registry.get(cid)
                # 登记表必有记录；防御性地把缺失登记按迟报处理
                if first_seen is not None and first_seen <= initial_version:
                    newly_listed += 1
                else:
                    late_reported += 1

        return {
            "week_start": week_start.isoformat(),
            "initial_version": initial_version,
            "final_version": final_version,
            "initial_week_cases": len(initial_week),
            "final_week_cases": len(final_week),
            "initial_unknown_onset": sum(1 for od in initial_map.values() if od is None),
            "final_unknown_onset": sum(1 for od in final_map.values() if od is None),
            "moved_out": len(moved_out),
            "newly_counted": len(new_in_week),
            "newly_listed": newly_listed,
            "late_reported": late_reported,
            "date_filled": date_filled,
            "cross_week_corrected": cross_week_corrected,
        }

    # -------------------------------------------------------------- publication
    def publication(self) -> tuple[dict, int]:
        """公开下载：只返回同一完整版本的副本与参数；撤下状态下返回 404 及原因。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = row["published_version"]
            if published is None:
                last = self._conn.execute(
                    f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
                ).fetchone()
                body = {
                    "detail": "当前无已发布的脱敏副本",
                    "state": "withdrawn" if last else "empty",
                    "latest_version": last["version"] if last else None,
                    "reason": last["reason"] if last else "尚未收到任何有效清单",
                }
                return body, 404
            sub = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions WHERE version = ?",
                (published,),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT age_band, zip, disease FROM publication_rows WHERE version = ? ORDER BY rowid",
                (published,),
            ).fetchall()
            body = {
                "version": sub["version"],
                "k": sub["k"],
                "params": {
                    "age_band_years": sub["age_band"],
                    "zip_prefix_digits": sub["zip_prefix"],
                },
                "retained": sub["retained"],
                "suppressed": sub["suppressed"],
                "rows": [dict(r) for r in rows],
            }
            return body, 200

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT published_version FROM service_state WHERE id = 1"
            ).fetchone()
            published = row["published_version"]
            last = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            state = "published" if published is not None else ("withdrawn" if last else "empty")
            return {
                "state": state,
                "published_version": published,
                "latest_submission": dict(last) if last else None,
            }

    def history(self) -> list[dict]:
        """提交历史（仅元数据，不含原始病例明细）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {SUBMISSION_COLS} FROM submissions ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]
