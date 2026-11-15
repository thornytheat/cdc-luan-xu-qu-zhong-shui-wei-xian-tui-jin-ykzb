"""模块 2 · 同键合并：按到达顺序执行判定表，产出最终行、墓碑、变更日志与冲突。

约束：
- 同键只保留最大 version；同版本同内容静默去重；同版本不同内容判冲突且**不覆盖**先到内容。
- 迟到事件仍走同一套版本规则（因此「高版本但更旧 ts」的迟到事件也会胜出）。
- 每条处理结果都写成变更日志，带文件行号，供结果页逐条追溯。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .archive import Event
from .settings import KIND_LABELS
from .tombstone import KeyState, evaluate, payload_diff

# 判定结论 → 计数器字段（对外口径固定，页面与测试都依赖这些名字）
COUNTER_KEYS = {
    "apply_insert": "applied_insert",
    "apply_update": "applied_update",
    "apply_delete": "applied_delete",
    "resurrect": "resurrected",
    "dedupe": "deduped_silent",
    "conflict": "conflicts",
    "superseded": "superseded",
    "ignored_after_tombstone": "ignored_after_tombstone",
}


def _fmt(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


@dataclass
class MergeResult:
    table: str
    rows: list[dict] = field(default_factory=list)
    tombstones: list[dict] = field(default_factory=list)
    changelog: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    key_max_ts: dict[str, datetime] = field(default_factory=dict)
    ignored_line_nos: list[int] = field(default_factory=list)

    @property
    def alive_rows(self) -> int:
        return len(self.rows)

    @property
    def tombstone_count(self) -> int:
        return len(self.tombstones)

    @property
    def key_count(self) -> int:
        return len(self.rows) + len(self.tombstones)


def merge_table(table: str, events: list[Event], late_lines: set[int] | None = None) -> MergeResult:
    """单表合并。`late_lines` 为迟到事件行号集合，仅用于标注与计数，不改变版本规则。"""
    late_lines = late_lines or set()
    result = MergeResult(table=table)
    counters = {
        "events": len(events),
        "applied_insert": 0,
        "applied_update": 0,
        "applied_delete": 0,
        "resurrected": 0,
        "deduped_silent": 0,
        "conflicts": 0,
        "superseded": 0,
        "ignored_after_tombstone": 0,
        "late": len(late_lines),
    }
    states: dict[str, KeyState] = {}
    seq = 0

    for event in events:
        seq += 1
        state = states.get(event.pk)
        outcome = evaluate(state, event)
        is_late = event.line_no in late_lines
        counters[COUNTER_KEYS[outcome.kind]] += 1

        if outcome.kind == "conflict":
            result.conflicts.append(
                {
                    "table": table,
                    "pk": event.pk,
                    "version": event.version,
                    "diff": outcome.extra.get("diff", []),
                    "first": {
                        "line_no": state.line_no,
                        "ts": _fmt(state.ts),
                        "op": state.op,
                        "schema_version": state.schema_version,
                        "after": state.after,
                    },
                    "second": {
                        "line_no": event.line_no,
                        "ts": event.ts_iso,
                        "op": event.op,
                        "schema_version": event.schema_version,
                        "after": event.after,
                    },
                    "reason": outcome.reason,
                }
            )
        elif outcome.kind == "ignored_after_tombstone":
            result.ignored_line_nos.append(event.line_no)
        elif outcome.kind != "superseded":
            # 更新状态：insert/update/delete/resurrect/dedupe(仅计数)
            if outcome.kind in ("apply_insert", "apply_update", "apply_delete", "resurrect"):
                if state is None:
                    state = KeyState(
                        pk=event.pk,
                        version=event.version,
                        op=event.op,
                        after=dict(event.after),
                        deleted=event.is_delete,
                        line_no=event.line_no,
                        ts=event.ts,
                        schema_version=event.schema_version,
                        tombstone_version=event.version if event.is_delete else None,
                        tombstone_line_no=event.line_no if event.is_delete else None,
                        applied=1,
                        first_line_no=event.line_no,
                    )
                    states[event.pk] = state
                else:
                    # 墓碑历史字段保留在 state 上，复活后作为审计信息输出
                    if event.is_delete:
                        state.tombstone_version = event.version
                        state.tombstone_line_no = event.line_no
                    state.version = event.version
                    state.op = event.op
                    state.after = dict(event.after)
                    state.deleted = event.is_delete
                    state.line_no = event.line_no
                    state.ts = event.ts
                    state.schema_version = event.schema_version
                    state.applied += 1

        # 已处理事件的最大 ts（墓碑后忽略的不算已处理）
        if outcome.kind != "ignored_after_tombstone":
            current = result.key_max_ts.get(event.pk)
            if current is None or event.ts > current:
                result.key_max_ts[event.pk] = event.ts

        entry = {
            "seq": seq,
            "table": table,
            "line_no": event.line_no,
            "pk": event.pk,
            "version": event.version,
            "op": event.op,
            "ts": event.ts_iso,
            "schema_version": event.schema_version,
            "kind": outcome.kind,
            "kind_label": KIND_LABELS.get(outcome.kind, outcome.kind),
            "late": is_late,
            "reason": outcome.reason,
        }
        if outcome.kind in ("apply_insert", "apply_update", "apply_delete", "resurrect"):
            entry["after"] = event.after
        if outcome.kind == "conflict":
            entry["diff"] = outcome.extra.get("diff", [])
        result.changelog.append(entry)

    for pk in sorted(states):
        state = states[pk]
        row = {
            "pk": pk,
            "version": state.version,
            "op": state.op,
            "ts": _fmt(state.ts),
            "line_no": state.line_no,
            "schema_version": state.schema_version,
            "applied": state.applied,
        }
        if state.deleted:
            row.update(
                {
                    "deleted": True,
                    "tombstone_version": state.tombstone_version,
                    "tombstone_line_no": state.tombstone_line_no,
                }
            )
            result.tombstones.append(row)
        else:
            row["after"] = state.after
            if state.tombstone_version is not None:
                row["resurrected_from"] = {
                    "tombstone_version": state.tombstone_version,
                    "tombstone_line_no": state.tombstone_line_no,
                }
            result.rows.append(row)

    counters["processed"] = counters["events"] - counters["ignored_after_tombstone"]
    counters["keys"] = len(states)
    counters["alive_rows"] = len(result.rows)
    counters["tombstones"] = len(result.tombstones)
    result.counters = counters
    return result


def merge_tables(
    events_by_table: dict[str, list[Event]],
    late_by_table: dict[str, list],
) -> dict[str, MergeResult]:
    """批量合并若干表；late_by_table 为 watermark.detect_late 的结果。"""
    out: dict[str, MergeResult] = {}
    for table, events in events_by_table.items():
        late_lines = {mark.line_no for mark in late_by_table.get(table, [])}
        out[table] = merge_table(table, events, late_lines)
    return out
