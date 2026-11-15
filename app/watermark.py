"""模块 4 · 乱序与水位线：迟到判定、水位线公式、未闭合键、推进轨迹。

口径：
- 迟到（late）：按「到达顺序」（文件行序）处理事件，若 `event.ts < 当前已处理最大 ts`
  则判迟到。相等不算迟到。迟到事件**仍然参与**同键合并（版本规则不变）。
- 键级最大 ts：该键所有「已处理」事件的最大 ts（墓碑后忽略的事件不算已处理）。
- 水位线 watermark = min(每个键的键级最大 ts)：水位线及以下的键可认为已闭合，
  高于水位线的键为未闭合键（仍在等待更晚的事件）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .archive import Event


def _fmt(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


@dataclass
class LateMark:
    """一条迟到事件的定位信息。"""

    table: str
    line_no: int
    pk: str
    version: int
    ts: datetime
    running_max_ts: datetime

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "line_no": self.line_no,
            "pk": self.pk,
            "version": self.version,
            "ts": _fmt(self.ts),
            "running_max_ts": _fmt(self.running_max_ts),
            "behind_seconds": int((self.running_max_ts - self.ts).total_seconds()),
        }


def detect_late(events: list[Event]) -> list[LateMark]:
    """按到达顺序检测迟到事件；必须传入**单表**事件（已按行号排序）。"""
    marks: list[LateMark] = []
    max_ts: datetime | None = None
    for event in events:
        if max_ts is not None and event.ts < max_ts:
            marks.append(
                LateMark(
                    table=event.table,
                    line_no=event.line_no,
                    pk=event.pk,
                    version=event.version,
                    ts=event.ts,
                    running_max_ts=max_ts,
                )
            )
        if max_ts is None or event.ts > max_ts:
            max_ts = event.ts
    return marks


@dataclass
class TableWatermark:
    """单表水位线结果。"""

    table: str
    watermark: str | None
    open_keys: int
    closed_keys: int
    laggard_keys: list[dict] = field(default_factory=list)
    key_max_ts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "watermark": self.watermark,
            "open_keys": self.open_keys,
            "closed_keys": self.closed_keys,
            "laggard_keys": self.laggard_keys,
        }


def watermark_for_table(table: str, key_max_ts: dict[str, datetime]) -> TableWatermark:
    """watermark = min(每个键已处理事件的最大 ts)。"""
    if not key_max_ts:
        return TableWatermark(table, None, 0, 0, [], {})
    low = min(key_max_ts.values())
    laggards = [
        {"pk": pk, "max_ts": _fmt(ts)}
        for pk, ts in sorted(key_max_ts.items(), key=lambda kv: (kv[1], kv[0]))
        if ts == low
    ][:8]
    closed = sum(1 for ts in key_max_ts.values() if ts <= low)
    return TableWatermark(
        table=table,
        watermark=_fmt(low),
        open_keys=len(key_max_ts) - closed,
        closed_keys=closed,
        laggard_keys=laggards,
        key_max_ts={pk: _fmt(ts) for pk, ts in key_max_ts.items()},
    )


def combine(tables: list[TableWatermark]) -> dict:
    """全局水位线 = min(各表水位线)；未闭合键数为各表之和。"""
    stamps = [t.watermark for t in tables if t.watermark]
    return {
        "watermark": min(stamps) if stamps else None,
        "open_keys": sum(t.open_keys for t in tables),
        "closed_keys": sum(t.closed_keys for t in tables),
        "keys": sum(len(t.key_max_ts) for t in tables),
    }


def trajectory(events: list[Event], step: int = 10) -> list[dict]:
    """水位线推进轨迹：每处理 step 条事件，记录一次前缀水位线与未闭合键数。

    前缀口径：只用已到达事件里每个键的最大 ts 计算 min，因此水位线只会单调不降；
    墓碑后忽略的事件仅影响 ts 前缀，不影响「已处理」结论。
    """
    if not events:
        return []
    points: list[dict] = []
    seen: dict[str, datetime] = {}
    for index, event in enumerate(events, start=1):
        current = seen.get(event.pk)
        if current is None or event.ts > current:
            seen[event.pk] = event.ts
        if index % step == 0 or index == len(events):
            low = min(seen.values())
            points.append(
                {
                    "events": index,
                    "watermark": _fmt(low),
                    "open_keys": sum(1 for ts in seen.values() if ts > low),
                    "closed_keys": sum(1 for ts in seen.values() if ts <= low),
                }
            )
    return points
