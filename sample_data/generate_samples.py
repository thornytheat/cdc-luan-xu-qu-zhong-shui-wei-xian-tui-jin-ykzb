#!/usr/bin/env python3
"""确定性生成 gb-624 的 CDC 样例事件与目标表 schema。

运行：python3 sample_data/generate_samples.py

产物（固定，不进随机数）：
- sample_data/events/orders.jsonl     60 条
- sample_data/events/customers.jsonl  60 条
- sample_data/events/inventory.jsonl  60 条
- sample_data/schemas/v1.json、v2.json

埋点（全表合计，脚本末尾自检）：
- 迟到（ts < 当前已处理最大 ts）8 条：6 条纯乱序 + 2 条墓碑后迟到
- 同版本不同内容冲突 3 条（每表 1 个键）
- 墓碑后低版本 c/u 被忽略 2 条（orders、inventory 各 1 个墓碑键）
- 同版本同内容重复 1 条（customers，应静默去重、不计冲突）

只有事件「到达顺序（文件行序）」与 ts 不同步时才会出现迟到：
主体事件按 ts 升序排布，迟到事件被移动到文件末尾。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVENTS_DIR = ROOT / "events"
SCHEMAS_DIR = ROOT / "schemas"

BASE = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
DROP = {"_minute", "_tail"}

CUSTOMER_IDS = ["C1001", "C1002", "C1003", "C1004", "C1005", "C1006"]
STATUSES = ["paid", "shipped", "pending"]
TIERS = ["standard", "silver", "gold", "platinum"]
WAREHOUSES = ["WH-SH-01", "WH-BJ-02", "WH-GZ-03"]
NAMES = [
    "张伟", "李娜", "王强", "刘洋", "陈静", "赵磊", "孙敏", "周涛",
    "吴倩", "郑昊", "冯雪", "蒋鹏",
]


def ts(minute: int) -> str:
    return (BASE + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make(pk, version, minute, op, after, sv=1, tail=False):
    return {
        "pk": pk,
        "version": version,
        "ts": ts(minute),
        "op": op,
        "after": after,
        "schema_version": sv,
        "_minute": minute,
        "_tail": tail,
    }


# --- 各表行内容 -------------------------------------------------------------
# 契约：同一个键（pk）的各版本必须保持身份字段（pk/name/email/sku/warehouse）一致，
# 只让可变字段（amount/qty/status/on_hand/reserved/tier）随版本变化。


def order_row(pk: str, ident: int, step: int, minute: int, sv: int = 1) -> dict:
    row = {
        "order_id": pk,
        "customer_id": CUSTOMER_IDS[ident % len(CUSTOMER_IDS)],
        "amount": 1200 + ident * 53 + step * 137,
        "qty": 1 + step % 4,
        "status": STATUSES[step % len(STATUSES)],
        "legacy_channel": "web" if ident % 2 == 0 else "app",
        "updated_at": ts(minute),
    }
    if sv == 2:
        row["fulfillment_note"] = None if step % 3 == 0 else "次日达"
    return row


def customer_row(pk: str, ident: int, step: int, minute: int, sv: int = 1) -> dict:
    row = {
        "customer_id": pk,
        "name": NAMES[ident % len(NAMES)],
        "email": f"{pk.lower()}@example.com" if ident % 5 else None,
        "tier": TIERS[step % len(TIERS)],
        "updated_at": ts(minute),
    }
    if sv == 2:
        row["loyalty_tier"] = "standard"
        row["phone"] = None if ident % 2 else "1390000" + f"{ident:04d}"
    return row


def inventory_row(pk: str, ident: int, step: int, minute: int, sv: int = 1) -> dict:
    row = {
        "sku": "SKU-" + pk[1:],
        "warehouse": WAREHOUSES[ident % len(WAREHOUSES)],
        "on_hand": 120 + ident * 7 + step,
        "reserved": step % 5,
        "updated_at": ts(minute),
    }
    if sv == 2:
        row["bin_code"] = f"A-{ident % 7 + 1:02d}-{step % 12 + 1:02d}"
    return row


TABLES = {
    "orders": {"prefix": "O", "builder": order_row, "events_per_key": 5},
    "customers": {"prefix": "C", "builder": customer_row, "events_per_key": 5},
    "inventory": {"prefix": "I", "builder": inventory_row, "events_per_key": 5},
}

# 主体键（group 决定 ts 分组，同组键的结束 ts 相同 → 水位线以下有多个已闭合键）
NORMAL_KEYS = 9
SPECIAL_EVENTS = 12  # 每个表的埋点事件条数（见 build_events）
TOTAL_PER_TABLE = 60


def normal_minute(index: int, version: int) -> int:
    return 4 + (index % 4) * 5 + version * 2


def build_events(table: str) -> list[dict]:
    cfg = TABLES[table]
    builder = cfg["builder"]
    prefix = cfg["prefix"]
    events: list[dict] = []

    def add(pk, version, minute, op, ident, step, sv=1, tail=False):
        events.append(make(pk, version, minute, op,
                           builder(pk, ident, step, minute, sv), sv, tail))

    # 1) 正常更新链：同键 version 递增、ts 递增
    for i in range(NORMAL_KEYS):
        pk = f"{prefix}{1001 + i}"
        for v in range(1, cfg["events_per_key"] + 1):
            minute = normal_minute(i, v)
            add(pk, v, minute, "c" if v == 1 else "u", i, v, 2 if minute >= 36 else 1)

    # 2) 尾部键（3 个版本，结束 ts 晚于任何分组，避免拉低水位线）
    pk_tail = f"{prefix}1999"
    for v in range(1, 4):
        add(pk_tail, v, 19 + v * 2, "c" if v == 1 else "u", 40, v)

    # 3) 埋点：同版本不同内容 → conflict（保留先到内容，记录两条事件）
    conflict_pk = f"{prefix}9001"
    add(conflict_pk, 1, 6, "c", 60, 1)
    second = builder(conflict_pk, 60, 2, 18, 1)
    second["amount" if table != "inventory" else "on_hand"] = 1800
    events.append(make(conflict_pk, 2, 18, "u", second, 1))
    third = dict(second)
    third["amount" if table != "inventory" else "on_hand"] = 2600
    events.append(make(conflict_pk, 2, 22, "u", third, 1))

    if table == "customers":
        # 4a) 同版本同内容重复上报 → 静默去重（after 完全一致，仅到达 ts 不同）
        dup_pk = f"{prefix}9002"
        add(dup_pk, 1, 7, "c", 61, 1)
        payload = builder(dup_pk, 61, 2, 15, 1)
        events.append(make(dup_pk, 2, 15, "u", payload, 1))
        events.append(make(dup_pk, 2, 17, "u", dict(payload), 1))
    else:
        # 4b) 墓碑：u v2 → d v3；随后迟到的 v1 c 被忽略，墓碑保留
        tomb_pk = f"{prefix}9002"
        add(tomb_pk, 2, 24, "u", 61, 2)
        add(tomb_pk, 3, 42, "d", 61, 3, 2)
        add(tomb_pk, 1, 5, "c", 61, 1, 1, tail=True)

    # 5) 纯乱序：低版本迟到 → 计入 late，合并时被更高版本压制
    lose_pk = f"{prefix}9003"
    add(lose_pk, 1, 9, "c", 62, 1)
    add(lose_pk, 3, 30, "u", 62, 3)
    add(lose_pk, 2, 15, "u", 62, 2, 1, tail=True)

    # 6) 纯乱序：高版本但 ts 更旧迟到 → 仍按 version 合并，版本高者胜出
    win_pk = f"{prefix}9004"
    add(win_pk, 1, 16, "c", 63, 1)
    add(win_pk, 2, 21, "u", 63, 2)
    add(win_pk, 3, 18, "u", 63, 3, 2, tail=True)

    assert len(events) == TOTAL_PER_TABLE, f"{table} 事件数 {len(events)} != {TOTAL_PER_TABLE}"

    # 到达顺序：非迟到事件按 (ts, 键, 版本) 升序，迟到事件整体移动到末尾
    head = [e for e in events if not e["_tail"]]
    tail = [e for e in events if e["_tail"]]
    head.sort(key=lambda e: (e["_minute"], e["pk"], e["version"]))
    return head + tail


# --- schema -----------------------------------------------------------------

COL = lambda name, type_, nullable=True, default=None: {  # noqa: E731
    "name": name, "type": type_, "nullable": nullable, "default": default
}

SCHEMA_V1 = {
    "version": 1,
    "label": "目标表结构 v1（线上现状，本次变更的基线）",
    "tables": {
        "orders": {
            "label": "订单库 orders",
            "columns": [
                COL("order_id", "string", False),
                COL("customer_id", "string", False),
                COL("amount", "long", False),
                COL("qty", "long", False),
                COL("status", "string", False),
                COL("legacy_channel", "string", True),
                COL("updated_at", "timestamp", False),
            ],
        },
        "customers": {
            "label": "客户库 customers",
            "columns": [
                COL("customer_id", "string", False),
                COL("name", "string", False),
                COL("email", "string", True),
                COL("tier", "string", False),
                COL("updated_at", "timestamp", False),
            ],
        },
        "inventory": {
            "label": "库存库 inventory",
            "columns": [
                COL("sku", "string", False),
                COL("warehouse", "string", False),
                COL("on_hand", "long", False),
                COL("reserved", "long", False),
                COL("updated_at", "timestamp", False),
            ],
        },
    },
}

SCHEMA_V2 = {
    "version": 2,
    "label": "目标表结构 v2（上游变更申请，等待门禁放行）",
    "tables": {
        "orders": {
            "label": "订单库 orders",
            "columns": [
                COL("order_id", "string", False),
                COL("customer_id", "string", False),
                COL("amount", "long", False),
                COL("qty", "int", False),
                COL("status", "string", False),
                COL("fulfillment_note", "string", True),
                COL("updated_at", "timestamp", False),
            ],
        },
        "customers": {
            "label": "客户库 customers",
            "columns": [
                COL("customer_id", "string", False),
                COL("name", "string", False),
                COL("email", "string", False),
                COL("tier", "string", False),
                COL("loyalty_tier", "string", False, "standard"),
                COL("phone", "string", True),
                COL("updated_at", "timestamp", False),
            ],
        },
        "inventory": {
            "label": "库存库 inventory",
            "columns": [
                COL("sku", "string", False),
                COL("warehouse", "string", False),
                COL("on_hand", "int", False),
                COL("reserved", "long", False),
                COL("bin_code", "string", True),
                COL("reorder_point", "long", False),
                COL("updated_at", "timestamp", False),
            ],
        },
    },
}


def late_lines(events: list[dict]) -> list[str]:
    """按文件行序计算迟到事件（与 app.watermark.detect_late 同口径）。"""
    out, max_minute = [], None
    for i, e in enumerate(events, start=1):
        minute = e["_minute"]
        if max_minute is not None and minute < max_minute:
            out.append(f"第{i}行 {e['pk']} v{e['version']} ts={e['ts']}")
        max_minute = minute if max_minute is None else max(max_minute, minute)
    return out


def dump_jsonl(events: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps({k: v for k, v in e.items() if k not in DROP},
                                ensure_ascii=False, sort_keys=False) + "\n")


def main() -> int:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    total_late = 0
    for table in TABLES:
        events = build_events(table)
        dump_jsonl(events, EVENTS_DIR / f"{table}.jsonl")
        late = late_lines(events)
        total_late += len(late)
        print(f"{table:10s} 事件 {len(events):3d}  迟到 {len(late)}")
        for item in late:
            print(f"           - {item}")
    (SCHEMAS_DIR / "v1.json").write_text(
        json.dumps(SCHEMA_V1, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (SCHEMAS_DIR / "v2.json").write_text(
        json.dumps(SCHEMA_V2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"合计迟到 {total_late} 条（预期 8）")
    assert total_late == 8, f"迟到数 {total_late} != 8"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
