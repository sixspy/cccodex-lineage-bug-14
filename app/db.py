import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineage_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_code TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('ODS','DWD','DWS','ADS')),
  owner_team TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('登记','在用','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_code TEXT NOT NULL UNIQUE,
  upstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  downstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  transform_kind TEXT NOT NULL DEFAULT '直抽',
  status TEXT NOT NULL CHECK (status IN ('草稿','已发布','已作废')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(upstream_id, downstream_id)
);

CREATE TABLE IF NOT EXISTS impact_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_code TEXT NOT NULL UNIQUE,
  root_node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','成功','失败')),
  affected_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS impact_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  hop INTEGER NOT NULL DEFAULT 0,
  UNIQUE(job_id, node_id)
);

CREATE TABLE IF NOT EXISTS notify_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  channel TEXT NOT NULL DEFAULT '站内',
  status TEXT NOT NULL CHECK (status IN ('待发','已发','失败')),
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  summary TEXT NOT NULL,
  node_count INTEGER NOT NULL,
  edge_count INTEGER NOT NULL,
  orphan_count INTEGER NOT NULL,
  uncovered_edges INTEGER NOT NULL,
  probe_no TEXT NOT NULL DEFAULT '',
  sample_source TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 独立探针队列：钉定样本的唯一落点，样本指纹在钉定瞬间锁定。
CREATE TABLE IF NOT EXISTS probe_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  probe_no TEXT NOT NULL UNIQUE,
  sample_source TEXT NOT NULL,
  sample_fp TEXT NOT NULL,
  edge_id INTEGER REFERENCES lineage_edges(id),
  edge_version TEXT NOT NULL DEFAULT '',
  pinned_by TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('已钉定','已核齐')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 归档层：冻住钉定瞬间的样本与边版本，用于后续对照校验。
CREATE TABLE IF NOT EXISTS probe_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  probe_no TEXT NOT NULL UNIQUE,
  edge_id INTEGER,
  edge_version TEXT NOT NULL DEFAULT '',
  sample_fp TEXT NOT NULL,
  sample_source TEXT NOT NULL,
  frozen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lab_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_db(db_path: str | None = None):
    """Serialized write transaction.

    以 BEGIN IMMEDIATE 先取写锁，保证同一时刻只有一个钉定/核齐请求落库；
    并发请求排队，抢不到锁的重复写入由唯一约束挡下，不会双写后静默成功。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(coverage_reports)").fetchall()
    }
    if "probe_no" not in cols:
        conn.execute(
            "ALTER TABLE coverage_reports ADD COLUMN probe_no TEXT NOT NULL DEFAULT ''"
        )
    if "sample_source" not in cols:
        conn.execute(
            "ALTER TABLE coverage_reports ADD COLUMN sample_source TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_probe_samples_edge "
        "ON probe_samples(edge_id)"
    )
    # 样本指纹锁定：同一指纹只许被一个探针钉定，数据库层兜底防双写。
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_probe_samples_fp "
            "ON probe_samples(sample_fp)"
        )
    except sqlite3.IntegrityError:
        # 历史脏数据有重复指纹时保留旧库可启动，写入侧仍会挡住新重复。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_probe_samples_fp "
            "ON probe_samples(sample_fp)"
        )


def init_schema(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
