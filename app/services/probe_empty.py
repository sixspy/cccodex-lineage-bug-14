"""抽样探针：独立队列落库 + 归档冻结 + 单一进度来源。

钉定探针只走一条路：在写事务里向 ``probe_samples`` 队列插入样本行，
同时把钉定瞬间的样本指纹与边版本冻结进 ``probe_archive``；覆盖进度、
设置页探针号与放行闸门都只读这一份队列，不再维护第二套展示真相。
"""
from __future__ import annotations

import hashlib
import sqlite3

from app.db import get_db, write_db
from app.services.errors import ServiceError

_SLUG = "probe_empty"
_OK = "探针已绿"
_BAD = "库无探针"
_TITLE = "抽样探针空行"
_DONE = "已核齐"
_PINNED = "已钉定"


# --------------------------------------------------------------------------
# 基础读写
# --------------------------------------------------------------------------

def _meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, value),
    )


def _edge_row(conn, edge_id):
    if edge_id in (None, "", 0, "0"):
        return None
    try:
        return conn.execute(
            "SELECT * FROM lineage_edges WHERE id=?", (int(edge_id),)
        ).fetchone()
    except (TypeError, ValueError):
        return None


def edge_version(edge) -> str:
    """边版本只由边自身可变的登记字段决定，任一字段变更即换版本。"""
    if edge is None:
        return ""
    raw = "|".join(
        str(edge[k])
        for k in ("edge_code", "upstream_id", "downstream_id", "transform_kind", "status")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _derive_fp(probe_no: str, sample_source: str, version: str) -> str:
    raw = f"{probe_no}|{sample_source}|{version}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _sample_rows(conn) -> list:
    return conn.execute(
        "SELECT * FROM probe_samples ORDER BY id"
    ).fetchall()


def _latest_row(conn):
    return conn.execute(
        "SELECT * FROM probe_samples ORDER BY id DESC LIMIT 1"
    ).fetchone()


def pack_rows(db_path: str | None = None) -> list[dict]:
    """队列内的钉定样本，库内无行就是空——不再用 decoy 撑计数。"""
    with get_db(db_path) as conn:
        rows = _sample_rows(conn)
        return [
            {
                "key": r["probe_no"],
                "value": r["sample_source"],
                "probe_no": r["probe_no"],
                "sample_source": r["sample_source"],
                "sample_fp": r["sample_fp"],
                "edge_id": r["edge_id"],
                "edge_version": r["edge_version"],
                "status": r["status"],
            }
            for r in rows
        ]


def truth_flag(db_path: str | None = None) -> str:
    """库内是否有探针落点行：有行才是真相已绿。"""
    with get_db(db_path) as conn:
        return _OK if _sample_rows(conn) else _BAD


def panel_chip(db_path: str | None = None) -> str:
    """展示芯片必须与放行闸门一致，不能单独刷绿。"""
    with get_db(db_path) as conn:
        allowed, _ = _gate_state(conn)
        return _OK if allowed else _BAD


# --------------------------------------------------------------------------
# 闸门：队列落点 + 已核对 + 边版本一致 + 来源对齐
# --------------------------------------------------------------------------

def _gate_state(conn) -> tuple[bool, str]:
    rows = _sample_rows(conn)
    if not rows:
        return False, f"{_TITLE}库内无落点，禁止放行"
    row = _latest_row(conn)
    probe_no = row["probe_no"]
    if row["status"] != _DONE:
        return False, f"探针 {probe_no} 尚未核对，禁止放行"
    archive = conn.execute(
        "SELECT * FROM probe_archive WHERE probe_no=?", (probe_no,)
    ).fetchone()
    if not archive:
        return False, f"探针 {probe_no} 缺归档冻结，禁止放行"
    current = _edge_row(conn, row["edge_id"])
    if current is None:
        return False, f"探针 {probe_no} 对应的边已不存在，禁止放行"
    current_v = edge_version(current)
    if current_v != archive["edge_version"] or row["edge_version"] != archive["edge_version"]:
        return False, f"探针 {probe_no} 样本与边版本对不上，禁止放行"
    if (
        row["sample_source"] != archive["sample_source"]
        or row["sample_fp"] != archive["sample_fp"]
    ):
        return False, f"探针 {probe_no} 样本来源与归档不一致，禁止放行"
    return True, ""


def gate_allows_pipeline(db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        allowed, _ = _gate_state(conn)
        return allowed


def probe_progress(db_path: str | None = None) -> dict:
    """覆盖进度侧读探针的唯一入口：与闸门同源，不再另拉展示芯片。"""
    with get_db(db_path) as conn:
        allowed, reason = _gate_state(conn)
        latest = _latest_row(conn)
        return {
            "allows": allowed,
            "reason": reason,
            "probe_no": latest["probe_no"] if latest else "",
            "sample_source": latest["sample_source"] if latest else "",
            "status": latest["status"] if latest else "",
        }


def require_pack_or_raise(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        allowed, reason = _gate_state(conn)
    if not allowed:
        raise ServiceError(reason, 409)


# --------------------------------------------------------------------------
# 钉定：一次写事务，双人争钉只有一个能落库
# --------------------------------------------------------------------------

def _resolve_edge(conn, edge_id_raw):
    edge = _edge_row(conn, edge_id_raw)
    if edge is None and edge_id_raw not in (None, "", 0, "0"):
        raise ServiceError("所选边不存在，无法钉探针", 404)
    if edge is None:
        edge = conn.execute(
            "SELECT * FROM lineage_edges WHERE status='已发布' ORDER BY id LIMIT 1"
        ).fetchone()
    if edge is None:
        raise ServiceError("库内没有可用边，请先登记并发布边", 409)
    return edge


def _next_probe_no(conn) -> str:
    n = int(
        conn.execute("SELECT COUNT(*) AS c FROM probe_samples").fetchone()["c"]
    ) + 1
    while True:
        probe_no = f"P-{n:04d}"
        if not conn.execute(
            "SELECT 1 FROM probe_samples WHERE probe_no=?", (probe_no,)
        ).fetchone():
            return probe_no
        n += 1


def _default_source(conn, edge) -> str:
    node = conn.execute(
        "SELECT node_code FROM lineage_nodes WHERE id=?", (edge["upstream_id"],)
    ).fetchone()
    return f"{edge['edge_code']}→{node['node_code']}" if node else edge["edge_code"]


def pin_probe(payload: dict | None = None, db_path: str | None = None) -> dict:
    """后台一次写入：队列 + 归档同事务提交，出错回白话。"""
    payload = payload or {}
    probe_no = str(payload.get("probe_no") or payload.get("probe") or "").strip()
    sample_source = str(
        payload.get("sample_source") or payload.get("source") or ""
    ).strip()
    raw_fp = str(payload.get("sample_fp") or payload.get("fp") or "").strip()
    edge_id_raw = payload.get("edge_id")
    pinned_by = str(payload.get("pinned_by") or payload.get("user") or "").strip()

    try:
        with write_db(db_path) as conn:
            edge = _resolve_edge(conn, edge_id_raw)
            version = edge_version(edge)
            if not probe_no:
                probe_no = _next_probe_no(conn)
            if not sample_source:
                sample_source = _default_source(conn, edge)

            existing = conn.execute(
                "SELECT * FROM probe_samples WHERE probe_no=?", (probe_no,)
            ).fetchone()
            if existing:
                raise ServiceError(
                    f"探针 {probe_no} 已被钉定，请勿重复落库", 409
                )
            if raw_fp and conn.execute(
                "SELECT 1 FROM probe_samples WHERE sample_fp=?", (raw_fp,)
            ).fetchone():
                raise ServiceError(
                    f"样本指纹已被探针 {_fp_owner(conn, raw_fp)} 钉定，请勿重复落库",
                    409,
                )

            sample_fp = raw_fp or _derive_fp(probe_no, sample_source, version)
            conn.execute(
                "INSERT INTO probe_samples(probe_no, sample_source, sample_fp, "
                "edge_id, edge_version, pinned_by, status) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    probe_no,
                    sample_source,
                    sample_fp,
                    int(edge["id"]),
                    version,
                    pinned_by,
                    _PINNED,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO probe_archive(probe_no, edge_id, edge_version, "
                "sample_fp, sample_source) VALUES (?,?,?,?,?)",
                (probe_no, int(edge["id"]), version, sample_fp, sample_source),
            )
    except sqlite3.OperationalError as exc:
        raise ServiceError("探针正被他人钉定，请稍后刷新重试", 409) from exc
    except sqlite3.IntegrityError as exc:
        raise ServiceError("探针已被他人钉定，请刷新后重试", 409) from exc

    _backfill_progress(db_path)
    return {
        "ok": True,
        "probe_no": probe_no,
        "sample_source": sample_source,
        "sample_fp": sample_fp,
        "edge_id": int(edge["id"]),
        "edge_version": version,
        "status": _PINNED,
        "chip": panel_chip(db_path),
        "truth": truth_flag(db_path),
        "pack_count": len(pack_rows(db_path)),
        "message": f"探针 {probe_no} 已钉定，待核对",
    }


def _fp_owner(conn, sample_fp: str) -> str:
    row = conn.execute(
        "SELECT probe_no FROM probe_samples WHERE sample_fp=?", (sample_fp,)
    ).fetchone()
    return row["probe_no"] if row else ""


def commit_action(payload: dict | None = None, db_path: str | None = None) -> dict:
    return pin_probe(payload, db_path)


# --------------------------------------------------------------------------
# 核对：必须见到库内落点行，只刷展示不算数
# --------------------------------------------------------------------------

def reconcile_probe(probe_no: str | None = None, db_path: str | None = None) -> dict:
    target = (probe_no or "").strip()
    with write_db(db_path) as conn:
        if target:
            row = conn.execute(
                "SELECT * FROM probe_samples WHERE probe_no=?", (target,)
            ).fetchone()
        else:
            row = _latest_row(conn)
        if not row:
            raise ServiceError(f"{_TITLE}库内无落点，先钉定探针再核对", 409)
        probe_no = row["probe_no"]
        archive = conn.execute(
            "SELECT * FROM probe_archive WHERE probe_no=?", (probe_no,)
        ).fetchone()
        if not archive:
            raise ServiceError(f"探针 {probe_no} 缺归档冻结，无法核对", 409)
        current = _edge_row(conn, row["edge_id"])
        if current is None:
            raise ServiceError(f"探针 {probe_no} 对应的边已不存在，无法核对", 409)
        if edge_version(current) != archive["edge_version"]:
            raise ServiceError(
                f"探针 {probe_no} 样本与边版本对不上，禁止核对放行", 409
            )
        if (
            row["sample_source"] != archive["sample_source"]
            or row["sample_fp"] != archive["sample_fp"]
        ):
            raise ServiceError(
                f"探针 {probe_no} 样本来源与归档不一致，禁止核对放行", 409
            )
        conn.execute(
            "UPDATE probe_samples SET status=? WHERE probe_no=?", (_DONE, probe_no)
        )
    _backfill_progress(db_path)
    return {
        "ok": True,
        "probe_no": probe_no,
        "sample_source": row["sample_source"],
        "status": _DONE,
        "chip": panel_chip(db_path),
        "truth": truth_flag(db_path),
        "pack_count": len(pack_rows(db_path)),
        "message": f"探针 {probe_no} 已核对，库内有落点行",
    }


def reconcile_display(db_path: str | None = None) -> dict:
    """设置页“核对探针”：走真实核对，无落点会白话报错。"""
    return reconcile_probe(db_path=db_path)


def paint_ok(db_path: str | None = None) -> str:
    """仅供展示的旧入口：只记标记，不写库内行，芯片仍按闸门显示。"""
    with get_db(db_path) as conn:
        _meta_set(conn, f"{_SLUG}_display_only", "1")
    return panel_chip(db_path)


def half_fix_display(db_path: str | None = None) -> dict:
    """半修：只刷展示，不写落点，复验仍失败。"""
    paint_ok(db_path)
    return {
        "panel_chip": panel_chip(db_path),
        "truth": truth_flag(db_path),
        "pack_count": len(pack_rows(db_path)),
        "half_fixed": False,
        "message": f"{_TITLE}仅刷新展示，库内无落点，复验仍失败",
    }


def _backfill_progress(db_path: str | None = None) -> None:
    from app.services import coverage as coverage_svc

    coverage_svc.run_coverage(db_path)


# --------------------------------------------------------------------------
# 快照 / 面板
# --------------------------------------------------------------------------

def snapshot(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        rows = _sample_rows(conn)
        latest = _latest_row(conn)
        allowed, reason = _gate_state(conn)
        truth = _OK if rows else _BAD
        chip = _OK if allowed else _BAD
        return {
            "title": _TITLE,
            "panel_chip": chip,
            "truth": truth,
            "probe_no": latest["probe_no"] if latest else "",
            "sample_source": latest["sample_source"] if latest else "",
            "edge_id": latest["edge_id"] if latest else None,
            "edge_version": latest["edge_version"] if latest else "",
            "status": latest["status"] if latest else "",
            "pack_count": len(rows),
            "durable_count": len(rows),
            "reconciled": bool(rows) and latest["status"] == _DONE,
            "gate_allowed": allowed,
            "gate_reason": reason,
            "aligned": allowed and truth == _OK and chip == _OK,
            "gate_bypass": False,
        }


def dashboard(db_path: str | None = None) -> dict:
    snap = snapshot(db_path)
    rows = pack_rows(db_path)
    return {
        "title": _TITLE,
        "chip": snap["panel_chip"],
        "truth": snap["truth"],
        "probe_no": snap["probe_no"],
        "sample_source": snap["sample_source"],
        "edge_id": snap["edge_id"],
        "edge_version": snap["edge_version"],
        "status": snap["status"],
        "reconciled": snap["reconciled"],
        "gate_allowed": snap["gate_allowed"],
        "gate_reason": snap["gate_reason"],
        "pack_count": snap["pack_count"],
        "durable_count": snap["durable_count"],
        "rows": rows,
        "panel_pack_mismatch": not snap["aligned"],
        "ok_chip": _OK,
        "bad_chip": _BAD,
        "label": "抽样探针",
    }


# --------------------------------------------------------------------------
# 事件旁路：只记录，不推进真相
# --------------------------------------------------------------------------

def list_events(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            (f"{_SLUG}_evt_%",),
        ).fetchall()
        return [{"key": r["key"], "note": r["value"]} for r in rows]


def record_event(note: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        c = int(_meta_get(conn, f"{_SLUG}_counter", "0") or "0") + 1
        _meta_set(conn, f"{_SLUG}_counter", str(c))
        _meta_set(conn, f"{_SLUG}_evt_{c}", note or _BAD)
    return dashboard(db_path)


# --------------------------------------------------------------------------
# 初始化：只清旧展示残留，绝不预置“已绿”
# --------------------------------------------------------------------------

def seed_skew(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        _meta_set(conn, f"{_SLUG}_counter", "0")
        for key in (
            f"{_SLUG}_panel_chip",
            f"{_SLUG}_truth",
            f"{_SLUG}_gate_bypass",
            f"{_SLUG}_pack_decoy",
            f"{_SLUG}_display_only",
        ):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
