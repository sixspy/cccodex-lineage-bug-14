"""探针落库形态：队列写入 / 归档冻结 / 单一进度源 / 闸门一致性。"""
import sqlite3

import pytest

from app.db import connect, get_db
from app.services import coverage, probe_empty, settings
from app.services.errors import ServiceError


def _edge_id(db_path):
    with get_db(db_path) as conn:
        return conn.execute(
            "SELECT id FROM lineage_edges WHERE status='已发布' ORDER BY id LIMIT 1"
        ).fetchone()["id"]


def _rows(db_path):
    with get_db(db_path) as conn:
        return conn.execute("SELECT * FROM probe_samples ORDER BY id").fetchall()


def test_seed_has_no_fake_green_and_gate_blocks(db_path):
    # 初始库无探针行，展示不得先绿，闸门必须拦住
    assert _rows(db_path) == []
    snap = probe_empty.snapshot(db_path)
    assert snap["panel_chip"] == probe_empty._BAD
    assert snap["truth"] == probe_empty._BAD
    assert snap["gate_allowed"] is False
    assert probe_empty.gate_allows_pipeline(db_path) is False
    with pytest.raises(ServiceError):
        probe_empty.require_pack_or_raise(db_path)


def test_half_fix_display_does_not_write_row(db_path):
    r = probe_empty.half_fix_display(db_path)
    assert r["half_fixed"] is False
    assert r["truth"] == probe_empty._BAD
    assert _rows(db_path) == []
    assert probe_empty.gate_allows_pipeline(db_path) is False


def test_pin_writes_queue_and_archive_atomically(db_path):
    eid = _edge_id(db_path)
    res = probe_empty.pin_probe(
        {"sample_source": "ods_orders 抽样", "edge_id": eid, "pinned_by": "alice"},
        db_path,
    )
    rows = _rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["probe_no"] == res["probe_no"] == "P-0001"
    assert row["sample_source"] == "ods_orders 抽样"
    assert row["sample_fp"]
    with get_db(db_path) as conn:
        arch = conn.execute(
            "SELECT * FROM probe_archive WHERE probe_no=?", (row["probe_no"],)
        ).fetchone()
    assert arch is not None
    assert arch["sample_fp"] == row["sample_fp"]
    assert arch["edge_version"] == row["edge_version"] == res["edge_version"]
    # 钉定但未核对：芯片不得绿，闸门不得放行
    assert probe_empty.snapshot(db_path)["panel_chip"] == probe_empty._BAD
    assert probe_empty.gate_allows_pipeline(db_path) is False


def test_reconcile_requires_row_then_allows_and_reports_source(db_path):
    eid = _edge_id(db_path)
    with pytest.raises(ServiceError):
        probe_empty.reconcile_probe(db_path=db_path)
    res = probe_empty.pin_probe({"sample_source": "pay 抽样", "edge_id": eid}, db_path)
    out = probe_empty.reconcile_probe(res["probe_no"], db_path)
    assert out["probe_no"] == res["probe_no"]
    assert out["sample_source"] == "pay 抽样"
    assert probe_empty.gate_allows_pipeline(db_path) is True
    # 覆盖进度给出样本来源与探针号
    rep = coverage.run_coverage(db_path)
    assert rep["probe_no"] == res["probe_no"]
    assert rep["sample_source"] == "pay 抽样"
    assert ("探针已核齐 " + res["probe_no"]) == coverage.latest_chip(db_path)


def test_edge_version_mismatch_blocks_gate(db_path):
    eid = _edge_id(db_path)
    res = probe_empty.pin_probe({"sample_source": "抽样", "edge_id": eid}, db_path)
    probe_empty.reconcile_probe(res["probe_no"], db_path)
    assert probe_empty.gate_allows_pipeline(db_path) is True
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE lineage_edges SET transform_kind='改口径' WHERE id=?", (eid,)
        )
    assert probe_empty.gate_allows_pipeline(db_path) is False
    snap = probe_empty.snapshot(db_path)
    assert snap["gate_allowed"] is False
    assert "版本" in snap["gate_reason"]


