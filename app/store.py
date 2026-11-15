"""SQLite 访问（薄封装）。

表：
- events       归档事件；主键 (source, fingerprint, occur) 按「来源 + 内容指纹」去重，
               同一文件重放 / 行号重排都不会重复导入（occur 是同指纹在文件内的第几次出现，
               保证文件内本来就有的同内容重复行依然各自归档，合并层照常静默去重）
- quarantine   隔离区：同来源同 (表, 键, 版本) 但内容不同的到来事件，待人工裁决
- replay_runs  整包重放运行记录（状态机：staged/published/unchanged/blocked/failed）
- publications 现网快照；is_live=1 的只有一行，原子替换（同事务作废旧版 + 写入新版）
- runs         工作台临时合并运行（分析用，非现网）
- meta         seed 版本等键值
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
  fingerprint    TEXT    NOT NULL,
  occur          INTEGER NOT NULL DEFAULT 0,
  line_no        INTEGER NOT NULL,
  table_name     TEXT    NOT NULL,
  pk             TEXT    NOT NULL,
  version        INTEGER NOT NULL,
  ts             TEXT    NOT NULL,
  op             TEXT    NOT NULL,
  after_json     TEXT    NOT NULL,
  schema_version INTEGER NOT NULL,
  batch          TEXT    NOT NULL DEFAULT '',
  archived_at    TEXT    NOT NULL DEFAULT '',
  PRIMARY KEY (source, fingerprint, occur)
);
CREATE INDEX IF NOT EXISTS idx_events_table ON events(table_name, line_no);
CREATE INDEX IF NOT EXISTS idx_events_identity ON events(source, table_name, pk, version);

CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  tables_json    TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  summary_json   TEXT NOT NULL,
  payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS quarantine (
  quarantine_id        TEXT PRIMARY KEY,
  created_at           TEXT NOT NULL,
  replay_id            TEXT NOT NULL,
  source               TEXT NOT NULL,
  table_name           TEXT NOT NULL,
  pk                   TEXT NOT NULL,
  version              INTEGER NOT NULL,
  incoming_fingerprint TEXT NOT NULL,
  incoming_json        TEXT NOT NULL,
  archived_json        TEXT NOT NULL,
  diff_json            TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'pending',
  resolved_at          TEXT,
  UNIQUE (source, incoming_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_quarantine_status ON quarantine(status, table_name);

CREATE TABLE IF NOT EXISTS replay_runs (
  replay_id      TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  status         TEXT NOT NULL,
  package        TEXT NOT NULL,
  tables_json    TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replay_created ON replay_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS publications (
  publication_id TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  replay_id      TEXT NOT NULL,
  tables_json    TEXT NOT NULL,
  summary_json   TEXT NOT NULL,
  snapshot_json  TEXT NOT NULL,
  is_live        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_publications_live ON publications(is_live);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class StoreError(RuntimeError):
    """存储层一致性错误（路由转成 5xx，重放运行记为 failed）。"""


def connect() -> sqlite3.Connection:
    settings.ensure_data_dir()
    conn = sqlite3.connect(settings.db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表；旧库（events 按 source+line_no 去重）自动迁移为内容指纹去重。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    legacy = bool(cols) and "fingerprint" not in cols
    if legacy:
        conn.execute("ALTER TABLE events RENAME TO events_legacy")
        conn.commit()
    conn.executescript(SCHEMA)
    if legacy:
        _migrate_legacy_events(conn)
    conn.commit()


def _migrate_legacy_events(conn: sqlite3.Connection) -> None:
    """把旧 events（无指纹）逐行迁入新表：指纹现场计算，occur 按行号顺序编号。"""
    rows = conn.execute(
        "SELECT * FROM events_legacy ORDER BY source, line_no"
    ).fetchall()
    seen: dict[tuple, int] = {}
    now = now_iso()
    for row in rows:
        event = _row_to_event(row)
        fp = event.fingerprint
        key = (row["source"], fp)
        occur = seen.get(key, 0)
        seen[key] = occur + 1
        conn.execute(
            """INSERT INTO events
               (source, fingerprint, occur, line_no, table_name, pk, version,
                ts, op, after_json, schema_version, batch, archived_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["source"], fp, occur, row["line_no"], row["table_name"], row["pk"],
             int(row["version"]), row["ts"], row["op"], row["after_json"],
             int(row["schema_version"]), "legacy", now),
        )
    conn.execute("DROP TABLE events_legacy")
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


