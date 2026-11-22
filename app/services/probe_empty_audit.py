"""抽样探针空行审计旁路：只读事件与面板，不写进度。"""
from __future__ import annotations

from app.db import get_db
from app.services.probe_empty import dashboard
from app.services.probe_empty_ledger import ledger_rows


def audit_list(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            ("probe_empty_evt_%",),
        ).fetchall()
        return [{"key": r["key"], "value": r["value"]} for r in rows]


def audit_summary(db_path: str | None = None) -> dict:
    return {
        "title": "抽样探针空行审计",
        "events": audit_list(db_path),
        "board": dashboard(db_path),
        "ledger": ledger_rows(db_path),
        "half": {
            "half_fixed": False,
            "message": "审计旁路只读，不改探针进度",
        },
    }
