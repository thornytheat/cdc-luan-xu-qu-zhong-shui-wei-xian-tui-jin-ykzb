"""核心规则 2/3：同键合并、去重、同版本冲突、墓碑与墓碑后忽略。"""
from __future__ import annotations

from datetime import datetime, timezone

from app import merge, watermark
from app.archive import Event


def ev(line_no: int, pk: str, version: int, minute: int, op: str = "u", after: dict | None = None,
       table: str = "orders", sv: int = 1) -> Event:
    return Event(
        table=table,
        line_no=line_no,
        pk=pk,
        version=version,
        ts=datetime(2026, 11, 10, 0, minute, tzinfo=timezone.utc),
        op=op,
        after=after if after is not None else {"pk_field": pk, "amount": 100 + version},
        schema_version=sv,
        source=f"{table}.jsonl",
    )


def run(events, late_lines=None):
    return merge.merge_table("orders", events, late_lines or set())


# --- 同版本：同内容去重 / 不同内容冲突 -------------------------------------


def test_same_version_same_content_is_silent_dedupe():
    events = [
        ev(1, "O1", 1, 1, "c", {"amount": 100}),
        ev(2, "O1", 2, 2, "u", {"amount": 200}),
        ev(3, "O1", 2, 3, "u", {"amount": 200}),  # 同版本 + 逐字相同内容
    ]
    result = run(events)
    assert result.counters["deduped_silent"] == 1
    assert result.counters["conflicts"] == 0
    assert len(result.rows) == 1
    assert result.rows[0]["version"] == 2
    assert result.rows[0]["after"] == {"amount": 200}
    kinds = [entry["kind"] for entry in result.changelog]
    assert "dedupe" in kinds


def test_same_version_different_content_is_conflict_and_keeps_first():
    events = [
        ev(1, "O1", 1, 1, "c", {"amount": 100}),
        ev(2, "O1", 2, 2, "u", {"amount": 200}),
        ev(3, "O1", 2, 3, "u", {"amount": 999}),  # 同版本不同内容
    ]
    result = run(events)
    assert result.counters["conflicts"] == 1
    assert result.counters["deduped_silent"] == 0
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["diff"] == ["amount"]
    assert conflict["first"]["line_no"] == 2          # 先到
    assert conflict["second"]["line_no"] == 3         # 后到
    assert conflict["first"]["after"]["amount"] == 200
    assert result.rows[0]["after"]["amount"] == 200   # 不被覆盖


def test_same_version_same_content_but_different_op_is_dedupe():
    events = [
        ev(1, "O1", 2, 1, "c", {"amount": 100}),
        ev(2, "O1", 2, 2, "u", {"amount": 100}),
    ]
    result = run(events)
    assert result.counters["deduped_silent"] == 1
    assert result.counters["conflicts"] == 0


# --- 墓碑 ----------------------------------------------------------------


def test_tombstone_wins_over_later_lower_version_and_is_kept():
    events = [
        ev(1, "O2", 2, 10, "u", {"amount": 200}),
        ev(2, "O2", 3, 20, "d", {}),
        ev(3, "O2", 1, 5, "c", {"amount": 100}),  # 墓碑后到达的低版本插入
    ]
    result = run(events)
    assert result.counters["applied_delete"] == 1
    assert result.counters["ignored_after_tombstone"] == 1
    assert result.ignored_line_nos == [3]
    assert result.rows == []
    assert len(result.tombstones) == 1
    tombstone = result.tombstones[0]
    assert tombstone["deleted"] is True
    assert tombstone["tombstone_version"] == 3
    assert tombstone["tombstone_line_no"] == 2  # 墓碑行号必须保留
    assert result.key_max_ts["O2"] == datetime(2026, 11, 10, 0, 20, tzinfo=timezone.utc)


def test_tombstone_only_deletes_its_own_key():
    events = [
        ev(1, "O3", 3, 10, "d", {}),
        ev(2, "O4", 1, 11, "c", {"amount": 10}),
        ev(3, "O3", 1, 12, "c", {"amount": 5}),
    ]
    result = run(events)
    assert [row["pk"] for row in result.tombstones] == ["O3"]
    assert [row["pk"] for row in result.rows] == ["O4"]
    assert result.counters["ignored_after_tombstone"] == 1


def test_higher_version_after_tombstone_resurrects():
    events = [
        ev(1, "O5", 3, 10, "d", {}),
        ev(2, "O5", 4, 20, "c", {"amount": 42}),
    ]
    result = run(events)
    assert result.counters["resurrected"] == 1
    assert len(result.rows) == 1
    assert result.rows[0]["version"] == 4
    assert result.rows[0]["resurrected_from"]["tombstone_version"] == 3


# --- 版本大小与迟到 ------------------------------------------------------


def test_lower_version_is_superseded_even_if_it_arrives_late():
    events = [
        ev(1, "O6", 1, 5, "c", {"amount": 1}),
        ev(2, "O6", 3, 30, "u", {"amount": 3}),
        ev(3, "O6", 2, 15, "u", {"amount": 2}),  # 迟到且版本更低
    ]
    late = watermark.detect_late(events)
    assert [mark.line_no for mark in late] == [3]
    result = run(events, {mark.line_no for mark in late})
    assert result.counters["superseded"] == 1
    assert result.counters["late"] == 1
    assert result.rows[0]["version"] == 3


def test_late_higher_version_still_wins():
    events = [
        ev(1, "O7", 1, 16, "c", {"amount": 1}),
        ev(2, "O7", 2, 21, "u", {"amount": 2}),
        ev(3, "O7", 3, 18, "u", {"amount": 3}),  # ts 更旧但版本更高
    ]
    late = watermark.detect_late(events)
    assert [mark.line_no for mark in late] == [3]
    result = run(events, {mark.line_no for mark in late})
    assert result.rows[0]["version"] == 3
    assert result.rows[0]["after"]["amount"] == 3
    entry = [item for item in result.changelog if item["line_no"] == 3][0]
    assert entry["late"] is True
    assert entry["kind"] == "apply_update"


# --- 与样例数据对齐 ------------------------------------------------------


def test_sample_tables_hit_embedded_counts(sample_events):
    per_table = {}
    for table, events in sample_events.items():
        late = watermark.detect_late(events)
        per_table[table] = merge.merge_table(table, events, {m.line_no for m in late})
    assert sum(len(r.conflicts) for r in per_table.values()) == 3
    assert per_table["orders"].counters["ignored_after_tombstone"] == 1
    assert per_table["inventory"].counters["ignored_after_tombstone"] == 1
    assert per_table["customers"].counters["ignored_after_tombstone"] == 0
    assert per_table["customers"].counters["deduped_silent"] == 1
    assert sum(len(r.tombstones) for r in per_table.values()) == 2
    assert per_table["orders"].rows[0]["pk"] == "O1001"
    assert per_table["orders"].conflicts[0]["diff"] == ["amount"]