def archive_events(conn: sqlite3.Connection, events: list[Event], batch: str = "") -> dict:
    """按「来源 + 内容指纹 + 同指纹序号」幂等归档，逐条给出处置结果。

    每条事件三种去向（disposition）：
    - duplicate：同 (source, fingerprint, occur) 已归档 → 跳过。
      同一文件重放、行号整体前移的重放都落在这里，不会新增记录。
    - conflict：指纹未见，但**先前批次**已归档同 (source, table, pk, version)
      的不同内容 → 隔离候选（本函数只标记不入库，由 replay 模块写隔离区）。
      同批次内的同版本不同内容不在此列（保留给合并层判冲突，口径不变）。
    - archived：其余 → 新归档。
    """
    now = now_iso()
    seen: dict[tuple, int] = {}
    inserted = 0
    dispositions: list[dict] = []
    for event in events:
        fp = event.fingerprint
        key = (event.source, fp)
        occur = seen.get(key, 0)
        seen[key] = occur + 1

        dup = conn.execute(
            "SELECT line_no, batch FROM events WHERE source=? AND fingerprint=? AND occur=?",
            (event.source, fp, occur),
        ).fetchone()
        if dup is not None:
            dispositions.append(
                {
                    "disposition": "duplicate",
                    "event": event,
                    "occur": occur,
                    "archived_line_no": int(dup["line_no"]),
                    "archived_batch": dup["batch"],
                }
            )
            continue

        prior = conn.execute(
            """SELECT fingerprint, occur, line_no, ts, op, after_json, schema_version, batch
               FROM events
               WHERE source=? AND table_name=? AND pk=? AND version=? AND batch<>?""",
            (event.source, event.table, event.pk, event.version, batch),
        ).fetchall()
        if prior and all(row["fingerprint"] != fp for row in prior):
            dispositions.append(
                {
                    "disposition": "conflict",
                    "event": event,
                    "occur": occur,
                    "prior": [dict(row) for row in prior],
                }
            )
            continue

        conn.execute(
            """INSERT INTO events
               (source, fingerprint, occur, line_no, table_name, pk, version,
                ts, op, after_json, schema_version, batch, archived_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.source, fp, occur, event.line_no, event.table, event.pk, event.version,
             event.ts_iso, event.op, json.dumps(event.after, ensure_ascii=False),
             event.schema_version, batch, now),
        )
        inserted += 1
        dispositions.append({"disposition": "archived", "event": event, "occur": occur})
    conn.commit()
    return {
        "attempted": len(events),
        "inserted": inserted,
        "skipped": len(events) - inserted,
        "dispositions": dispositions,
    }


def seed_sample(conn: sqlite3.Connection, events_dir: Path | None = None) -> dict:
    """把 sample_data/events/*.jsonl 逐表幂等导入；返回本次导入统计。"""
    events_dir = events_dir or settings.EVENTS_DIR
    batch = f"seed:{settings.SEED_VERSION}"
    total = {"attempted": 0, "inserted": 0, "skipped": 0, "tables": {}}
    for item in settings.TABLES:
        path = events_dir / item["file"]
        events = load_events_file(path, item["name"])
        stats = archive_events(conn, events, batch=batch)
        total["attempted"] += stats["attempted"]
        total["inserted"] += stats["inserted"]
        total["skipped"] += stats["skipped"]
        total["tables"][item["name"]] = {
            "attempted": stats["attempted"],
            "inserted": stats["inserted"],
            "skipped": stats["skipped"],
        }
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
    """按归档先后（rowid = 到达顺序）返回某表归档事件。"""
    rows = conn.execute(
        "SELECT * FROM events WHERE table_name=? ORDER BY rowid", (table,)
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


def count_events(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE table_name=?", (table,)
    ).fetchone()
    return int(row["n"])


def count_events_by_source(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE source=?", (source,)
    ).fetchone()
    return int(row["n"])


def missing_event_keys(conn: sqlite3.Connection, keys: list[tuple]) -> list[tuple]:
    """归档核对：返回 (source, fingerprint, occur) 中不在库里的部分（应为空）。"""
    missing: list[tuple] = []
    for source, fp, occur in keys:
        row = conn.execute(
            "SELECT 1 FROM events WHERE source=? AND fingerprint=? AND occur=?",
            (source, fp, occur),
        ).fetchone()
        if row is None:
            missing.append((source, fp, occur))
    return missing


def replace_event_content(
    conn: sqlite3.Connection, source: str, fingerprint: str, occur: int, incoming: dict
) -> None:
    """裁决「采用到来」：把归档中同版本旧内容替换为到来内容（归档位置与批次不变）。"""
    cursor = conn.execute(
        """UPDATE events
           SET fingerprint=?, after_json=?, ts=?, op=?, schema_version=?
           WHERE source=? AND fingerprint=? AND occur=?""",
        (
            incoming["fingerprint"],
            json.dumps(incoming["after"], ensure_ascii=False),
            incoming["ts"],
            incoming["op"],
            int(incoming["schema_version"]),
            source,
            fingerprint,
            int(occur),
        ),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise StoreError(
            f"裁决替换失败：{source} 指纹 {fingerprint[:12]}… 第 {occur} 次出现不在归档中"
        )
    conn.commit()


# --- quarantine ------------------------------------------------------------


def insert_quarantine(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        """INSERT INTO quarantine
           (quarantine_id, created_at, replay_id, source, table_name, pk, version,
            incoming_fingerprint, incoming_json, archived_json, diff_json, status, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record["quarantine_id"],
            record["created_at"],
            record["replay_id"],
            record["source"],
            record["table_name"],
            record["pk"],
            int(record["version"]),
            record["incoming_fingerprint"],
            json.dumps(record["incoming"], ensure_ascii=False),
            json.dumps(record["archived"], ensure_ascii=False),
            json.dumps(record["diff"], ensure_ascii=False),
            record["status"],
            record["resolved_at"],
        ),
    )
    conn.commit()


def _row_to_quarantine(row: sqlite3.Row) -> dict:
    return {
        "quarantine_id": row["quarantine_id"],
        "created_at": row["created_at"],
        "replay_id": row["replay_id"],
        "source": row["source"],
        "table_name": row["table_name"],
        "pk": row["pk"],
        "version": int(row["version"]),
        "incoming_fingerprint": row["incoming_fingerprint"],
        "incoming": json.loads(row["incoming_json"]),
        "archived": json.loads(row["archived_json"]),
        "diff": json.loads(row["diff_json"]),
        "status": row["status"],
        "resolved_at": row["resolved_at"],
    }


def get_quarantine(conn: sqlite3.Connection, quarantine_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quarantine WHERE quarantine_id=?", (quarantine_id,)
    ).fetchone()
    return _row_to_quarantine(row) if row else None


def find_quarantine_by_incoming(
    conn: sqlite3.Connection, source: str, incoming_fingerprint: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quarantine WHERE source=? AND incoming_fingerprint=?",
        (source, incoming_fingerprint),
    ).fetchone()
    return _row_to_quarantine(row) if row else None


def list_quarantine(
    conn: sqlite3.Connection,
    status: str | None = None,
    tables: list[str] | None = None,
    replay_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM quarantine WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if tables:
        sql += " AND table_name IN (%s)" % ",".join("?" for _ in tables)
        params.extend(tables)
    if replay_id:
        sql += " AND replay_id=?"
        params.append(replay_id)
    sql += " ORDER BY created_at, quarantine_id LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_quarantine(row) for row in rows]


def count_quarantine(
    conn: sqlite3.Connection, status: str = "pending", tables: list[str] | None = None
) -> int:
    sql = "SELECT COUNT(*) AS n FROM quarantine WHERE status=?"
    params: list = [status]
    if tables:
        sql += " AND table_name IN (%s)" % ",".join("?" for _ in tables)
        params.extend(tables)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"])


def mark_quarantine_resolved(conn: sqlite3.Connection, quarantine_id: str, decision: str) -> None:
    conn.execute(
        "UPDATE quarantine SET status=?, resolved_at=? WHERE quarantine_id=?",
        (decision, now_iso(), quarantine_id),
    )
    conn.commit()


# --- replay runs -----------------------------------------------------------


def save_replay_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO replay_runs
           (replay_id, created_at, status, package, tables_json, schema_version, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            run["replay_id"],
            run["created_at"],
            run["status"],
            run["package"],
            json.dumps(run["tables"], ensure_ascii=False),
            int(run["schema_version"]),
            json.dumps(run, ensure_ascii=False),
        ),
    )
    conn.commit()


def get_replay_run(conn: sqlite3.Connection, replay_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload_json FROM replay_runs WHERE replay_id=?", (replay_id,)
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def list_replay_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT payload_json FROM replay_runs ORDER BY created_at DESC, replay_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def replay_run_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM replay_runs").fetchone()
    return int(row["n"])


# --- publications ----------------------------------------------------------


def publish(conn: sqlite3.Connection, publication: dict) -> None:
    """原子替换现网：同一事务里作废旧 live + 写入新 live；任何失败都回滚，旧版保留。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE publications SET is_live=0 WHERE is_live=1")
        conn.execute(
            """INSERT INTO publications
               (publication_id, created_at, replay_id, tables_json, summary_json, snapshot_json, is_live)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (
                publication["publication_id"],
                publication["created_at"],
                publication["replay_id"],
                json.dumps(publication["tables"], ensure_ascii=False),
                json.dumps(publication["summary"], ensure_ascii=False),
                json.dumps(publication["snapshot"], ensure_ascii=False),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _row_to_publication(row: sqlite3.Row) -> dict:
    return {
        "publication_id": row["publication_id"],
        "created_at": row["created_at"],
        "replay_id": row["replay_id"],
        "tables": json.loads(row["tables_json"]),
        "summary": json.loads(row["summary_json"]),
        "snapshot": json.loads(row["snapshot_json"]),
        "is_live": bool(row["is_live"]),
    }


def live_publication(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM publications WHERE is_live=1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return _row_to_publication(row) if row else None


def list_publications(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM publications ORDER BY created_at DESC, publication_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_publication(row) for row in rows]


def publication_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM publications").fetchone()
    return int(row["n"])


# --- runs（工作台分析运行，非现网） -----------------------------------------


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
