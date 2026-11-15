"""整包重放：内容指纹去重、隔离待裁决、三重核对、原子替换、失败续跑、上传重放。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import archive, replay, settings, store


def write_jsonl(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def load_seed_events(table: str) -> list[dict]:
    path = settings.EVENTS_DIR / f"{table}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_sample_package(conn, schema_version: int = 2) -> dict:
    return replay.execute_replay(conn, replay.sample_package_files(), package="replay",
                                 schema_version=schema_version)


# --- 内容指纹 ----------------------------------------------------------------


def test_fingerprint_ignores_line_no():
    events = archive.load_events_file(settings.EVENTS_DIR / "orders.jsonl", "orders")
    first = events[0]
    same = archive.Event(
        table=first.table, line_no=999, pk=first.pk, version=first.version, ts=first.ts,
        op=first.op, after=dict(first.after), schema_version=first.schema_version, source=first.source,
    )
    assert same.fingerprint == first.fingerprint  # 行号不同、内容一致 → 同一事件
    changed = archive.Event(
        table=first.table, line_no=first.line_no, pk=first.pk, version=first.version, ts=first.ts,
        op=first.op, after={**first.after, "amount": 1}, schema_version=first.schema_version,
        source=first.source,
    )
    assert changed.fingerprint != first.fingerprint  # 内容变了 → 新指纹


# --- 重放去重 ----------------------------------------------------------------


def test_shifted_replay_adds_no_new_events(conn, tmp_path):
    """行号整体前移的同一文件：全部判重复，不新增记录，核对通过后正常发布。"""
    shifted = load_seed_events("orders")[4:]  # 源库压缩掉前 4 行，行号整体前移
    path = write_jsonl(tmp_path / "orders.jsonl", shifted)

    run = replay.execute_replay(conn, [(path, "orders")], package="test-shift")

    assert run["status"] == "published"
    entry = run["files"][0]
    assert entry["archived_new"] == 0
    assert entry["duplicates"] == len(shifted)
    assert store.total_events(conn) == 180          # 一条都没有新增
    assert run["checks"]["ok"] is True
    # 被跳过原因里能看到行号前移对照
    reasons = [s["reason"] for s in entry["skipped"]]
    assert any("重排" in reason for reason in reasons)


def test_sample_package_new_orders_not_dropped(conn):
    """核心缺陷回归：重放文件里的新订单必须归档并进候选，而不是被行号误判丢掉。"""
    run = run_sample_package(conn)

    orders = run["files"][0]
    assert orders["archived_new"] == 3               # O7001 v1/v2、O7002 v1
    assert orders["duplicates"] == 55
    assert orders["quarantined"] == 1                # O1001 v2 被源库改过内容
    assert {k["pk"] for k in orders["new_keys"]} == {"O7001", "O7002"}
    assert store.total_events(conn) == 184           # 180 + 3 订单 + 1 库存

    # 有隔离待裁决 → 不发布；但新订单已经安全躺在归档里（续跑的基础）
    assert run["status"] == "blocked"
    assert run["checks"]["source"]["ok"] is True
    assert run["checks"]["archive"]["ok"] is True
    assert run["checks"]["candidate"]["ok"] is True
    assert run["checks"]["quarantine"]["ok"] is False
    assert store.live_publication(conn) is None

    quarantine = store.list_quarantine(conn, status="pending")
    assert len(quarantine) == 1
    record = quarantine[0]
    assert record["pk"] == "O1001" and record["version"] == 2
    assert record["diff"] == ["amount"]
    assert record["archived"]["after"]["amount"] == 1474
    assert record["incoming"]["after"]["amount"] == 1509


def test_quarantine_is_idempotent_across_retries(conn):
    """同一文件反复重放：隔离记录只建一条，事件不重复追加。"""
    run_sample_package(conn)
    run_sample_package(conn)
    assert store.total_events(conn) == 184
    assert store.count_quarantine(conn, status="pending") == 1
    assert len(store.list_quarantine(conn)) == 1


def test_keep_archived_then_resume_publishes_old_content(conn):
    """裁决「保留归档」→ 重跑同一文件续跑 → 发布；归档内容保持原样。"""
    run1 = run_sample_package(conn)
    qid = run1["quarantine"][0]["quarantine_id"]
    replay.resolve_quarantine(conn, qid, "keep_archived")

    run2 = run_sample_package(conn)
    assert run2["status"] == "published"
    orders = run2["files"][0]
    assert orders["archived_new"] == 0               # 续跑：上一轮的归档不重复追加
    assert orders["skipped_resolved"] == 1           # 已裁决的到来内容被跳过
    assert store.total_events(conn) == 184

    live = store.live_publication(conn)
    assert live["publication_id"] == run2["publication_id"]
    rows = {row["pk"]: row for row in live["snapshot"]["tables"]["orders"]["rows"]}
    assert "O7001" in rows and "O7002" in rows       # 新订单进入现网
    v2 = conn.execute(
        "SELECT after_json FROM events WHERE pk='O1001' AND version=2"
    ).fetchone()
    assert json.loads(v2["after_json"])["amount"] == 1474   # 保留归档内容

    run3 = run_sample_package(conn)
    assert run3["status"] == "unchanged"             # 候选与现网一致，不做无意义替换
    assert store.publication_count(conn) == 1


def test_use_incoming_replaces_archived_content(conn):
    """裁决「采用到来」→ 归档同版本内容被替换 → 重跑后候选反映新内容。"""
    run1 = run_sample_package(conn)
    qid = run1["quarantine"][0]["quarantine_id"]
    replay.resolve_quarantine(conn, qid, "use_incoming")

    v2 = conn.execute(
        "SELECT after_json FROM events WHERE pk='O1001' AND version=2"
    ).fetchone()
    assert json.loads(v2["after_json"])["amount"] == 1509   # 归档内容已替换

    run2 = run_sample_package(conn)
    assert run2["status"] == "published"
    assert run2["files"][0]["quarantined"] == 0      # 到来内容已在归档，判重复
    assert store.total_events(conn) == 184

    with pytest.raises(Exception, match="不能重复裁决"):
        replay.resolve_quarantine(conn, qid, "keep_archived")


def test_double_resolve_rejected(conn):
    run1 = run_sample_package(conn)
    qid = run1["quarantine"][0]["quarantine_id"]
    replay.resolve_quarantine(conn, qid, "keep_archived")
    with pytest.raises(replay.ReplayError):
        replay.resolve_quarantine(conn, qid, "use_incoming")
    with pytest.raises(replay.ReplayError):
        replay.resolve_quarantine(conn, "Q-ghost", "keep_archived")
    with pytest.raises(replay.ReplayError):
        replay.resolve_quarantine(conn, qid, "bogus")


# --- 原子替换与失败保留旧版 ----------------------------------------------------


def test_publish_failure_keeps_old_live(conn, tmp_path, monkeypatch):
    """发布动作异常 → 事务回滚，旧现网不动，重放记为 failed。"""
    shifted = load_seed_events("orders")[4:]
    path = write_jsonl(tmp_path / "orders.jsonl", shifted)
    first = replay.execute_replay(conn, [(path, "orders")], package="baseline")
    assert first["status"] == "published"
    live_before = store.live_publication(conn)

    def boom(_conn, _publication):
        raise RuntimeError("模拟发布中途故障")

    # 候选与现网不同（多一条新事件）才会走到发布；发布故障 → 回滚、旧版保留
    new_event = {
        "pk": "O8001", "version": 1, "ts": "2026-11-10T03:00:00Z", "op": "c",
        "after": {"order_id": "O8001", "customer_id": "C1001", "amount": 500, "qty": 1,
                  "status": "paid", "legacy_channel": "web", "updated_at": "2026-11-10T03:00:00Z"},
        "schema_version": 1,
    }
    failing_path = write_jsonl(tmp_path / "failing" / "orders.jsonl", shifted + [new_event])

    monkeypatch.setattr(store, "publish", boom)
    with pytest.raises(RuntimeError):
        replay.execute_replay(conn, [(failing_path, "orders")], package="will-fail", replay_id="P-FAIL-1")

    failed = store.get_replay_run(conn, "P-FAIL-1")
    assert failed["status"] == "failed"
    assert "模拟发布中途故障" in failed["error"]
    live_after = store.live_publication(conn)
    assert live_after["publication_id"] == live_before["publication_id"]  # 旧版保留


def test_blocked_run_keeps_old_live_then_resume(conn, tmp_path):
    """核对未通过 → 保留旧版；裁决后重跑同一文件 → 从已归档位置续跑并原子替换。"""
    shifted_path = write_jsonl(tmp_path / "orders.jsonl", load_seed_events("orders")[4:])
    baseline = replay.execute_replay(conn, [(shifted_path, "orders")], package="baseline")
    assert baseline["status"] == "published"
    live_v1 = store.live_publication(conn)

    blocked = run_sample_package(conn)
    assert blocked["status"] == "blocked"
    assert store.live_publication(conn)["publication_id"] == live_v1["publication_id"]

    replay.resolve_quarantine(conn, blocked["quarantine"][0]["quarantine_id"], "keep_archived")
    resumed = run_sample_package(conn)
    assert resumed["status"] == "published"
    live_v2 = store.live_publication(conn)
    assert live_v2["publication_id"] != live_v1["publication_id"]
    # 续跑没有重复追加：orders 归档 = 60 原始 + 3 新订单
    assert store.count_events(conn, "orders") == 63
    assert store.total_events(conn) == 184


# --- 旧库迁移 ----------------------------------------------------------------


def test_legacy_events_table_is_migrated(tmp_path, monkeypatch):
    """旧版 (source, line_no) 去重的库自动迁移为内容指纹去重，且不丢行、不重导。"""
    db = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("CDCWATER_DB", str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE events (
          source TEXT NOT NULL, line_no INTEGER NOT NULL, table_name TEXT NOT NULL,
          pk TEXT NOT NULL, version INTEGER NOT NULL, ts TEXT NOT NULL, op TEXT NOT NULL,
          after_json TEXT NOT NULL, schema_version INTEGER NOT NULL,
          PRIMARY KEY (source, line_no)
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    events = archive.load_events_file(settings.EVENTS_DIR / "orders.jsonl", "orders")
    for event in events[:10]:
        row = event.to_row()
        conn.execute(
            """INSERT INTO events
               (source, line_no, table_name, pk, version, ts, op, after_json, schema_version)
               VALUES (:source, :line_no, :table, :pk, :version, :ts, :op, :after_json, :schema_version)""",
            row,
        )
    conn.commit()
    conn.close()

    conn = store.connect()
    store.init_db(conn)  # 触发迁移
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "fingerprint" in cols
    assert store.count_events(conn, "orders") == 10

    stats = store.archive_events(conn, events, batch="replay-1")
    dispositions = [d["disposition"] for d in stats["dispositions"]]
    assert dispositions[:10].count("duplicate") == 10     # 迁移来的行判重复
    assert stats["inserted"] == len(events) - 10          # 其余正常归档
    conn.close()


# --- API ---------------------------------------------------------------------


def test_replay_page_and_api_flow(client):
    page = client.get("/replay")
    assert page.status_code == 200
    assert "整包重放" in page.text

    first = client.post("/api/replay", json={"package": "replay", "schema_version": 2})
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "blocked"
    assert body["files"]["orders.jsonl"]["archived_new"] == 3

    detail = client.get(f"/api/replays/{body['replay_id']}")
    assert detail.status_code == 200
    assert detail.json()["checks"]["quarantine"]["ok"] is False

    listing = client.get("/api/replays").json()
    assert listing["count"] >= 1

    quarantine = client.get("/api/quarantine?status=pending").json()
    assert quarantine["count"] == 1
    qid = quarantine["quarantine"][0]["quarantine_id"]

    resolved = client.post(f"/api/quarantine/{qid}/resolve", json={"decision": "use_incoming"})
    assert resolved.status_code == 200
    assert resolved.json()["quarantine"]["status"] == "use_incoming"

    second = client.post("/api/replay", json={"package": "replay", "schema_version": 2}).json()
    assert second["status"] == "published"
    assert second["publication_id"]

    third = client.post("/api/replay", json={"package": "replay", "schema_version": 2}).json()
    assert third["status"] == "unchanged"

    live = client.get("/api/publication").json()["live"]
    assert live["publication_id"] == second["publication_id"]
    assert live["summary"]["alive_rows"] == 43

    snap = client.get("/api/publication/snapshot")
    assert snap.status_code == 200
    payload = json.loads(snap.content)
    order_pks = {row["pk"] for row in payload["tables"]["orders"]["rows"]}
    assert {"O7001", "O7002"} <= order_pks

    # 页面呈现：被跳过原因 / 隔离事件 / 替换前后对照
    page = client.get(f"/replay?replay_id={second['replay_id']}")
    assert "被跳过原因" in page.text
    assert "隔离事件" in page.text
    assert "替换前后对照" in page.text
    assert "已裁决 · 采用到来" in page.text


def test_replay_page_shows_skip_reasons_and_quarantine(client):
    run = client.post("/api/replay", json={"package": "replay"}).json()
    page = client.get(f"/replay?replay_id={run['replay_id']}")
    assert page.status_code == 200
    assert "重排" in page.text                      # 行号前移的跳过原因
    assert "O1001" in page.text                     # 隔离事件键
    assert "保留归档" in page.text and "采用到来" in page.text  # 裁决按钮
    assert "核对未通过" in page.text                 # 保留旧版提示


def test_api_replay_rejects_unknown_package(client):
    response = client.post("/api/replay", json={"package": "ghost"})
    assert response.status_code == 400
    assert "未知的重放包" in response.json()["detail"]


def test_quarantine_resolve_unknown_is_400(client):
    response = client.post("/api/quarantine/Q-ghost/resolve", json={"decision": "keep_archived"})
    assert response.status_code == 400
    assert "不存在" in response.json()["detail"]


def test_upload_replay_roundtrip(client):
    before = client.get("/health").json()["events"]
    new_event = {
        "pk": "O8001", "version": 1, "ts": "2026-11-10T03:00:00Z", "op": "c",
        "after": {"order_id": "O8001", "customer_id": "C1001", "amount": 500, "qty": 1,
                  "status": "paid", "legacy_channel": "web", "updated_at": "2026-11-10T03:00:00Z"},
        "schema_version": 1,
    }
    content = (json.dumps(new_event, ensure_ascii=False) + "\n").encode("utf-8")
    first = client.post(
        "/api/replay/upload",
        files={"file": ("orders.jsonl", content, "application/jsonl")},
        data={"table": "orders", "schema_version": "2"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "published"
    assert client.get("/health").json()["events"] == before + 1

    # 重传同一文件：不重复追加，候选与现网一致 → unchanged
    second = client.post(
        "/api/replay/upload",
        files={"file": ("orders.jsonl", content, "application/jsonl")},
        data={"table": "orders", "schema_version": "2"},
    )
    assert second.json()["status"] == "unchanged"
    assert client.get("/health").json()["events"] == before + 1

    live = client.get("/api/publication").json()["live"]
    assert live is not None


def test_upload_rejects_unknown_table_and_empty_file(client):
    bad_name = client.post(
        "/api/replay/upload",
        files={"file": ("ghost.jsonl", b'{"pk":"X"}\n', "application/jsonl")},
    )
    assert bad_name.status_code == 400
    assert "无法识别" in bad_name.json()["detail"]

    empty = client.post(
        "/api/replay/upload",
        files={"file": ("orders.jsonl", b"  \n", "application/jsonl")},
        data={"table": "orders"},
    )
    assert empty.status_code == 422


def test_upload_parse_error_is_422_and_run_recorded(client):
    response = client.post(
        "/api/replay/upload",
        files={"file": ("orders.jsonl", b'{"pk": }\n', "application/jsonl")},
        data={"table": "orders"},
    )
    assert response.status_code == 422
    runs = client.get("/api/replays").json()["runs"]
    assert runs[0]["status"] == "failed"


def test_publication_snapshot_404_before_any_publish(client):
    response = client.get("/api/publication/snapshot")
    assert response.status_code == 404
    assert client.get("/api/publication").json()["live"] is None
