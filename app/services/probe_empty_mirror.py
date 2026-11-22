"""抽样探针空行镜像面：与主队列同源，不另造一套亮灯。"""
from __future__ import annotations

from app.db import get_db
from app.services.probe_empty import dashboard, paint_ok, truth_flag

_SLUG = "probe_empty_mirror"
_OK = "探针已绿"
_BAD = "库无探针"


def mirror_chip(db_path: str | None = None) -> str:
    """镜像芯片直接取主队列闸门结果，不再单独存一份可被刷绿的标记。"""
    return dashboard(db_path)["chip"]


def mirror_board(db_path: str | None = None) -> dict:
    dash = dashboard(db_path)
    return {
        "mirror_chip": dash["chip"],
        "truth": truth_flag(db_path),
        "dash": dash,
        "panel_pack_mismatch": not dash["gate_allowed"],
        "title": "抽样探针空行镜像",
    }


def half_mirror(db_path: str | None = None) -> dict:
    """半修只碰展示，库内仍无落点。"""
    paint_ok(db_path)
    dash = dashboard(db_path)
    return {
        "mirror_chip": dash["chip"],
        "truth": truth_flag(db_path),
        "half_fixed": False,
        "message": "镜像半修仅刷展示，库内无落点，复验仍失败",
    }
