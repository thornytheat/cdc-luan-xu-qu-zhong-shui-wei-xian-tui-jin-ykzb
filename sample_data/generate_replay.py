#!/usr/bin/env python3
"""确定性生成「源库压缩历史后的整包重放」样例包。

运行：python3 sample_data/generate_replay.py

场景：源库压缩历史 → 文件行号整体前移；包里混有真正的新事件，
以及一条「同版本号、内容被源库修正」的事件（用来演示隔离待裁决）。

产物（固定，不进随机数）：
- sample_data/replay/orders.jsonl     59 条：去掉前 4 行（历史被压缩），
  O1001 v2 的 amount 被改（同版本不同内容 → 重放时应隔离），
  末尾追加 3 条新订单事件（O7001 v1/v2、O7002 v1）
- sample_data/replay/customers.jsonl  56 条：去掉前 4 行，其余原样
  （纯行号前移 → 重放时应全部判重复，不新增记录）
- sample_data/replay/inventory.jsonl  57 条：去掉前 4 行，末尾追加 1 条新事件（I7001 v1）
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "events"
DST = ROOT / "replay"
DROP_HEAD = 4  # 源库压缩掉的历史行数（后续行号整体前移）

# 源库压缩期间到达的真实新订单（旧系统会按行号把它们当成已归档旧行丢掉）
NEW_ORDER_EVENTS = [
    {
        "pk": "O7001", "version": 1, "ts": "2026-11-10T02:00:00Z", "op": "c",
        "after": {"order_id": "O7001", "customer_id": "C1002", "amount": 2360, "qty": 2,
                  "status": "paid", "legacy_channel": "app", "updated_at": "2026-11-10T02:00:00Z"},
        "schema_version": 1,
    },
    {
        "pk": "O7001", "version": 2, "ts": "2026-11-10T02:05:00Z", "op": "u",
        "after": {"order_id": "O7001", "customer_id": "C1002", "amount": 2360, "qty": 3,
                  "status": "shipped", "legacy_channel": "app", "updated_at": "2026-11-10T02:05:00Z"},
        "schema_version": 1,
    },
    {
        "pk": "O7002", "version": 1, "ts": "2026-11-10T02:10:00Z", "op": "c",
        "after": {"order_id": "O7002", "customer_id": "C1004", "amount": 980, "qty": 1,
                  "status": "pending", "legacy_channel": "web", "updated_at": "2026-11-10T02:10:00Z"},
        "schema_version": 1,
    },
]

NEW_INVENTORY_EVENTS = [
    {
        "pk": "I7001", "version": 1, "ts": "2026-11-10T02:00:00Z", "op": "c",
        "after": {"sku": "SKU-7001", "warehouse": "WH-SH-01", "on_hand": 240, "reserved": 0,
                  "updated_at": "2026-11-10T02:00:00Z"},
        "schema_version": 1,
    },
]

# 同版本号、内容被源库修正：O1001 v2 的 amount 1474 → 1509（重放时应隔离待裁决）
CORRECTION = {"pk": "O1001", "version": 2, "field": "amount", "value": 1509}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def dump(events: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)

    orders = load(SRC / "orders.jsonl")[DROP_HEAD:]
    corrected = 0
    for event in orders:
        if event["pk"] == CORRECTION["pk"] and event["version"] == CORRECTION["version"]:
            event["after"][CORRECTION["field"]] = CORRECTION["value"]
            corrected += 1
    assert corrected == 1, f"预期修正 1 条 O1001 v2，实际 {corrected}"
    orders += NEW_ORDER_EVENTS

    customers = load(SRC / "customers.jsonl")[DROP_HEAD:]
    inventory = load(SRC / "inventory.jsonl")[DROP_HEAD:] + NEW_INVENTORY_EVENTS

    dump(orders, DST / "orders.jsonl")
    dump(customers, DST / "customers.jsonl")
    dump(inventory, DST / "inventory.jsonl")

    print(f"orders.jsonl     {len(orders)} 条（压缩 {DROP_HEAD} 行 + 修正 1 条 + 新增 {len(NEW_ORDER_EVENTS)} 条）")
    print(f"customers.jsonl  {len(customers)} 条（压缩 {DROP_HEAD} 行，纯行号前移）")
    print(f"inventory.jsonl  {len(inventory)} 条（压缩 {DROP_HEAD} 行 + 新增 {len(NEW_INVENTORY_EVENTS)} 条）")
    assert len(orders) == 59 and len(customers) == 56 and len(inventory) == 57
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
