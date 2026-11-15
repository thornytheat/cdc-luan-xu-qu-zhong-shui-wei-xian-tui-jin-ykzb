"""模块 1 · 事件归档：JSONL 解析、字段校验、幂等入库与样例概览。

归档口径（对外承诺）：
- 每个事件保留 `source`（文件名）与 `line_no`（文件行号，从 1 开始），
  行号是后续所有结论的「依据坐标」，必须原样保留。
- 归档只做校验与入库，不做任何业务判定；重复启动不重复导入（唯一键 source+line_no）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OP_CREATE = "c"
OP_UPDATE = "u"
OP_DELETE = "d"
VALID_OPS = (OP_CREATE, OP_UPDATE, OP_DELETE)
VALID_SCHEMA_VERSIONS = (1, 2)


class EventError(ValueError):
    """事件格式非法（行列位置明确，便于页面与 4xx 文案直接引用）。"""


def parse_ts(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise EventError("ts 必须是非空 ISO 时间字符串")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:  # pragma: no cover - 由上层拿到明确文案
        raise EventError(f"ts 无法解析为 ISO 时间：{raw}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Event:
    table: str
    line_no: int
    pk: str
    version: int
    ts: datetime
    op: str
    after: dict
    schema_version: int
    source: str = ""

    @property
    def ts_iso(self) -> str:
        return self.ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def is_delete(self) -> bool:
        return self.op == OP_DELETE

    def to_row(self) -> dict:
        return {
            "table": self.table,
            "line_no": self.line_no,
            "pk": self.pk,
            "version": self.version,
            "ts": self.ts_iso,
            "op": self.op,
            "after_json": json.dumps(self.after, ensure_ascii=False),
            "schema_version": self.schema_version,
            "source": self.source,
        }

    def brief(self) -> dict:
        return {
            "table": self.table,
            "line_no": self.line_no,
            "pk": self.pk,
            "version": self.version,
            "ts": self.ts_iso,
            "op": self.op,
            "schema_version": self.schema_version,
            "after": self.after,
        }


def parse_event(obj: object, table: str, line_no: int, source: str = "") -> Event:
    where = f"{source or table} 第 {line_no} 行"
    if not isinstance(obj, dict):
        raise EventError(f"{where}：事件必须是 JSON 对象")
    for field in ("pk", "version", "ts", "op"):
        if field not in obj:
            raise EventError(f"{where}：缺少字段 {field}")

    pk = obj["pk"]
    if not isinstance(pk, str) or not pk.strip():
        raise EventError(f"{where}：pk 必须是非空字符串")

    version = obj["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise EventError(f"{where}：version 必须是 >=1 的整数")

    op = obj["op"]
    if op not in VALID_OPS:
        raise EventError(f"{where}：op 必须是 c/u/d 之一，收到 {op!r}")

    after = obj.get("after", {})
    if after is None:
        after = {}
    if not isinstance(after, dict):
        raise EventError(f"{where}：after 必须是对象")

    schema_version = obj.get("schema_version", 1)
    if schema_version not in VALID_SCHEMA_VERSIONS:
        raise EventError(f"{where}：schema_version 必须是 1 或 2，收到 {schema_version!r}")

    return Event(
        table=table,
        line_no=line_no,
        pk=pk.strip(),
        version=version,
        ts=parse_ts(obj["ts"]),
        op=op,
        after=after,
        schema_version=schema_version,
        source=source,
    )


def load_events_file(path: Path, table: str) -> list[Event]:
    """按文件行序读取一个 JSONL 文件；空行跳过，非法行抛 EventError（带行号）。"""
    source = path.name
    events: list[Event] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise EventError(f"{source} 第 {line_no} 行：不是合法 JSON（{exc.msg}）") from exc
            events.append(parse_event(obj, table, line_no, source))
    return events


def load_sample_events(events_dir: Path, tables: list[dict]) -> dict[str, list[Event]]:
    """读取全部样例表；任一表文件缺失即报错（不静默降级）。"""
    out: dict[str, list[Event]] = {}
    for item in tables:
        path = events_dir / item["file"]
        if not path.exists():
            raise EventError(f"缺少样例文件：{path}")
        out[item["name"]] = load_events_file(path, item["name"])
    return out


def summarize(events: list[Event]) -> dict:
    """概览：条数 / op 分布 / schema 版本分布 / 键数 / 版本区间 / 时间区间。"""
    if not events:
        return {
            "events": 0, "ops": {}, "schema_versions": {}, "keys": 0,
            "max_version": 0, "first_ts": None, "last_ts": None,
        }
    ops: dict[str, int] = {}
    versions: dict[str, int] = {}
    for event in events:
        ops[event.op] = ops.get(event.op, 0) + 1
        key = str(event.schema_version)
        versions[key] = versions.get(key, 0) + 1
    return {
        "events": len(events),
        "ops": ops,
        "schema_versions": versions,
        "keys": len({event.pk for event in events}),
        "max_version": max(event.version for event in events),
        "first_ts": min(event.ts for event in events).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_ts": max(event.ts for event in events).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
