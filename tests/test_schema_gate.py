"""核心规则 5：schema 门禁三类结论矩阵（可放行 / 放行+回填 / 阻断）。"""
from __future__ import annotations

from app import schema_gate, settings


def col(name, type_="string", nullable=True, default=None):
    return {"name": name, "type": type_, "nullable": nullable, "default": default}


def doc(columns, version=1, table="t"):
    return {"version": version, "tables": {table: {"label": table, "columns": columns}}}


def one(base_cols, target_cols, table="t"):
    findings = schema_gate.compare_table(table, base_cols, target_cols)
    assert len(findings) == 1, findings
    return findings[0]


# --- 新增列三种结论 ------------------------------------------------------


def test_added_nullable_column_is_compatible():
    finding = one([col("id", "string", False)], [col("id", "string", False), col("note", "string", True)])
    assert finding.verdict == "compatible"
    assert finding.change == "added_column"
    assert finding.column == "note"
    assert "nullable" in finding.reason
    assert finding.position == "v2 第 2 列（新增）"


def test_added_column_with_default_needs_backfill():
    finding = one(
        [col("id", "string", False)],
        [col("id", "string", False), col("tier", "string", False, "standard")],
    )
    assert finding.verdict == "compatible_with_backfill"
    assert "回填" in finding.reason
    assert finding.after == "string NOT NULL DEFAULT 'standard'"


def test_added_not_null_column_without_default_is_blocked():
    finding = one(
        [col("id", "string", False)],
        [col("id", "string", False), col("reorder_point", "long", False)],
    )
    assert finding.verdict == "blocked"
    assert "无默认值" in finding.reason


# --- 删除列 / 类型变化 ---------------------------------------------------


def test_removed_column_is_blocked_and_keeps_v1_position():
    finding = one([col("id", "string", False), col("legacy", "string", True)], [col("id", "string", False)])
    assert finding.verdict == "blocked"
    assert finding.change == "removed_column"
    assert finding.position == "v1 第 2 列（v2 已移除）"
    assert finding.before == "string NULL"


def test_narrowing_long_to_int_is_blocked():
    finding = one([col("qty", "long", False)], [col("qty", "int", False)])
    assert finding.verdict == "blocked"
    assert finding.change == "type_narrowed"
    assert "收窄" in finding.reason


def test_widening_int_to_long_is_compatible():
    finding = one([col("qty", "int", False)], [col("qty", "long", False)])
    assert finding.verdict == "compatible"
    assert finding.change == "type_widened"


def test_incompatible_type_family_is_blocked():
    finding = one([col("code", "string", True)], [col("code", "long", True)])
    assert finding.verdict == "blocked"
    assert finding.change == "type_incompatible"


# --- 可空性 --------------------------------------------------------------


def test_nullable_to_not_null_without_default_is_blocked():
    finding = one([col("email", "string", True)], [col("email", "string", False)])
    assert finding.verdict == "blocked"
    assert finding.change == "not_null_tightened"


def test_nullable_to_not_null_with_default_needs_backfill():
    finding = one(
        [col("email", "string", True)],
        [col("email", "string", False, "unknown@example.com")],
    )
    assert finding.verdict == "compatible_with_backfill"


def test_relaxing_to_nullable_is_compatible():
    finding = one([col("email", "string", False)], [col("email", "string", True)])
    assert finding.verdict == "compatible"
    assert finding.change == "nullability_relaxed"


# --- 汇总判定 ------------------------------------------------------------


def test_gate_verdict_is_worst_finding_and_counts():
    base = doc([col("id", "string", False), col("qty", "long", False), col("old", "string", True)])
    target = doc(
        [col("id", "string", False), col("qty", "int", False), col("note", "string", True)],
        version=2,
    )
    result = schema_gate.gate(base, target, ["t"])
    assert result.verdict == "blocked"
    assert result.counts == {"compatible": 1, "compatible_with_backfill": 0, "blocked": 2}
    assert result.base_version == 1 and result.target_version == 2
    assert result.by_table()["t"]["verdict"] == "blocked"


def test_gate_on_identical_schema_is_compatible():
    base = doc([col("id", "string", False)])
    result = schema_gate.gate(base, base, ["t"])
    assert result.findings == []
    assert result.verdict == "compatible"


def test_gate_reports_missing_table_instead_of_passing_silently():
    base = doc([col("id", "string", False)], table="known")
    result = schema_gate.gate(base, base, ["known", "missing"])
    assert result.skipped_tables == ["missing"]


# --- 样例 v1 → v2 全矩阵 -------------------------------------------------


def test_sample_schema_matrix():
    base = schema_gate.load_schema(settings.SCHEMAS_DIR / "v1.json")
    target = schema_gate.load_schema(settings.SCHEMAS_DIR / "v2.json")
    result = schema_gate.gate(base, target, settings.TABLE_NAMES)
    assert result.verdict == "blocked"
    assert result.counts == {"compatible": 3, "compatible_with_backfill": 1, "blocked": 5}
    by_table = result.by_table()
    assert len(by_table["orders"]["findings"]) == 3
    assert by_table["orders"]["verdict"] == "blocked"
    assert by_table["customers"]["counts"]["compatible_with_backfill"] == 1
    changes = {(f.table, f.column): f.change for f in result.findings}
    assert changes[("orders", "legacy_channel")] == "removed_column"
    assert changes[("orders", "qty")] == "type_narrowed"
    assert changes[("orders", "fulfillment_note")] == "added_column"
    assert changes[("customers", "email")] == "not_null_tightened"
    assert changes[("inventory", "reorder_point")] == "added_column"
