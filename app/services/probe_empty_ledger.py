"""抽样探针空行旁路账：只记旁路事件，不回写主进度。

探针真相与覆盖进度只认 ``probe_samples`` 队列；旁路账仅留痕，不能把芯片刷绿。
"""
from __future__ import annotations

from app.db import get_db
from app.services.probe_empty import dashboard, panel_chip, record_event, truth_flag

_SLUG = "probe_empty"
_TITLE = "抽样探针空行"
_OK = "探针已绿"
_BAD = "库无探针"


def append_ledger(ref_id: int, note: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (f"{_SLUG}_ledger_{ref_id}", note or _TITLE),
        )
    record_event(f"ref={ref_id} {note}", db_path)
    return {
        "ref_id": ref_id,
        "note": note,
        "panel": panel_chip(db_path),
        "truth": truth_flag(db_path),
        "message": "旁路已记，探针进度以库内落点为准",
    }


def ledger_rows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            (f"{_SLUG}_ledger_%",),
        ).fetchall()
        chip = panel_chip(db_path)
        return [
            {"key": r["key"], "value": r["value"], "chip": chip}
            for r in rows
        ]


def board(db_path: str | None = None) -> dict:
    dash = dashboard(db_path)
    dash["ledger"] = ledger_rows(db_path)
    return dash
