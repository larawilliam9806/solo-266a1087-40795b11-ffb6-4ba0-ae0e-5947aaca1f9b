"""FastAPI 入口：哨点病例脱敏副本发布服务。

接口：
- POST /api/submissions   整批提交新清单（<=40 条，可带发病日期）；有效提交立即
                          撤下旧副本，能达标则发布新版本，否则保持撤下并说明原因；
                          非法提交返回 422，不影响旧版本。
- GET  /api/publication   公开下载当前发布的脱敏副本与参数（同一完整版本）；
                          撤下状态下返回 404 及原因。
- GET  /api/status        当前发布状态。
- GET  /api/submissions   提交历史（仅元数据）。
- GET  /api/weekly-reconciliation  周报核对（令牌保护；未发布版本也可核对）。
- GET  /health            健康检查。
"""
from __future__ import annotations

import hmac
import os
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .models import SubmissionIn
from .store import Store, VersionNotFound

RECONCILE_TOKEN_ENV = "APP_RECONCILE_TOKEN"


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("APP_DB_PATH", "./app.db"))
    app = FastAPI(title="哨点病例脱敏副本发布服务", version="1.1.0")
    app.state.store = store

    def require_token(authorization: str | None = Header(default=None)) -> None:
        """核对接口的令牌校验：令牌由环境变量 APP_RECONCILE_TOKEN 配置。

        未配置令牌或未携带/携带错误令牌均拒绝（401），使用恒定时间比较防时序泄露。
        """
        expected = os.environ.get(RECONCILE_TOKEN_ENV, "").strip()
        if not expected:
            raise HTTPException(status_code=401, detail="核对接口未配置访问令牌")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
        presented = authorization[len("Bearer "):].strip()
        if not presented or not hmac.compare_digest(presented, expected):
            raise HTTPException(status_code=401, detail="令牌无效")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/submissions", status_code=201)
    def submit(payload: SubmissionIn):
        body, status = store.submit(payload)
        return JSONResponse(body, status_code=status)

    @app.get("/api/publication")
    def publication():
        body, status = store.publication()
        return JSONResponse(body, status_code=status)

    @app.get("/api/status")
    def status():
        return store.status()

    @app.get("/api/submissions")
    def submissions():
        return {"submissions": store.history()}

    @app.get(
        "/api/weekly-reconciliation",
        dependencies=[Depends(require_token)],
    )
    def weekly_reconciliation(
        week_start: date = Query(..., description="周一日期（YYYY-MM-DD）"),
        initial_version: int = Query(..., ge=1, description="初报提交版号"),
        final_version: int = Query(..., ge=1, description="定稿提交版号"),
    ):
        if week_start.weekday() != 0:
            raise HTTPException(status_code=422, detail="week_start 必须是周一日期")
        if initial_version >= final_version:
            raise HTTPException(
                status_code=422,
                detail="initial_version 必须早于 final_version",
            )
        try:
            return store.reconcile(week_start, initial_version, final_version)
        except VersionNotFound:
            raise HTTPException(status_code=404, detail="指定的提交版号不存在")

    return app


app = create_app(os.environ.get("APP_DB_PATH", "./app.db"))
