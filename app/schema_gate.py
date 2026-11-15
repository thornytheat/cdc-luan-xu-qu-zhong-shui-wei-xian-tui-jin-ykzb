"""模块 5 · Schema 门禁：对比目标表 v1/v2，逐条给出可放行 / 放行+回填 / 阻断。

结论口径（固定，不随表变化）：
- `compatible`            新增 nullable 列、类型放宽、允许空放宽 → 可直接放行
- `compatible_with_backfill` 新增列带默认值 → 放行，但必须回填历史行
- `blocked`               删除列、数值类型收窄、类型族不兼容、新增非空列且无默认值、
                          可空列收紧为非空且无默认值

判定顺序（同一列只出一条结论，避免重复计数）：
1) v1 有、v2 无 → 删除列（blocked）
2) 两侧都有 → 类型变化 / 可空性变化
3) v1 无、v2 有 → 新增列（nullable → 带默认 → 阻断）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 数值类型由小到大；target 比 base 小即「收窄」
NUMERIC_ORDER = {"int": 0, "long": 1, "decimal": 2}
VERDICT_ORDER = {"compatible": 0, "compatible_with_backfill": 1, "blocked": 2}


class SchemaError(ValueError):
    """schema 文档缺失或结构非法。"""


@dataclass
class Finding:
    table: str
    change: str
    column: str
    verdict: str
    reason: str
    position: str
    before: str | None = None
    after: str | None = None

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "change": self.change,
            "column": self.column,
            "verdict": self.verdict,
            "reason": self.reason,
            "position": self.position,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class GateResult:
    findings: list[Finding] = field(default_factory=list)
    base_version: int = 0
    target_version: int = 0
    skipped_tables: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.findings:
            return "compatible"
        worst = max(VERDICT_ORDER[f.verdict] for f in self.findings)
        return {0: "compatible", 1: "compatible_with_backfill", 2: "blocked"}[worst]

    @property
    def counts(self) -> dict:
        out = {"compatible": 0, "compatible_with_backfill": 0, "blocked": 0}
        for finding in self.findings:
            out[finding.verdict] += 1
        return out

    def to_dict(self) -> dict:
        return {
            "base_version": self.base_version,
            "target_version": self.target_version,
            "verdict": self.verdict,
            "counts": self.counts,
            "findings": [f.to_dict() for f in self.findings],
            "tables": self.by_table(),
            "skipped_tables": self.skipped_tables,
        }

    def by_table(self) -> dict:
        out: dict[str, dict] = {}
        for finding in self.findings:
            bucket = out.setdefault(
                finding.table,
                {"table": finding.table, "verdict": "compatible",
                 "counts": {"compatible": 0, "compatible_with_backfill": 0, "blocked": 0},
                 "findings": []},
            )
            bucket["findings"].append(finding.to_dict())
            bucket["counts"][finding.verdict] += 1
            if VERDICT_ORDER[finding.verdict] > VERDICT_ORDER[bucket["verdict"]]:
                bucket["verdict"] = finding.verdict
        return out


def load_schema(path: Path) -> dict:
    if not path.exists():
        raise SchemaError(f"缺少 schema 文件：{path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"schema 不是合法 JSON：{path.name}（{exc.msg}）") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("tables"), dict):
        raise SchemaError(f"schema 缺少 tables 对象：{path.name}")
    return doc


def _fmt_col(col: dict) -> str:
    nullable = "NULL" if col.get("nullable", True) else "NOT NULL"
    default = col.get("default")
    text = f"{col.get('type')} {nullable}"
    if default is not None:
        text += f" DEFAULT {default!r}"
    return text


def _columns(doc: dict, table: str) -> list[dict]:
    tables = doc.get("tables") or {}
    spec = tables.get(table)
    if spec is None:
        raise SchemaError(f"schema 缺少表定义：{table}")
    columns = spec.get("columns")
    if not isinstance(columns, list) or not columns:
        raise SchemaError(f"schema 表 {table} 缺少 columns")
    return columns


def compare_table(table: str, base_cols: list[dict], target_cols: list[dict]) -> list[Finding]:
    """单表对比：同一列最多产出一条结论。"""
    base = {col["name"]: col for col in base_cols}
    target = {col["name"]: col for col in target_cols}
    findings: list[Finding] = []

    for index, name in enumerate([col["name"] for col in base_cols], start=1):
        if name not in target:
            findings.append(
                Finding(
                    table=table,
                    change="removed_column",
                    column=name,
                    verdict="blocked",
                    reason=f"v2 删除列 {name}（v1 第 {index} 列，{_fmt_col(base[name])}）："
                           "历史数据与下游查询会丢字段，必须先下线依赖再变更",
                    position=f"v1 第 {index} 列（v2 已移除）",
                    before=_fmt_col(base[name]),
                )
            )

    for index, name in enumerate([col["name"] for col in target_cols], start=1):
        if name not in base:
            col = target[name]
            nullable = bool(col.get("nullable", True))
            default = col.get("default")
            if nullable:
                verdict, change = "compatible", "added_column"
                reason = f"新增 nullable 列 {name}：旧行读作 NULL，不阻塞写入"
            elif default is not None:
                verdict, change = "compatible_with_backfill", "added_column"
                reason = (f"新增列 {name} 带默认值 {default!r}：放行，但需回填历史行"
                          "（否则旧行取默认值，口径需与业务确认）")
            else:
                verdict, change = "blocked", "added_column"
                reason = (f"新增非空列 {name} 且无默认值：已有行无法补值，回填必然失败，"
                          "必须先带上默认值或允许 NULL")
            findings.append(
                Finding(
                    table=table,
                    change=change,
                    column=name,
                    verdict=verdict,
                    reason=reason,
                    position=f"v2 第 {index} 列（新增）",
                    after=_fmt_col(col),
                )
            )
            continue

        before, after = base[name], target[name]
        position = f"v2 第 {index} 列"
        before_text, after_text = _fmt_col(before), _fmt_col(after)
        before_type, after_type = before.get("type"), after.get("type")
        before_null = bool(before.get("nullable", True))
        after_null = bool(after.get("nullable", True))
        default = after.get("default")

        if before_type != after_type:
            if before_type in NUMERIC_ORDER and after_type in NUMERIC_ORDER:
                narrowed = NUMERIC_ORDER[after_type] < NUMERIC_ORDER[before_type]
                if narrowed:
                    findings.append(
                        Finding(table, "type_narrowed", name, "blocked",
                                f"类型收窄 {before_type}→{after_type}（{name}）：超出范围的既有值会写失败或被截断",
                                position, before_text, after_text)
                    )
                else:
                    findings.append(
                        Finding(table, "type_widened", name, "compatible",
                                f"类型放宽 {before_type}→{after_type}（{name}）：旧值仍可表示，可直接放行",
                                position, before_text, after_text)
                    )
            else:
                findings.append(
                    Finding(table, "type_incompatible", name, "blocked",
                            f"类型族不兼容 {before_type}→{after_type}（{name}）：需要显式转换与双写验证",
                            position, before_text, after_text)
                )
            continue

        if before_null and not after_null:
            if default is not None:
                findings.append(
                    Finding(table, "not_null_tightened", name, "compatible_with_backfill",
                            f"{name} 收紧为非空且带默认值 {default!r}：放行，但需把历史 NULL 回填成默认值",
                            position, before_text, after_text)
                )
            else:
                findings.append(
                    Finding(table, "not_null_tightened", name, "blocked",
                            f"{name} 由可空收紧为非空且无默认值：历史 NULL 行会违反约束",
                            position, before_text, after_text)
                )
        elif not before_null and after_null:
            findings.append(
                Finding(table, "nullability_relaxed", name, "compatible",
                        f"{name} 放宽为可空：旧数据全部满足，可直接放行",
                        position, before_text, after_text)
            )

    return findings


def gate(base_doc: dict, target_doc: dict, tables: list[str]) -> GateResult:
    """对选中的表做门禁；schema 中缺失的表记为 skipped（页面提示，不静默当通过）。"""
    result = GateResult(
        base_version=int(base_doc.get("version", 0)),
        target_version=int(target_doc.get("version", 0)),
    )
    for table in tables:
        if table not in (base_doc.get("tables") or {}) or table not in (target_doc.get("tables") or {}):
            result.skipped_tables.append(table)
            continue
        result.findings.extend(
            compare_table(table, _columns(base_doc, table), _columns(target_doc, table))
        )
    return result


def structural_changes(base_doc: dict, target_doc: dict, tables: list[str]) -> list[dict]:
    """只描述结构差异（新增/删除/类型/可空），不给出门禁结论，供首页展示变更申请内容。"""
    out: list[dict] = []
    for table in tables:
        if table not in (base_doc.get("tables") or {}) or table not in (target_doc.get("tables") or {}):
            out.append({"table": table, "label": table, "added": [], "removed": [],
                        "type_changed": [], "nullability": [], "columns_v1": [], "columns_v2": []})
            continue
        base_cols = _columns(base_doc, table)
        target_cols = _columns(target_doc, table)
        base = {col["name"]: col for col in base_cols}
        target = {col["name"]: col for col in target_cols}
        type_changed = [
            {"name": name, "before": base[name].get("type"), "after": target[name].get("type")}
            for name in [col["name"] for col in target_cols]
            if name in base and base[name].get("type") != target[name].get("type")
        ]
        nullability = [
            {"name": name,
             "before": "NULL" if base[name].get("nullable", True) else "NOT NULL",
             "after": "NULL" if target[name].get("nullable", True) else "NOT NULL"}
            for name in [col["name"] for col in target_cols]
            if name in base
            and bool(base[name].get("nullable", True)) != bool(target[name].get("nullable", True))
        ]
        out.append(
            {
                "table": table,
                "label": (target_doc["tables"][table].get("label") or table),
                "added": [col["name"] for col in target_cols if col["name"] not in base],
                "removed": [col["name"] for col in base_cols if col["name"] not in target],
                "type_changed": type_changed,
                "nullability": nullability,
                "columns_v1": [_fmt_col(col) for col in base_cols],
                "columns_v2": [_fmt_col(col) for col in target_cols],
            }
        )
    return out
