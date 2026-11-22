"""覆盖对账：节点与已发布边的孤儿/未覆盖统计 + 探针样本来源。"""
from __future__ import annotations

from app.db import get_db
from app.services.errors import ServiceError


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _latest_probe(conn):
    return conn.execute(
        "SELECT * FROM probe_samples ORDER BY id DESC LIMIT 1"
    ).fetchone()


def run_coverage(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        nodes = conn.execute(
            "SELECT id, status FROM lineage_nodes"
        ).fetchall()
        edges = conn.execute(
            "SELECT id, upstream_id, downstream_id, status FROM lineage_edges"
        ).fetchall()
        live_nodes = [n for n in nodes if n["status"] != "停用"]
        published = [e for e in edges if e["status"] == "已发布"]
        linked = set()
        for e in published:
            linked.add(int(e["upstream_id"]))
            linked.add(int(e["downstream_id"]))
        orphan = sum(1 for n in live_nodes if int(n["id"]) not in linked)
        # draft edges that share endpoints with published = uncovered drafts
        uncovered = sum(1 for e in edges if e["status"] == "草稿")
        probe = _latest_probe(conn)
        probe_no = probe["probe_no"] if probe else ""
        sample_source = probe["sample_source"] if probe else ""
        summary = (
            f"在用节点 {len(live_nodes)}，已发布边 {len(published)}，"
            f"孤儿节点 {orphan}，草稿边 {uncovered}"
        )
        if probe_no:
            summary += f"；探针 {probe_no} 样本来源 {sample_source}"
        else:
            summary += "；库内无探针落点"
        if orphan:
            summary += "；存在未挂边节点须人工核对"
        cur = conn.execute(
            "INSERT INTO coverage_reports(summary, node_count, edge_count, "
            "orphan_count, uncovered_edges, probe_no, sample_source) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                summary,
                len(live_nodes),
                len(published),
                orphan,
                uncovered,
                probe_no,
                sample_source,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        )


def latest_chip(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT orphan_count FROM coverage_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "未对账"
        from app.services.probe_empty import probe_progress

    progress = probe_progress(db_path)
    if not progress["allows"]:
        return "有缺口" if int(row["orphan_count"]) > 0 else "已齐套"
    # 探针核齐才放行，芯片须亮出探针号，不能泛指“已绿”。
    if progress["probe_no"]:
        return f"探针已核齐 {progress['probe_no']}"
    return "已齐套"
