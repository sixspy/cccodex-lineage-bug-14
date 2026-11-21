"""台参数：影响深度与通知渠道。"""
from __future__ import annotations

from app.db import get_db
from app.services.errors import ServiceError


def list_settings(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value, label FROM lab_settings ORDER BY key"
        ).fetchall()
        return [dict(r) for r in rows]


def update_setting(key: str, value: str, db_path: str | None = None) -> dict:
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        raise ServiceError("参数键与值不能为空")
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        if not row:
            raise ServiceError("参数不存在", 404)
        conn.execute(
            "UPDATE lab_settings SET value=? WHERE key=?", (value, key)
        )
        return dict(
            conn.execute(
                "SELECT key, value, label FROM lab_settings WHERE key=?", (key,)
            ).fetchone()
        )



def pin_contract(fingerprint: str, db_path: str | None = None) -> dict:
    from app.services.probe_empty import commit_action, panel_chip, pack_rows

    commit_action({"fp": fingerprint}, db_path)
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lab_settings(key, value, label) VALUES (?,?,?)",
            ("contract_fp", fingerprint or "legacy", "契约指纹"),
        )
    return {
        "fingerprint": fingerprint,
        "chip": panel_chip(db_path),
        "pack_count": len(pack_rows(db_path)),
        "message": "契约已绿",
    }