def test_duplicate_pin_conflicts_loudly(db_path):
    eid = _edge_id(db_path)
    first = probe_empty.pin_probe(
        {"probe_no": "P-9000", "sample_fp": "fp-shared", "sample_source": "a", "edge_id": eid},
        db_path,
    )
    assert first["probe_no"] == "P-9000"
    # 同指纹另一探针号 -> 拒
    with pytest.raises(ServiceError):
        probe_empty.pin_probe(
            {"probe_no": "P-9001", "sample_fp": "fp-shared", "sample_source": "b", "edge_id": eid},
            db_path,
        )
    # 同探针号 -> 拒
    with pytest.raises(ServiceError):
        probe_empty.pin_probe(
            {"probe_no": "P-9000", "sample_fp": "fp-other", "sample_source": "c", "edge_id": eid},
            db_path,
        )
    assert len(_rows(db_path)) == 1


def test_settings_contract_pins_and_shows_probe_no(db_path):
    out = settings.pin_contract("fp-contract", db_path)
    assert out["probe_no"] == "P-0001"
    assert out["sample_source"]
    panel = settings.probe_panel(db_path)
    assert panel["probe_no"] == "P-0001"


def test_concurrent_pin_second_fails_and_does_not_silently_succeed(db_path):
    """两人争钉同一探针：唯一约束 + 写事务，只有一个成功。"""
    eid = _edge_id(db_path)
    conn1 = connect(db_path)
    conn2 = connect(db_path)
    conn2.execute("PRAGMA busy_timeout = 200")
    try:
        conn1.execute("BEGIN IMMEDIATE")
        conn1.execute(
            "INSERT INTO probe_samples(probe_no, sample_source, sample_fp, edge_id, "
            "edge_version, pinned_by, status) VALUES (?,?,?,?,?,?,?)",
            ("P-0001", "alice", "fp-a", eid, "v1", "alice", "已钉定"),
        )
        # 第二人抢锁不得成，随后被唯一约束挡下
        with pytest.raises(sqlite3.OperationalError):
            conn2.execute("BEGIN IMMEDIATE")
        conn1.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn2.execute(
                "INSERT INTO probe_samples(probe_no, sample_source, sample_fp, "
                "edge_id, edge_version, pinned_by, status) VALUES (?,?,?,?,?,?,?)",
                ("P-0001", "bob", "fp-b", eid, "v1", "bob", "已钉定"),
            )
        conn2.rollback()
        assert len(_rows(db_path)) == 1
    finally:
        conn1.close()
        conn2.close()


def test_two_threads_pin_same_fp_only_one_wins(db_path):
    """并发调用 pin_probe：一个成功，另一个收到白话冲突而非静默成功。"""
    import threading

    eid = _edge_id(db_path)
    outcomes = []
    barrier = threading.Barrier(2)

    def worker(name):
        barrier.wait()
        try:
            probe_empty.pin_probe(
                {"probe_no": "P-7777", "sample_fp": "fp-race",
                 "sample_source": name, "edge_id": eid, "pinned_by": name},
                db_path,
            )
            outcomes.append(("ok", name))
        except ServiceError as exc:
            outcomes.append(("conflict", exc.message))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 2
    ok = [o for o in outcomes if o[0] == "ok"]
    conflicts = [o for o in outcomes if o[0] == "conflict"]
    assert len(ok) == 1, outcomes
    assert len(conflicts) == 1, outcomes
    assert "已被" in conflicts[0][1] or "重复" in conflicts[0][1]
    assert len(_rows(db_path)) == 1


def test_ledger_note_does_not_green(db_path):
    from app.services import probe_empty_ledger

    out = probe_empty_ledger.append_ledger(1, "旁路留痕", db_path)
    assert out["truth"] == probe_empty._BAD
    assert out["panel"] == probe_empty._BAD
    assert _rows(db_path) == []
