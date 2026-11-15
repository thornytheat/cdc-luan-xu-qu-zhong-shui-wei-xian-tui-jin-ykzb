"""接口测试：成功、4xx、以及「加载样例 → 合并 → 读结论 → 导出快照」端到端。"""
from __future__ import annotations

import json

import pytest


def test_health_reports_seeded_events(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["events"] == 180          # 3 表 × 60 条，lifespan 已幂等 seed
    assert body["app"] == "cdcwater"


def test_pages_render(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "CDC 乱序去重与水位线推进" in index.text
    assert "样例事件概览" in index.text

    workbench = client.get("/workbench")
    assert workbench.status_code == 200
    assert "执行合并" in workbench.text
    assert "orders" in workbench.text


def test_seed_is_idempotent(client):
    first = client.post("/api/seed").json()
    second = client.post("/api/seed").json()
    assert first["inserted"] == 0        # lifespan 已经导过一遍
    assert second["inserted"] == 0
    assert second["total_events"] == 180
    assert second["counts"] == {"orders": 60, "customers": 60, "inventory": 60}


def test_merge_success_returns_expected_summary(client):
    response = client.post("/api/merge", json={"tables": ["orders", "customers", "inventory"],
                                              "schema_version": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"].startswith("R")
    summary = body["summary"]
    assert summary["conflicts"] == 3
    assert summary["late"] == 8
    assert summary["ignored_after_tombstone"] == 2
    assert summary["deduped_silent"] == 1
    assert summary["alive_rows"] == 40
    assert summary["tombstones"] == 2
    assert summary["watermark"] == "2026-11-10T00:14:00Z"
    assert summary["open_keys"] == 33
    assert summary["gate_verdict"] == "blocked"
    assert summary["gate_counts"] == {"compatible": 3, "compatible_with_backfill": 1, "blocked": 5}
    assert body["result_url"] == f"/result/{body['run_id']}"


def test_merge_single_table_only_evaluates_that_table(client):
    response = client.post("/api/merge", json={"tables": ["customers"], "schema_version": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["tables"] == ["customers"]
    assert body["summary"]["alive_rows"] == 14        # customers 无墓碑
    assert body["summary"]["late"] == 2
    assert body["summary"]["gate_counts"]["blocked"] == 1   # 只有 email 收紧


def test_unknown_table_is_rejected_with_400(client):
    response = client.post("/api/merge", json={"tables": ["ghost_table"], "schema_version": 2})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "未知的表" in detail and "ghost_table" in detail


def test_empty_table_list_is_rejected(client):
    response = client.post("/api/merge", json={"tables": [], "schema_version": 2})
    assert response.status_code == 400
    assert "至少选择一张表" in response.json()["detail"]


def test_invalid_schema_version_is_4xx(client):
    response = client.post("/api/merge", json={"tables": ["orders"], "schema_version": 9})
    assert response.status_code == 422


def test_unknown_run_returns_404_and_page_shows_reason(client):
    api = client.get("/api/runs/R-ghost")
    assert api.status_code == 404
    page = client.get("/result/R-ghost")
    assert page.status_code == 200
    assert "没有找到" in page.text


def test_result_page_lists_conflicts_late_and_gate(client):
    run_id = client.post("/api/merge", json={"tables": ["orders", "customers", "inventory"],
                                             "schema_version": 2}).json()["run_id"]
    page = client.get(f"/result/{run_id}")
    assert page.status_code == 200
    text = page.text
    assert "同版本冲突逐条" in text
    assert "第 58 行" in text            # 墓碑后迟到事件的位置
    assert "2026-11-10T00:14:00Z" in text  # 水位线
    assert "Schema 门禁逐条结论" in text
    assert "类型收窄" in text
    assert "快照预览" in text


def test_snapshot_download_contains_full_payload(client):
    run_id = client.post("/api/merge", json={"tables": ["orders", "customers", "inventory"],
                                             "schema_version": 2}).json()["run_id"]
    response = client.get(f"/api/runs/{run_id}/snapshot")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith(f'{run_id}.json"')
    snapshot = json.loads(response.content)
    assert snapshot["export"]["run_id"] == run_id
    assert snapshot["counters"]["conflicts"] == 3
    assert snapshot["counters"]["late"] == 8
    assert snapshot["watermark"]["watermark"] == "2026-11-10T00:14:00Z"
    assert snapshot["gate"]["verdict"] == "blocked"
    assert set(snapshot["tables"]) == {"orders", "customers", "inventory"}
    assert snapshot["tables"]["orders"]["row_count"] == 13
    assert len(snapshot["changelog"]) == 180
    assert len(snapshot["late_events"]) == 8


def test_review_page_lists_runs_and_changelog(client):
    run_id = client.post("/api/merge", json={"tables": ["orders"], "schema_version": 2}).json()["run_id"]
    page = client.get(f"/review?run_id={run_id}")
    assert page.status_code == 200
    assert run_id in page.text
    assert "变更日志" in page.text
    filtered = client.get(f"/review?run_id={run_id}&kind=conflict")
    assert filtered.status_code == 200
    assert "同版本冲突" in filtered.text


def test_runs_api_lists_history(client):
    run_id = client.post("/api/merge", json={"tables": ["orders"], "schema_version": 1}).json()["run_id"]
    listing = client.get("/api/runs").json()
    assert listing["count"] >= 1
    assert listing["runs"][0]["run_id"] == run_id
    # schema v1 对 v1 无变更：门禁可放行，用于对照
    assert listing["runs"][0]["summary"]["gate_verdict"] == "compatible"


def test_end_to_end_sample_flow(client):
    """验收主流程：加载样例 → 合并 → 读结论 → 导出快照。"""
    samples = client.get("/api/samples").json()
    assert samples["total"] == 180

    merge_response = client.post(
        "/api/merge", json={"tables": ["orders", "customers", "inventory"], "schema_version": 2}
    )
    assert merge_response.status_code == 200
    run_id = merge_response.json()["run_id"]

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["summary"]["conflicts"] == 3
    assert detail["summary"]["late"] == 8
    assert detail["watermark"]["watermark"] == "2026-11-10T00:14:00Z"
    assert detail["tables"]["orders"]["row_count"] == 13
    assert detail["tables"]["customers"]["row_count"] == 14
    assert detail["tables"]["inventory"]["row_count"] == 13
    assert detail["gate"]["counts"] == {"compatible": 3, "compatible_with_backfill": 1, "blocked": 5}

    snapshot = client.get(f"/api/runs/{run_id}/snapshot")
    assert snapshot.status_code == 200
    payload = json.loads(snapshot.content)
    assert payload["export"]["schema_version"] == 2
    assert payload["tables"]["orders"]["tombstones"][0]["pk"] == "O9002"
    assert {item["table"] for item in payload["conflicts"]} == {"orders", "customers", "inventory"}
