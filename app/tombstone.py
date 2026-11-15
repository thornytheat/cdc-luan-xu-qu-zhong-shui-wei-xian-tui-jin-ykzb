"""模块 3 · 墓碑处理：同键版本比较的判定表（纯函数，merge 只负责执行）。

判定表（state 为该键已处理状态，event 为新到事件）：

| 情形                                   | 结论                    |
|----------------------------------------|-------------------------|
| 首见键 + op=c/u                        | 写入（apply_insert）    |
| 首见键 + op=d                          | 写入墓碑（apply_delete）|
| version > 当前 且 当前是墓碑           | 复活（resurrect）       |
| version > 当前 且 当前存活             | 覆盖（apply_update）    |
| version == 当前 且 内容一致            | 静默去重（dedupe）      |
| version == 当前 且 内容不同            | 冲突（conflict）        |
| version <  当前 且 当前是墓碑          | 墓碑后忽略              |
| version <  当前 且 当前存活            | 低版本丢弃（superseded）|

墓碑必须保留：墓碑版本大于后续到达的 c/u 时，键最终仍是删除态，且墓碑记录不丢。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .archive import Event

Decision = Literal[
    "apply_insert",
    "apply_update",
    "apply_delete",
    "resurrect",
    "dedupe",
    "conflict",
    "ignored_after_tombstone",
    "superseded",
]


@dataclass
class KeyState:
    """一个键的已处理状态（只保留获胜事件，冲突不覆盖）。"""

    pk: str
    version: int
    op: str
    after: dict
    deleted: bool
    line_no: int
    ts: datetime
    schema_version: int
    tombstone_version: int | None = None
    tombstone_line_no: int | None = None
    applied: int = 0
    first_line_no: int | None = None


@dataclass
class Outcome:
    kind: Decision
    reason: str
    extra: dict = field(default_factory=dict)


def canonical(after: dict) -> str:
    """内容归一化：键排序 + 紧凑分隔符，用于「同版本是否同内容」判定。"""
    return json.dumps(after or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def same_payload(left: dict, right: dict) -> bool:
    return canonical(left) == canonical(right)


def payload_diff(left: dict, right: dict) -> list[str]:
    """列出两个 payload 不一致的字段（含「只在一边出现」的字段），供冲突依据展示。"""
    left, right = left or {}, right or {}
    keys = sorted(set(left) | set(right))
    return [key for key in keys if key not in left or key not in right
            or canonical({key: left[key]}) != canonical({key: right[key]})]


def evaluate(state: KeyState | None, event: Event) -> Outcome:
    """按判定表给出结论，并写明中文依据（结果页逐条展示）。"""
    if state is None:
        if event.is_delete:
            return Outcome("apply_delete", f"首见键即为删除事件 v{event.version}，写入墓碑")
        return Outcome("apply_insert", f"首见键，写入 v{event.version} 行")

    if event.version > state.version:
        if state.deleted and not event.is_delete:
            return Outcome(
                "resurrect",
                f"墓碑 v{state.tombstone_version}（第 {state.tombstone_line_no} 行）之后出现更高版本 "
                f"v{event.version}，键复活",
            )
        if event.is_delete:
            return Outcome(
                "apply_delete",
                f"删除事件 v{event.version} > 已处理 v{state.version}，写入新墓碑",
            )
        verb = "覆盖更新" if state.op != "d" else "覆盖"
        return Outcome("apply_update", f"v{event.version} > 已处理 v{state.version}，{verb}")

    if event.version == state.version:
        if state.deleted and event.is_delete:
            return Outcome("dedupe", "同版本且都是删除事件，静默去重")
        if state.deleted != event.is_delete:
            return Outcome(
                "conflict",
                f"同版本 v{event.version} 却一边是删除、一边是内容写入，判冲突",
            )
        if same_payload(state.after, event.after):
            return Outcome("dedupe", "同版本且内容一致，判重复上报，静默去重（不计冲突）")
        diff = payload_diff(state.after, event.after)
        return Outcome(
            "conflict",
            f"同版本 v{event.version} 内容不同（字段 {', '.join(diff)}），判冲突，保留先到内容",
            {"diff": diff},
        )

    if state.deleted:
        return Outcome(
            "ignored_after_tombstone",
            f"墓碑版本 v{state.tombstone_version}（第 {state.tombstone_line_no} 行）高于本条 "
            f"v{event.version}，忽略且不改状态",
        )
    return Outcome(
        "superseded",
        f"v{event.version} 低于已处理 v{state.version}，丢弃（同键只保留最大版本）",
    )
