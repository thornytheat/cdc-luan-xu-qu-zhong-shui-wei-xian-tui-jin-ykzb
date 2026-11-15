"""模块 7 · 整包重放与原子发布。

修复的核心缺陷：源库压缩历史后行号整体前移，旧归档按「文件名 + 行号」去重，
会把重放文件里的新订单当成已归档旧行丢掉，而快照照样发布。

本模块口径：
- 重复识别按「来源 + 内容指纹」（表/键/版本/op/时间/内容归一化后的 sha256），与行号无关：
  同一文件重放 → 全部判重复，不新增记录；行号重排的真实变更 → 指纹不变仍判重复；
  真正的新事件（新指纹）→ 归档。
- 同来源、同 (表, 键, 版本) 但内容不同 → 隔离待裁决，不进归档、不进候选结果。
- 发布前三重核对 + 隔离清零：来源（文件每行都有去向、归档期间未被改动）、
  归档（非隔离事件逐条在库、各来源增量一致）、候选（候选结果覆盖归档全部事件、
  新归档事件全部进入候选）。全部通过才在一个事务里原子替换现网。
- 任一核对失败或执行异常 → 保留旧版；已归档事件留在库中，
  重跑同一文件即从已归档位置续跑（已归档的判重复，不重复追加）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import archive, runner, settings, snapshot, store, tombstone

SKIP_SAMPLE_LIMIT = 50
KEY_LIST_LIMIT = 20

STATUS_LABELS = {
    "staged": "进行中",
    "published": "已原子替换现网",
    "unchanged": "候选与现网一致 · 未替换",
    "blocked": "核对未通过 · 已保留旧版",
    "failed": "执行失败 · 已保留旧版",
}

QUARANTINE_STATUS_LABELS = {
    "pending": "待裁决",
    "keep_archived": "已裁决 · 保留归档",
    "use_incoming": "已裁决 · 采用到来",
}


class ReplayError(ValueError):
    """可预期的重放错误（路由转成 4xx）。"""


def new_replay_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"P{stamp}-{uuid.uuid4().hex[:4].upper()}"


def new_publication_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"PB{stamp}-{uuid.uuid4().hex[:4].upper()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# --- 阶段 1：归档（按来源 + 内容指纹去重，同版本不同内容隔离） -----------------


@dataclass
class IntakeResult:
    """单文件归档结果（内存态，核对用；写库的是 to_entry 的摘要）。"""

    source: str
    table: str
    path: str
    sha256: str
    lines: int
    pre_count: int
    post_count: int = 0
    archived: list = field(default_factory=list)          # [(event, occur)]
    duplicates: list = field(default_factory=list)        # [(event, occur, archived_line_no)]
    quarantined: list = field(default_factory=list)       # [quarantine record]
    skipped_resolved: list = field(default_factory=list)  # [(event, quarantine record)]

    def accounted(self) -> int:
        return (
            len(self.archived)
            + len(self.duplicates)
            + len(self.quarantined)
            + len(self.skipped_resolved)
        )


def _create_quarantine(conn, replay_id: str, event: archive.Event, prior_rows: list[dict]) -> dict:
    """同来源同版本不同内容 → 写隔离区（待裁决）。prior_rows 为归档中同身份的既有事件。"""
    prior = prior_rows[0]
    archived_after = json.loads(prior["after_json"])
    record = {
        "quarantine_id": f"Q{uuid.uuid4().hex[:8].upper()}",
        "created_at": store.now_iso(),
        "replay_id": replay_id,
        "source": event.source,
        "table_name": event.table,
        "pk": event.pk,
        "version": event.version,
        "incoming_fingerprint": event.fingerprint,
        "incoming": {**event.brief(), "fingerprint": event.fingerprint},
        "archived": {
            "line_no": int(prior["line_no"]),
            "ts": prior["ts"],
            "op": prior["op"],
            "after": archived_after,
            "schema_version": int(prior["schema_version"]),
            "batch": prior["batch"],
            "fingerprint": prior["fingerprint"],
            "occur": int(prior["occur"]),
        },
        "diff": tombstone.payload_diff(archived_after, event.after),
        "op_changed": prior["op"] != event.op,
        "ts_changed": prior["ts"] != event.ts_iso,
        "status": "pending",
        "resolved_at": None,
    }
    store.insert_quarantine(conn, record)
    return record


def intake_file(conn, path: Path, table: str, replay_id: str) -> IntakeResult:
    """归档一个重放文件：重复判重、隔离写区、新事件入库（本函数按文件提交）。

    同一文件重跑是幂等的：已归档的判重复，已隔离的沿用原隔离记录，
    已裁决「保留归档」的判跳过且不再次隔离。
    """
    source = path.name
    events = archive.load_events_file(path, table)  # EventError 上抛，路由转 422
    result = IntakeResult(
        source=source,
        table=table,
        path=str(path),
        sha256=sha256_file(path),
        lines=len(events),
        pre_count=store.count_events_by_source(conn, source),
    )
    raw = store.archive_events(conn, events, batch=replay_id)
    for item in raw["dispositions"]:
        event = item["event"]
        kind = item["disposition"]
        if kind == "archived":
            result.archived.append((event, item["occur"]))
        elif kind == "duplicate":
            result.duplicates.append((event, item["occur"], item["archived_line_no"]))
        else:  # conflict：同来源同版本不同内容
            existing = store.find_quarantine_by_incoming(conn, source, event.fingerprint)
            if existing is not None and existing["status"] == "keep_archived":
                result.skipped_resolved.append((event, existing))
                continue
            if existing is not None and existing["status"] == "use_incoming":
                # 归档内容已被裁决替换为到来内容，正常应判重复；防御性兜底
                result.duplicates.append((event, item["occur"], event.line_no))
                continue
            if existing is None:
                existing = _create_quarantine(conn, replay_id, event, item["prior"])
            result.quarantined.append(existing)
    result.post_count = store.count_events_by_source(conn, source)
    return result


def _skip_samples(result: IntakeResult) -> list[dict]:
    """被跳过事件逐条原因（页面「被跳过原因」对照）。"""
    samples: list[dict] = []
    for event, _occur, archived_line in result.duplicates[:SKIP_SAMPLE_LIMIT]:
        if archived_line == event.line_no:
            reason = f"内容一致，已归档（同为第 {archived_line} 行）"
        else:
            reason = f"行号 {archived_line}→{event.line_no}（重排），内容一致，判重复"
        samples.append(
            {
                "line_no": event.line_no,
                "pk": event.pk,
                "version": event.version,
                "kind": "duplicate",
                "reason": reason,
            }
        )
    for event, record in result.skipped_resolved[:SKIP_SAMPLE_LIMIT]:
        samples.append(
            {
                "line_no": event.line_no,
                "pk": event.pk,
                "version": event.version,
                "kind": "resolved",
                "reason": f"同版本内容不同，已裁决保留归档（{record['resolved_at']}），跳过到来内容",
            }
        )
    return samples


def intake_entry(result: IntakeResult) -> dict:
    """写进运行记录的每文件摘要（计数 + 抽样，完整明细在 events/quarantine 表里）。"""
    return {
        "source": result.source,
        "table": result.table,
        "sha256": result.sha256,
        "lines": result.lines,
        "archived_new": len(result.archived),
        "duplicates": len(result.duplicates),
        "quarantined": len(result.quarantined),
        "skipped_resolved": len(result.skipped_resolved),
        "pre_count": result.pre_count,
        "post_count": result.post_count,
        "new_keys": [
            {"pk": event.pk, "version": event.version, "line_no": event.line_no}
            for event, _occur in result.archived[:KEY_LIST_LIMIT]
        ],
        "skipped": _skip_samples(result),
    }


# --- 阶段 2：候选结果（用归档全量重建） ---------------------------------------


def build_candidate(conn, tables: list[str], schema_version: int) -> dict:
    """候选结果 = 对归档全量跑一次合并 + 水位线 + 门禁（复用工作台同一套口径）。"""
    events_by_table = {table: store.fetch_events(conn, table) for table in tables}
    base_doc, target_doc = runner.load_schemas(schema_version)
    return runner.analyze(tables, events_by_table, base_doc, target_doc)


def shape_candidate_tables(candidate: dict, tables: list[str]) -> dict:
    """把候选结果整形成快照的表结构（与现网快照同形，便于对照与替换）。"""
    shaped = snapshot.shape_tables(candidate)
    return {table: shaped[table] for table in tables}


# --- 阶段 3：三重核对 + 隔离清零 ----------------------------------------------


def run_checks(conn, intakes: list[IntakeResult], tables: list[str], candidate: dict) -> dict:
    """来源 / 归档 / 候选三重核对 + 隔离清零；全部通过才允许原子替换现网。"""
    # 1) 来源核对：文件每一行都有明确去向；归档期间文件未被改动（sha256 复核）
    per_file = []
    source_ok = True
    for it in intakes:
        accounted = it.accounted()
        lines_ok = accounted == it.lines
        hash_ok = sha256_file(Path(it.path)) == it.sha256
        source_ok = source_ok and lines_ok and hash_ok
        per_file.append(
            {
                "source": it.source,
                "lines": it.lines,
                "accounted": accounted,
                "lines_ok": lines_ok,
                "hash_ok": hash_ok,
            }
        )

    # 2) 归档核对：应归档/应判重复的事件逐条在库；每个来源的归档增量与入库计数一致
    expected_keys: list[tuple] = []
    for it in intakes:
        expected_keys.extend((it.source, e.fingerprint, o) for e, o in it.archived)
        expected_keys.extend((it.source, e.fingerprint, o) for e, o, _line in it.duplicates)
    missing = store.missing_event_keys(conn, expected_keys)
    count_mismatch = [
        {"source": it.source, "expected": it.pre_count + len(it.archived), "actual": it.post_count}
        for it in intakes
        if it.post_count != it.pre_count + len(it.archived)
    ]
    archive_ok = not missing and not count_mismatch

    # 3) 候选核对：候选结果覆盖归档全部事件；本次新归档事件全部进入候选
    #    （旧缺陷「新订单被丢掉、快照照样发布」会在这里被拦下）
    per_table = []
    candidate_ok = True
    for table in tables:
        logged = len([c for c in candidate["changelog"] if c["table"] == table])
        archived_n = store.count_events(conn, table)
        candidate_ok = candidate_ok and logged == archived_n
        per_table.append({"table": table, "changelog": logged, "archived": archived_n})
    logged_fps = {c["fingerprint"] for c in candidate["changelog"]}
    new_fps = {e.fingerprint for it in intakes for e, _o in it.archived}
    missing_new = sorted(new_fps - logged_fps)
    candidate_ok = candidate_ok and not missing_new

    # 4) 隔离清零：受影响表还有「同版本不同内容」待裁决时，不允许替换现网
    pending = store.list_quarantine(conn, status="pending", tables=tables)
    quarantine_ok = not pending

    checks = {
        "source": {
            "ok": source_ok,
            "title": "来源核对",
            "detail": "每个文件每一行都有去向（新归档 / 重复 / 隔离 / 已裁决跳过），且归档期间文件未被改动",
            "files": per_file,
        },
        "archive": {
            "ok": archive_ok,
            "title": "归档核对",
            "detail": "非隔离事件逐条在库，各来源归档增量与入库计数一致",
            "checked": len(expected_keys),
            "missing": [f"{s} 指纹 {fp[:12]}…#{o}" for s, fp, o in missing[:10]],
            "count_mismatch": count_mismatch,
        },
        "candidate": {
            "ok": candidate_ok,
            "title": "候选核对",
            "detail": "候选结果覆盖归档全部事件，本次新归档事件全部进入候选",
            "tables": per_table,
            "missing_new": [fp[:12] + "…" for fp in missing_new[:10]],
        },
        "quarantine": {
            "ok": quarantine_ok,
            "title": "隔离清零",
            "detail": "受影响表仍有待裁决的同版本不同内容事件时，不替换现网",
            "pending": len(pending),
            "pending_keys": [f"{q['table_name']} {q['pk']} v{q['version']}" for q in pending[:10]],
        },
    }
    checks["ok"] = all(item["ok"] for item in checks.values())
    return checks


# --- 阶段 4：替换前后对照 + 原子替换 ------------------------------------------


def compute_diff(live: dict | None, candidate_tables: dict, tables: list[str]) -> dict:
    """替换前后对照：现网（before） vs 候选（after），逐表给行数/墓碑/水位线与键级差异。"""
    live_tables = (live or {}).get("snapshot", {}).get("tables", {}) if live else {}
    out: dict[str, dict] = {}
    for table in tables:
        before = live_tables.get(table)
        after = candidate_tables[table]
        before_rows = {row["pk"]: row for row in before["rows"]} if before else {}
        after_rows = {row["pk"]: row for row in after["rows"]}
        before_tombs = {row["pk"] for row in before["tombstones"]} if before else set()
        after_tombs = {row["pk"] for row in after["tombstones"]}
        added = sorted(set(after_rows) - set(before_rows))
        removed = sorted(set(before_rows) - set(after_rows))
        changed = sorted(
            pk
            for pk in set(before_rows) & set(after_rows)
            if before_rows[pk]["version"] != after_rows[pk]["version"]
            or before_rows[pk].get("after") != after_rows[pk].get("after")
        )
        tombs_added = sorted(after_tombs - before_tombs)
        out[table] = {
            "table": table,
            "label": settings.TABLE_LABELS.get(table, table),
            "before": None
            if before is None
            else {
                "rows": len(before_rows),
                "tombstones": len(before_tombs),
                "watermark": before["watermark"]["watermark"],
            },
            "after": {
                "rows": len(after_rows),
                "tombstones": len(after_tombs),
                "watermark": after["watermark"]["watermark"],
            },
            "rows_added": added[:KEY_LIST_LIMIT],
            "rows_added_count": len(added),
            "rows_removed": removed[:KEY_LIST_LIMIT],
            "rows_removed_count": len(removed),
            "rows_changed": [
                {
                    "pk": pk,
                    "version_before": before_rows[pk]["version"],
                    "version_after": after_rows[pk]["version"],
                }
                for pk in changed[:KEY_LIST_LIMIT]
            ],
            "rows_changed_count": len(changed),
            "tombstones_added": tombs_added[:KEY_LIST_LIMIT],
            "tombstones_added_count": len(tombs_added),
        }
    return out


def _merge_watermark(tables: dict) -> dict:
    stamps = [p["watermark"]["watermark"] for p in tables.values() if p["watermark"]["watermark"]]
    return {
        "watermark": min(stamps) if stamps else None,
        "open_keys": sum(p["watermark"]["open_keys"] for p in tables.values()),
        "closed_keys": sum(p["watermark"]["closed_keys"] for p in tables.values()),
        "per_table": {name: p["watermark"] for name, p in tables.items()},
    }


def build_publication(
    replay_id: str, candidate: dict, candidate_tables: dict, live: dict | None, tables: list[str]
) -> dict:
    """拼新现网：本次覆盖的表用候选结果，未覆盖的表沿用旧现网（仍是整体一次原子替换）。"""
    publication_id = new_publication_id()
    now = store.now_iso()
    merged = dict((live or {}).get("snapshot", {}).get("tables", {})) if live else {}
    for table in tables:
        merged[table] = candidate_tables[table]
    watermark = _merge_watermark(merged)
    snap = {
        "export": {
            "app": "cdcwater",
            "app_name": "CDC 乱序去重与水位线推进",
            "publication_id": publication_id,
            "replay_id": replay_id,
            "generated_at": now,
            "tables": sorted(merged),
            "published_tables": tables,
            "schema_pair": [candidate["gate"]["base_version"], candidate["gate"]["target_version"]],
        },
        "watermark": watermark,
        "counters": candidate["counters"],
        "tables": merged,
        "gate": candidate["gate"],
        "changelog": candidate["changelog"],
        "late_events": candidate["late_events"],
        "conflicts": candidate["conflicts"],
    }
    summary = {
        "alive_rows": sum(p["row_count"] for p in merged.values()),
        "tombstones": sum(p["tombstone_count"] for p in merged.values()),
        "watermark": watermark["watermark"],
        "open_keys": watermark["open_keys"],
        "tables": len(merged),
        "published_tables": tables,
    }
    return {
        "publication_id": publication_id,
        "created_at": now,
        "replay_id": replay_id,
        "tables": sorted(merged),
        "summary": summary,
        "snapshot": snap,
    }


def candidate_same_as_live(live: dict | None, candidate_tables: dict, tables: list[str]) -> bool:
    """候选与现网逐表一致 → 不做无意义替换（重试同一文件的幂等保证）。"""
    if live is None:
        return False
    live_tables = live["snapshot"].get("tables", {})
    return all(
        table in live_tables and _canonical(live_tables[table]) == _canonical(candidate_tables[table])
        for table in tables
    )


# --- 编排 --------------------------------------------------------------------


def execute_replay(
    conn,
    files: list[tuple],
    package: str,
    schema_version: int = 2,
    replay_id: str | None = None,
) -> dict:
    """整包重放：归档 → 候选 → 三重核对 → 原子替换（或保留旧版）。

    files 为 [(path, table)]；每个文件归档后立即落库一次运行记录，
    中途失败时重跑同一文件即从已归档位置续跑。
    """
    if schema_version not in (1, 2):
        raise ReplayError(f"schema 版本只支持 1 或 2，收到 {schema_version}")
    replay_id = replay_id or new_replay_id()
    tables: list[str] = []
    for _path, table in files:
        if table not in settings.TABLE_NAMES:
            raise ReplayError(f"未知的表：{table}；可选 {', '.join(settings.TABLE_NAMES)}")
        if table not in tables:
            tables.append(table)

    run = {
        "replay_id": replay_id,
        "created_at": store.now_iso(),
        "status": "staged",
        "package": package,
        "tables": tables,
        "schema_version": int(schema_version),
        "files": [],
        "checks": {},
        "quarantine": [],
        "diff": {},
        "publication_id": None,
        "error": None,
    }
    store.save_replay_run(conn, run)
    intakes: list[IntakeResult] = []
    try:
        for path, table in files:
            result = intake_file(conn, Path(path), table, replay_id)
            intakes.append(result)
            run["files"].append(intake_entry(result))
            store.save_replay_run(conn, run)  # 逐文件落库：崩溃后重跑同一文件即续跑

        candidate = build_candidate(conn, tables, schema_version)
        candidate_tables = shape_candidate_tables(candidate, tables)
        run["checks"] = run_checks(conn, intakes, tables, candidate)
        run["quarantine"] = store.list_quarantine(conn, replay_id=replay_id)
        live = store.live_publication(conn)
        run["diff"] = compute_diff(live, candidate_tables, tables)

        if run["checks"]["ok"]:
            if candidate_same_as_live(live, candidate_tables, tables):
                run["status"] = "unchanged"
            else:
                publication = build_publication(
                    replay_id, candidate, candidate_tables, live, tables
                )
                store.publish(conn, publication)  # 原子替换；失败回滚、旧版保留
                run["publication_id"] = publication["publication_id"]
                run["status"] = "published"
        else:
            run["status"] = "blocked"  # 核对未通过：保留旧版，可处理后重跑续跑
        store.save_replay_run(conn, run)
        return store.get_replay_run(conn, replay_id)
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = str(exc)
        store.save_replay_run(conn, run)
        raise


def resolve_quarantine(conn, quarantine_id: str, decision: str) -> dict:
    """裁决隔离事件：keep_archived 保留归档 / use_incoming 用到来内容替换归档同版本内容。"""
    record = store.get_quarantine(conn, quarantine_id)
    if record is None:
        raise ReplayError(f"隔离记录不存在：{quarantine_id}")
    if record["status"] != "pending":
        raise ReplayError(f"隔离记录已裁决（{record['status']}），不能重复裁决")
    if decision not in ("keep_archived", "use_incoming"):
        raise ReplayError("decision 必须是 keep_archived 或 use_incoming")
    if decision == "use_incoming":
        store.replace_event_content(
            conn,
            record["source"],
            record["archived"]["fingerprint"],
            record["archived"]["occur"],
            record["incoming"],
        )
    store.mark_quarantine_resolved(conn, quarantine_id, decision)
    return store.get_quarantine(conn, quarantine_id)


def sample_package_files() -> list[tuple]:
    """内置样例重放包（源库压缩历史后的整包重放）。"""
    files = []
    for item in settings.TABLES:
        path = settings.REPLAY_DIR / item["file"]
        if not path.exists():
            raise ReplayError(f"样例重放包缺少文件：{path}")
        files.append((path, item["name"]))
    return files
