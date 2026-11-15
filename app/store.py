"""SQLite 访问（薄封装）。

表：
- events  归档事件；主键 (source, line_no) 保证重复启动/重复 seed 不会重复导入
- runs    每次合并运行的元信息 + 完整结果 JSON（结果页、复核页、快照下载共用）
- meta    seed 版本等键值
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import settings
from .archive import Event, load_events_file, parse_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  source         TEXT    NOT NULL,
  line_no        INTEGER NOT NULL,
  table_name     TEXT    NOT NULL,
  pk             TEXT    NOT NULL,
  version        INTEGER NOT NULL,
  ts             TEXT    NOT NULL,
  op             TEXT    NOT NULL,
  after_json     TEXT    NOT NULL,
  schema_version INTEGER NOT NULL,
  PRIMARY KEY (source, line_no)
);
CREATE INDEX IF NOT EXISTS idx_events_table ON events(table_name, line_no);

CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  tables_json    TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  summary_json   TEXT NOT NULL,
  payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    settings.ensure_data_dir()
    conn = sqlite3.connect(settings.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- meta ------------------------------------------------------------------


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --- events ----------------------------------------------------------------


def archive_events(conn: sqlite3.Connection, events: list[Event]) -> dict:
    """幂等归档：主键冲突一律忽略（重复启动/重复 seed 不产生脏数据）。"""
    inserted = 0
    for event in events:
        row = event.to_row()
        cursor = conn.execute(
            """INSERT OR IGNORE INTO events
               (source, line_no, table_name, pk, version, ts, op, after_json, schema_version)
               VALUES (:source, :line_no, :table, :pk, :version, :ts, :op, :after_json, :schema_version)""",
            row,
        )
        inserted += cursor.rowcount
    conn.commit()
    return {"attempted": len(events), "inserted": inserted, "skipped": len(events) - inserted}


def seed_sample(conn: sqlite3.Connection, events_dir: Path | None = None) -> dict:
    """把 sample_data/events/*.jsonl 逐表幂等导入；返回本次导入统计。"""
    events_dir = events_dir or settings.EVENTS_DIR
    total = {"attempted": 0, "inserted": 0, "skipped": 0, "tables": {}}
    for item in settings.TABLES:
        path = events_dir / item["file"]
        events = load_events_file(path, item["name"])
        stats = archive_events(conn, events)
        total["attempted"] += stats["attempted"]
        total["inserted"] += stats["inserted"]
        total["skipped"] += stats["skipped"]
        total["tables"][item["name"]] = stats
    set_meta(conn, "seed_version", settings.SEED_VERSION)
    set_meta(conn, "seeded_at", now_iso())
    total["seed_version"] = settings.SEED_VERSION
    return total


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        table=row["table_name"],
        line_no=int(row["line_no"]),
        pk=row["pk"],
        version=int(row["version"]),
        ts=parse_ts(row["ts"]),
        op=row["op"],
        after=json.loads(row["after_json"]),
        schema_version=int(row["schema_version"]),
        source=row["source"],
    )


def fetch_events(conn: sqlite3.Connection, table: str) -> list[Event]:
    """按文件行号（= 到达顺序）返回某表归档事件。"""
    rows = conn.execute(
        "SELECT * FROM events WHERE table_name=? ORDER BY source, line_no", (table,)
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def event_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT table_name, COUNT(*) AS n FROM events GROUP BY table_name"
    ).fetchall()
    return {row["table_name"]: int(row["n"]) for row in rows}


def total_events(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
    return int(row["n"])


# --- runs ------------------------------------------------------------------


def save_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, created_at, tables_json, schema_version, summary_json, payload_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            run["run_id"],
            run["created_at"],
            json.dumps(run["tables"], ensure_ascii=False),
            int(run["schema_version"]),
            json.dumps(run["summary"], ensure_ascii=False),
            json.dumps(run["result"], ensure_ascii=False),
        ),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "tables": json.loads(row["tables_json"]),
        "schema_version": int(row["schema_version"]),
        "summary": json.loads(row["summary_json"]),
        "result": json.loads(row["payload_json"]),
    }


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, created_at, tables_json, schema_version, summary_json FROM runs "
        "ORDER BY created_at DESC, run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "tables": json.loads(row["tables_json"]),
                "schema_version": int(row["schema_version"]),
                "summary": json.loads(row["summary_json"]),
            }
        )
    return out


def run_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def event_overview(conn: sqlite3.Connection) -> dict[str, dict]:
    """首页概览：按表统计 op / schema 版本 / 键数 / 时间区间（全部来自归档表）。"""
    out: dict[str, dict] = {}
    for item in settings.TABLES:
        table = item["name"]
        rows = conn.execute(
            "SELECT op, schema_version, pk, ts FROM events WHERE table_name=?", (table,)
        ).fetchall()
        ops: dict[str, int] = {}
        versions: dict[str, int] = {}
        stamps: list[str] = []
        keys: set[str] = set()
        for row in rows:
            ops[row["op"]] = ops.get(row["op"], 0) + 1
            versions[str(row["schema_version"])] = versions.get(str(row["schema_version"]), 0) + 1
            keys.add(row["pk"])
            stamps.append(row["ts"])
        out[table] = {
            "table": table,
            "label": item["label"],
            "desc": item["desc"],
            "file": item["file"],
            "events": len(rows),
            "ops": ops,
            "schema_versions": versions,
            "keys": len(keys),
            "first_ts": min(stamps) if stamps else None,
            "last_ts": max(stamps) if stamps else None,
        }
    return out
