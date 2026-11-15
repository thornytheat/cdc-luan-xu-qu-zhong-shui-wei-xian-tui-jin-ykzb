"""设置、路径与样例清单（全部集中在这里，其他模块只引用常量）。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "cdcwater · CDC 乱序去重与水位线推进"
APP_SHORT = "cdcwater"
BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = BASE_DIR / "sample_data"
EVENTS_DIR = SAMPLE_DIR / "events"
SCHEMAS_DIR = SAMPLE_DIR / "schemas"
DATA_DIR = Path(os.environ.get("CDCWATER_DATA_DIR", str(BASE_DIR / "data")))
SEED_VERSION = "seed-2026-11-10.1"

TABLES = [
    {
        "name": "orders",
        "label": "订单库 orders",
        "file": "orders.jsonl",
        "desc": "订单主表，含金额/数量/状态更新；埋有 1 条同版本冲突与 1 个删除墓碑",
    },
    {
        "name": "customers",
        "label": "客户库 customers",
        "file": "customers.jsonl",
        "desc": "客户档案，含一次重复上报（同版本同内容，应静默去重）",
    },
    {
        "name": "inventory",
        "label": "库存库 inventory",
        "file": "inventory.jsonl",
        "desc": "库存快照，含 1 个删除墓碑与其后的迟到插入",
    },
]
TABLE_NAMES = [item["name"] for item in TABLES]
TABLE_LABELS = {item["name"]: item["label"] for item in TABLES}

OP_LABELS = {"c": "新增 c", "u": "更新 u", "d": "删除 d"}

# 变更日志种类 → 页面展示文案
KIND_LABELS = {
    "applied_insert": "写入新行",
    "applied_update": "覆盖更新",
    "applied_delete": "写入墓碑",
    "deduped": "静默去重",
    "conflict": "同版本冲突",
    "superseded": "低版本丢弃",
    "ignored_after_tombstone": "墓碑后忽略",
    "resurrected": "墓碑后复活",
}

VERDICT_LABELS = {
    "compatible": "可放行",
    "compatible_with_backfill": "放行+回填",
    "blocked": "阻断",
}

CHANGE_LABELS = {
    "removed_column": "删除列",
    "added_column": "新增列",
    "type_narrowed": "类型收窄",
    "type_widened": "类型放宽",
    "type_incompatible": "类型不兼容",
    "not_null_tightened": "非空收紧",
    "nullability_relaxed": "允许空放宽",
}

# 演示说明用：样例里故意埋的点（与 sample_data/generate_samples.py 输出一致）
EMBEDDED = [
    ("乱序迟到事件", 8, "其中 2 条同时是「墓碑后迟到」，合并被忽略"),
    ("同版本不同内容冲突", 3, "每表 1 条，结果页列出两条事件行号与差异字段"),
    ("同版本同内容重复", 1, "customers 表，静默去重、不计冲突"),
    ("删除墓碑", 2, "orders / inventory 各 1 个，墓碑保留、后续低版本被忽略"),
]

DEMO_STEPS = [
    "在「工作台」勾选要合并的表（默认 3 张全选）与要评估的目标 schema 版本（v2）。",
    "点「执行合并」：后端归档事件 → 同键合并 → 墓碑判定 → 迟到/水位线计算 → schema 门禁。",
    "跳到结果页查看每表合并行数、冲突逐条依据、迟到位置、水位线与门禁结论。",
    "结果页底部预览快照并下载 JSON；「复核与导出」页可回看历史运行与变更日志。",
]


def db_path() -> Path:
    """SQLite 文件路径（测试通过 CDCWATER_DB 覆盖）。"""
    override = os.environ.get("CDCWATER_DB")
    if override:
        return Path(override)
    return DATA_DIR / "cdcwater.sqlite3"


def ensure_data_dir() -> Path:
    path = db_path().parent
    path.mkdir(parents=True, exist_ok=True)
    return path
