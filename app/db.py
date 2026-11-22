"""SQLite 连接与建表。

编号按来源区分：灯杆号、单号都用 (来源台账, 台账内编号) 作为自然主键，
避免不同台账之间编号撞车。

资产清单支持“整版换表”：
- ``asset_version`` 记录每次成功换表的版本，撤销复算按提交时生效的整版清单判定；
- ``asset.active=0`` 表示该资产号在新版清单中消失（停用），行本身保留，
  已入仓记录仍可回查原资产号；
- 同一资产号换表后可改挂到另一根灯杆（更新 pole_source/pole_no，id 不变）。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

def _db_path() -> Path:
    # 每次连接时解析，允许测试通过 POLE_RECON_DB 切换库；容器内路径固定不变
    return Path(
        os.environ.get("POLE_RECON_DB")
        or Path(__file__).resolve().parent.parent / "data" / "recon.db"
    )

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('asset','ticket','confirm')),
    filename    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 资产号清单：资产号全局唯一。active=0 表示换表后停用（行保留供入仓回查）。
CREATE TABLE IF NOT EXISTS asset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_no    TEXT NOT NULL UNIQUE,
    pole_source TEXT NOT NULL,
    pole_no     TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    version_id  INTEGER,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 故障单：单号按来源台账区分 (source + ticket_no 唯一)
CREATE TABLE IF NOT EXISTS ticket (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    ticket_no    TEXT NOT NULL,
    pole_source  TEXT NOT NULL,
    pole_no      TEXT NOT NULL,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(source, ticket_no)
);

-- 同杆确认：可撤销。同一对灯杆只保留一条活动确认，撤销后可再次确认。
CREATE TABLE IF NOT EXISTS confirmation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_a     TEXT NOT NULL,
    pole_no_a    TEXT NOT NULL,
    source_b     TEXT NOT NULL,
    pole_no_b    TEXT NOT NULL,
    confirmed_by TEXT NOT NULL DEFAULT '',
    active       INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_confirmation_active ON confirmation(active);

-- 已核对（已入仓）单子的归属，锁定不动
CREATE TABLE IF NOT EXISTS verified (
    ticket_id   INTEGER PRIMARY KEY REFERENCES ticket(id),
    asset_id    INTEGER NOT NULL REFERENCES asset(id),
    group_key   TEXT NOT NULL,
    verified_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 资产清单整版版本：每次换表成功生成一版
CREATE TABLE IF NOT EXISTS asset_version (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 选定的撤销方案（撤销一张同杆确认）
CREATE TABLE IF NOT EXISTS undo_plan (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key        TEXT NOT NULL,
    confirmation_id  INTEGER NOT NULL REFERENCES confirmation(id),
    edge_label       TEXT NOT NULL,
    asset_version_id INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 整组入仓清单
CREATE TABLE IF NOT EXISTS warehouse (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key        TEXT NOT NULL UNIQUE,
    asset_version_id INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS warehouse_item (
    warehouse_id INTEGER NOT NULL REFERENCES warehouse(id),
    ticket_id    INTEGER NOT NULL REFERENCES ticket(id),
    asset_id     INTEGER NOT NULL REFERENCES asset(id),
    PRIMARY KEY (warehouse_id, ticket_id)
);
"""

# 旧库（换表功能上线前的库）升级：补列并把现有清单登记为第 1 版。
def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    if _table_columns(conn, "asset") and "active" not in _table_columns(conn, "asset"):
        conn.execute("ALTER TABLE asset ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    asset_cols = _table_columns(conn, "asset")
    if asset_cols and "version_id" not in asset_cols:
        conn.execute("ALTER TABLE asset ADD COLUMN version_id INTEGER")
    if _table_columns(conn, "undo_plan") and "asset_version_id" not in _table_columns(
        conn, "undo_plan"
    ):
        conn.execute("ALTER TABLE undo_plan ADD COLUMN asset_version_id INTEGER")
    if _table_columns(conn, "warehouse") and "asset_version_id" not in _table_columns(
        conn, "warehouse"
    ):
        conn.execute("ALTER TABLE warehouse ADD COLUMN asset_version_id INTEGER")

    seeded = conn.execute("SELECT COUNT(*) c FROM asset_version").fetchone()["c"]
    if seeded:
        return
    # 旧库已有资产（哪怕还没有故障单/入仓记录）：把现有清单登记为第 1 版
    if conn.execute("SELECT COUNT(*) c FROM asset").fetchone()["c"] == 0:
        return  # 空库：首次上传换表时自然建版本，无需占位
    cur = conn.execute(
        "INSERT INTO asset_version(filename) VALUES (?)",
        ("旧库升级：沿用库内现有资产清单",),
    )
    vid = cur.lastrowid
    conn.execute("UPDATE asset SET version_id=? WHERE version_id IS NULL", (vid,))
    conn.execute(
        "UPDATE undo_plan SET asset_version_id=? WHERE asset_version_id IS NULL", (vid,)
    )
    conn.execute(
        "UPDATE warehouse SET asset_version_id=? WHERE asset_version_id IS NULL", (vid,)
    )


def get_db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
