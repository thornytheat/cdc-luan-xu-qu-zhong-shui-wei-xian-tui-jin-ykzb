"""核心规则 4：迟到判定、水位线公式、未闭合键与推进轨迹。"""
from __future__ import annotations

from datetime import datetime, timezone

from app import watermark
from app.archive import Event


def ev(line_no: int, pk: str, minute: int, version: int = 1, table: str = "orders") -> Event:
    return Event(
        table=table,
        line_no=line_no,
        pk=pk,
        version=version,
        ts=datetime(2026, 11, 10, 0, minute, tzinfo=timezone.utc),
        op="u",
        after={"amount": minute},
        schema_version=1,
        source=f"{table}.jsonl",
    )


def stamp(minute: int) -> str:
    return datetime(2026, 11, 10, 0, minute, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_detect_late_reports_position_and_boundary_ts():
    events = [ev(1, "A", 10), ev(2, "A", 20), ev(3, "B", 15)]
    marks = watermark.detect_late(events)
    assert len(marks) == 1
    assert marks[0].line_no == 3
    assert marks[0].pk == "B"
    assert marks[0].ts == datetime(2026, 11, 10, 0, 15, tzinfo=timezone.utc)
    assert marks[0].running_max_ts == datetime(2026, 11, 10, 0, 20, tzinfo=timezone.utc)
    assert marks[0].to_dict()["behind_seconds"] == 300


def test_equal_ts_is_not_late():
    events = [ev(1, "A", 10), ev(2, "B", 10), ev(3, "C", 10)]
    assert watermark.detect_late(events) == []


def test_first_event_can_never_be_late():
    events = [ev(1, "A", 50), ev(2, "B", 5)]
    marks = watermark.detect_late(events)
    assert [mark.line_no for mark in marks] == [2]


def test_watermark_is_min_of_key_max_ts():
    key_max = {
        "A": datetime(2026, 11, 10, 0, 14, tzinfo=timezone.utc),
        "B": datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc),
        "C": datetime(2026, 11, 10, 0, 14, tzinfo=timezone.utc),
        "D": datetime(2026, 11, 10, 0, 42, tzinfo=timezone.utc),
    }
    result = watermark.watermark_for_table("orders", key_max)
    assert result.watermark == stamp(14)
    assert result.closed_keys == 2          # A、C 的 max ts == 水位线
    assert result.open_keys == 2            # B、D 仍在水位线之上
    assert [item["pk"] for item in result.laggard_keys] == ["A", "C"]


def test_watermark_empty_table_is_none():
    result = watermark.watermark_for_table("empty", {})
    assert result.watermark is None
    assert result.open_keys == 0
    assert result.closed_keys == 0


def test_combine_takes_global_min_and_sums_open_keys():
    left = watermark.TableWatermark("orders", stamp(14), 11, 3, [], {})
    right = watermark.TableWatermark("customers", stamp(20), 5, 9, [], {})
    combined = watermark.combine([left, right])
    assert combined["watermark"] == stamp(14)
    assert combined["open_keys"] == 16
    assert combined["closed_keys"] == 12
    assert combined["keys"] == 0  # key_max_ts 未传入时只统计键数 = 0，页面用分表数据

    left.key_max_ts = {f"K{i}": stamp(14) for i in range(3)}
    right.key_max_ts = {f"J{i}": stamp(20) for i in range(9)}
    assert watermark.combine([left, right])["keys"] == 12


def test_trajectory_is_monotonic_and_matches_final_watermark():
    events = [ev(i, f"K{i % 4}", i, version=1) for i in range(1, 31)]
    events.append(ev(31, "K0", 5, version=2))  # 迟到事件，ts 更旧
    points = watermark.trajectory(events, step=10)
    assert [point["events"] for point in points] == [10, 20, 30, 31]
    stamps = [point["watermark"] for point in points]
    assert stamps == sorted(stamps)  # 水位线单调不降
    key_max = {}
    for event in events:
        if event.pk not in key_max or event.ts > key_max[event.pk]:
            key_max[event.pk] = event.ts
    final = watermark.watermark_for_table("orders", key_max)
    assert points[-1]["watermark"] == final.watermark
    assert points[-1]["open_keys"] == final.open_keys


def test_sample_data_late_counts(sample_events):
    per_table = {table: watermark.detect_late(events) for table, events in sample_events.items()}
    assert len(per_table["orders"]) == 3
    assert len(per_table["customers"]) == 2
    assert len(per_table["inventory"]) == 3
    assert sum(len(items) for items in per_table.values()) == 8
    # 墓碑后的迟到事件：orders / inventory 各 1 条（都在第 58 行）
    assert per_table["orders"][0].line_no == 58
    assert per_table["inventory"][0].line_no == 58
    # 迟到事件 ts 一定小于当时的最大 ts
    for marks in per_table.values():
        for mark in marks:
            assert mark.ts < mark.running_max_ts
